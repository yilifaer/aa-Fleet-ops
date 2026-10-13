"""Changes FleetOps made to a fleet that ESI may not show yet.

ESI answers the fleet routes from its own cache for 5 seconds, so a read right after a change can
still show the old fleet. Every successful change stores what it expects here; the fleet view and the
checks before the next change apply it to the stored snapshot until a read made at least
``ESI_READ_LAG`` seconds after the change shows ESI's own state. Entries live in the Django cache:
losing them only means the old state shows a few seconds longer, nothing wrong is ever stored.

Change kinds (each applied idempotently):
``wing_created {wing_id, name}``, ``wing_renamed {wing_id, name}``, ``wing_deleted {wing_id}``,
``squad_created {wing_id, squad_id, name}``, ``squad_renamed {squad_id, name}``, ``squad_deleted {squad_id}``,
``member_moved {character_id, role, wing_id, squad_id}`` and ``invited {character_id, name, role, wing_id,
squad_id}``.
"""

import time

from django.core.cache import cache

from fleetops.services.fleet_rules import NO_UNIT, normalize_wings

EXPECT_TTL = 30  # seconds, safety expiry of a structure or position change
ESI_READ_LAG = 6  # a read at least this long after a change is trusted over the change
INVITE_TTL = 120  # invitations show as pending this long unless the character joins

STRUCTURE_KINDS = (
    "wing_created",
    "wing_renamed",
    "wing_deleted",
    "squad_created",
    "squad_renamed",
    "squad_deleted",
)
MEMBER_KINDS = ("member_moved",)
INVITE_KINDS = ("invited",)
KINDS = STRUCTURE_KINDS + MEMBER_KINDS + INVITE_KINDS

NEW_WING_NAME = "Wing"
NEW_SQUAD_NAME = "Squad"
# Fields of a tracked member used by the fleet view and the change checks.
MEMBER_FIELDS = (
    "character_id",
    "character_name",
    "fleet_role",
    "wing_id",
    "squad_id",
    "ship_type_id",
    "ship_type_name",
    "solar_system_id",
    "solar_system_name",
    "station_id",
    "takes_fleet_warp",
)


def _key(operation_id):
    return f"fleetops:fleet:{operation_id}:expect"


def _ttl(change) -> int:
    return INVITE_TTL if change.get("kind") in INVITE_KINDS else EXPECT_TTL


def expect(operation_id, change: dict) -> None:
    """Remember a change FleetOps just made; ``change["kind"]`` is one of KINDS."""
    if change.get("kind") not in KINDS:
        raise ValueError(f"Unknown fleet change {change.get('kind')!r}")
    now = time.time()
    entries = [entry for entry in cache.get(_key(operation_id)) or [] if now - entry["at"] < _ttl(entry)]
    entries.append({**change, "at": change.get("at", now)})
    cache.set(_key(operation_id), entries, INVITE_TTL)


def expectations(operation_id) -> list[dict]:
    """The remembered changes that have not expired, oldest first."""
    now = time.time()
    entries = [entry for entry in cache.get(_key(operation_id)) or [] if now - entry["at"] < _ttl(entry)]
    return sorted(entries, key=lambda entry: entry["at"])


def forget(operation_id) -> None:
    cache.delete(_key(operation_id))


def _still_expected(change, read_at) -> bool:
    """A change counts until a read made ESI_READ_LAG seconds after it has replaced it."""
    return read_at is None or read_at.timestamp() < change["at"] + ESI_READ_LAG


def _unit(value):
    """A wing or squad id as stored for members: -1 for none."""
    return NO_UNIT if value in (None, "", NO_UNIT) else int(value)


def member_dict(member) -> dict:
    """A tracked member (FleetMemberState or dict) in the shape the overlay works on.

    The dict keeps the FleetMemberState row under ``state`` (None for dict input) and adds
    ``role`` and ``name`` for the fleet rule helpers.
    """
    if isinstance(member, dict):
        data = dict(member)
        data.setdefault("state", None)
    else:
        data = {field: getattr(member, field, None) for field in MEMBER_FIELDS}
        data["state"] = member
    data.setdefault("fleet_role", data.get("role", ""))
    data["role"] = data.get("fleet_role") or data.get("role") or ""
    data.setdefault("character_name", data.get("name", ""))
    data["name"] = data.get("character_name") or data.get("name") or ""
    data["wing_id"] = _unit(data.get("wing_id"))
    data["squad_id"] = _unit(data.get("squad_id"))
    data.setdefault("pending", False)
    return data


def _apply_structure(wings, change) -> None:
    kind = change["kind"]
    by_id = {wing["id"]: wing for wing in wings}
    if kind == "wing_created":
        if change["wing_id"] not in by_id:
            name = change.get("name") or NEW_WING_NAME
            wings.append({"id": change["wing_id"], "name": name, "squads": [], "pending": True})
        elif change.get("name"):
            by_id[change["wing_id"]].update(name=change["name"], pending=True)
    elif kind == "wing_renamed":
        wing = by_id.get(change["wing_id"])
        if wing is not None:
            wing.update(name=change["name"], pending=True)
    elif kind == "wing_deleted":
        wings[:] = [wing for wing in wings if wing["id"] != change["wing_id"]]
    elif kind == "squad_created":
        wing = by_id.get(change["wing_id"])
        if wing is None:
            return
        squad = next((squad for squad in wing["squads"] if squad["id"] == change["squad_id"]), None)
        if squad is None:
            name = change.get("name") or NEW_SQUAD_NAME
            wing["squads"].append({"id": change["squad_id"], "name": name, "pending": True})
        elif change.get("name"):
            squad.update(name=change["name"], pending=True)
    elif kind == "squad_renamed":
        for wing in wings:
            for squad in wing["squads"]:
                if squad["id"] == change["squad_id"]:
                    squad.update(name=change["name"], pending=True)
    elif kind == "squad_deleted":
        for wing in wings:
            wing["squads"] = [squad for squad in wing["squads"] if squad["id"] != change["squad_id"]]


def _apply_member(members, change) -> None:
    member = members.get(int(change["character_id"]))
    if member is None:
        # The member left; a move cannot bring it back.
        return
    role = change["role"]
    member.update(
        role=role,
        fleet_role=role,
        wing_id=_unit(change.get("wing_id")),
        squad_id=_unit(change.get("squad_id")),
        pending=True,
    )


def current_structure(operation, *, members=None) -> tuple[list[dict], dict[int, dict]]:
    """The fleet as FleetOps expects it now: ``(wings, members by character id)``.

    Starts from the stored snapshot (``operation.fleet_wings`` and the active member states, or the
    given ``members``) and applies the remembered changes that no late enough read has replaced.
    Wings, squads and members changed that way carry ``"pending": True``; everything else ``False``.
    """
    wings = [
        {**wing, "pending": False, "squads": [{**squad, "pending": False} for squad in wing["squads"]]}
        for wing in normalize_wings(operation.fleet_wings or [])
    ]
    if members is None:
        members = operation.member_states.filter(is_active=True)
    by_id = {}
    for member in members:
        data = member_dict(member)
        by_id[int(data["character_id"])] = data
    changes = expectations(operation.pk)
    for change in changes:
        if change["kind"] in STRUCTURE_KINDS and _still_expected(change, operation.fleet_wings_read_at):
            _apply_structure(wings, change)
        elif change["kind"] in MEMBER_KINDS and _still_expected(change, operation.last_esi_update):
            _apply_member(by_id, change)
    return wings, by_id


def pending_invitations(operation_id, active_ids) -> list[dict]:
    """Invitations sent in the last two minutes to characters that are not in the fleet yet, newest first.

    Each is ``{character_id, name, role, wing_id, squad_id, at}``; a character invited twice shows once.
    """
    active = {int(character_id) for character_id in active_ids}
    latest = {}
    for change in expectations(operation_id):
        if change["kind"] in INVITE_KINDS and int(change["character_id"]) not in active:
            latest[int(change["character_id"])] = change
    invitations = [
        {
            "character_id": character_id,
            "name": change.get("name") or f"Character {character_id}",
            "role": change.get("role") or "squad_member",
            "wing_id": change.get("wing_id"),
            "squad_id": change.get("squad_id"),
            "at": change["at"],
        }
        for character_id, change in latest.items()
    ]
    return sorted(invitations, key=lambda invitation: -invitation["at"])
