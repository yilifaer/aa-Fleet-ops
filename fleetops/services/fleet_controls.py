"""Kicking members from an EVE fleet with the FC's ESI token.

One member, everyone outside a chosen system, or the capsule pilots. The FC character and the fleet
boss are never kicked. Every kick is recorded as an operation step and in the audit log, and the
fleet is read again right after it so the fleet page shows who is left.
"""

from dataclasses import dataclass

from allianceauth.services.hooks import get_extension_logger
from django.contrib.auth import get_user_model
from esi.models import Token
from kombu.exceptions import OperationalError as QueueError

from fleetops.constants import FLEET_SCOPES
from fleetops.models import FleetMemberState, FleetOperation
from fleetops.providers.esi import FleetESIError, kick_fleet_member
from fleetops.services.audit import audit
from fleetops.services.errors import log_failure, report_failure
from fleetops.services.operations import set_action
from fleetops.services.tracking import sync_operation
from fleetops.tasks import kick_members as kick_members_task

logger = get_extension_logger(__name__)

# Known Capsule group type IDs commonly seen in Fleet ESI. Name fallback keeps
# this safe if a future capsule type is added before local SDE data catches up.
KNOWN_CAPSULE_TYPE_IDS = {670, 33328}

OUTSIDE_SYSTEM = "outside_system"
CAPSULES = "capsules"
# Per purpose: operation step, audit action and the step name in error texts.
PURPOSES = {
    OUTSIDE_SYSTEM: ("kick_outside_system", "fleet.kick_outside_system", "Kick outside system"),
    CAPSULES: ("kick_capsules", "fleet.kick_capsules", "Capsule kick"),
}
# Batches up to this size are kicked during the request; bigger ones in the background.
SYNC_KICK_LIMIT = 10
FAILURES_SHOWN = 5

ALREADY_LEFT = "already left the fleet"
NOT_ACTIVE = "Members can only be kicked from an active fleet."
NO_ESI_FLEET = "This fleet has no ESI fleet to kick members from."
PROTECTED = "The FC character and the fleet boss are never kicked."
NOT_IN_FLEET = "This character is not in the fleet."
NO_WRITE_TOKEN = "The FC character has no ESI token with esi-fleets.write_fleet.v1"


class KickRefused(ValueError):
    """The kick is not allowed; nothing was sent to ESI."""


@dataclass
class KickResult:
    ok: bool
    message: str
    character_id: int
    character_name: str


def is_capsule_member(member: FleetMemberState) -> bool:
    name = (member.ship_type_name or "").strip().lower()
    return member.ship_type_id in KNOWN_CAPSULE_TYPE_IDS or name == "capsule" or name.endswith(" capsule")


def pod_members(operation):
    return [m for m in operation.member_states.filter(is_active=True) if is_capsule_member(m)]


def protected_character_ids(operation):
    """The FC character and the fleet boss: no kick ever targets them."""
    return {operation.fc_character_id, operation.fleet_boss_character_id} - {None}


def kickable_pod_members(operation):
    """Capsule pilots that Kick Capsules removes: never the FC or the fleet boss.

    The button count and the kick both use this list, so the count is what actually happens.
    """
    protected = protected_character_ids(operation)
    return [m for m in pod_members(operation) if m.character_id not in protected]


def fc_can_kick(operation):
    """Whether the FC character has a token with the fleet scopes.

    The token is not refreshed here, so the fleet page does not call EVE SSO on every view.
    """
    return (
        Token.objects.filter(user_id=operation.fc_user_id, character_id=operation.fc_character_id)
        .require_scopes(FLEET_SCOPES)
        .exists()
    )


def member_label(member):
    return f"{member.character_name} ({member.corporation_name or 'unknown corporation'})"


def _check_fleet(operation):
    if operation.status != FleetOperation.Status.ACTIVE:
        raise KickRefused(NOT_ACTIVE)
    if not operation.esi_fleet_id:
        raise KickRefused(NO_ESI_FLEET)


def _send_kick(operation, character_id):
    kick_fleet_member(operation.fc_user, operation.fc_character_id, operation.esi_fleet_id, character_id)


def fresh_sync(operation):
    """Read the members once more, past django-esi's response cache; a failed read is ignored."""
    try:
        sync_operation(operation, fresh=True)
    except FleetESIError:
        # The next tracking poll reads the fleet again and reports its problems.
        pass
    except Exception as exc:
        log_failure(logger, "Fleet sync after kicks", exc, operation)


def kick_member(operation, character_id, *, actor) -> KickResult:
    """Kick one member right away.

    Raises KickRefused for the FC character, the fleet boss, a character that is not in the fleet and
    a fleet that is not active.
    """
    _check_fleet(operation)
    character_id = int(character_id)
    if character_id in protected_character_ids(operation):
        raise KickRefused(PROTECTED)
    member = operation.member_states.filter(character_id=character_id, is_active=True).first()
    if member is None:
        raise KickRefused(NOT_IN_FLEET)
    label = member_label(member)
    error = ""
    try:
        _send_kick(operation, character_id)
    except FleetESIError as exc:
        error = ALREADY_LEFT if exc.code == "MEMBER_NOT_FOUND" else str(exc)
    except Exception as exc:
        error = report_failure(logger, "Fleet kick", exc, operation)
    message = f"Kicked {label}." if not error else f"Could not kick {label}: {error.rstrip('.')}."
    set_action(operation, "kick_member", not error, message)
    audit(
        actor,
        "fleet.kick_member",
        operation,
        None,
        {
            "character_id": character_id,
            "character_name": member.character_name,
            "corporation_id": member.corporation_id,
            "corporation_name": member.corporation_name,
            "kicked": not error,
            "error": error,
        },
    )
    if not error or error == ALREADY_LEFT:
        fresh_sync(operation)
    return KickResult(not error, message, character_id, member.character_name)


def eligible_members(operation, purpose, context=None):
    """The members a batch of ``purpose`` may kick now."""
    if purpose == CAPSULES:
        return kickable_pod_members(operation)
    system_id = (context or {}).get("system_id")
    protected = protected_character_ids(operation)
    return [
        member
        for member in operation.member_states.filter(is_active=True)
        if member.character_id not in protected and member.solar_system_id != system_id
    ]


def _batch_message(purpose, context, kicked, failures, skipped):
    if purpose == CAPSULES:
        message = f"Kicked {len(kicked)} capsule member(s)."
    else:
        system = context.get("system_name") or context.get("system_id")
        message = f"Kicked {len(kicked)} member(s) outside {system}."
    if failures:
        details = "; ".join(f"{member.character_name}: {error}" for member, error in failures[:FAILURES_SHOWN])
        message += f" {len(failures)} failed. {details}"
    if skipped:
        message += f" {len(skipped)} skipped: no longer in the fleet or no longer a target."
    return message


def _kick_batch(operation, members, *, actor, purpose, context, skipped):
    action, audit_action, step = PURPOSES[purpose]
    kicked, failures, gone = [], [], False
    for member in members:
        try:
            _send_kick(operation, member.character_id)
            kicked.append(member)
        except Exception as exc:
            # ESI no longer finds a member that left; the read below takes them off the list.
            gone = gone or getattr(exc, "code", "") == "MEMBER_NOT_FOUND"
            failures.append((member, report_failure(logger, step, exc, operation)))
    set_action(operation, action, not failures, _batch_message(purpose, context, kicked, failures, skipped))
    audit(
        actor,
        audit_action,
        operation,
        None,
        {
            **{key: context[key] for key in ("system_id", "system_name") if key in context},
            "requested": [member.character_id for member in members] + list(skipped),
            "kicked": [member.character_name for member in kicked],
            "kicked_ids": [member.character_id for member in kicked],
            "failures": [
                {"character": member.character_name, "character_id": member.character_id, "error": error}
                for member, error in failures
            ],
            "skipped": list(skipped),
        },
    )
    if kicked or gone:
        fresh_sync(operation)
    return {
        "queued": False,
        "kicked": [member.character_name for member in kicked],
        "failures": [{"character": member.character_name, "error": error} for member, error in failures],
        "skipped": list(skipped),
        "target_count": len(members),
    }


def kick_members(operation, character_ids, *, actor, purpose, context=None):
    """Kick the eligible members among ``character_ids`` (``None``: every eligible member).

    ``purpose`` is ``outside_system`` (``context`` holds ``system_id`` and ``system_name``) or
    ``capsules``. Requested members that are no longer eligible are skipped. Up to SYNC_KICK_LIMIT
    kicks run now; bigger batches run in the background and report under the operation steps.
    Returns ``queued``, ``kicked`` (names), ``failures``, ``skipped`` (ids) and ``target_count``.
    """
    _check_fleet(operation)
    context = dict(context or {})
    eligible = eligible_members(operation, purpose, context)
    if character_ids is None:
        targets, skipped = eligible, []
    else:
        by_id = {member.character_id: member for member in eligible}
        requested = list(dict.fromkeys(int(character_id) for character_id in character_ids))
        targets = [by_id[character_id] for character_id in requested if character_id in by_id]
        skipped = [character_id for character_id in requested if character_id not in by_id]
    if not targets:
        return {"queued": False, "kicked": [], "failures": [], "skipped": skipped, "target_count": 0}
    if len(targets) > SYNC_KICK_LIMIT:
        try:
            kick_members_task.delay(
                operation.pk,
                [member.character_id for member in targets],
                getattr(actor, "pk", None),
                purpose,
                {**context, "skipped": skipped},
            )
        except QueueError as exc:
            # Without a reachable queue the kicks run in this request instead.
            log_failure(logger, "Queueing the kicks", exc, operation)
        else:
            return {"queued": True, "kicked": [], "failures": [], "skipped": skipped, "target_count": len(targets)}
    return _kick_batch(operation, targets, actor=actor, purpose=purpose, context=context, skipped=skipped)


def run_kick_batch(operation_id, character_ids, actor_id, purpose, context):
    """Kick a batch of members in the background (the body of ``tasks.kick_members``).

    ``purpose`` is ``outside_system`` or ``capsules``; ``context`` holds what the audit entry needs.
    Members are checked again, since they may have moved or left since the batch was queued.
    """
    operation = FleetOperation.objects.select_related("fc_user").filter(pk=operation_id).first()
    if operation is None:
        return None
    context = dict(context or {})
    skipped = list(context.pop("skipped", []))
    actor = get_user_model().objects.filter(pk=actor_id).first() if actor_id else None
    ids = [int(character_id) for character_id in character_ids]
    try:
        _check_fleet(operation)
    except KickRefused as exc:
        action, audit_action, _step = PURPOSES[purpose]
        set_action(operation, action, False, f"Nobody was kicked. {exc}")
        audit(actor, audit_action, operation, None, {"requested": ids, "kicked": [], "skipped": ids + skipped})
        return {"queued": False, "kicked": [], "failures": [], "skipped": ids + skipped, "target_count": 0}
    by_id = {member.character_id: member for member in eligible_members(operation, purpose, context)}
    targets = [by_id[character_id] for character_id in ids if character_id in by_id]
    skipped += [character_id for character_id in ids if character_id not in by_id]
    return _kick_batch(operation, targets, actor=actor, purpose=purpose, context=context, skipped=skipped)


def _member_row(member):
    return {
        "character_id": member.character_id,
        "name": member.character_name,
        "corporation": member.corporation_name,
        "ship": member.ship_type_name or (str(member.ship_type_id) if member.ship_type_id else ""),
        "system": member.solar_system_name or (str(member.solar_system_id) if member.solar_system_id else ""),
        "system_id": member.solar_system_id,
    }


def outside_system_preview(operation, system_id=None):
    """Systems of the fleet with their member counts and the members outside the chosen system.

    Without ``system_id`` the FC character's system is chosen, or the most populated one when the FC
    is not in the member list.
    """
    members = sorted(operation.member_states.filter(is_active=True), key=lambda member: member.character_name)
    systems = {}
    for member in members:
        if member.solar_system_id is None:
            continue
        entry = systems.setdefault(
            member.solar_system_id,
            {"id": member.solar_system_id, "name": _member_row(member)["system"], "count": 0},
        )
        entry["count"] += 1
    systems = sorted(systems.values(), key=lambda entry: (-entry["count"], entry["name"]))
    fc_member = next((member for member in members if member.character_id == operation.fc_character_id), None)
    fc_system_id = fc_member.solar_system_id if fc_member else None
    selected = system_id if system_id is not None else (fc_system_id or (systems[0]["id"] if systems else None))
    targets = eligible_members(operation, OUTSIDE_SYSTEM, {"system_id": selected}) if selected is not None else []
    return {
        "systems": systems,
        "selected_system_id": selected,
        "fc_system_id": fc_system_id,
        "members": [_member_row(member) for member in sorted(targets, key=lambda member: member.character_name)],
    }


def capsule_preview(operation):
    """The capsule pilots Kick Capsules would kick, in the shape of the outside-system preview."""
    pods = sorted(kickable_pod_members(operation), key=lambda member: member.character_name)
    return {
        "systems": [],
        "selected_system_id": None,
        "fc_system_id": None,
        "members": [_member_row(member) for member in pods],
    }
