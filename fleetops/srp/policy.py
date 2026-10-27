# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""SRP rules: which payout rule applies, what a loss is worth under the policy and who may claim it.

``calculate`` is a pure function of a policy snapshot, the loss and the monthly limit states, so the same
numbers come out when a claim is submitted, recalculated or looked at later. The checks that need the
database (rule matching, the settings snapshot, eligibility) sit next to it.
"""

from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal

from allianceauth.authentication.models import CharacterOwnership
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy

from fleetops.constants import CAPSULE_TYPE_IDS, MAX_ISK  # noqa: F401 - CAPSULE_TYPE_IDS is part of the interface
from fleetops.models import SrpClaim, SrpCorporationLimit, SrpPayoutRule, SrpSettings, srp_doctrine_key
from fleetops.srp import display
from fleetops.srp.types import (
    CORPORATION_CAP_DEFAULT,
    CORPORATION_CAP_OVERRIDE,
    LIMIT_CORPORATION,
    LIMIT_LOSS_CAP,
    LIMIT_PERSONAL,
    MATCH_DOCTRINE,
    MATCH_DOCTRINE_SHIP,
    MATCH_SHIP,
    CalcNote,
    Calculation,
    LimitState,
    PolicySnapshot,
    RuleSnapshot,
)

IN_FLEET = SrpPayoutRule.Scope.IN_FLEET
OUT_OF_FLEET = SrpPayoutRule.Scope.OUT_OF_FLEET
ANY_SCOPE = SrpPayoutRule.Scope.ANY

BASIS_RULE = "rule"
BASIS_PERCENTAGE = "percentage"
BASIS_NONE = "none"

# Texts for the member; the reasons a loss cannot be claimed.
BLOCK_SRP_DISABLED = gettext_lazy("FleetOps SRP is not enabled.")
BLOCK_LINK_CLOSED = gettext_lazy("This SRP link is closed.")
BLOCK_NO_PILOT = gettext_lazy("This loss has no pilot.")
BLOCK_NOT_YOUR_CHARACTER = gettext_lazy("This character is not on your Alliance Auth account.")
BLOCK_NO_MAIN = gettext_lazy("Set a main character first.")
BLOCK_FLEET_ONLY = gettext_lazy("Only losses in FleetOps fleets are covered.")
BLOCK_CAPSULE = gettext_lazy("Capsule losses are not covered.")

# Ties between limits that leave the same amount: the monthly limits come before the per-loss cap,
# and the personal limit before the corporation's.
_BINDING_ORDER = {LIMIT_PERSONAL: 0, LIMIT_CORPORATION: 1, LIMIT_LOSS_CAP: 2}


def doctrine_key(source: str, external_id: str, name: str) -> str:
    """The key a payout rule and a fleet share when they name the same doctrine."""
    return srp_doctrine_key(source, external_id, name)


def operation_doctrine_keys(operation) -> set[str]:
    """The doctrine keys of a fleet: ``source:external_id`` and the casefolded name, whichever it has.

    A rule that names a doctrine of a doctrine app matches by source and ID; a rule with a custom name
    matches by the name, so a fleet can match both.
    """
    if operation is None:
        return set()
    keys = set()
    source = (operation.doctrine_source or "").strip()
    external_id = (operation.doctrine_external_id or "").strip()
    if source and external_id:
        keys.add(srp_doctrine_key(source, external_id, ""))
    name_key = srp_doctrine_key("", "", operation.doctrine_name)
    if name_key:
        keys.add(name_key)
    return keys


def _rule_rank(rule, scope):
    """Lower is more specific: doctrine and ship, doctrine, ship; a rule of the scope beats ``any``; a
    doctrine of a doctrine app beats a custom doctrine name."""
    if rule.doctrine_key and rule.ship_type_id:
        level = 0
    elif rule.doctrine_key:
        level = 1
    else:
        level = 2
    return level, 0 if rule.scope == scope else 1, 1 if rule.doctrine_key.startswith("name:") else 0, rule.pk


def match_rule(
    *, ship_type_id: int, scope: str, operation=None, capsules_need_rule: bool = False
) -> SrpPayoutRule | None:
    """The active payout rule for a loss, most specific first: doctrine and ship, doctrine, ship; within a
    level a rule of the scope beats ``any``. Doctrine rules need an in-fleet loss of a fleet with that doctrine.

    With ``capsules_need_rule`` a capsule is only matched by a rule that names the capsule, so a doctrine rule
    for any ship never takes the place of the capsule entry of the payout table.
    """
    condition = Q(ship_type_id=ship_type_id, doctrine_key="")
    keys = operation_doctrine_keys(operation) if scope == IN_FLEET else set()
    if keys:
        any_ship = () if capsules_need_rule and ship_type_id in CAPSULE_TYPE_IDS else (0,)
        condition |= Q(doctrine_key__in=keys, ship_type_id__in=(ship_type_id, *any_ship))
    candidates = SrpPayoutRule.objects.filter(is_active=True, scope__in=(ANY_SCOPE, scope)).filter(condition)
    return min(candidates, key=lambda rule: _rule_rank(rule, scope), default=None)


def rule_match(rule) -> str:
    """How specific a rule is: ``doctrine_ship``, ``doctrine`` or ``ship``."""
    if rule.doctrine_key and rule.ship_type_id:
        return MATCH_DOCTRINE_SHIP
    return MATCH_DOCTRINE if rule.doctrine_key else MATCH_SHIP


def rule_snapshot(rule) -> RuleSnapshot | None:
    if rule is None or isinstance(rule, RuleSnapshot):
        return rule
    return RuleSnapshot(
        id=rule.pk,
        label=rule.label,
        ship_type_id=rule.ship_type_id,
        ship_type_name=rule.ship_type_name,
        doctrine_name=rule.doctrine_name,
        scope=rule.scope,
        amount=rule.amount,
        match=rule_match(rule),
    )


def names_capsule(rule) -> bool:
    """Whether a rule is a capsule entry of the payout table: it names a capsule itself. A doctrine rule for
    any ship does not make capsules covered."""
    return rule is not None and rule.ship_type_id in CAPSULE_TYPE_IDS


def corporation_cap(corporation_id, srp, overrides=None) -> tuple[int | None, str]:
    """``(cap, source)`` of a corporation's monthly limit: its own limit when it has one (an override without
    a cap means no limit for that corporation), else the default of the settings.

    ``overrides`` is ``{corporation_id: monthly_cap}`` for callers that resolve many corporations at once.
    """
    if corporation_id:
        if overrides is None:
            row = SrpCorporationLimit.objects.filter(corporation_id=corporation_id).first()
            overrides = {corporation_id: row.monthly_cap} if row is not None else {}
        if corporation_id in overrides:
            return overrides[corporation_id], CORPORATION_CAP_OVERRIDE
    return srp.corporation_monthly_cap, CORPORATION_CAP_DEFAULT


def current_policy(*, corporation_id: int | None, rule=None, settings_obj=None, overrides=None) -> PolicySnapshot:
    """The SRP settings as a snapshot, with the corporation's cap (override or default) and ``rule``.

    ``overrides`` is ``{corporation_id: monthly_cap}`` for callers that snapshot many claims at once.
    """
    srp = settings_obj or SrpSettings.get_solo()
    corporation_monthly_cap, cap_source = corporation_cap(corporation_id, srp, overrides)
    return PolicySnapshot(
        value_source=srp.value_source,
        percentage=display.to_decimal(srp.percentage) or Decimal("0"),
        in_fleet_loss_cap=srp.in_fleet_loss_cap,
        out_of_fleet_loss_cap=srp.out_of_fleet_loss_cap,
        personal_monthly_cap=srp.personal_monthly_cap,
        personal_cap_includes_in_fleet=srp.personal_cap_includes_in_fleet,
        corporation_monthly_cap=corporation_monthly_cap,
        corporation_cap_source=cap_source,
        corporation_cap_includes_in_fleet=srp.corporation_cap_includes_in_fleet,
        over_limit_action=srp.over_limit_action,
        capsules_need_rule=srp.capsules_need_rule,
        claim_deadline_days=srp.claim_deadline_days,
        rule=rule_snapshot(rule),
    )


def loss_cap(policy: PolicySnapshot, scope: str) -> int | None:
    """The per-loss cap of the scope."""
    return policy.in_fleet_loss_cap if scope == IN_FLEET else policy.out_of_fleet_loss_cap


def percentage_applies(policy: PolicySnapshot, ship_type_id: int) -> bool:
    """Whether a loss without payout rule is paid as a percentage of its value: not for capsules while they
    need a rule, and not at 0%."""
    if ship_type_id in CAPSULE_TYPE_IDS and policy.capsules_need_rule:
        return False
    return policy.percentage > 0


def counts_toward_personal(policy: PolicySnapshot, scope: str) -> bool:
    return scope == OUT_OF_FLEET or policy.personal_cap_includes_in_fleet


def counts_toward_corporation(policy: PolicySnapshot, scope: str) -> bool:
    return scope == OUT_OF_FLEET or policy.corporation_cap_includes_in_fleet


def _percentage_amount(value: Decimal, percentage: Decimal) -> int:
    """``value x percentage / 100`` rounded down to whole ISK."""
    share = (value * percentage).scaleb(-2)
    return int(share.to_integral_value(rounding=ROUND_FLOOR))


def calculate(
    *,
    policy: PolicySnapshot,
    scope: str,
    ship_type_id: int,
    loss_value,
    value_source: str,
    limits: tuple[LimitState, ...],
    now=None,
) -> Calculation:
    """Pure calculation: rule amount or value x percentage, the per-loss cap of the scope, then the most
    restrictive of the applicable monthly limits.

    Capsules are paid from a capsule entry of the payout table only while the policy says so; a percentage
    never applies to them then.
    """
    value = display.to_decimal(loss_value)
    if value is not None and value < 0:
        value = None
    capsule_rule_only = ship_type_id in CAPSULE_TYPE_IDS and policy.capsules_need_rule
    rule = policy.rule
    if rule is not None and capsule_rule_only and not names_capsule(rule):
        rule = None

    notes: list[CalcNote] = []
    basis = BASIS_NONE
    base = 0
    if rule is not None:
        basis, base = BASIS_RULE, rule.amount
    elif capsule_rule_only:
        notes.append(CalcNote("no_rule"))
    elif policy.percentage <= 0:
        notes += [CalcNote("no_rule"), CalcNote("percentage_zero")]
    elif value is None:
        notes.append(CalcNote("no_value"))
    else:
        basis, base = BASIS_PERCENTAGE, _percentage_amount(value, policy.percentage)
    base = max(0, min(base, MAX_ISK))

    cap = loss_cap(policy, scope)
    suggested = base if cap is None else min(base, cap)
    if suggested < base:
        notes.append(CalcNote("loss_cap", {"cap": cap}))

    remaining = [(state.kind, state.effective_remaining) for state in limits]
    remaining = [(kind, amount) for kind, amount in remaining if amount is not None]
    limited = min([suggested, *(amount for _kind, amount in remaining)])
    over_limit = limited < suggested

    candidates = [(amount, _BINDING_ORDER.get(kind, 1), kind) for kind, amount in remaining if amount < base]
    if cap is not None and cap < base:
        candidates.append((cap, _BINDING_ORDER[LIMIT_LOSS_CAP], LIMIT_LOSS_CAP))
    binding = min(candidates)[2] if candidates else ""
    if over_limit:
        monthly = min((_BINDING_ORDER.get(kind, 1), kind) for kind, amount in remaining if amount == limited)
        notes.append(CalcNote("monthly_limit", {"kind": monthly[1], "remaining": limited}))

    return Calculation(
        basis=basis,
        loss_value=value,
        value_source=value_source or "",
        base=base,
        cap=cap,
        suggested=suggested,
        limits=tuple(limits),
        limited=limited,
        over_limit=over_limit,
        binding_limit=binding,
        notes=tuple(notes),
        computed_at=now or timezone.now(),
    )


def main_character(user):
    """The user's main character, or None."""
    try:
        return user.profile.main_character
    except ObjectDoesNotExist:
        return None


def pay_to(claim) -> tuple[int | None, str]:
    """``(character_id, name)`` of the character to send the ISK of ``claim`` to.

    The claimant's main character as it is now, so that a main the player has left behind (a character that
    was sold or moved to another account) is not paid. Only a claim without an account, or an account without
    a main, falls back to the main recorded with the claim and then to the pilot. Read the claim with
    ``select_related("user__profile__main_character")`` to list many without a query each.
    """
    main = main_character(claim.user) if claim.user_id is not None else None
    if main is not None:
        return main.character_id, main.character_name
    return claim.main_character_id or claim.character_id, claim.main_character_name or claim.character_name


def owned_character(user, character_id):
    """The ``EveCharacter`` with that ID when ``user`` owns it, else None."""
    ownership = (
        CharacterOwnership.objects.filter(user=user, character__character_id=character_id)
        .select_related("character")
        .first()
    )
    return ownership.character if ownership else None


def eligibility(*, km, user, scope: str, link, settings_obj, now=None, operation=None) -> list[str]:
    """Reasons (texts for the member) why the loss cannot be claimed; empty when it can.

    A claim that exists for the killmail is not a reason here; the preview and the submit step look for it.
    ``link`` is the fleet link the claim is made through; only an open link accepts new claims. ``operation``
    is the fleet whose payout rules apply, the one of ``link`` unless given: a claim that exists already is
    checked without its link (it may be closed) but against the rules of its fleet.
    """
    srp = settings_obj or SrpSettings.get_solo()
    now = now or timezone.now()
    blockers = []
    if not srp.enabled:
        blockers.append(str(BLOCK_SRP_DISABLED))
    elif link is not None and not link.is_open:
        blockers.append(str(BLOCK_LINK_CLOSED))

    if km.victim_character_id is None:
        blockers.append(str(BLOCK_NO_PILOT))
    elif owned_character(user, km.victim_character_id) is None:
        blockers.append(str(BLOCK_NOT_YOUR_CHARACTER))
    if main_character(user) is None:
        blockers.append(str(BLOCK_NO_MAIN))

    if scope == OUT_OF_FLEET and not srp.out_of_fleet_enabled:
        blockers.append(str(BLOCK_FLEET_ONLY))
    if srp.claim_deadline_days and km.killmail_time < now - timedelta(days=srp.claim_deadline_days):
        blockers.append(
            gettext("Losses older than %(days)s days cannot be submitted.") % {"days": srp.claim_deadline_days}
        )
    if km.ship_type_id in CAPSULE_TYPE_IDS and srp.capsules_need_rule:
        if operation is None and link is not None:
            operation = link.operation
        rule = match_rule(ship_type_id=km.ship_type_id, scope=scope, operation=operation, capsules_need_rule=True)
        if not names_capsule(rule):
            blockers.append(str(BLOCK_CAPSULE))
    return blockers


# The settings a claim is calculated with; the version of the snapshot and the rule are compared separately.
_POLICY_FIELDS = (
    "value_source",
    "percentage",
    "in_fleet_loss_cap",
    "out_of_fleet_loss_cap",
    "personal_monthly_cap",
    "personal_cap_includes_in_fleet",
    "corporation_monthly_cap",
    "corporation_cap_source",
    "corporation_cap_includes_in_fleet",
    "over_limit_action",
    "capsules_need_rule",
    "claim_deadline_days",
)


def outdated_pending_claims(settings_obj=None) -> list[int]:
    """The IDs of the pending claims that were calculated with other SRP settings than the current ones.

    A claim keeps the policy of its submission (a snapshot of the settings, the corporation's cap and the payout
    rule it matched); only a recalculation applies new settings. A claim is outdated when any setting of its
    snapshot differs from the current one, or when its payout rule has been deleted, switched off or has another
    amount since. A rule added later that would match the claim more closely is not detected: the reviewer can
    always recalculate. Three queries (the claims, the corporation limits, the rules), whatever the number of
    claims.
    """
    srp = settings_obj or SrpSettings.get_solo()
    pending = list(SrpClaim.objects.filter(status=SrpClaim.Status.PENDING).values_list("pk", "corporation_id", "policy"))
    if not pending:
        return []
    overrides = dict(SrpCorporationLimit.objects.values_list("corporation_id", "monthly_cap"))
    current_by_corporation: dict = {}
    snapshots = {pk: PolicySnapshot.from_json(policy) for pk, _corporation, policy in pending}
    rule_ids = {snapshot.rule.id for snapshot in snapshots.values() if snapshot.rule is not None and snapshot.rule.id}
    rules = dict(SrpPayoutRule.objects.filter(pk__in=rule_ids, is_active=True).values_list("pk", "amount"))
    outdated = []
    for pk, corporation_id, _policy in pending:
        stored = snapshots[pk]
        if corporation_id not in current_by_corporation:
            current_by_corporation[corporation_id] = current_policy(
                corporation_id=corporation_id, settings_obj=srp, overrides=overrides
            )
        current = current_by_corporation[corporation_id]
        changed = any(getattr(stored, name) != getattr(current, name) for name in _POLICY_FIELDS)
        if not changed and stored.rule is not None and stored.rule.id:
            changed = rules.get(stored.rule.id) != stored.rule.amount
        if changed:
            outdated.append(pk)
    return outdated


def outdated_pending_claim_count(settings_obj=None) -> int:
    """How many pending claims were calculated with SRP settings that have changed since."""
    return len(outdated_pending_claims(settings_obj))
