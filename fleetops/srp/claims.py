# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Submitting claims: the preview a member confirms, the claim itself, withdrawing and submitting again.

The helpers at the top (``record_event``, ``evaluate``, ``apply_evaluation``, the warning helpers) are shared
with ``review``, which decides on the claims created here.
"""

import copy
from dataclasses import dataclass
from datetime import timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal

from allianceauth.services.hooks import get_extension_logger
from django.core import signing
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from fleetops import texts
from fleetops.models import MAX_DATABASE_ID, SrpCategory, SrpClaim, SrpClaimEvent, SrpFleetLink, SrpKillmail, SrpSettings
from fleetops.providers.zkillboard import MIN_KILL_AGE
from fleetops.services.audit import audit
from fleetops.services.identity import character_label
from fleetops.services.periods import format_eve
from fleetops.srp import access, bans, categories, discovery, display, killmails, legacy, notify, presence, quota
from fleetops.srp import policy as rules
from fleetops.srp import values as srp_values
from fleetops.srp.types import (
    BINDING_ORDER,
    LIMIT_CATEGORY_CORPORATION,
    LIMIT_CATEGORY_PERSONAL,
    LIMIT_CORPORATION,
    LIMIT_PERSONAL,
    MONTHLY_KINDS,
    Calculation,
    ClaimIdentity,
    ClaimPreview,
    ClaimWarning,
    KillmailRef,
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

# Warnings that follow from the calculation and are replaced when a claim is calculated again.
CALCULATION_WARNING_CODES = frozenset({"over_limit", "reduced_by_limit", "value_estimate", "value_pending", "value_missing"})
# Warnings that follow from the fleet presence check and are replaced when it is checked again.
PRESENCE_WARNING_CODES = frozenset(
    {display.NEAR, display.NOT_IN_FLEET, display.NEVER_SEEN, display.REGISTERED, display.OUTSIDE_WINDOW, "ship_mismatch"}
)

# Warnings about the same loss on another site's claims (aa-srp); replaced when the loss of a claim is replaced.
EXTERNAL_WARNING_CODES = frozenset({"external_claim"})
# A reviewer who moved a claim between fleet and category leaves the claimant only the comment and the loss.
REVIEWER_MOVE_ACTIONS = (Action.RECLASSIFY, Action.RECATEGORIZE)
# "Not given" for the arguments of ``edit_claim``: None is a value there (no category, no link).
UNSET = object()

# Warnings that follow from the link of the claim (a loss of another day than the standing link's) and are replaced
# whenever the link, the scope or the category changes. They are kept apart from the presence codes on purpose:
# checking the fleet presence again must not drop them.
LINK_WARNING_CODES = frozenset({"outside_link_date"})

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
    if kind == LIMIT_CATEGORY_PERSONAL:
        return texts.SRP_LIMIT_CATEGORY_PERSONAL()
    if kind == LIMIT_CATEGORY_CORPORATION:
        return texts.SRP_LIMIT_CATEGORY_CORPORATION()
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


def require_native(claim) -> None:
    """Refuse an action on a claim imported from aa-srp (IMP-6): it is a record of what happened there. Only an
    internal note may be added to it. Raises ``SrpStateError`` (code ``imported``)."""
    if claim.source == SrpClaim.Source.AASRP:
        raise SrpStateError("imported", texts.SRP_IMPORTED_READ_ONLY())


def lock_native(claim) -> SrpClaim:
    """``lock_claim`` for the actions that change a claim: an imported claim is refused."""
    locked = lock_claim(claim)
    require_native(locked)
    return locked


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
    audit_reason=None,
) -> SrpClaimEvent:
    """Write the claim event (the timeline of the claim) and the audit entry of the same step.

    The audit action is ``srp.claim_<action>``; a value update is audited as a recalculation. ``old`` and ``new``
    are the audit values (status, amounts, scope), ``reason`` is the reason given. The text of an internal event
    (a manager's note) stays on the claim event: the audit log is open to people who may not see it. ``audit_reason``
    replaces the reason of the audit entry (``""`` keeps a text on the event only, such as a member's comment).
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
    if audit_reason is None:
        audit_reason = "" if internal else reason
    audit(actor, f"srp.claim_{audit_action}", claim, old, new, audit_reason)
    if claim.link_id and from_status and from_status != claim.status:
        # A claim that is settled (or unsettled again) can complete its link, or take it back to closed.
        from fleetops.srp import links

        links.settle_after_commit(claim.link_id)
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
    category,
    exclude_claim_id=None,
    locking: bool = False,
    now=None,
) -> Evaluation:
    """Match the payout rule, snapshot the policy, pick the loss value and calculate with the monthly limits.

    ``category`` is the claim's category (None for none). It has no default on purpose: a caller that forgot
    it would quietly calculate the claim as one without a category.

    ``locking`` reads the usage rows for update: use it inside ``quota.quota_lock()`` when the result is stored.
    """
    rule = rules.match_rule(
        ship_type_id=km.ship_type_id,
        scope=scope,
        operation=operation,
        capsules_need_rule=settings_obj.capsules_need_rule,
        category=category,
    )
    policy = rules.current_policy(
        corporation_id=corporation_id, rule=rule, settings_obj=settings_obj, category=category
    )
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
    """The monthly limit that kept an amount down (``personal``, ``corporation`` or one of the category's two);
    empty without one."""
    if calculation.binding_limit in MONTHLY_KINDS:
        return calculation.binding_limit
    states = [state for state in calculation.limits if state.effective_remaining is not None]
    if not states:
        return ""
    return min(states, key=lambda state: (state.effective_remaining, BINDING_ORDER.get(state.kind, 9))).kind


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
    # The link warnings are renewed on every path that sets the presence (submit, recheck, change of scope).
    apply_link_warnings(claim)


def outside_link_date(link, killmail_time) -> bool:
    """Whether a loss happened on another EVE day than the date of the standing link it is claimed through."""
    link_date = getattr(link, "link_date", None)
    if link is None or link_date is None or killmail_time is None:
        return False
    return killmail_time.astimezone(dt_timezone.utc).date() != link_date


def link_warnings_for(link, killmail_time) -> list[ClaimWarning]:
    """The warnings that follow from the link: ``outside_link_date`` (batch approval skips such a claim)."""
    if not outside_link_date(link, killmail_time):
        return []
    return [
        ClaimWarning(
            "outside_link_date",
            display.LEVEL_WARNING,
            {
                "loss_date": killmail_time.astimezone(dt_timezone.utc).date().isoformat(),
                "link_date": link.link_date.isoformat(),
            },
        )
    ]


def link_warnings(claim) -> list[ClaimWarning]:
    """The link warnings of a claim as it is now."""
    return link_warnings_for(claim.link if claim.link_id else None, claim.killmail_time)


def apply_link_warnings(claim) -> None:
    """Replace the link warnings of the claim (in memory); other warnings stay."""
    claim.warnings = replace_warnings(claim.warnings, LINK_WARNING_CODES, link_warnings(claim))


def calculation_snapshot(claim) -> dict:
    """What a recalculation replaces, kept in the claim event."""
    return {
        "scope": claim.scope,
        "category_id": claim.category_id,
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


def fleet_fields(link, category=None) -> dict:
    """The claim fields that name the fleet of an in-fleet claim (all empty for an out-of-fleet claim) and the
    claim's category."""
    if link is None:
        return {
            "link": None,
            "operation": None,
            "fleet_label": "",
            "fc_user": None,
            "fc_main_character_name": "",
            "category": category,
        }
    operation = link.operation
    if operation is None:
        # A link without a fleet (a standing link): the claim carries the link's name; there is no FC.
        return {
            "category": category,
            "link": link,
            "operation": None,
            "fleet_label": link.name,
            "fc_user": link.fc_user,
            "fc_main_character_name": link.fc_main_character_name,
        }
    return {
        "category": category,
        "link": link,
        "operation": operation,
        "fleet_label": fleet_label(operation),
        "fc_user": link.fc_user,
        "fc_main_character_name": link.fc_main_character_name or operation.fc_main_character_name,
    }


def loss_presence(scope, link, km) -> PresenceResult:
    """The fleet presence of the victim for a loss claimed through ``link`` (not applicable out of fleet)."""
    if scope != Scope.IN_FLEET or link is None or link.operation_id is None or km.victim_character_id is None:
        # Only a link with a fleet has a roster to check; a standing link is never "in fleet".
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
    if access.is_srp_staff(user) and existing.main_character_name:
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
    if km.origin == SrpKillmail.Origin.AASRP or km.killmail_time > now - MIN_KILL_AGE:
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


def _chosen_category(category_id) -> SrpCategory | None:
    """The category named in a preview token; an ID that no longer exists is refused like an unavailable one."""
    if category_id is None:
        return None
    try:
        return SrpCategory.objects.get(pk=category_id)
    except (SrpCategory.DoesNotExist, TypeError, ValueError):
        raise SrpError("category_unavailable", texts.SRP_CATEGORY_UNAVAILABLE()) from None


def is_standing(link) -> bool:
    return link is not None and link.kind == SrpFleetLink.Kind.STANDING


def standing_target(*, scope, link, category, killmail_time, srp):
    """``(link, blocker)`` for a loss outside a fleet that was not made through a link but with a category.

    The category's open standing link of the day of the loss is used when there is one. Without one the claim only
    records the category, unless the settings (``category_claims_without_link``) ask for a link: then ``blocker`` says
    that the category has no open link for that day. Other claims (in a fleet, through a link, without a category)
    are left as they are.
    """
    if scope != Scope.OUT_OF_FLEET or link is not None or category is None:
        return link, None
    from fleetops.srp import links

    found = links.resolve_standing_link(category, links.eve_date_of(killmail_time))
    if found is not None:
        return found, None
    if not srp.category_claims_without_link:
        return None, texts.SRP_NO_LINK_FOR_DAY()
    return None, None


def prepare_claim(
    user, text: str, *, link: SrpFleetLink | None = None, category: SrpCategory | None = None
) -> ClaimPreview:
    """Read the pasted killmail and check it for ``user``: ownership, eligibility, fleet presence, payout
    rule, policy and limits. Throttled to 20 checks per 10 minutes and user. Raises ``SrpError``.

    ``link`` makes it a loss of that fleet. ``category`` is the category a member chose for a loss outside a
    fleet (ignored in a fleet). A loss that is not the user's, or that is claimed already, gets its blockers
    only: no calculation, presence or policy.
    """
    _throttle(user)
    reference = killmails.parse_killmail_reference(text)
    return _prepare(user, reference, link=link, category=category, source=SrpClaim.Source.PASTE)


def prepare_claim_for_ref(
    user,
    ref: KillmailRef,
    *,
    link: SrpFleetLink | None = None,
    category: SrpCategory | None = None,
    source: str = SrpClaim.Source.PASTE,
) -> ClaimPreview:
    """The check of ``prepare_claim`` for a killmail that is known already (a loss the scan found: ID and hash).
    The same throttle applies."""
    _throttle(user)
    return _prepare(user, ref, link=link, category=category, source=source)


def _prepare(user, reference, *, link, category, source) -> ClaimPreview:
    srp = SrpSettings.get_solo()
    now = timezone.now()
    km = killmails.fetch_killmail(reference, zkill_ok=srp.zkillboard_enabled)
    scope = Scope.IN_FLEET if link is not None and link.kind == link.Kind.FLEET else Scope.OUT_OF_FLEET
    preview = ClaimPreview(killmail=km, scope=scope, link=link, user=user, source=source)

    existing = SrpClaim.objects.filter(killmail=km).first()
    if existing is not None:
        preview.existing_claim = existing
        preview.blockers = [duplicate_message(user, existing)]
        return preview

    operation = link.operation if link is not None else None
    # The category first: its payout rules decide, for example, whether a capsule is covered. A category that is
    # required but not chosen stops the check with a message instead of becoming a blocker.
    resolved = categories.resolve_category(
        scope=scope, link=link, operation=operation, chosen=category, settings_obj=srp
    )
    preview.category = resolved
    # A category without a link: the standing link of that category for the day of the loss, if there is one.
    link, no_link = standing_target(
        scope=scope, link=link, category=resolved, killmail_time=km.killmail_time, srp=srp
    )
    preview.link = link
    preview.blockers = rules.eligibility(
        km=km,
        user=user,
        scope=scope,
        link=link,
        settings_obj=srp,
        now=now,
        category=resolved,
        via_standing_link=is_standing(link),
        missing_alts=discovery.unauthorized_characters(user, srp),
    )
    if no_link:
        preview.blockers.append(no_link)
    preview.identity = claim_identity(user, km)
    identity = preview.identity
    if identity is None or identity.main_character_id is None:
        return preview

    preview.presence = loss_presence(scope, link, km)
    if scope == Scope.OUT_OF_FLEET and srp.enabled:
        preview.suggested_link = presence.find_fleet_for_loss(km.victim_character_id, km.killmail_time)
    evaluation_args = {
        "km": km,
        "user_id": user.pk,
        "corporation_id": identity.corporation_id,
        "scope": scope,
        "operation": operation,
        "settings_obj": srp,
        "category": resolved,
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
        *link_warnings_for(link, km.killmail_time),
        *_external_warnings(km.killmail_id),
    ]
    preview.eligible = not preview.blockers
    if preview.eligible:
        preview.token = signing.dumps(
            {
                "km": km.pk,
                "link": link.code if link is not None else None,
                "u": user.pk,
                "cat": category.pk if category is not None and scope == Scope.OUT_OF_FLEET and not is_standing(link) else None,
                "src": source,
            },
            salt=PREVIEW_SALT,
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
    scope = Scope.IN_FLEET if link is not None and link.kind == link.Kind.FLEET else Scope.OUT_OF_FLEET
    chosen = _chosen_category(data.get("cat")) if scope == Scope.OUT_OF_FLEET else None
    # Tokens of earlier versions carry no source: those claims were pasted.
    source = data.get("src") if data.get("src") in (SrpClaim.Source.PASTE, SrpClaim.Source.DISCOVERED) else SrpClaim.Source.PASTE
    now = timezone.now()
    presence_result = loss_presence(scope, link, km)
    external = _external_warnings(km.killmail_id)

    with quota.quota_lock() as srp:
        # With the settings of the lock: no read of its own, and none at all while discovery is off.
        missing_alts = discovery.unauthorized_characters(user, srp)
        # The killmail again, locked: a value stored since it was read above is used, and a value that arrives
        # later waits for this claim and then recalculates it.
        try:
            km = SrpKillmail.objects.select_for_update().get(pk=km.pk)
        except SrpKillmail.DoesNotExist:
            raise SrpError("expired", EXPIRED_TEXT) from None
        duplicate = _duplicate(user, km)
        if duplicate is not None:
            raise duplicate
        if link is not None:
            # The link as it is now: completing or closing a link takes this lock too.
            try:
                link.refresh_from_db(fields=["is_open"])
            except SrpFleetLink.DoesNotExist:
                raise SrpError("expired", EXPIRED_TEXT) from None
        operation = link.operation if link is not None else None
        category = categories.resolve_category(
            scope=scope, link=link, operation=operation, chosen=chosen, settings_obj=srp
        )
        # A category without a link: the standing link of the day of the loss as it is now.
        token_link = link
        link, no_link = standing_target(
            scope=scope, link=link, category=category, killmail_time=km.killmail_time, srp=srp
        )
        if link is not token_link:
            # A standing link has no roster: the presence is "not applicable" and costs no query.
            presence_result = loss_presence(scope, link, km)
        blockers = rules.eligibility(
            km=km,
            user=user,
            scope=scope,
            link=link,
            settings_obj=srp,
            now=now,
            category=category,
            via_standing_link=is_standing(link),
            missing_alts=missing_alts,
        )
        if no_link:
            blockers.append(no_link)
        if blockers:
            raise SrpError("not_eligible", blockers[0])
        identity = claim_identity(user, km)
        evaluation = evaluate(
            km=km,
            user_id=user.pk,
            corporation_id=identity.corporation_id,
            scope=scope,
            operation=operation,
            settings_obj=srp,
            category=category,
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
            source=source,
            status=status,
            submitted_at=now,
            **fleet_fields(link, category),
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
        # A member who keeps submitting although a personal monthly limit is used up (setting, off by default).
        auto_ban = bans.auto_ban_for_cap(user, claim, evaluation.calculation, srp, now=now)
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
                "category": category.key if category is not None else None,
                "source": source,
                **({"auto_reject": rejection} if rejection else {}),
                **({"auto_banned": True} if auto_ban is not None else {}),
            },
            new={
                "status": claim.status,
                "scope": scope,
                "category": category.key if category is not None else None,
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
        locked = lock_native(claim)
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
        missing_alts = discovery.unauthorized_characters(actor, srp)
        locked = lock_native(claim)
        if locked.status != Status.WITHDRAWN:
            raise wrong_state(locked, texts.SRP_ONLY_WITHDRAWN_RESUBMITTED(), Status.PENDING)
        km = locked.killmail
        # The claim's own link is passed but not checked for being open: a closed link does not stop a claim that
        # exists. A claim under a standing link is not held back by the switch for losses outside a fleet; the payout
        # rules of its fleet still apply, as they do in the calculation below.
        blockers = rules.eligibility(
            km=km,
            user=actor,
            scope=locked.scope,
            link=locked.link,
            operation=locked.operation,
            settings_obj=srp,
            now=now,
            category=locked.category,
            via_standing_link=is_standing(locked.link),
            check_link_open=False,
            missing_alts=missing_alts,
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
            category=locked.category,
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
        # The numbers were calculated anew: a reviewer who opened the claim before must see it again (INT-13).
        locked.member_revision += 1
        locked.member_edited_at = now
        locked.save()
        auto_ban = bans.auto_ban_for_cap(actor, locked, evaluation.calculation, srp, now=now)
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
                **({"auto_banned": True} if auto_ban is not None else {}),
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
        locked = lock_native(claim)
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
            category=locked.category,
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


# --- The claimant changes a pending claim (INT-13) ----------------------------------------------------------


def resolve_target(text):
    """``(link, category)`` for a choice like ``fleet:<code>``, ``link:<code>`` or ``category:<id>``. Raises ``SrpError``.

    The same choices the list of found losses offers; an empty text is not handled here (the callers decide what it
    means)."""
    kind, _separator, value = (text or "").strip().partition(":")
    if kind in ("fleet", "link"):
        wanted = SrpFleetLink.Kind.FLEET if kind == "fleet" else SrpFleetLink.Kind.STANDING
        link = (
            SrpFleetLink.objects.select_related("operation__fleet_type", "fc_user", "category")
            .filter(code=value[:64], kind=wanted)
            .first()
        )
        if link is None:
            raise SrpError("link_unavailable", texts.SRP_LOSS_LINK_UNAVAILABLE())
        return link, None
    if kind == "category" and value.isdigit() and int(value) <= MAX_DATABASE_ID:
        category = SrpCategory.objects.filter(pk=int(value)).first()
        if category is None:
            raise SrpError("category_unavailable", texts.SRP_CATEGORY_UNAVAILABLE())
        return None, category
    raise SrpError("invalid_target", texts.SRP_LOSS_LINK_UNAVAILABLE())


@dataclass(slots=True)
class ClaimEdit:
    """The outcome of ``edit_claim``: the claim (saved, or as it would be after a dry run), what changed and the
    calculation before and after."""

    claim: SrpClaim
    changed: list
    previous: dict
    evaluation: Evaluation | None = None
    killmail: SrpKillmail | None = None
    old_killmail_id: int | None = None
    month_changed: bool = False
    revision: int = 0


def _link_code(link):
    return link.code if link is not None else None


def _category_key(category):
    return category.key if category is not None else None


def _edit_target(*, locked, srp, category, target_link, killmail_time):
    """``(scope, link, category, no_link)`` a change of category or link leads to; raises ``SrpError``.

    A fleet link makes the claim in-fleet with the category of the fleet; a standing link gives its own category;
    a category alone (or nothing) is a claim outside a fleet, put under the day's standing link of the category when
    there is one (see ``standing_target``).
    """
    if target_link is not UNSET and target_link is not None:
        link = (
            SrpFleetLink.objects.select_related("operation__fleet_type", "fc_user", "category")
            .filter(pk=target_link.pk)
            .first()
        )
        if link is None:
            raise SrpError("link_unavailable", texts.SRP_LOSS_LINK_UNAVAILABLE())
        if link.kind == SrpFleetLink.Kind.FLEET and link.operation_id is not None:
            scope = Scope.IN_FLEET
        elif link.kind == SrpFleetLink.Kind.STANDING:
            scope = Scope.OUT_OF_FLEET
        else:
            raise SrpError("link_unavailable", texts.SRP_LOSS_LINK_UNAVAILABLE())
        resolved = categories.resolve_category(
            scope=scope, link=link, operation=link.operation, chosen=None, settings_obj=srp
        )
        return scope, link, resolved, None
    chosen = None if category is UNSET else category
    resolved = categories.resolve_category(
        scope=Scope.OUT_OF_FLEET, link=None, operation=None, chosen=chosen, settings_obj=srp
    )
    link, no_link = standing_target(
        scope=Scope.OUT_OF_FLEET, link=None, category=resolved, killmail_time=killmail_time, srp=srp
    )
    return Scope.OUT_OF_FLEET, link, resolved, no_link


def edit_claim(
    claim: SrpClaim,
    *,
    actor,
    comment=UNSET,
    category=UNSET,
    target_link=UNSET,
    new_ref: KillmailRef | None = None,
    expected_revision: int | None = None,
    dry_run: bool = False,
) -> ClaimEdit:
    """The claimant changes a pending claim: the comment, the category or link, the loss. Raises ``SrpError``.

    ``comment`` is the new text; ``category`` and ``target_link`` name where the loss belongs (``None`` is a value:
    no category, no link; ``UNSET`` leaves it as it is); ``new_ref`` replaces the loss (read from ESI before the lock is
    taken). A reviewer who moved the claim (reclassify, recategorize) leaves the claimant only the comment and the loss.
    A change of category, link or loss calculates the claim again like a new one, with the rules of the new place
    (eligibility without the "link is open" check where the link does not change); a result that would reject the claim
    on the spot (``over_limit_action`` reject, nothing left) is refused and the claim stays as it was.

    Writes ``member_revision`` + 1 and ``member_edited_at`` and the ``member_edit`` event (audit ``srp.claim_member_edit``;
    the text of a comment stays on the event). ``expected_revision`` is the revision the previewed change was made
    for. With ``dry_run`` nothing is written and ``ClaimEdit.claim`` is the claim as it would be.
    """
    if actor is None or claim.user_id != actor.pk:
        raise SrpError("forbidden", texts.SRP_ONLY_CLAIMANT_EDITS())
    if comment is not UNSET:
        comment = _clean_comment(comment)
    km_new = None
    if new_ref is not None:
        settings_now = SrpSettings.get_solo()
        if not settings_now.member_edit_enabled:
            raise SrpError("edit_off", texts.SRP_MEMBER_EDIT_OFF())
        if not settings_now.member_swap_loss_enabled:
            raise SrpError("swap_off", texts.SRP_EDIT_SWAP_OFF())
        # The network read comes before the lock; the quota lock never waits for ESI.
        km_new = killmails.fetch_killmail(new_ref, zkill_ok=settings_now.zkillboard_enabled)
    now = timezone.now()

    with quota.quota_lock() as srp:
        missing_alts = discovery.unauthorized_characters(actor, srp)
        locked = lock_native(claim)
        if not srp.member_edit_enabled:
            raise SrpError("edit_off", texts.SRP_MEMBER_EDIT_OFF())
        if locked.status != Status.PENDING:
            raise wrong_state(locked, texts.SRP_ONLY_PENDING_EDITED())
        if expected_revision is not None and locked.member_revision != expected_revision:
            raise SrpStateError("changed", texts.SRP_EDIT_STALE())
        ban = bans.active_ban(actor, now)
        if ban is not None:
            # A banned member can still withdraw the claim.
            raise SrpError("banned", bans.ban_message(ban))
        if km_new is not None and not srp.member_swap_loss_enabled:
            raise SrpError("swap_off", texts.SRP_EDIT_SWAP_OFF())

        wants_where = category is not UNSET or target_link is not UNSET
        if wants_where and locked.events.filter(action__in=REVIEWER_MOVE_ACTIONS).exists():
            raise SrpError("after_review", texts.SRP_EDIT_AFTER_REVIEW())

        work = copy.copy(locked) if dry_run else locked
        old_link_id = locked.link_id
        old_km = locked.killmail
        km = old_km
        changed = []
        before = {}
        after = {}

        if comment is not UNSET and comment != locked.member_comment:
            changed.append("comment")
            before["comment"] = locked.member_comment
            after["comment"] = comment
            work.member_comment = comment

        link, scope, resolved, no_link = locked.link, locked.scope, locked.category, None
        where_changed = False
        if km_new is not None:
            try:
                km_new = SrpKillmail.objects.select_for_update().get(pk=km_new.pk)
            except SrpKillmail.DoesNotExist:
                raise SrpError("expired", EXPIRED_TEXT) from None
            if km_new.pk == old_km.pk:
                raise SrpError("same_loss", texts.SRP_EDIT_SAME_LOSS())
            duplicate = _duplicate(actor, km_new)
            if duplicate is not None:
                raise duplicate
            km = km_new
        if wants_where:
            scope, link, resolved, no_link = _edit_target(
                locked=locked, srp=srp, category=category, target_link=target_link, killmail_time=km.killmail_time
            )
            where_changed = (scope, link.pk if link is not None else None, resolved.pk if resolved is not None else None) != (
                locked.scope,
                locked.link_id,
                locked.category_id,
            )
        swapped = km_new is not None

        previous = calculation_snapshot(locked)
        evaluation = None
        if where_changed or swapped:
            blockers = rules.eligibility(
                km=km,
                user=actor,
                scope=scope,
                link=link,
                operation=link.operation if link is not None else None,
                settings_obj=srp,
                now=now,
                category=resolved,
                via_standing_link=is_standing(link),
                # The link of a claim that stays where it is need not be open; a new one has to be.
                check_link_open=where_changed,
                missing_alts=missing_alts,
            )
            if where_changed and no_link:
                blockers.append(no_link)
            if blockers:
                raise SrpError("not_eligible", blockers[0])

        if where_changed:
            if _link_code(link) != _link_code(locked.link):
                before["link"], after["link"] = _link_code(locked.link), _link_code(link)
                changed.append("link")
            if (resolved.pk if resolved is not None else None) != locked.category_id:
                before["category"], after["category"] = _category_key(locked.category), _category_key(resolved)
                changed.append("category")
            if scope != locked.scope:
                before["scope"], after["scope"] = locked.scope, scope
            work.scope = scope
            for field, value in fleet_fields(link, resolved).items():
                setattr(work, field, value)
        if swapped:
            identity = claim_identity(actor, km)
            changed.append("loss")
            before["killmail_id"], after["killmail_id"] = old_km.pk, km.pk
            work.killmail = km
            work.killmail_time = km.killmail_time
            work.ship_type_id = km.ship_type_id
            work.ship_type_name = km.ship_type_name
            work.solar_system_name = km.solar_system_name
            if identity is not None:
                work.character_id, work.character_name = identity.character_id, identity.character_name
        if not changed:
            raise SrpError("nothing_changed", texts.SRP_EDIT_NOTHING())

        if where_changed or swapped:
            evaluation = evaluate(
                km=km,
                user_id=locked.user_id,
                corporation_id=locked.corporation_id,
                scope=work.scope,
                operation=work.operation,
                settings_obj=srp,
                category=work.category,
                exclude_claim_id=locked.pk,
                locking=True,
                now=now,
            )
            status, _reason, _rejection = decide_submission(evaluation.calculation, evaluation.policy)
            if status == Status.REJECTED:
                raise SrpError("edit_would_reject", texts.SRP_EDIT_WOULD_REJECT())
            apply_evaluation(work, evaluation, km)
            apply_presence(work, loss_presence(work.scope, work.link, km), now)
            if swapped:
                work.warnings = replace_warnings(work.warnings, EXTERNAL_WARNING_CODES, _external_warnings(km.killmail_id))

        month_changed = swapped and quota.claim_month(old_km) != quota.claim_month(km)
        work.member_revision = locked.member_revision + 1
        work.member_edited_at = now
        result = ClaimEdit(
            claim=work,
            changed=changed,
            previous=previous,
            evaluation=evaluation,
            killmail=km,
            old_killmail_id=old_km.pk if swapped else None,
            month_changed=month_changed,
            revision=locked.member_revision,
        )
        if dry_run:
            return result

        try:
            with transaction.atomic():
                work.save()
        except IntegrityError:
            # The killmail was claimed meanwhile by a row that bypassed the lock (an import).
            duplicate = _duplicate(actor, km)
            if duplicate is None:
                raise
            raise duplicate from None
        if swapped and km.origin != SrpKillmail.Origin.AASRP and km.zkill_next_attempt_at is None:
            # A killmail read for this change waits for its value like any other; an imported placeholder never does.
            killmails.schedule_zkill(km)
        data = {"changed": changed, "from": before, "to": after, "revision": work.member_revision}
        if evaluation is not None:
            data["previous"] = previous
        if swapped:
            data["old_killmail_id"] = old_km.pk
        audit_new = {key: value for key, value in after.items() if key != "comment"}
        if "comment" in after:
            audit_new["comment_length"] = len(after["comment"])
        record_event(
            work,
            Action.MEMBER_EDIT,
            actor=actor,
            from_status=Status.PENDING,
            amount=work.suggested_amount,
            reason=after.get("comment", ""),
            data=data,
            old={"revision": locked.member_revision, "suggested_amount": previous["suggested_amount"]},
            new={
                "changed": changed,
                "revision": work.member_revision,
                "suggested_amount": work.suggested_amount,
                **audit_new,
            },
            audit_reason="",
        )
        if "link" in changed:
            # The claim left its old link (that one may be settled now) and joined another.
            from fleetops.srp import links as srp_links

            srp_links.settle_after_commit(old_link_id, link.pk if link is not None else None)
    sync_claim(claim, work)
    result.claim = claim
    return result


EDIT_SALT = "fleetops.srp.edit"
EDIT_MAX_AGE = 3600


def edit_reference(user, text: str = "", loss_id=None) -> KillmailRef | None:
    """The loss a member wants to put on a claim instead: pasted text, or a loss of the member's own list of found
    losses (by killmail ID). None when neither is given; both is an error. Counts toward the preview throttle."""
    text = (text or "").strip()
    if text and loss_id:
        raise SrpError("loss_source", texts.SRP_EDIT_LOSS_SOURCE())
    if text:
        _throttle(user)
        return killmails.parse_killmail_reference(text)
    if loss_id:
        loss = discovery.owned_loss(user, loss_id) if 0 < int(loss_id) <= MAX_DATABASE_ID else None
        if loss is None:
            raise SrpError("loss_unavailable", texts.SRP_LOSS_LINK_UNAVAILABLE())
        _throttle(user)
        return KillmailRef(loss.killmail_id, loss.killmail_hash or None, "discovered")
    return None


def _edit_arguments(target, comment, ref):
    """The keyword arguments of ``edit_claim`` for the posted choices (``target`` is ``fleet:<code>`` and the like)."""
    arguments = {}
    if comment is not UNSET:
        arguments["comment"] = comment
    if target:
        link, category = resolve_target(target)
        arguments["target_link"] = link
        if link is None:
            arguments["category"] = category
    if ref is not None:
        arguments["new_ref"] = ref
    return arguments


def preview_edit(user, claim, *, comment=UNSET, target: str = "", ref: KillmailRef | None = None):
    """``(ClaimEdit, token)`` for a change the member asked for: what the claim would be after it, nothing written.
    The token (signed, one hour, this user) carries the change and the revision it was checked against, for
    ``submit_edit``. Raises ``SrpError``."""
    arguments = _edit_arguments(target, comment, ref)
    result = edit_claim(claim, actor=user, dry_run=True, **arguments)
    payload = {"claim": claim.pk, "rev": result.revision, "u": user.pk}
    if comment is not UNSET:
        payload["cmt"] = comment
    if target:
        payload["tgt"] = target
    if ref is not None:
        payload["km"] = result.killmail.pk
    return result, signing.dumps(payload, salt=EDIT_SALT)


def submit_edit(user, claim, token: str) -> ClaimEdit:
    """Apply the change a previewed token stands for. A claim that changed since the preview (its revision moved on) or
    an expired token is refused. Raises ``SrpError``."""
    try:
        data = signing.loads(token or "", salt=EDIT_SALT, max_age=EDIT_MAX_AGE)
    except signing.BadSignature:
        raise SrpError("invalid_token", EXPIRED_TEXT) from None
    if not isinstance(data, dict) or data.get("u") != user.pk or data.get("claim") != claim.pk:
        raise SrpError("invalid_token", EXPIRED_TEXT)
    ref = KillmailRef(int(data["km"]), None, "edit") if data.get("km") else None
    arguments = _edit_arguments(data.get("tgt") or "", data["cmt"] if "cmt" in data else UNSET, ref)
    return edit_claim(claim, actor=user, expected_revision=data.get("rev"), **arguments)
