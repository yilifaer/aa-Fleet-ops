"""Kicking members from an EVE fleet with the FC's ESI token.

One member, everyone outside a chosen system, or the capsule pilots. The FC character and the fleet
boss are never kicked. Every kick is recorded as an operation step and in the audit log, and the
fleet is read again right after it and once more after ESI's own cache, so the fleet page shows who
is left. Kicks are fleet changes like every other: they are refused with the reason after a boss
handover, while the fleet cannot be read or without the FC character's write token, they hold the
fleet's write lock, and big batches run as fleet jobs, so they never overlap another change.
"""

import time
from dataclasses import dataclass

from allianceauth.services.hooks import get_extension_logger
from django.contrib.auth import get_user_model
from esi.models import Token

from fleetops.constants import FLEET_SCOPES
from fleetops.models import FleetMemberState, FleetOperation
from fleetops.providers.esi import FleetESIError, kick_fleet_member
from fleetops.services import fleet_access, fleet_jobs, fleet_refresh
from fleetops.services.audit import audit
from fleetops.services.errors import log_failure, report_failure
from fleetops.services.fleet_access import FleetManageError
from fleetops.services.fleet_overlay import ESI_READ_LAG
from fleetops.services.operations import set_action
from fleetops.services.tracking import sync_operation

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
# How long a batch kicked during the request holds the fleet at most.
BATCH_LOCK_TIMEOUT = 5 * 60

ALREADY_LEFT = "already left the fleet"
# The provider's code for a kick ESI answered with 404.
MEMBER_NOT_FOUND = "MEMBER_NOT_FOUND"
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
    # The HTTP status of the answer to the Fleet tab: 404 when the member already left, 409 when the
    # FC character is no longer the fleet boss or the fleet is gone, 502 for other failures.
    status: int = 200


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


def _check_writable(operation):
    """Refuse a kick before anything is sent when the fleet cannot be changed now.

    After a boss handover, while the fleet cannot be read, without the FC character's write token or
    while a fleet job runs, the kick is refused with the reason the Fleet tab shows.
    """
    problem = fleet_access.fleet_problem(operation)
    if problem:
        raise KickRefused(problem)


def _send_kick(operation, character_id):
    kick_fleet_member(operation.fc_user, operation.fc_character_id, operation.esi_fleet_id, character_id)


def _not_found_problem(operation) -> str:
    """Why ESI answered a kick with 404 when it was not because the member left; empty otherwise.

    Only the fleet boss can kick, so after a boss handover ESI answers every kick with 404 too. One
    fresh boss check tells them apart, as after any other fleet change, and keeps what it found for
    the Fleet tab.
    """
    problem = fleet_refresh.check_boss(operation)
    return fleet_refresh.problem_message(operation, problem) if problem is not None else ""


def read_after_kicks(operation):
    """Read the fleet now and once more after ESI's own 5 second cache, so the kicked members leave the page."""
    fresh_sync(operation)
    fleet_refresh.schedule_refresh(operation, delays=(ESI_READ_LAG,))


def fresh_sync(operation):
    """Read the members once more, past django-esi's response cache; a failed read is ignored."""
    try:
        sync_operation(operation, fresh=True)
    except FleetESIError:
        # The next tracking poll reads the fleet again and reports its problems.
        pass
    except Exception as exc:
        log_failure(logger, "Fleet sync after kicks", exc, operation)


def _kick_error(operation, character_id) -> tuple[str, str]:
    """Kick one member; returns ``(short error, why the fleet cannot be changed)``, both empty on success."""
    try:
        _send_kick(operation, character_id)
    except FleetESIError as exc:
        if exc.code != MEMBER_NOT_FOUND:
            return str(exc), ""
        problem = _not_found_problem(operation)
        return problem or ALREADY_LEFT, problem
    except Exception as exc:
        return report_failure(logger, "Fleet kick", exc, operation), ""
    return "", ""


def kick_member(operation, character_id, *, actor, sync=True) -> KickResult:
    """Kick one member right away.

    Raises KickRefused for the FC character, the fleet boss, a character that is not in the fleet, a
    fleet that is not active, after a boss handover, while the fleet cannot be read, without the
    write token and while another change or a job runs for the fleet. With ``sync`` off the fleet is
    not read again here; the caller schedules the reads.
    """
    _check_fleet(operation)
    character_id = int(character_id)
    if character_id in protected_character_ids(operation):
        raise KickRefused(PROTECTED)
    member = operation.member_states.filter(character_id=character_id, is_active=True).first()
    if member is None:
        raise KickRefused(NOT_IN_FLEET)
    _check_writable(operation)
    label = member_label(member)
    try:
        with fleet_jobs.fleet_write_lock(operation):
            error, problem = _kick_error(operation, character_id)
    except FleetManageError as exc:
        raise KickRefused(exc.message) from exc
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
            # Shown under "Recent fleet changes" on the Fleet tab.
            "result": "failed" if error else "ok",
            "summary": message,
        },
    )
    left = error == ALREADY_LEFT
    if sync and (not error or left):
        read_after_kicks(operation)
    status = 200 if not error else 409 if problem else 404 if left else 502
    return KickResult(not error, message, character_id, member.character_name, status)


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


def _job_problem(operation, job) -> str:
    """Why a background batch stops before its next kick: the fleet ended, changed boss or lost its token."""
    operation.refresh_from_db(
        fields=["status", "esi_fleet_id", "missing_reason", "missing_detail", "fleet_boss_character_id"]
    )
    fleet_jobs.touch(job)
    return fleet_access.fleet_problem(operation, job_id=job["id"])


def _kick_batch(operation, members, *, actor, purpose, context, skipped, job=None):
    """Kick ``members`` one after the other; ``job`` is the fleet job of a background batch.

    The batch stops, and the remaining members are skipped, when the fleet cannot be changed any
    more: a boss handover or a gone fleet (a boss check after ESI answered a kick with 404), a
    forbidden write, a missing scope or ESI's error limit. A background batch also checks the fleet
    before each kick, like every fleet job, and ends with JobStopped.
    """
    action, audit_action, step = PURPOSES[purpose]
    kicked, failures, gone, stopped = [], [], False, ""
    # When a boss check last found the FC character still the boss; ESI caches that answer anyway.
    boss_confirmed_at = None
    for index, member in enumerate(members):
        if job is not None:
            # Like every fleet job, a background batch checks the fleet before each change.
            stopped = _job_problem(operation, job)
            if stopped:
                skipped = [*skipped, *(other.character_id for other in members[index:])]
                break
        try:
            _send_kick(operation, member.character_id)
            kicked.append(member)
            progress = f"Kicked {member_label(member)}."
        except Exception as exc:
            error = report_failure(logger, step, exc, operation)
            if getattr(exc, "code", "") == MEMBER_NOT_FOUND:
                recent = boss_confirmed_at is not None and (
                    time.monotonic() - boss_confirmed_at < fleet_refresh.BOSS_CHECK_SECONDS
                )
                stopped = "" if recent else _not_found_problem(operation)
                if stopped:
                    error = stopped
                else:
                    # ESI no longer finds a member that left; the read below takes them off the list.
                    boss_confirmed_at = boss_confirmed_at if recent else time.monotonic()
                    gone = True
            else:
                # A forbidden write or ESI's error limit ends the batch, as it ends other fleet jobs.
                stopped = fleet_jobs.stop_reason(exc)
            failures.append((member, error))
            progress = f"{member.character_name}: {error}"
        if job is not None:
            # Progress of a background batch on the fleet page.
            fleet_jobs.step(job, ok=kicked[-1:] == [member], message=progress)
        if stopped:
            skipped = [*skipped, *(other.character_id for other in members[index + 1 :])]
            break
    message = _batch_message(purpose, context, kicked, failures, skipped)
    if stopped:
        message += f" Stopped: {stopped}"
    set_action(operation, action, not failures and not stopped, message)
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
            **({"stopped": stopped} if stopped else {}),
            "result": "failed" if failures or stopped else "ok",
            "summary": message,
        },
    )
    if kicked or gone:
        read_after_kicks(operation)
    if stopped and job is not None:
        raise fleet_jobs.JobStopped(stopped)
    return {
        "queued": False,
        "kicked": [member.character_name for member in kicked],
        "failures": [{"character": member.character_name, "error": error} for member, error in failures],
        "skipped": list(skipped),
        "target_count": len(members),
        **({"stopped": stopped} if stopped else {}),
    }


def kick_members(operation, character_ids, *, actor, purpose, context=None):
    """Kick the eligible members among ``character_ids`` (``None``: every eligible member).

    ``purpose`` is ``outside_system`` (``context`` holds ``system_id`` and ``system_name``) or
    ``capsules``. Requested members that are no longer eligible are skipped. Up to SYNC_KICK_LIMIT
    kicks run now; bigger batches run in the background and report under the operation steps.
    Returns ``queued``, ``kicked`` (names), ``failures``, ``skipped`` (ids), ``target_count`` and,
    when a batch run now stopped early, ``stopped`` (the reason). Raises KickRefused when the fleet
    cannot be changed now, before anything is sent.
    """
    _check_fleet(operation)
    _check_writable(operation)
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
        ids = [member.character_id for member in targets]
        try:
            fleet_jobs.start_job(
                operation,
                kind="kick",
                label=kick_job_label(purpose, context, len(ids)),
                payload=kick_job_payload(ids, purpose, {**context, "skipped": skipped}),
                actor=actor,
                total=len(ids),
            )
        except FleetManageError as exc:
            if exc.status != 503:
                raise KickRefused(exc.message) from exc
            # Without a reachable queue the kicks run in this request instead.
        else:
            return {"queued": True, "kicked": [], "failures": [], "skipped": skipped, "target_count": len(targets)}
    try:
        with fleet_jobs.fleet_write_lock(operation, timeout=BATCH_LOCK_TIMEOUT):
            return _kick_batch(operation, targets, actor=actor, purpose=purpose, context=context, skipped=skipped)
    except FleetManageError as exc:
        raise KickRefused(exc.message) from exc


def kick_job_label(purpose, context, count):
    """The name of a background kick batch on the fleet page."""
    if purpose == CAPSULES:
        return f"Kick {count} capsule pilot{'' if count == 1 else 's'}"
    system = context.get("system_name") or context.get("system_id")
    return f"Kick {count} member{'' if count == 1 else 's'} outside {system}"


def kick_job_payload(character_ids, purpose, context):
    ids = [int(character_id) for character_id in character_ids]
    return {"character_ids": ids, "purpose": purpose, "context": context}


def run_kick_job(job, operation, payload, actor):
    """Runner of the fleet jobs of kind "kick" (see ``fleet_jobs``); records its own step and audit entry."""
    return _run_batch(
        operation, payload["character_ids"], actor, payload["purpose"], payload.get("context"), job=job
    )


def run_kick_batch(operation_id, character_ids, actor_id, purpose, context):
    """Kick a batch of members away from the web request.

    ``purpose`` is ``outside_system`` or ``capsules``; ``context`` holds what the audit entry needs.
    Members are checked again, since they may have moved or left since the batch was queued.
    """
    operation = FleetOperation.objects.select_related("fc_user").filter(pk=operation_id).first()
    if operation is None:
        return None
    actor = get_user_model().objects.filter(pk=actor_id).first() if actor_id else None
    return _run_batch(operation, character_ids, actor, purpose, context)


def _run_batch(operation, character_ids, actor, purpose, context, *, job=None):
    context = dict(context or {})
    skipped = list(context.pop("skipped", []))
    ids = [int(character_id) for character_id in character_ids]
    try:
        _check_fleet(operation)
        if job is None:
            # A job checks the fleet before each kick instead (see _kick_batch).
            _check_writable(operation)
    except KickRefused as exc:
        action, audit_action, _step = PURPOSES[purpose]
        message = f"Nobody was kicked. {exc}"
        set_action(operation, action, False, message)
        audit(
            actor,
            audit_action,
            operation,
            None,
            {
                "requested": ids,
                "kicked": [],
                "skipped": ids + skipped,
                "stopped": str(exc),
                "result": "failed",
                "summary": message,
            },
        )
        if job is not None:
            raise fleet_jobs.JobStopped(str(exc)) from exc
        return {"queued": False, "kicked": [], "failures": [], "skipped": ids + skipped, "target_count": 0}
    by_id = {member.character_id: member for member in eligible_members(operation, purpose, context)}
    targets = [by_id[character_id] for character_id in ids if character_id in by_id]
    left = [character_id for character_id in ids if character_id not in by_id]
    skipped += left
    if job is not None:
        # The job counted every member it was queued for; those that left or are no longer a target
        # since are not kicked.
        note = f"{len(left)} skipped: no longer in the fleet or no longer a target." if left else ""
        fleet_jobs.set_remaining(job, len(targets), message=note)
    return _kick_batch(operation, targets, actor=actor, purpose=purpose, context=context, skipped=skipped, job=job)


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
