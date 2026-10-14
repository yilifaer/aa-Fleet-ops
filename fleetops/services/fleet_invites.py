# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Inviting Alliance Auth characters into a running fleet from the Fleet tab.

Any character registered in Alliance Auth by an active user can be invited. Invitations go to ESI
with the FC character's token, never with the token of the person clicking: one in the request,
several as an "invite" fleet job. The character still has to accept in game; until it joins (or
for two minutes) the fleet view lists the invitation as pending (``fleet_overlay``).
"""

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from allianceauth.services.hooks import get_extension_logger

from fleetops import texts
from fleetops.providers.esi import invite_fleet_member
from fleetops.services import fleet_jobs, fleet_overlay, fleet_refresh
from fleetops.services.audit import audit
from fleetops.services.fleet_access import FleetManageError, require_writable
from fleetops.services.fleet_manage import (
    RULE_STATUS,
    VIEW_REFRESHED,
    FleetActionResult,
    failure_answer,
    job_failure,
    structure,
)
from fleetops.services.fleet_rules import (
    COMMANDER_ROLES,
    FleetRuleError,
    check_fleet_room,
    check_invite,
    squad_index,
    stored_position_labels,
)

logger = get_extension_logger(__name__)

# Invites of more characters than this are refused; two or more run as a background job.
MAX_INVITES_PER_JOB = 50
# Searches need at least this many characters; an empty search lists the actor's own characters.
MIN_QUERY_LENGTH = 3

# Stored messages (English): they go into audit entries and job messages, and the pages show them through
# ``translate_stored``.
NO_CHARACTERS = texts.INVITE_NO_CHARACTERS()
TOO_MANY = texts.INVITE_TOO_MANY(limit=MAX_INVITES_PER_JOB)
ONE_COMMANDER = texts.INVITE_ONE_COMMANDER()
NOBODY = texts.INVITE_NOBODY()
# Rule failures that hold for every invitation of a job, so the job stops at the first one.
JOB_STOP_RULES = frozenset({"not_found", "limit"})


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


def _main_name(user) -> str:
    profile = getattr(user, "profile", None)
    main = getattr(profile, "main_character", None)
    return main.character_name if main else ""


def invite_candidates(query, *, actor, operation, limit=20) -> list[dict]:
    """Characters of active Alliance Auth users matching ``query``.

    Each entry: ``character_id``, ``name``, ``corporation``, ``alliance``, ``main_name``,
    ``in_fleet``, ``invited`` (an invitation is pending) and ``is_mine``. An empty query lists the
    actor's own characters ("My characters"); one shorter than ``MIN_QUERY_LENGTH`` finds nothing.
    """
    query = " ".join(str(query or "").split())
    ownerships = CharacterOwnership.objects.filter(user__is_active=True).select_related(
        "character", "user__profile__main_character"
    )
    if not query:
        if actor is None or not actor.is_authenticated:
            return []
        ownerships = ownerships.filter(user=actor)
        limit = max(limit, MAX_INVITES_PER_JOB)
    elif len(query) < MIN_QUERY_LENGTH:
        return []
    else:
        ownerships = ownerships.filter(character__character_name__icontains=query)
    ownerships = list(ownerships.order_by("character__character_name", "character__character_id")[:limit])
    ids = [ownership.character.character_id for ownership in ownerships]
    active = set(
        operation.member_states.filter(is_active=True, character_id__in=ids).values_list("character_id", flat=True)
    )
    invited = {invitation["character_id"] for invitation in fleet_overlay.pending_invitations(operation.pk, active)}
    actor_id = getattr(actor, "pk", None)
    return [
        {
            "character_id": ownership.character.character_id,
            "name": ownership.character.character_name,
            "corporation": ownership.character.corporation_name or "",
            "alliance": ownership.character.alliance_name or "",
            "main_name": _main_name(ownership.user),
            "in_fleet": ownership.character.character_id in active,
            "invited": ownership.character.character_id in invited,
            "is_mine": ownership.user_id == actor_id,
        }
        for ownership in ownerships
    ]


def registered_names(character_ids) -> dict[int, str]:
    """Names of the characters that active Alliance Auth users registered, by character id."""
    rows = CharacterOwnership.objects.filter(
        user__is_active=True, character__character_id__in=list(character_ids)
    ).values_list("character__character_id", "character__character_name")
    return {int(character_id): name for character_id, name in rows}


def _unknown_name(character_id, known) -> str:
    return known.get(character_id) or f"Character {character_id}"


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


def _position(operation, role, wing_id, squad_id, wings, members, known) -> dict:
    """The ESI position of the invitation; raises FleetManageError when it is not possible.

    A squad without its wing takes the wing from the known structure.
    """
    if squad_id and not wing_id and known:
        found = squad_index(wings).get(int(squad_id))
        if found is None:
            fleet_refresh.schedule_refresh(operation)
            raise FleetManageError(f"{texts.RULE_SQUAD_GONE()} {VIEW_REFRESHED}", 404)
        wing_id = found[0]["id"]
    try:
        return check_invite(role, wing_id, squad_id, wings=wings if known else None, members=list(members.values()))
    except FleetRuleError as exc:
        message = str(exc)
        if exc.code == "not_found":
            fleet_refresh.schedule_refresh(operation)
            message = f"{message} {VIEW_REFRESHED}"
        raise FleetManageError(message, RULE_STATUS.get(exc.code, 400)) from exc


def _invite_sent(name, body, wings=()) -> str:
    """The answer to an invitation that was sent, with where it puts the character."""
    role = body["role"]
    if role == "squad_member" and "squad_id" not in body:
        return texts.INVITE_SENT_ANY_SQUAD(name=name)
    wing, squad = stored_position_labels(role, body.get("wing_id"), body.get("squad_id"), wings)
    if role == "fleet_commander":
        return texts.INVITE_SENT_FLEET_COMMANDER(name=name)
    if role == "wing_commander":
        return texts.INVITE_SENT_WING_COMMANDER(name=name, wing=wing)
    if role == "squad_commander":
        return texts.INVITE_SENT_SQUAD_COMMANDER(name=name, wing=wing, squad=squad)
    return texts.INVITE_SENT_TO_SQUAD(name=name, wing=wing, squad=squad)


def _position_record(body, wings) -> dict:
    """A position for audit entries: raw ids plus the names at the time of the invitation."""
    if body["role"] == "squad_member" and "squad_id" not in body:
        wing, squad = "", ""
    else:
        wing, squad = stored_position_labels(body["role"], body.get("wing_id"), body.get("squad_id"), wings)
    return {
        "role": body["role"],
        "wing_id": body.get("wing_id"),
        "squad_id": body.get("squad_id"),
        "wing": wing,
        "squad": squad,
    }


def _expected(character_id, name, body) -> dict:
    return {
        "kind": "invited",
        "character_id": int(character_id),
        "name": name,
        "role": body["role"],
        "wing_id": body.get("wing_id"),
        "squad_id": body.get("squad_id"),
    }


def _send(operation, character_id, name, body) -> None:
    """The ESI call of one invitation, with the FC character's token."""
    invite_fleet_member(
        operation.fc_user,
        operation.fc_character_id,
        operation.esi_fleet_id,
        int(character_id),
        body["role"],
        body.get("wing_id"),
        body.get("squad_id"),
        name=name,
    )


# ---------------------------------------------------------------------------
# Inviting
# ---------------------------------------------------------------------------


def _skipped_note(skipped) -> str:
    return " " + texts.SKIPPED_LIST(items="; ".join(skipped)) if skipped else ""


def _invite_one(operation, actor, character_id, name, body, wings, skipped) -> FleetActionResult:
    record = {"character_id": int(character_id), "name": name, **_position_record(body, wings)}
    try:
        with fleet_jobs.fleet_write_lock(operation):
            _send(operation, character_id, name, body)
    except FleetManageError:
        raise
    except Exception as exc:
        message, status = failure_answer(operation, exc, "Inviting a character")
        details = {**record, "result": "failed", "summary": message, "error": message}
        audit(actor, "fleet.invite", operation, None, details)
        return FleetActionResult(False, message, level="error", status=status)
    fleet_overlay.expect(operation.pk, _expected(character_id, name, body))
    message = _invite_sent(name, body, wings)
    audit(actor, "fleet.invite", operation, None, {**record, "result": "ok", "summary": message})
    return FleetActionResult(True, message + _skipped_note(skipped), data={"character_ids": [int(character_id)]})


def invite_job_label(count, body, wings=()) -> str:
    if body["role"] == "squad_member" and "squad_id" in body:
        wing, squad = stored_position_labels(body["role"], body.get("wing_id"), body.get("squad_id"), wings)
        return texts.INVITE_JOB_INTO(count=count, wing=wing, squad=squad)
    return texts.INVITE_JOB(count=count)


def invite_characters(
    operation, character_ids, *, actor, role="squad_member", wing_id=None, squad_id=None
) -> FleetActionResult:
    """Invite characters: one in the request, up to ``MAX_INVITES_PER_JOB`` as an "invite" job.

    Characters that are not registered by an active Alliance Auth user are refused, members of the
    fleet are skipped; both are named in the answer. Without wing and squad a squad member joins
    any squad with free positions.
    """
    require_writable(operation, actor)
    ids = list(dict.fromkeys(int(character_id) for character_id in character_ids or ()))
    if not ids:
        raise FleetManageError(NO_CHARACTERS, 400)
    if len(ids) > MAX_INVITES_PER_JOB:
        raise FleetManageError(TOO_MANY, 400)
    if role in COMMANDER_ROLES and len(ids) > 1:
        raise FleetManageError(ONE_COMMANDER, 400)
    wings, members, known = structure(operation)
    body = _position(operation, role, wing_id, squad_id, wings, members, known)

    names = registered_names(ids)
    unknown = [character_id for character_id in ids if character_id not in names]
    known_names = dict(
        EveCharacter.objects.filter(character_id__in=unknown).values_list("character_id", "character_name")
    )
    targets, skipped, refused = [], [], []
    for character_id in ids:
        if character_id not in names:
            refused.append(texts.INVITE_NOT_REGISTERED_ITEM(name=_unknown_name(character_id, known_names)))
        elif character_id in members:
            skipped.append(texts.INVITE_IN_FLEET_ITEM(name=names[character_id]))
        else:
            targets.append(character_id)
    skipped = refused + skipped
    if not targets:
        if refused:
            return FleetActionResult(False, NOBODY + _skipped_note(skipped), level="error", status=400)
        return FleetActionResult(True, NOBODY + _skipped_note(skipped), level="info")
    try:
        check_fleet_room(len(members), adding=len(targets))
    except FleetRuleError as exc:
        raise FleetManageError(str(exc), RULE_STATUS.get(exc.code, 409)) from exc

    if len(targets) == 1:
        character_id = targets[0]
        return _invite_one(operation, actor, character_id, names[character_id], body, wings, skipped)
    job = fleet_jobs.start_job(
        operation,
        kind="invite",
        label=invite_job_label(len(targets), body, wings),
        payload={
            "invites": [[character_id, names[character_id]] for character_id in targets],
            "role": body["role"],
            "wing_id": body.get("wing_id"),
            "squad_id": body.get("squad_id"),
        },
        actor=actor,
        total=len(targets),
    )
    return FleetActionResult(
        True,
        texts.INVITING_IN_BACKGROUND(count=len(targets)) + _skipped_note(skipped),
        level="info",
        data={"job_id": job["id"], "character_ids": targets},
    )


def run_bulk_invite(job, operation, payload, actor) -> None:
    """Runner of the "invite" fleet job.

    Each invitation is checked again against the fleet as it is now: characters who joined in the
    meantime are skipped. A failed invitation (CSPA charge, already in another fleet …) is reported
    and the job goes on; it stops when the fleet is full, the squad is gone, or the failure ends
    the job (see ``job_failure``).
    """
    role, wing_id, squad_id = payload["role"], payload.get("wing_id"), payload.get("squad_id")
    results, failures = [], []
    position = {"role": role, "wing_id": wing_id, "squad_id": squad_id}
    try:
        for character_id, name in payload["invites"]:
            fleet_jobs.ensure_writable(job, operation, actor)
            entry = {"character_id": int(character_id), "name": name}
            results.append(entry)
            wings, members, known = structure(operation)
            if int(character_id) in members:
                entry["result"] = "skipped"
                fleet_jobs.step(job, ok=True, message=texts.INVITE_IN_FLEET(name=name))
                continue
            try:
                check_fleet_room(len(members))
                body = check_invite(
                    role, wing_id, squad_id, wings=wings if known else None, members=list(members.values())
                )
            except FleetRuleError as exc:
                entry.update(result="failed", error=str(exc))
                failures.append({"character": name, "character_id": int(character_id), "error": str(exc)})
                fleet_jobs.step(job, ok=False, message=texts.NAMED_ERROR(name=name, error=str(exc)))
                if exc.code in JOB_STOP_RULES:
                    raise fleet_jobs.JobStopped(str(exc)) from exc
                continue
            position = _position_record(body, wings)
            try:
                _send(operation, character_id, name, body)
            except Exception as exc:
                message, stop = job_failure(job, operation, exc, "Inviting a character")
                entry.update(result="failed", error=message)
                failures.append({"character": name, "character_id": int(character_id), "error": message})
                fleet_jobs.step(job, ok=False, message=message)
                if stop:
                    raise fleet_jobs.JobStopped(stop) from exc
                continue
            fleet_overlay.expect(operation.pk, _expected(character_id, name, body))
            entry["result"] = "ok"
            fleet_jobs.step(job, ok=True, message=texts.INVITE_SENT(name=name))
    finally:
        job["audit"] = {
            "position": position,
            "invites": results,
            "ok": sum(1 for entry in results if entry.get("result") == "ok"),
            "failed": failures,
        }

