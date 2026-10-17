# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Submitting claims: the preview a member confirms, the claim itself, withdrawing and submitting again.

The helpers at the top (``record_event``, ``evaluate``, ``apply_evaluation``, the warning helpers) are shared
with ``review``, which decides on the claims created here.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from allianceauth.services.hooks import get_extension_logger
from django.core import signing
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from fleetops import texts
from fleetops.models import SrpClaim, SrpClaimEvent, SrpFleetLink, SrpKillmail, SrpSettings
from fleetops.providers.zkillboard import MIN_KILL_AGE
from fleetops.services.audit import audit
from fleetops.services.identity import character_label
from fleetops.services.periods import format_eve
from fleetops.srp import access, display, killmails, legacy, notify, presence, quota
from fleetops.srp import policy as rules
from fleetops.srp import values as srp_values
from fleetops.srp.types import (
    LIMIT_CORPORATION,
    LIMIT_PERSONAL,
    Calculation,
    ClaimIdentity,
    ClaimPreview,
    ClaimWarning,
    PolicySnapshot,
    PresenceResult,
    SrpDuplicateError,
    SrpError,
    SrpStateError,
)

logger = get_extension_logger(__name__)

PREVIEW_SALT = "fleetops.srp.preview"
PREVIEW_MAX_AGE = 3600
# Checks per user: 20 in 10 minutes.
CHECK_LIMIT = 20
CHECK_WINDOW = 600
MAX_COMMENT_LENGTH = 1000
MAX_REASON_LENGTH = 1000

Status = SrpClaim.Status
Scope = SrpClaim.Scope
Action = SrpClaimEvent.Action

MONTHLY_KINDS = (LIMIT_PERSONAL, LIMIT_CORPORATION)
# Warnings that follow from the calculation and are replaced when a claim is calculated again.
CALCULATION_WARNING_CODES = frozenset({"over_limit", "reduced_by_limit", "value_estimate", "value_pending", "value_missing"})
# Warnings that follow from the fleet presence check and are replaced when it is checked again.
PRESENCE_WARNING_CODES = frozenset(
    {display.NEAR, display.NOT_IN_FLEET, display.NEVER_SEEN, display.REGISTERED, display.OUTSIDE_WINDOW, "ship_mismatch"}
)

# Stored messages (English): the pages show them through ``translate_stored``.
EXPIRED_TEXT = texts.SRP_CHECK_EXPIRED()
THROTTLED_TEXT = texts.SRP_THROTTLED()
OWN_DUPLICATE_TEXT = texts.SRP_OWN_DUPLICATE()
OTHER_DUPLICATE_TEXT = texts.SRP_OTHER_DUPLICATE()
# The reasons ``clean_reason`` knows by name: the message for a missing one and the one for a long one.
REASON_MESSAGES = {
    "reason": (texts.SRP_NEED_REASON, texts.SRP_REASON_TOO_LONG),
    "reason for the rejection": (texts.APPROVAL_REASON_REQUIRED, texts.SRP_REJECTION_REASON_TOO_LONG),
    "reason for revoking the approval": (texts.SRP_NEED_REASON_REVOKE, texts.SRP_REVOKE_REASON_TOO_LONG),
    "reason for undoing the payment": (texts.SRP_NEED_REASON_UNPAY, texts.SRP_UNPAY_REASON_TOO_LONG),
    "reason for reopening the claim": (texts.SRP_NEED_REASON_REOPEN, texts.SRP_REOPEN_REASON_TOO_LONG),
}

MAX_STORED_VALUE = Decimal(10**17)
CENT = Decimal("0.01")


# --- Shared helpers (also used by review) -------------------------------------------------------------


def status_label(status) -> str:
    """The status in words, lower case, for a stored message: English, translated where it is shown."""
    word = texts.CLAIM_STATUS_WORDS.get(str(status))
    return word() if word else str(status)


def limit_name(kind) -> str:
    """The monthly limit that ``kind`` names, in words, for a stored message: English."""
    if kind == LIMIT_PERSONAL:
        return texts.SRP_LIMIT_PERSONAL()
    if kind == LIMIT_CORPORATION:
        return texts.SRP_LIMIT_CORPORATION()
    return f"monthly {kind} limit" if kind else texts.SRP_LIMIT_MONTHLY()


def wrong_state(claim, needs: str, target=None) -> SrpStateError:
    """The error for an action that cannot start from the claim's status: "This claim is already approved."
    when the claim already is where the action leads, else ``needs``."""
    if target is not None and claim.status == target:
        return SrpStateError("already", texts.SRP_ALREADY_STATUS(status=status_label(target)))
    return SrpStateError("wrong_state", needs)


def lock_claim(claim) -> SrpClaim:
    """The claim row locked for this transaction; call it after ``quota.quota_lock()`` when both are needed."""
    try:
        return SrpClaim.objects.select_for_update().get(pk=claim.pk)
    except SrpClaim.DoesNotExist:
        raise SrpError("missing", texts.SRP_CLAIM_GONE()) from None


def sync_claim(target, source):
    """Copy the stored fields of ``source`` (the locked row) onto the caller's instance."""
    for field in source._meta.concrete_fields:
        setattr(target, field.attname, getattr(source, field.attname))
        if field.is_relation:
            target._state.fields_cache.pop(field.name, None)
    return target


def notify_after_commit(claim, action: str) -> None:
    """Tell the claimant once the decision is committed; a failure is logged and ignored."""
    transaction.on_commit(lambda: notify.notify_claimant(claim, action))


def record_event(
    claim,
    action: str,
    *,
    actor,
    from_status: str = "",
    amount=None,
    reason: str = "",
    internal: bool = False,
    data=None,
    old=None,
    new=None,
) -> SrpClaimEvent:
    """Write the claim event (the timeline of the claim) and the audit entry of the same step.

    The audit action is ``srp.claim_<action>``; a value update is audited as a recalculation. ``old`` and ``new``
    are the audit values (status, amounts, scope), ``reason`` is the reason given. The text of an internal event
    (a manager's note) stays on the claim event: the audit log is open to people who may not see it.
    """
    action = getattr(action, "value", action)
    event = SrpClaimEvent.objects.create(
        claim=claim,
        actor=actor,
        action=action,
        from_status=from_status,
        to_status=claim.status,
        amount=amount,
        reason=reason,
        internal=internal,
        data=data or {},
    )
    audit_action = "recalculate" if action == Action.VALUE_UPDATE else action
    audit(actor, f"srp.claim_{audit_action}", claim, old, new, "" if internal else reason)
    return event


def clean_reason(reason, *, required: bool = False, what: str = "reason") -> str:
    """A reason or note, trimmed; ``SrpError`` when it is required and empty or too long."""
    text = (reason or "").strip()
    needed, too_long = REASON_MESSAGES[what]
    if required and not text:
        raise SrpError("reason_required", needed())
    if len(text) > MAX_REASON_LENGTH:
        raise SrpError("reason_too_long", too_long(max=MAX_REASON_LENGTH))
    return text


def stored_value(value):
    """A loss value as the claim stores it: cents, below the column's range."""
    if value is None:
        return None
    return min(Decimal(value), MAX_STORED_VALUE).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(slots=True)
class Evaluation:
    """The rule, policy, value and calculation of a loss for one claimant."""

    rule: object
    policy: PolicySnapshot
    calculation: Calculation
    loss_value: Decimal | None
    value_source: str


def evaluate(
    *,
    km,
    user_id,
    corporation_id,
    scope: str,
    operation,
    settings_obj,
    exclude_claim_id=None,
    locking: bool = False,
    now=None,
) -> Evaluation:
    """Match the payout rule, snapshot the policy, pick the loss value and calculate with the monthly limits.

    ``locking`` reads the usage rows for update: use it inside ``quota.quota_lock()`` when the result is stored.
    """
    rule = rules.match_rule(
        ship_type_id=km.ship_type_id,
        scope=scope,
        operation=operation,
        capsules_need_rule=settings_obj.capsules_need_rule,
    )
    policy = rules.current_policy(corporation_id=corporation_id, rule=rule, settings_obj=settings_obj)
    value, source = srp_values.loss_value(km, policy.value_source)
    limits = quota.limit_states(
        user_id=user_id,
        corporation_id=corporation_id,
        scope=scope,
        killmail_time=km.killmail_time,
        policy=policy,
        exclude_claim_id=exclude_claim_id,
        locking=locking,
    )
    calculation = rules.calculate(
        policy=policy,
        scope=scope,
        ship_type_id=km.ship_type_id,
        loss_value=value,
        value_source=source,
        limits=limits,
        now=now,
    )
    return Evaluation(rule, policy, calculation, value, source)


def monthly_limit_kind(calculation) -> str:
    """The monthly limit that kept an amount down: ``personal`` or ``corporation``; empty without one."""
    if calculation.binding_limit in MONTHLY_KINDS:
        return calculation.binding_limit
    states = [state for state in calculation.limits if state.effective_remaining is not None]
    if not states:
        return ""
    return min(states, key=lambda state: (state.effective_remaining, 0 if state.kind == LIMIT_PERSONAL else 1)).kind


def calculation_warnings(calculation, policy, km) -> list[ClaimWarning]:
    """The warnings that follow from a calculation: over the monthly limit (or cut to what is left in
    ``reject`` mode), an estimated value and a value that is not available."""
    found = []
    if calculation.over_limit:
        params = {
            "kind": monthly_limit_kind(calculation),
            "requested": calculation.suggested,
            "remaining": calculation.limited,
        }
        if policy.over_limit_action == SrpSettings.OverLimitAction.REJECT and calculation.limited > 0:
            found.append(ClaimWarning("reduced_by_limit", display.LEVEL_WARNING, params))
        else:
            found.append(ClaimWarning("over_limit", display.LEVEL_DANGER, params))
    if calculation.basis == rules.BASIS_PERCENTAGE:
        if calculation.value_source == srp_values.VALUE_ESI_ESTIMATE:
            found.append(ClaimWarning("value_estimate", display.LEVEL_WARNING))
    elif (
        calculation.basis == rules.BASIS_NONE
        and calculation.loss_value is None
        and rules.percentage_applies(policy, km.ship_type_id)
    ):
        waiting = km.zkill_status in (SrpKillmail.ZkillStatus.PENDING, SrpKillmail.ZkillStatus.MISSING)
        found.append(ClaimWarning("value_pending" if waiting else "value_missing", display.LEVEL_WARNING))
    return found


def presence_warnings(result) -> list[ClaimWarning]:
    """The warnings of a fleet presence result: the check itself unless the character was in the fleet, and
    a ship that differs from the one lost."""
    found = []
    if result is None:
        return found
    if result.level != display.LEVEL_OK:
        found.append(ClaimWarning(result.code, result.level, dict(result.params)))
    if result.tracked_ship_type_id or result.tracked_ship_name:
        # A ship without a name is shown by its type ID when the warning is read.
        ship = result.tracked_ship_name or ""
        found.append(
            ClaimWarning(
                "ship_mismatch", display.LEVEL_INFO, {"ship": ship, "ship_type_id": result.tracked_ship_type_id}
            )
        )
    return found


def replace_warnings(existing, codes, fresh) -> list[dict]:
    """The stored warnings without those of ``codes``, with ``fresh`` first."""
    kept = [item for item in existing or () if isinstance(item, dict) and item.get("code") not in codes]
    return [warning.as_json() for warning in fresh] + kept


def apply_evaluation(claim, evaluation, km) -> None:
    """Write a calculation onto the claim (in memory): amounts, basis, rule, snapshots, flags and the
    warnings that follow from it. Other warnings stay."""
    calculation, policy = evaluation.calculation, evaluation.policy
    claim.loss_value = stored_value(calculation.loss_value)
    claim.value_source = calculation.value_source
    claim.payout_basis = calculation.basis
    claim.payout_rule = evaluation.rule
    claim.policy = policy.as_json()
    claim.calculation = calculation.as_json()
    claim.base_amount = calculation.base
    claim.suggested_amount = calculation.suggested
    claim.limited_amount = calculation.limited
    claim.over_limit = calculation.over_limit
    claim.counts_personal = rules.counts_toward_personal(policy, claim.scope)
    claim.counts_corporation = rules.counts_toward_corporation(policy, claim.scope)
    claim.warnings = replace_warnings(
        claim.warnings, CALCULATION_WARNING_CODES, calculation_warnings(calculation, policy, km)
    )


def apply_presence(claim, result, now) -> None:
    """Write a fleet presence result onto the claim (in memory), with its warnings."""
    claim.fleet_check = result.code
    claim.fleet_check_params = dict(result.params)
    claim.fleet_checked_at = now
    claim.warnings = replace_warnings(claim.warnings, PRESENCE_WARNING_CODES, presence_warnings(result))


def calculation_snapshot(claim) -> dict:
    """What a recalculation replaces, kept in the claim event."""
    return {
        "scope": claim.scope,
        "payout_basis": claim.payout_basis,
        "loss_value": None if claim.loss_value is None else str(claim.loss_value),
        "value_source": claim.value_source,
        "base_amount": claim.base_amount,
        "suggested_amount": claim.suggested_amount,
        "limited_amount": claim.limited_amount,
        "over_limit": claim.over_limit,
        "policy": claim.policy,
        "calculation": claim.calculation,
        "warnings": claim.warnings,
    }


def fleet_label(operation) -> str:
    """``<type> — <FC> — YYYY-MM-DD HH:MM EVE``, kept on the claim when the fleet is deleted.

    The type is named as FleetOps shows it everywhere: its short name when it has one.
    """
    fc = operation.fc_main_character_name or operation.fc_character_name
    return f"{operation.fleet_type} — {fc} — {format_eve(operation.started_at or operation.created_at)} EVE"


def fleet_fields(link) -> dict:
    """The claim fields that name the fleet of an in-fleet claim (all empty for an out-of-fleet claim)."""
    if link is None:
        return {"link": None, "operation": None, "fleet_label": "", "fc_user": None, "fc_main_character_name": ""}
    operation = link.operation
    return {
        "link": link,
        "operation": operation,
        "fleet_label": fleet_label(operation),
        "fc_user": link.fc_user,
        "fc_main_character_name": link.fc_main_character_name or operation.fc_main_character_name,
    }


def loss_presence(scope, link, km) -> PresenceResult:
    """The fleet presence of the victim for a loss claimed through ``link`` (not applicable out of fleet)."""
    if scope != Scope.IN_FLEET or link is None or km.victim_character_id is None:
        return PresenceResult(display.NOT_APPLICABLE, display.presence_level(display.NOT_APPLICABLE))
    return presence.check_presence(
        link.operation,
        km.victim_character_id,
        km.killmail_time,
        ship_type_id=km.ship_type_id,
        ship_name=km.ship_type_name,
    )


def claim_identity(user, km) -> ClaimIdentity | None:
    """Who the victim is for ``user``: the character, and the main with its corporation and alliance when the
    character is on the user's account. None for a loss without a pilot."""
    if km.victim_character_id is None:
        return None
    character = rules.owned_character(user, km.victim_character_id)
    if character is None:
        return ClaimIdentity(
            character_id=km.victim_character_id, character_name=character_label(km.victim_character_id)
        )
    main = rules.main_character(user)
    return ClaimIdentity(
        character_id=character.character_id,
        character_name=character.character_name,
        main_character_id=getattr(main, "character_id", None),
        main_character_name=getattr(main, "character_name", ""),
        corporation_id=getattr(main, "corporation_id", None),
        corporation_name=getattr(main, "corporation_name", "") or "",
        corporation_ticker=getattr(main, "corporation_ticker", "") or "",
        alliance_id=getattr(main, "alliance_id", None),
        alliance_name=getattr(main, "alliance_name", "") or "",
    )


def duplicate_message(user, existing) -> str:
    """Why a killmail cannot be claimed twice: the claimant's own claim, or somebody else's (managers see who)."""
    if existing.user_id is not None and existing.user_id == user.pk:
        return OWN_DUPLICATE_TEXT
    if access.is_manager(user) and existing.main_character_name:
        return texts.SRP_CLAIMED_BY(name=existing.main_character_name)
    return OTHER_DUPLICATE_TEXT


def decide_submission(calculation, policy) -> tuple[str, str, dict]:
    """The status a new claim starts with, the reason when it is rejected on the spot and that reason as a
    code with its parameters (kept in the claim event, so the text can be shown in another language later).

    Over the monthly limit the policy either sends the claim to review (with a warning) or rejects it. A claim
    that only partly fits is never rejected: it goes to review, cut to what is left. Nothing left rejects it.
    """
    if not calculation.over_limit or calculation.limited > 0:
        return Status.PENDING, "", {}
    if policy.over_limit_action != SrpSettings.OverLimitAction.REJECT:
        return Status.PENDING, "", {}
    kind = monthly_limit_kind(calculation)
    state = calculation.limit(kind)
    cap = state.cap if state else None
    text = texts.SRP_REJECTED_LIMIT_USED_UP(limit=limit_name(kind), cap=display.format_isk(cap))
    return Status.REJECTED, text, {"code": "limit_used_up", "kind": kind, "cap": cap}


# --- Preview --------------------------------------------------------------------------------------------


def _throttle(user) -> None:
    """At most ``CHECK_LIMIT`` previews per ``CHECK_WINDOW`` seconds and user."""
    key = f"fleetops:srp:check:{user.pk}"
    try:
        cache.add(key, 0, CHECK_WINDOW)
        try:
            count = cache.incr(key)
        except ValueError:
            # The window ended between the two calls.
            cache.set(key, 1, CHECK_WINDOW)
            count = 1
    except Exception as exc:
        # Without the cache there is no counter; the checks still work.
        logger.warning("FleetOps: SRP check throttle unavailable (%s).", type(exc).__name__)
        return
    if count > CHECK_LIMIT:
        raise SrpError("throttled", THROTTLED_TEXT)


def _refresh_value(km, now) -> bool:
    """Ask zKillboard for the value once when the preview needs it and the killmail is old enough.

    Returns whether the killmail has new values.
    """
    if km.killmail_time > now - MIN_KILL_AGE:
        return False
    if km.zkill_next_attempt_at is not None and km.zkill_next_attempt_at > now:
        return False
    try:
        changed = bool(srp_values.refresh_zkill(km))
    except Exception as exc:
        # The value arrives with the sweep; the preview shows it as pending.
        logger.warning("FleetOps: zKillboard value for killmail %s not read (%s).", km.pk, type(exc).__name__)
        return False
    if changed:
        km.refresh_from_db()
    return changed


def _external_warnings(killmail_id) -> list[ClaimWarning]:
    try:
        found = legacy.external_claims(killmail_id)
    except Exception as exc:
        logger.debug("FleetOps: external SRP claims of killmail %s not read (%s).", killmail_id, type(exc).__name__)
        return []
    return [ClaimWarning("external_claim", display.LEVEL_WARNING, {"claims": list(found)})] if found else []


def prepare_claim(user, text: str, *, link: SrpFleetLink | None = None) -> ClaimPreview:
    """Read the pasted killmail and check it for ``user``: ownership, eligibility, fleet presence, payout
    rule, policy and limits. Throttled to 20 checks per 10 minutes and user. Raises ``SrpError``.

    ``link`` makes it a loss of that fleet. A loss that is not the user's, or that is claimed already, gets
    its blockers only: no calculation, presence or policy.
    """
    _throttle(user)
    srp = SrpSettings.get_solo()
    now = timezone.now()
    reference = killmails.parse_killmail_reference(text)
    km = killmails.fetch_killmail(reference, zkill_ok=srp.zkillboard_enabled)
    scope = Scope.IN_FLEET if link is not None else Scope.OUT_OF_FLEET
    preview = ClaimPreview(killmail=km, scope=scope, link=link, user=user)

    existing = SrpClaim.objects.filter(killmail=km).first()
    if existing is not None:
        preview.existing_claim = existing
        preview.blockers = [duplicate_message(user, existing)]
        return preview

    preview.blockers = rules.eligibility(km=km, user=user, scope=scope, link=link, settings_obj=srp, now=now)
    preview.identity = claim_identity(user, km)
    identity = preview.identity
    if identity is None or identity.main_character_id is None:
        return preview

    preview.presence = loss_presence(scope, link, km)
    if scope == Scope.OUT_OF_FLEET and srp.enabled:
        preview.suggested_link = presence.find_fleet_for_loss(km.victim_character_id, km.killmail_time)
    operation = link.operation if link is not None else None
    evaluation_args = {
        "km": km,
        "user_id": user.pk,
        "corporation_id": identity.corporation_id,
        "scope": scope,
        "operation": operation,
        "settings_obj": srp,
        "now": now,
    }
    evaluation = evaluate(**evaluation_args)
    waiting = km.zkill_status in (SrpKillmail.ZkillStatus.PENDING, SrpKillmail.ZkillStatus.MISSING)
    wants_value = (
        evaluation.rule is None and rules.percentage_applies(evaluation.policy, km.ship_type_id) and waiting
    )
    if srp.zkillboard_enabled and wants_value and _refresh_value(km, now):
        evaluation = evaluate(**evaluation_args)
    preview.policy, preview.calculation = evaluation.policy, evaluation.calculation
    preview.warnings = [
        *presence_warnings(preview.presence),
        *calculation_warnings(evaluation.calculation, evaluation.policy, km),
        *_external_warnings(km.killmail_id),
    ]
    preview.eligible = not preview.blockers
    if preview.eligible:
        preview.token = signing.dumps(
            {"km": km.pk, "link": link.code if link is not None else None, "u": user.pk}, salt=PREVIEW_SALT
        )
    return preview


# --- Submission and the member's own actions -----------------------------------------------------------------


def _read_token(user, token) -> dict:
    try:
        data = signing.loads(token or "", salt=PREVIEW_SALT, max_age=PREVIEW_MAX_AGE)
    except signing.BadSignature:
        raise SrpError("invalid_token", EXPIRED_TEXT) from None
    if not isinstance(data, dict) or data.get("u") != user.pk:
        # Not this user's check: the same answer as an expired one.
        raise SrpError("invalid_token", EXPIRED_TEXT)
    return data


def _clean_comment(comment) -> str:
    text = (comment or "").strip()
    if len(text) > MAX_COMMENT_LENGTH:
        raise SrpError("comment_too_long", texts.SRP_COMMENT_TOO_LONG(max=MAX_COMMENT_LENGTH))
    return text


def _duplicate(user, km) -> SrpDuplicateError | None:
    existing = SrpClaim.objects.filter(killmail=km).first()
    if existing is None:
        return None
    return SrpDuplicateError(duplicate_message(user, existing), claim=existing)


def submit_claim(user, token: str, *, comment: str = "") -> SrpClaim:
    """Create the claim for a preview token (signed, one hour, same user): checks again under the quota lock,
    applies the over-limit rules and writes the claim event and the audit entry. Raises ``SrpError``.

    A killmail that is claimed already (also by a submission that ran at the same time) raises
    ``SrpDuplicateError`` with the existing claim.
    """
    data = _read_token(user, token)
    comment = _clean_comment(comment)
    try:
        km = SrpKillmail.objects.get(pk=data.get("km"))
    except (SrpKillmail.DoesNotExist, TypeError, ValueError):
        raise SrpError("expired", EXPIRED_TEXT) from None
    link = None
    if data.get("link"):
        link = SrpFleetLink.objects.select_related("operation__fleet_type", "fc_user").filter(code=data["link"]).first()
        if link is None:
            raise SrpError("expired", EXPIRED_TEXT)
    scope = Scope.IN_FLEET if link is not None else Scope.OUT_OF_FLEET
    now = timezone.now()
    presence_result = loss_presence(scope, link, km)
    external = _external_warnings(km.killmail_id)

    with quota.quota_lock() as srp:
        # The killmail again, locked: a value stored since it was read above is used, and a value that arrives
        # later waits for this claim and then recalculates it.
        try:
            km = SrpKillmail.objects.select_for_update().get(pk=km.pk)
        except SrpKillmail.DoesNotExist:
            raise SrpError("expired", EXPIRED_TEXT) from None
        duplicate = _duplicate(user, km)
        if duplicate is not None:
            raise duplicate
        blockers = rules.eligibility(km=km, user=user, scope=scope, link=link, settings_obj=srp, now=now)
        if blockers:
            raise SrpError("not_eligible", blockers[0])
        identity = claim_identity(user, km)
        evaluation = evaluate(
            km=km,
            user_id=user.pk,
            corporation_id=identity.corporation_id,
            scope=scope,
            operation=link.operation if link is not None else None,
            settings_obj=srp,
            locking=True,
            now=now,
        )
        status, reason, rejection = decide_submission(evaluation.calculation, evaluation.policy)
        claim = SrpClaim(
            killmail=km,
            scope=scope,
            user=user,
            character_id=identity.character_id,
            character_name=identity.character_name,
            main_character_id=identity.main_character_id,
            main_character_name=identity.main_character_name,
            corporation_id=identity.corporation_id,
            corporation_name=identity.corporation_name,
            corporation_ticker=identity.corporation_ticker,
            alliance_id=identity.alliance_id,
            alliance_name=identity.alliance_name,
            killmail_time=km.killmail_time,
            ship_type_id=km.ship_type_id,
            ship_type_name=km.ship_type_name,
            solar_system_name=km.solar_system_name,
            member_comment=comment,
            status=status,
            submitted_at=now,
            **fleet_fields(link),
        )
        apply_evaluation(claim, evaluation, km)
        apply_presence(claim, presence_result, now)
        claim.warnings = [*claim.warnings, *(warning.as_json() for warning in external)]
        if status == Status.REJECTED:
            claim.auto_rejected = True
            claim.decision_reason = reason
            claim.decided_at = now
        try:
            with transaction.atomic():
                claim.save(force_insert=True)
        except IntegrityError:
            duplicate = _duplicate(user, km)
            if duplicate is None:
                raise
            raise duplicate from None
        action = Action.AUTO_REJECT if claim.auto_rejected else Action.SUBMIT
        record_event(
            claim,
            action,
            actor=user,
            amount=claim.suggested_amount,
            reason=reason,
            data={
                "scope": scope,
                "payout_basis": claim.payout_basis,
                "suggested_amount": claim.suggested_amount,
                "limited_amount": claim.limited_amount,
                "over_limit": claim.over_limit,
                "link": link.code if link is not None else None,
                **({"auto_reject": rejection} if rejection else {}),
            },
            new={
                "status": claim.status,
                "scope": scope,
                "killmail_id": km.pk,
                "ship_type_id": km.ship_type_id,
                "suggested_amount": claim.suggested_amount,
                "limited_amount": claim.limited_amount,
            },
        )
        if claim.auto_rejected:
            notify_after_commit(claim, "auto_reject")
    return claim


def withdraw_claim(claim: SrpClaim, *, actor, reason: str = "") -> SrpClaim:
    """Pending to withdrawn, by the claimant."""
    if actor is None or claim.user_id != actor.pk:
        raise SrpError("forbidden", texts.SRP_ONLY_CLAIMANT_WITHDRAWS())
    reason = clean_reason(reason)
    with quota.quota_lock():
        locked = lock_claim(claim)
        if locked.status != Status.PENDING:
            raise wrong_state(locked, texts.SRP_ONLY_PENDING_WITHDRAWN(), Status.WITHDRAWN)
        previous = locked.status
        locked.status = Status.WITHDRAWN
        locked.save(update_fields=["status", "updated_at"])
        record_event(
            locked,
            Action.WITHDRAW,
            actor=actor,
            from_status=previous,
            reason=reason,
            old={"status": previous},
            new={"status": locked.status},
        )
    return sync_claim(claim, locked)


def resubmit_claim(claim: SrpClaim, *, actor) -> SrpClaim:
    """Withdrawn to pending, recalculated with the current policy, by the claimant.

    The claim is checked again like a new one (SRP switched on, the deadline); a closed fleet link does not
    stop a claim that exists already. Over the monthly limit the usual rules apply, rejection included.
    """
    if actor is None or claim.user_id != actor.pk:
        raise SrpError("forbidden", texts.SRP_ONLY_CLAIMANT_RESUBMITS())
    now = timezone.now()
    with quota.quota_lock() as srp:
        locked = lock_claim(claim)
        if locked.status != Status.WITHDRAWN:
            raise wrong_state(locked, texts.SRP_ONLY_WITHDRAWN_RESUBMITTED(), Status.PENDING)
        km = locked.killmail
        # No link is given: a closed link does not stop a claim that exists, but the payout rules of its fleet
        # still apply, as they do in the calculation below.
        blockers = rules.eligibility(
            km=km, user=actor, scope=locked.scope, link=None, operation=locked.operation, settings_obj=srp, now=now
        )
        if blockers:
            raise SrpError("not_eligible", blockers[0])
        previous = locked.status
        evaluation = evaluate(
            km=km,
            user_id=locked.user_id,
            corporation_id=locked.corporation_id,
            scope=locked.scope,
            operation=locked.operation,
            settings_obj=srp,
            exclude_claim_id=locked.pk,
            locking=True,
            now=now,
        )
        status, reason, rejection = decide_submission(evaluation.calculation, evaluation.policy)
        apply_evaluation(locked, evaluation, km)
        locked.status = status
        locked.submitted_at = now
        locked.auto_rejected = status == Status.REJECTED
        locked.decision_reason = reason
        locked.decided_by = None
        locked.decided_at = now if status == Status.REJECTED else None
        locked.save()
        record_event(
            locked,
            Action.AUTO_REJECT if locked.auto_rejected else Action.RESUBMIT,
            actor=actor,
            from_status=previous,
            amount=locked.suggested_amount,
            reason=reason,
            data={
                "suggested_amount": locked.suggested_amount,
                "limited_amount": locked.limited_amount,
                "over_limit": locked.over_limit,
                **({"auto_reject": rejection} if rejection else {}),
            },
            old={"status": previous},
            new={"status": locked.status, "suggested_amount": locked.suggested_amount},
        )
        if locked.auto_rejected:
            notify_after_commit(locked, "auto_reject")
    return sync_claim(claim, locked)


def recalculate_under_lock(claim, *, actor, reason: str, action: str = Action.RECALCULATE) -> SrpClaim:
    """Recalculate a pending claim with the current policy and limits; the previous calculation stays in
    the claim event. Takes the quota lock itself.

    Used by the reviewer's recalculation and, with ``Action.VALUE_UPDATE``, when the loss value arrives.
    """
    now = timezone.now()
    with quota.quota_lock() as srp:
        locked = lock_claim(claim)
        if locked.status != Status.PENDING:
            raise wrong_state(locked, texts.SRP_ONLY_PENDING_RECALCULATED())
        km = locked.killmail
        previous = calculation_snapshot(locked)
        evaluation = evaluate(
            km=km,
            user_id=locked.user_id,
            corporation_id=locked.corporation_id,
            scope=locked.scope,
            operation=locked.operation,
            settings_obj=srp,
            exclude_claim_id=locked.pk,
            locking=True,
            now=now,
        )
        apply_evaluation(locked, evaluation, km)
        locked.save()
        record_event(
            locked,
            action,
            actor=actor,
            from_status=locked.status,
            amount=locked.suggested_amount,
            reason=reason,
            data={"previous": previous},
            old={
                "suggested_amount": previous["suggested_amount"],
                "limited_amount": previous["limited_amount"],
                "payout_basis": previous["payout_basis"],
            },
            new={
                "suggested_amount": locked.suggested_amount,
                "limited_amount": locked.limited_amount,
                "payout_basis": locked.payout_basis,
            },
        )
    return sync_claim(claim, locked)


def _refresh_shown_value(claim) -> None:
    """A claim paid from the payout table does not depend on the value; only the value shown is updated."""
    with quota.quota_lock():
        locked = lock_claim(claim)
        if locked.status != Status.PENDING:
            return
        policy = PolicySnapshot.from_json(locked.policy)
        value, source = srp_values.loss_value(locked.killmail, policy.value_source)
        value = stored_value(value)
        if value == locked.loss_value and source == locked.value_source:
            return
        previous = {
            "loss_value": None if locked.loss_value is None else str(locked.loss_value),
            "value_source": locked.value_source,
        }
        locked.loss_value, locked.value_source = value, source
        calculation = dict(locked.calculation or {})
        calculation["loss_value"] = None if value is None else str(value)
        calculation["value_source"] = source
        locked.calculation = calculation
        locked.save(update_fields=["loss_value", "value_source", "calculation", "updated_at"])
        record_event(
            locked,
            Action.VALUE_UPDATE,
            actor=None,
            from_status=locked.status,
            reason=texts.SRP_REASON_ZKILL_VALUE(),
            data={"previous": previous, "loss_value": calculation["loss_value"], "value_source": source},
            old=previous,
            new={"loss_value": calculation["loss_value"], "value_source": source},
        )


def on_value_updated(km: SrpKillmail) -> None:
    """The killmail got a value: recalculate its pending claim when the payout depends on the value.

    A claim paid from the payout table only shows the new value. Decided and withdrawn claims never change.
    """
    claim = SrpClaim.objects.filter(killmail=km, status=Status.PENDING).first()
    if claim is None:
        return
    if claim.payout_basis == SrpClaim.PayoutBasis.RULE:
        _refresh_shown_value(claim)
        return
    try:
        recalculate_under_lock(claim, actor=None, reason=texts.SRP_REASON_ZKILL_VALUE(), action=Action.VALUE_UPDATE)
    except SrpStateError:
        # Decided or withdrawn while the value was being looked up.
        return
