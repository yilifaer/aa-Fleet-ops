# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Wing, squad and member position changes of a running fleet.

Every change is made with the FC (fleet boss) character's ESI token, never with the token of
the person clicking. One change runs in the request; moves of several members run as a fleet job.

Each change follows the same steps: ``fleet_access.require_writable`` (who may change the fleet
and whether it can be changed now), checks against the fleet as FleetOps expects it
(``fleet_overlay.current_structure``), the ESI call under ``fleet_jobs.fleet_write_lock``, then
the expected change for the fleet view, an audit entry ``fleet.<action>`` and fresh reads of the
fleet (``fleet_refresh.schedule_refresh``). A write that ESI answers with 404 checks whether the
FC character is still the fleet boss: only the boss can change a fleet.
"""

from dataclasses import dataclass, field

from allianceauth.services.hooks import get_extension_logger

from fleetops import texts
from fleetops.providers.esi import (
    FleetESIError,
    create_fleet_squad,
    create_fleet_wing,
    delete_fleet_squad,
    delete_fleet_wing,
    move_fleet_member,
    rename_fleet_squad,
    rename_fleet_wing,
)
from fleetops.services import fleet_jobs, fleet_overlay, fleet_refresh
from fleetops.services.audit import audit
from fleetops.services.errors import report_failure
from fleetops.services.fleet_access import FleetManageError, require_writable
from fleetops.services.fleet_rules import (
    COMMANDER_ROLES,
    FleetRuleError,
    check_add_squad,
    check_add_wing,
    check_delete_squad,
    check_delete_wing,
    check_move,
    check_rename_squad,
    check_rename_wing,
    clean_unit_name,
    stored_position_labels,
    stored_squad_label,
    stored_wing_label,
)

logger = get_extension_logger(__name__)

# Moves of more members than this are refused; two or more run as a background job.
MAX_BULK_MOVES = 50

# Stored messages (English): they go into audit entries and job messages, and the pages show them through
# ``translate_stored``.
NO_MEMBERS = texts.MOVE_NO_MEMBERS()
TOO_MANY_MOVES = texts.MOVE_TOO_MANY(limit=MAX_BULK_MOVES)
ONE_COMMANDER = texts.MOVE_ONE_COMMANDER()
VIEW_REFRESHED = texts.VIEW_REFRESHED()
# FleetRuleError codes (see fleet_rules) and the HTTP status of the refusal.
RULE_STATUS = {"invalid": 400, "not_found": 404, "limit": 409, "not_empty": 409, "occupied": 409}
# ESI failures that mean the change cannot be made now, not that ESI failed.
REFUSED_CODES = frozenset({"MISSING_WRITE_SCOPE", "MISSING_READ_SCOPE"})


@dataclass
class FleetActionResult:
    """The answer of a fleet action, shown as a message and sent to the Fleet tab's scripts."""

    ok: bool
    message: str
    level: str = "success"
    status: int = 200
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MoveRequest:
    character_id: int
    role: str
    wing_id: int | None = None
    squad_id: int | None = None


# ---------------------------------------------------------------------------
# Shared steps
# ---------------------------------------------------------------------------


def _fc(operation):
    """The arguments every ESI write starts with: the FC user, the FC character and the fleet."""
    return operation.fc_user, operation.fc_character_id, operation.esi_fleet_id


def _structure_known(operation) -> bool:
    """Whether FleetOps has read the wings at least once; without them ESI alone checks the units."""
    return operation.fleet_wings_read_at is not None


def structure(operation):
    """``(wings, members by character id, structure known)`` as FleetOps expects the fleet now."""
    wings, members = fleet_overlay.current_structure(operation)
    return wings, members, _structure_known(operation)


_structure = structure


def _refused(operation, exc: FleetRuleError) -> FleetManageError:
    """The refusal of a change that breaks EVE's fleet rules; nothing was sent to ESI."""
    message = str(exc)
    if exc.code == "not_found":
        # The page showed a wing, squad or member that is gone: read the fleet again.
        fleet_refresh.schedule_refresh(operation)
        message = f"{message} {VIEW_REFRESHED}"
    return FleetManageError(message, RULE_STATUS.get(exc.code, 400))


def _unit_name(value) -> str:
    """A wing or squad name for a create; empty means "keep EVE's name". FleetRuleError when too long."""
    return clean_unit_name(value) if str(value or "").strip() else ""


def _send(operation, *calls):
    """Make the ESI calls in order while holding the fleet's write lock.

    Each call gets the results of the calls before it (a rename gets the id of the wing just
    created). Returns ``(results, error)``: the results of the calls that succeeded and the
    exception of the first that failed (None when all succeeded); later calls are not made. A busy
    fleet raises FleetManageError (409).
    """
    results = []
    with fleet_jobs.fleet_write_lock(operation):
        for call in calls:
            try:
                results.append(call(*results))
            except Exception as exc:
                return results, exc
    return results, None


def failure_answer(operation, exc, step, *, refresh=True) -> tuple[str, int]:
    """``(message, HTTP status)`` for a failed ESI change; the text is safe to show and store.

    A 404 means the FC character is no longer the fleet boss, the fleet is gone, or the wing,
    squad or member no longer exists: one fresh boss check tells them apart (409 for the first
    two). In the last case the fleet is read again unless ``refresh`` is off (jobs read it when
    they end).
    """
    if isinstance(exc, FleetESIError):
        if exc.code == "FLEET_WRITE_NOT_FOUND":
            problem = fleet_refresh.check_boss(operation)
            if problem is not None:
                return fleet_refresh.problem_message(operation, problem), 409
            if refresh:
                fleet_refresh.schedule_refresh(operation)
            return str(exc), 404
        if exc.code in REFUSED_CODES:
            return str(exc), 409
        return str(exc), 502
    if isinstance(exc, FleetRuleError):
        return str(exc), 400
    return report_failure(logger, step, exc, operation), 502


def job_failure(job, operation, exc, step) -> tuple[str, str]:
    """``(message, reason to stop the job or "")`` for a failed ESI call of a fleet job.

    A handover or a gone fleet ends the job, like a forbidden write, a missing scope or ESI's error
    limit; a wing, squad or character that ESI does not know only fails this step.
    """
    message, status = failure_answer(operation, exc, step, refresh=False)
    if isinstance(exc, FleetESIError) and exc.code == "FLEET_WRITE_NOT_FOUND":
        return message, message if status == 409 else ""
    return message, fleet_jobs.stop_reason(exc)


def _record(actor, action, operation, old, new, *, ok, summary, error=""):
    """The audit entry of one change that reached ESI, successful or not."""
    details = {**(new or {}), "result": "ok" if ok else "failed", "summary": summary}
    if error:
        details["error"] = error
    audit(actor, action, operation, old, details)


def _failed(operation, actor, action, exc, step, *, old=None, new=None) -> FleetActionResult:
    message, status = failure_answer(operation, exc, step)
    _record(actor, action, operation, old, new, ok=False, summary=message, error=message)
    return FleetActionResult(False, message, level="error", status=status)


def _done(operation, actor, action, message, *, old=None, new=None, expected=(), data=None, level="success"):
    for change in expected:
        fleet_overlay.expect(operation.pk, change)
    _record(actor, action, operation, old, new, ok=True, summary=message)
    fleet_refresh.schedule_refresh(operation)
    return FleetActionResult(True, message, level=level, data=data or {})


# ---------------------------------------------------------------------------
# Wings
# ---------------------------------------------------------------------------


def add_wing(operation, *, actor, name="") -> FleetActionResult:
    """Create a wing, then name it when ``name`` is given; ``data`` carries the new ``wing_id``."""
    require_writable(operation, actor)
    wings, _members, known = _structure(operation)
    try:
        name = _unit_name(name)
        if known:
            check_add_wing(wings)
    except FleetRuleError as exc:
        raise _refused(operation, exc) from exc
    fc_user, fc_character_id, fleet_id = _fc(operation)
    calls = [lambda: create_fleet_wing(fc_user, fc_character_id, fleet_id)]
    if name:
        calls.append(lambda wing_id: rename_fleet_wing(fc_user, fc_character_id, fleet_id, wing_id, name))
    results, error = _send(operation, *calls)
    if not results:
        return _failed(operation, actor, "fleet.wing_create", error, "Creating a wing", new={"name": name})
    wing_id = results[0]
    data = {"wing_id": wing_id}
    if error is not None:
        # The wing exists; only its name is EVE's.
        rename_error, _status = failure_answer(operation, error, "Renaming the wing")
        message = texts.WING_CREATED_RENAME_FAILED(error=rename_error)
        _record(
            actor,
            "fleet.wing_rename",
            operation,
            {"wing_id": wing_id, "name": ""},
            {"wing_id": wing_id, "name": name},
            ok=False,
            summary=rename_error,
            error=rename_error,
        )
        return _done(
            operation,
            actor,
            "fleet.wing_create",
            message,
            new={"wing_id": wing_id, "name": ""},
            expected=[{"kind": "wing_created", "wing_id": wing_id, "name": ""}],
            data=data,
            level="warning",
        )
    return _done(
        operation,
        actor,
        "fleet.wing_create",
        texts.WING_CREATED_NAMED(name=name) if name else texts.WING_CREATED(),
        new={"wing_id": wing_id, "name": name},
        expected=[{"kind": "wing_created", "wing_id": wing_id, "name": name}],
        data=data,
    )


def rename_wing(operation, wing_id, name, *, actor) -> FleetActionResult:
    require_writable(operation, actor)
    wings, _members, known = _structure(operation)
    try:
        if known:
            _wing, name = check_rename_wing(wings, wing_id, name)
        else:
            name = clean_unit_name(name)
    except FleetRuleError as exc:
        raise _refused(operation, exc) from exc
    old_name = stored_wing_label(wing_id, wings)
    if known and old_name == name:
        return FleetActionResult(True, texts.WING_ALREADY_NAMED(name=name), level="info", data={"wing_id": wing_id})
    old = {"wing_id": wing_id, "name": old_name}
    new = {"wing_id": wing_id, "name": name}
    fc_user, fc_character_id, fleet_id = _fc(operation)
    _results, error = _send(operation, lambda: rename_fleet_wing(fc_user, fc_character_id, fleet_id, wing_id, name))
    if error is not None:
        return _failed(operation, actor, "fleet.wing_rename", error, "Renaming a wing", old=old, new=new)
    return _done(
        operation,
        actor,
        "fleet.wing_rename",
        texts.WING_RENAMED(old=old_name, name=name),
        old=old,
        new=new,
        expected=[{"kind": "wing_renamed", "wing_id": wing_id, "name": name}],
        data={"wing_id": wing_id},
    )


def delete_wing(operation, wing_id, *, actor) -> FleetActionResult:
    """Delete an empty wing (no wing commander, no squad members)."""
    require_writable(operation, actor)
    wings, members, known = _structure(operation)
    # Without a wings read only ESI knows the wing; the members' own wing ids still show whether it is empty.
    checked = wings if known else [{"id": wing_id, "name": "", "squads": []}]
    try:
        check_delete_wing(checked, wing_id, members.values())
    except FleetRuleError as exc:
        raise _refused(operation, exc) from exc
    name = stored_wing_label(wing_id, wings)
    old = {"wing_id": wing_id, "name": name}
    fc_user, fc_character_id, fleet_id = _fc(operation)
    _results, error = _send(operation, lambda: delete_fleet_wing(fc_user, fc_character_id, fleet_id, wing_id))
    if error is not None:
        return _failed(operation, actor, "fleet.wing_delete", error, "Deleting a wing", old=old)
    return _done(
        operation,
        actor,
        "fleet.wing_delete",
        texts.WING_DELETED(name=name),
        old=old,
        expected=[{"kind": "wing_deleted", "wing_id": wing_id}],
        data={"wing_id": wing_id},
    )


# ---------------------------------------------------------------------------
# Squads
# ---------------------------------------------------------------------------


def add_squad(operation, wing_id, *, actor, name="") -> FleetActionResult:
    """Create a squad in a wing, then name it when ``name`` is given; ``data`` carries the new ``squad_id``."""
    require_writable(operation, actor)
    wings, _members, known = _structure(operation)
    try:
        name = _unit_name(name)
        if known:
            check_add_squad(wings, wing_id)
    except FleetRuleError as exc:
        raise _refused(operation, exc) from exc
    wing_name = stored_wing_label(wing_id, wings)
    fc_user, fc_character_id, fleet_id = _fc(operation)
    calls = [lambda: create_fleet_squad(fc_user, fc_character_id, fleet_id, wing_id)]
    if name:
        calls.append(lambda squad_id: rename_fleet_squad(fc_user, fc_character_id, fleet_id, squad_id, name))
    results, error = _send(operation, *calls)
    if not results:
        return _failed(
            operation,
            actor,
            "fleet.squad_create",
            error,
            "Creating a squad",
            new={"wing_id": wing_id, "wing": wing_name, "name": name},
        )
    squad_id = results[0]
    data = {"wing_id": wing_id, "squad_id": squad_id}
    if error is not None:
        rename_error, _status = failure_answer(operation, error, "Renaming the squad")
        _record(
            actor,
            "fleet.squad_rename",
            operation,
            {"squad_id": squad_id, "name": ""},
            {"squad_id": squad_id, "name": name},
            ok=False,
            summary=rename_error,
            error=rename_error,
        )
        return _done(
            operation,
            actor,
            "fleet.squad_create",
            texts.SQUAD_CREATED_RENAME_FAILED(error=rename_error),
            new={"wing_id": wing_id, "squad_id": squad_id, "wing": wing_name, "name": ""},
            expected=[{"kind": "squad_created", "wing_id": wing_id, "squad_id": squad_id, "name": ""}],
            data=data,
            level="warning",
        )
    message = (
        texts.SQUAD_CREATED_NAMED(name=name, wing=wing_name) if name else texts.SQUAD_CREATED(wing=wing_name)
    )
    return _done(
        operation,
        actor,
        "fleet.squad_create",
        message,
        new={"wing_id": wing_id, "squad_id": squad_id, "wing": wing_name, "name": name},
        expected=[{"kind": "squad_created", "wing_id": wing_id, "squad_id": squad_id, "name": name}],
        data=data,
    )


def rename_squad(operation, squad_id, name, *, actor) -> FleetActionResult:
    require_writable(operation, actor)
    wings, _members, known = _structure(operation)
    try:
        if known:
            _wing, _squad, name = check_rename_squad(wings, squad_id, name)
        else:
            name = clean_unit_name(name)
    except FleetRuleError as exc:
        raise _refused(operation, exc) from exc
    old_name = stored_squad_label(squad_id, wings)
    if known and old_name == name:
        return FleetActionResult(
            True, texts.SQUAD_ALREADY_NAMED(name=name), level="info", data={"squad_id": squad_id}
        )
    old = {"squad_id": squad_id, "name": old_name}
    new = {"squad_id": squad_id, "name": name}
    fc_user, fc_character_id, fleet_id = _fc(operation)
    _results, error = _send(operation, lambda: rename_fleet_squad(fc_user, fc_character_id, fleet_id, squad_id, name))
    if error is not None:
        return _failed(operation, actor, "fleet.squad_rename", error, "Renaming a squad", old=old, new=new)
    return _done(
        operation,
        actor,
        "fleet.squad_rename",
        texts.SQUAD_RENAMED(old=old_name, name=name),
        old=old,
        new=new,
        expected=[{"kind": "squad_renamed", "squad_id": squad_id, "name": name}],
        data={"squad_id": squad_id},
    )


def delete_squad(operation, squad_id, *, actor) -> FleetActionResult:
    """Delete an empty squad."""
    require_writable(operation, actor)
    wings, members, known = _structure(operation)
    # Without a wings read only ESI knows the squad; the members' own squad ids still show whether it is empty.
    checked = wings if known else [{"id": 0, "name": "", "squads": [{"id": squad_id, "name": ""}]}]
    try:
        wing, _squad = check_delete_squad(checked, squad_id, members.values())
    except FleetRuleError as exc:
        raise _refused(operation, exc) from exc
    wing_id = wing["id"] if known else None
    name = stored_squad_label(squad_id, wings)
    old = {"wing_id": wing_id, "squad_id": squad_id, "name": name}
    fc_user, fc_character_id, fleet_id = _fc(operation)
    _results, error = _send(operation, lambda: delete_fleet_squad(fc_user, fc_character_id, fleet_id, squad_id))
    if error is not None:
        return _failed(operation, actor, "fleet.squad_delete", error, "Deleting a squad", old=old)
    return _done(
        operation,
        actor,
        "fleet.squad_delete",
        texts.SQUAD_DELETED(name=name),
        old=old,
        expected=[{"kind": "squad_deleted", "squad_id": squad_id}],
        data={"squad_id": squad_id},
    )


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


def _moved_message(name, role, wing_id, squad_id, wings) -> str:
    wing, squad = stored_position_labels(role, wing_id, squad_id, wings)
    if role == "fleet_commander":
        return texts.MOVED_FLEET_COMMANDER(name=name)
    if role == "wing_commander":
        return texts.MOVED_WING_COMMANDER(name=name, wing=wing)
    if role == "squad_commander":
        return texts.MOVED_SQUAD_COMMANDER(name=name, wing=wing, squad=squad)
    return texts.MOVED_TO_SQUAD(name=name, wing=wing, squad=squad)


def _position(role, wing_id, squad_id, wings) -> dict:
    """A position for audit entries: raw ids plus the names at the time of the change."""
    wing, squad = stored_position_labels(role, wing_id, squad_id, wings)
    return {"role": role, "wing_id": wing_id, "squad_id": squad_id, "wing": wing, "squad": squad}


def _left_name(operation, character_id) -> str:
    """The name of a character that is not in the fleet (any more)."""
    name = (
        operation.member_states.filter(character_id=character_id)
        .exclude(character_name="")
        .values_list("character_name", flat=True)
        .first()
    )
    return name or f"Character {character_id}"


def _unique_moves(moves) -> list[MoveRequest]:
    """One move per member: the first one given wins."""
    seen, unique = set(), []
    for move in moves:
        if int(move.character_id) not in seen:
            seen.add(int(move.character_id))
            unique.append(move)
    return unique


def _check_commander_moves(moves) -> None:
    """Each commander position takes one member, so a bulk move never fills one twice."""
    slots = set()
    for move in moves:
        if move.role not in COMMANDER_ROLES:
            continue
        slot = (
            move.role,
            move.wing_id if move.role != "fleet_commander" else None,
            move.squad_id if move.role == "squad_commander" else None,
        )
        if slot in slots:
            raise FleetManageError(ONE_COMMANDER, 400)
        slots.add(slot)


def _check_one_move(operation, move, wings, members, known):
    """``(member, ESI body or None when it is already there)``.

    Raises FleetRuleError when the move is not possible, with the code "gone" for a member who is
    no longer in the fleet.
    """
    member = members.get(int(move.character_id))
    if member is None:
        raise FleetRuleError(texts.LEFT_FLEET(name=_left_name(operation, move.character_id)), "gone")
    body = check_move(
        member,
        move.role,
        move.wing_id,
        move.squad_id,
        wings=wings if known else None,
        members=list(members.values()),
    )
    return member, body


def _send_move(operation, member, body) -> None:
    """The ESI call of one move, with the FC character's token."""
    fc_user, fc_character_id, fleet_id = _fc(operation)
    move_fleet_member(
        fc_user,
        fc_character_id,
        fleet_id,
        int(member["character_id"]),
        body["role"],
        body.get("wing_id"),
        body.get("squad_id"),
        name=member["name"],
    )


def _moved_change(member, body) -> dict:
    return {
        "kind": "member_moved",
        "character_id": int(member["character_id"]),
        "role": body["role"],
        "wing_id": body.get("wing_id"),
        "squad_id": body.get("squad_id"),
    }


def _member_old(member, wings) -> dict:
    return {
        "character_id": int(member["character_id"]),
        "character_name": member["name"],
        **_position(member["role"], member.get("wing_id"), member.get("squad_id"), wings),
    }


def _move_one(operation, move, *, actor) -> FleetActionResult:
    wings, members, known = _structure(operation)
    try:
        member, body = _check_one_move(operation, move, wings, members, known)
    except FleetRuleError as exc:
        if exc.code == "gone":
            fleet_refresh.schedule_refresh(operation)
            raise FleetManageError(str(exc), 404) from exc
        raise _refused(operation, exc) from exc
    name = member["name"]
    if body is None:
        return FleetActionResult(
            True,
            texts.ALREADY_THERE(name=name),
            level="info",
            data={"character_ids": [int(member["character_id"])]},
        )
    old = _member_old(member, wings)
    new = _position(body["role"], body.get("wing_id"), body.get("squad_id"), wings)
    _results, error = _send(operation, lambda: _send_move(operation, member, body))
    if error is not None:
        return _failed(operation, actor, "fleet.member_move", error, "Moving a fleet member", old=old, new=new)
    return _done(
        operation,
        actor,
        "fleet.member_move",
        _moved_message(name, body["role"], body.get("wing_id"), body.get("squad_id"), wings),
        old=old,
        new=new,
        expected=[_moved_change(member, body)],
        data={"character_ids": [int(member["character_id"])]},
    )


def move_job_label(moves, wings=()) -> str:
    """The name of a bulk move on the fleet page, e.g. "Move 3 members to Main / Alpha"."""
    targets = {(move.role, move.wing_id, move.squad_id) for move in moves}
    if len(targets) == 1:
        role, wing_id, squad_id = next(iter(targets))
        if role == "squad_member":
            wing, squad = stored_position_labels(role, wing_id, squad_id, wings)
            return texts.MOVE_JOB_TO(count=len(moves), wing=wing, squad=squad)
    return texts.MOVE_JOB(count=len(moves))


def _skipped_note(skipped) -> str:
    if not skipped:
        return ""
    return " " + texts.SKIPPED_LIST(items="; ".join(skipped))


def _start_bulk_move(operation, moves, *, actor) -> FleetActionResult:
    wings, members, known = _structure(operation)
    targets, skipped, gone = [], [], False
    for move in moves:
        try:
            member, body = _check_one_move(operation, move, wings, members, known)
        except FleetRuleError as exc:
            if exc.code == "gone":
                gone = True
                skipped.append(texts.LEFT_FLEET_ITEM(**texts.LEFT_FLEET.match(str(exc))))
                continue
            # The target itself is wrong (a unit that is gone, a taken commander position).
            raise _refused(operation, exc) from exc
        if body is None:
            skipped.append(texts.ALREADY_THERE_ITEM(name=member["name"]))
            continue
        targets.append(move)
    if not targets:
        if gone:
            # The page listed members who left: read the fleet again.
            fleet_refresh.schedule_refresh(operation)
        return FleetActionResult(True, texts.NOBODY_MOVED() + _skipped_note(skipped), level="info")
    if len(targets) == 1:
        result = _move_one(operation, targets[0], actor=actor)
        result.message += _skipped_note(skipped)
        return result
    payload = {
        "moves": [[int(move.character_id), move.role, move.wing_id, move.squad_id] for move in targets],
    }
    job = fleet_jobs.start_job(
        operation,
        kind="move",
        label=move_job_label(targets, wings),
        payload=payload,
        actor=actor,
        total=len(targets),
    )
    return FleetActionResult(
        True,
        texts.MOVING_IN_BACKGROUND(count=len(targets)) + _skipped_note(skipped),
        level="info",
        data={"job_id": job["id"], "character_ids": [int(move.character_id) for move in targets]},
    )


def move_members(operation, moves: list[MoveRequest], *, actor) -> FleetActionResult:
    """Move members to a position: one in the request, up to ``MAX_BULK_MOVES`` as a "move" job."""
    moves = _unique_moves(moves)
    require_writable(operation, actor)
    if not moves:
        raise FleetManageError(NO_MEMBERS, 400)
    if len(moves) > MAX_BULK_MOVES:
        raise FleetManageError(TOO_MANY_MOVES, 400)
    _check_commander_moves(moves)
    if len(moves) == 1:
        return _move_one(operation, moves[0], actor=actor)
    return _start_bulk_move(operation, moves, actor=actor)


def run_bulk_move(job, operation, payload, actor) -> None:
    """Runner of the "move" fleet job.

    Each move is checked again against the fleet as it is now (members may have moved or left
    since the job was queued). A failed move is reported and the job goes on, unless the
    failure ends the job: the FC character is no longer the fleet boss, the fleet is gone, the
    write is forbidden or ESI's error limit is reached.
    """
    results, failures = [], []
    try:
        for character_id, role, wing_id, squad_id in payload["moves"]:
            fleet_jobs.ensure_writable(job, operation, actor)
            move = MoveRequest(int(character_id), role, wing_id, squad_id)
            entry = {"character_id": move.character_id, "role": role, "wing_id": wing_id, "squad_id": squad_id}
            results.append(entry)
            wings, members, known = _structure(operation)
            current = members.get(move.character_id)
            entry["character_name"] = current["name"] if current else _left_name(operation, move.character_id)
            try:
                member, body = _check_one_move(operation, move, wings, members, known)
            except FleetRuleError as exc:
                entry.update(result="failed", error=str(exc))
                failures.append(
                    {"character": entry["character_name"], "character_id": move.character_id, "error": str(exc)}
                )
                fleet_jobs.step(job, ok=False, message=str(exc))
                continue
            if body is None:
                entry["result"] = "unchanged"
                fleet_jobs.step(job, ok=True, message=texts.ALREADY_THERE(name=member["name"]))
                continue
            try:
                _send_move(operation, member, body)
            except Exception as exc:
                _move_failed(job, operation, member, exc, entry, failures)
                continue
            fleet_overlay.expect(operation.pk, _moved_change(member, body))
            entry["result"] = "ok"
            fleet_jobs.step(
                job,
                ok=True,
                message=_moved_message(member["name"], body["role"], body.get("wing_id"), body.get("squad_id"), wings),
            )
    finally:
        job["audit"] = {
            "moves": results,
            "ok": sum(1 for entry in results if entry.get("result") == "ok"),
            "failed": failures,
        }


def _move_failed(job, operation, member, exc, entry, failures) -> None:
    """Record a failed move of a job; raises JobStopped when the failure ends the job."""
    message, stop = job_failure(job, operation, exc, "Moving a fleet member")
    # A handover or a gone fleet ends the job; a member who left only fails this move, with ESI's text.
    not_found = isinstance(exc, FleetESIError) and exc.code == "FLEET_WRITE_NOT_FOUND"
    error = str(exc) if not_found else message
    entry.update(result="failed", error=error)
    failures.append({"character": member["name"], "character_id": int(member["character_id"]), "error": error})
    fleet_jobs.step(job, ok=False, message=error)
    if stop:
        raise fleet_jobs.JobStopped(stop)
