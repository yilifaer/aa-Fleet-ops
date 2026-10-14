# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""EVE fleet structure rules: wings, squads, positions and ship functions.

Pure helpers without database or ESI access, shared by model validators, forms, the
ESI provider, the fleet services and background jobs. Wings and squads are plain
dicts as ``normalize_wings`` returns them; members are dicts or objects with
``character_id``, ``role`` (or ``fleet_role``), ``wing_id``, ``squad_id`` and
optionally ``name`` (or ``character_name``), so ``FleetMemberState`` rows work too.

Rule violations raise ``FleetRuleError``, a ``ValueError`` with a message that can
be shown to users and a ``code`` for the caller's answer: ``invalid`` (bad input),
``not_found`` (unknown wing or squad), ``limit`` (EVE's size limits), ``not_empty``
(delete of a unit with members) or ``occupied`` (commander position taken).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from django.utils.translation import gettext, gettext_lazy

from fleetops import texts

FLEET_ROLES = ("fleet_commander", "wing_commander", "squad_commander", "squad_member")
COMMANDER_ROLES = FLEET_ROLES[:3]
# Labels of the positions, translated for whoever looks at them.
ROLE_LABELS = dict(texts.FLEET_ROLE_LABELS)

MAX_WINGS, MAX_SQUADS_PER_WING, FLEET_MAX_MEMBERS = 5, 5, 256
UNIT_NAME_MAX = 10  # ESI maxLength for wing and squad names
NO_UNIT = -1  # ESI's wing_id/squad_id for positions above a wing or squad
MAX_ID = 2**63 - 1

FLEET_COMMAND = gettext_lazy("Fleet Command")
WING_COMMAND = gettext_lazy("Wing Command")

SHIP_CATEGORY_ID = 6
RULE_KINDS = ("type", "group")
# Titan, Dreadnought, Freighter, Carrier, Supercarrier, Rorqual, Jump Freighter,
# Industrial Command Ship, Force Auxiliary, Lancer Dreadnought, Command Carrier.
DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS = "30,485,513,547,659,883,902,941,1538,4594,5120"

NAME_LENGTH_MESSAGE = texts.RULE_NAME_LENGTH(max=UNIT_NAME_MAX)

_LIST_SEPARATORS = re.compile(r"[\s,;]+")


class FleetRuleError(ValueError):
    """A rule violation; the message is a stored text (English) that ``translate_stored`` shows in any language."""

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _field(obj, *names, default=None):
    for name in names:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
        elif hasattr(obj, name):
            return getattr(obj, name)
    return default


def _as_int(value) -> int | None:
    """``value`` as an int, or None when it is not a whole number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        digits = text[1:] if text[:1] == "-" else text
        if digits.isascii() and digits.isdigit():
            return int(text)
    return None


def _unit(value) -> int | None:
    """A wing or squad id from stored data; None for -1, blanks and garbage."""
    number = _as_int(value)
    return number if number is not None and 0 < number <= MAX_ID else None


def _unit_id(value, kind: str) -> int | None:
    """A wing or squad id given for a change; -1 and blanks mean "none"."""
    if value is None or value == "":
        return None
    number = _as_int(value)
    if number == NO_UNIT:
        return None
    if number is None or not 0 < number <= MAX_ID:
        invalid = texts.RULE_INVALID_WING_ID if kind == "wing" else texts.RULE_INVALID_SQUAD_ID
        raise FleetRuleError(invalid(value=value))
    return number


# ---------------------------------------------------------------------------
# Names, id lists and structure layouts
# ---------------------------------------------------------------------------


def _clean_name(value) -> str:
    text = "" if value is None else str(value)
    # Line breaks and tabs become spaces; other control and format characters are dropped.
    text = "".join(char for char in text if char.isspace() or unicodedata.category(char)[0] != "C")
    return " ".join(text.split())


def clean_unit_name(value) -> str:
    """A wing or squad name as ESI accepts it: trimmed, single spaces, 1 to 10 characters."""
    name = _clean_name(value)
    if not 0 < len(name) <= UNIT_NAME_MAX:
        raise FleetRuleError(NAME_LENGTH_MESSAGE)
    return name


def parse_id_list(value) -> list[int]:
    """``"1, 2;3"`` -> ``[1, 2, 3]``; duplicates are dropped, the order is kept."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        tokens = [str(item).strip() for item in value]
    else:
        tokens = _LIST_SEPARATORS.split(str(value))
    ids = []
    for token in tokens:
        if not token:
            continue
        number = int(token) if token.isascii() and token.isdigit() else None
        if number is None or not 0 < number <= MAX_ID:
            raise FleetRuleError(texts.RULE_INVALID_ID(token=token))
        if number not in ids:
            ids.append(number)
    return ids


def format_id_list(ids) -> str:
    return ",".join(str(number) for number in parse_id_list(ids))


def _layout_name(text: str, line: int, kind: str) -> str:
    name = _clean_name(text)
    if not name:
        raise FleetRuleError((texts.RULE_WING_NAME_EMPTY if kind == "wing" else texts.RULE_SQUAD_NAME_EMPTY)(line=line))
    if len(name) > UNIT_NAME_MAX:
        too_long = texts.RULE_WING_NAME_LONG if kind == "wing" else texts.RULE_SQUAD_NAME_LONG
        raise FleetRuleError(too_long(line=line, name=name, max=UNIT_NAME_MAX))
    return name


def parse_structure_layout(text) -> list[dict]:
    """Parse a structure template: one ``Wing name: Squad A, Squad B`` line per wing.

    Blank lines and lines starting with ``#`` are skipped. Returns
    ``[{"name": str, "squads": [str, ...]}]``; duplicate names are allowed, as in EVE.
    """
    wings = []
    for number, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if len(wings) == MAX_WINGS:
            raise FleetRuleError(texts.RULE_LINE_TOO_MANY_WINGS(line=number, max=MAX_WINGS))
        wing_text, colon, squads_text = line.partition(":")
        name = _layout_name(wing_text, number, "wing")
        if not colon or not squads_text.strip():
            raise FleetRuleError(texts.RULE_LINE_NEEDS_SQUAD(line=number, name=name))
        if ":" in squads_text:
            raise FleetRuleError(texts.RULE_LINE_COLON(line=number))
        squads = [_layout_name(part, number, "squad") for part in squads_text.split(",")]
        if len(squads) > MAX_SQUADS_PER_WING:
            raise FleetRuleError(
                texts.RULE_LINE_TOO_MANY_SQUADS(line=number, name=name, count=len(squads), max=MAX_SQUADS_PER_WING)
            )
        wings.append({"name": name, "squads": squads})
    if not wings:
        raise FleetRuleError(texts.RULE_NO_WINGS())
    return wings


# ---------------------------------------------------------------------------
# Wings and squads
# ---------------------------------------------------------------------------


def normalize_wings(raw) -> list[dict]:
    """ESI wings (dicts or models) as ``[{"id", "name", "squads": [{"id", "name"}]}]``, ESI order kept."""
    wings = []
    for item in raw or ():
        wing_id = _unit(_field(item, "id"))
        if wing_id is None:
            continue
        squads = []
        for squad in _field(item, "squads") or ():
            squad_id = _unit(_field(squad, "id"))
            if squad_id is not None:
                squads.append({"id": squad_id, "name": str(_field(squad, "name") or "")})
        wings.append({"id": wing_id, "name": str(_field(item, "name") or ""), "squads": squads})
    return wings


def wing_index(wings) -> dict[int, dict]:
    """Wings by id; the first entry wins, so newer names can be listed before older ones."""
    index = {}
    for wing in wings or ():
        index.setdefault(wing["id"], wing)
    return index


def squad_index(wings) -> dict[int, tuple[dict, dict]]:
    """``squad_id -> (wing, squad)``; the first entry wins, as in ``wing_index``."""
    index = {}
    for wing in wings or ():
        for squad in wing["squads"]:
            index.setdefault(squad["id"], (wing, squad))
    return index


def wing_label(wing_id, wings=()) -> str:
    """The name of a wing; a wing without a name is called by its ID (in the active language)."""
    wing = wing_index(wings).get(_unit(wing_id))
    return (wing and wing["name"]) or gettext("Wing %(id)s") % {"id": wing_id}


def squad_label(squad_id, wings=()) -> str:
    """The name of a squad; a squad without a name is called by its ID (in the active language)."""
    found = squad_index(wings).get(_unit(squad_id))
    return (found and found[1]["name"]) or gettext("Squad %(id)s") % {"id": squad_id}


def stored_wing_label(wing_id, wings=()) -> str:
    """``wing_label`` in English, for text that is stored."""
    with texts.stored_language():
        return wing_label(wing_id, wings)


def stored_squad_label(squad_id, wings=()) -> str:
    """``squad_label`` in English, for text that is stored."""
    with texts.stored_language():
        return squad_label(squad_id, wings)


def position_labels(role, wing_id, squad_id, wings=()) -> tuple[str, str]:
    """Wing and squad names of a fleet position.

    ``("Fleet Command", "")`` for the fleet commander, ``("Logi", "Wing Command")`` for a
    wing commander and ``("Main", "Alpha")`` in a squad; unknown ids read "Wing <id>". The fixed words
    follow the active language; a stored text asks for English (``texts.stored_language``).
    """
    wing_id, squad_id = _unit(wing_id), _unit(squad_id)
    if role == "fleet_commander" or (wing_id is None and squad_id is None):
        return str(FLEET_COMMAND), ""
    if wing_id is None:
        # Only the squad is known: take its wing from the structure.
        found = squad_index(wings).get(squad_id)
        wing = (found[0]["name"] or wing_label(found[0]["id"])) if found else gettext("Unknown wing")
        return wing, squad_label(squad_id, wings)
    wing = wing_label(wing_id, wings)
    if role == "wing_commander" or squad_id is None:
        return wing, str(WING_COMMAND)
    return wing, squad_label(squad_id, wings)


def stored_position_labels(role, wing_id, squad_id, wings=()) -> tuple[str, str]:
    """``position_labels`` in English, for text that is stored."""
    with texts.stored_language():
        return position_labels(role, wing_id, squad_id, wings)


def _require_wing(wings, wing_id) -> dict:
    wing = wing_index(wings).get(_unit(wing_id))
    if wing is None:
        raise FleetRuleError(texts.RULE_WING_GONE(), "not_found")
    return wing


def _require_squad(wings, squad_id) -> tuple[dict, dict]:
    found = squad_index(wings).get(_unit(squad_id))
    if found is None:
        raise FleetRuleError(texts.RULE_SQUAD_GONE(), "not_found")
    return found


# ---------------------------------------------------------------------------
# Members and positions
# ---------------------------------------------------------------------------


def member_role(member) -> str:
    return str(_field(member, "role", "fleet_role", default="") or "")


def member_name(member) -> str:
    name = _field(member, "name", "character_name")
    return str(name) if name else gettext("Character %(id)s") % {"id": _field(member, "character_id")}


def member_position(member) -> dict:
    """A member's position in the shape of ``position_body`` (-1 ids left out)."""
    role = member_role(member)
    position = {"role": role}
    if role != "fleet_commander":
        wing_id, squad_id = _unit(_field(member, "wing_id")), _unit(_field(member, "squad_id"))
        if wing_id is not None:
            position["wing_id"] = wing_id
        if squad_id is not None and role != "wing_commander":
            position["squad_id"] = squad_id
    return position


def position_body(role, wing_id=None, squad_id=None, *, invite=False) -> dict:
    """The ESI body for moving (or, with ``invite``, inviting) a character to a position.

    The fleet commander takes neither id, a wing commander only ``wing_id``, squad
    commanders and members both. An invited squad member may also take neither and then
    joins any squad with free positions. -1 counts as "no id", as in ESI's member rows.
    """
    if role not in FLEET_ROLES:
        raise FleetRuleError(texts.RULE_NOT_A_POSITION(role=role))
    wing_id, squad_id = _unit_id(wing_id, "wing"), _unit_id(squad_id, "squad")
    if role == "fleet_commander":
        if wing_id is not None or squad_id is not None:
            raise FleetRuleError(texts.RULE_FLEET_COMMANDER_PLACE())
        return {"role": role}
    if role == "wing_commander":
        if wing_id is None:
            raise FleetRuleError(texts.RULE_WING_COMMANDER_NEEDS_WING())
        if squad_id is not None:
            raise FleetRuleError(texts.RULE_WING_COMMANDER_NO_SQUAD())
        return {"role": role, "wing_id": wing_id}
    if wing_id is None and squad_id is None and invite and role == "squad_member":
        return {"role": role}
    if wing_id is None or squad_id is None:
        if invite and role == "squad_member":
            raise FleetRuleError(texts.RULE_ANY_SQUAD_HINT())
        if role == "squad_commander":
            raise FleetRuleError(texts.RULE_SQUAD_COMMANDER_NEEDS_PLACE())
        raise FleetRuleError(texts.RULE_SQUAD_MEMBER_NEEDS_PLACE())
    return {"role": role, "wing_id": wing_id, "squad_id": squad_id}


def wing_members(members, wing_id, wings=()) -> list:
    """Members placed in a wing: its commander and everyone in its squads."""
    wing_id = _unit(wing_id)
    wing = wing_index(wings).get(wing_id)
    squad_ids = {squad["id"] for squad in wing["squads"]} if wing else set()
    return [
        member
        for member in members
        if member_role(member) != "fleet_commander"
        and (_unit(_field(member, "wing_id")) == wing_id or _unit(_field(member, "squad_id")) in squad_ids)
    ]


def squad_members(members, squad_id) -> list:
    """Members placed in a squad, its commander included."""
    squad_id = _unit(squad_id)
    return [
        member
        for member in members
        if member_role(member) in ("squad_commander", "squad_member") and _unit(_field(member, "squad_id")) == squad_id
    ]


def commander(members, role, wing_id=None, squad_id=None):
    """The member holding a commander position, or None."""
    for member in members:
        if member_role(member) != role:
            continue
        if role == "fleet_commander":
            return member
        if role == "wing_commander" and _unit(_field(member, "wing_id")) == _unit(wing_id):
            return member
        if role == "squad_commander" and _unit(_field(member, "squad_id")) == _unit(squad_id):
            return member
    return None


def _check_units(body: dict, wings) -> None:
    if "squad_id" in body:
        wing, _squad = _require_squad(wings, body["squad_id"])
        if wing["id"] != body["wing_id"]:
            raise FleetRuleError(texts.RULE_SQUAD_NOT_IN_WING(), "not_found")
    elif "wing_id" in body:
        _require_wing(wings, body["wing_id"])


def _check_slot(body: dict, members, wings, character_id=None) -> None:
    role = body["role"]
    if role not in COMMANDER_ROLES:
        return
    holder = commander(members, role, body.get("wing_id"), body.get("squad_id"))
    if holder is None or _as_int(_field(holder, "character_id")) == _as_int(character_id):
        return
    # The message is stored with the job or the audit entry, so its names are written in English.
    with texts.stored_language():
        holder_name = member_name(holder)
        if role == "fleet_commander":
            message = texts.RULE_FLEET_COMMANDER_TAKEN(holder=holder_name)
        elif role == "wing_commander":
            message = texts.RULE_WING_COMMANDER_TAKEN(unit=wing_label(body["wing_id"], wings), holder=holder_name)
        else:
            message = texts.RULE_SQUAD_COMMANDER_TAKEN(unit=squad_label(body["squad_id"], wings), holder=holder_name)
    raise FleetRuleError(message, "occupied")


def check_move(member, role, wing_id=None, squad_id=None, *, wings=None, members=()) -> dict | None:
    """Check moving ``member`` and return the ESI body, or None when it is already there.

    ``wings`` is the known structure; None skips the wing and squad checks (structure not
    read yet). ``members`` are the current fleet members, for the commander positions.
    """
    body = position_body(role, wing_id, squad_id)
    if wings is not None:
        _check_units(body, wings)
    if member_position(member) == body:
        return None
    _check_slot(body, members, wings or (), _field(member, "character_id"))
    return body


def check_invite(role="squad_member", wing_id=None, squad_id=None, *, wings=None, members=()) -> dict:
    """Check an invitation position and return its ESI body (without ``character_id``)."""
    body = position_body(role, wing_id, squad_id, invite=True)
    if wings is not None:
        _check_units(body, wings)
    _check_slot(body, members, wings or ())
    return body


def check_fleet_room(member_count: int, adding: int = 1) -> None:
    free = FLEET_MAX_MEMBERS - member_count
    if adding <= free:
        return
    if free <= 0:
        raise FleetRuleError(texts.RULE_FLEET_FULL(max=FLEET_MAX_MEMBERS), "limit")
    raise FleetRuleError(texts.RULE_FLEET_ROOM(count=free, max=FLEET_MAX_MEMBERS), "limit")


def check_add_wing(wings) -> None:
    if len(wings or ()) >= MAX_WINGS:
        raise FleetRuleError(texts.RULE_TOO_MANY_WINGS(max=MAX_WINGS), "limit")


def check_add_squad(wings, wing_id) -> dict:
    wing = _require_wing(wings, wing_id)
    if len(wing["squads"]) >= MAX_SQUADS_PER_WING:
        raise FleetRuleError(texts.RULE_TOO_MANY_SQUADS(max=MAX_SQUADS_PER_WING), "limit")
    return wing


def check_rename_wing(wings, wing_id, name) -> tuple[dict, str]:
    return _require_wing(wings, wing_id), clean_unit_name(name)


def check_rename_squad(wings, squad_id, name) -> tuple[dict, dict, str]:
    wing, squad = _require_squad(wings, squad_id)
    return wing, squad, clean_unit_name(name)


def check_delete_wing(wings, wing_id, members) -> dict:
    """Only empty wings can be deleted; their squads may stay as long as they are empty."""
    wing = _require_wing(wings, wing_id)
    if wing_members(members, wing["id"], wings):
        raise FleetRuleError(texts.RULE_WING_NOT_EMPTY(), "not_empty")
    return wing


def check_delete_squad(wings, squad_id, members) -> tuple[dict, dict]:
    wing, squad = _require_squad(wings, squad_id)
    count = len(squad_members(members, squad["id"]))
    if count:
        raise FleetRuleError(texts.RULE_SQUAD_NOT_EMPTY(count=count), "not_empty")
    return wing, squad


# ---------------------------------------------------------------------------
# Ship functions
# ---------------------------------------------------------------------------


def parse_fleet_roles(value) -> tuple[str, ...]:
    """Commander positions from ``"wing_commander, fleet_commander"``, in fleet order."""
    if value is None:
        return ()
    items = value if isinstance(value, (list, tuple, set, frozenset)) else _LIST_SEPARATORS.split(str(value))
    found = set()
    for item in items:
        role = str(item).strip()
        if not role:
            continue
        if role.lower() not in COMMANDER_ROLES:
            raise FleetRuleError(texts.RULE_NOT_A_COMMANDER_POSITION(role=role))
        found.add(role.lower())
    return tuple(role for role in COMMANDER_ROLES if role in found)


def format_fleet_roles(value) -> str:
    return ",".join(parse_fleet_roles(value))


def _stored_roles(value) -> tuple[str, ...]:
    # Stored values were validated on save; anything unknown is ignored rather than fatal. The
    # validator accepts any letter case, so stored values are compared without it.
    roles = (role.strip().lower() for role in _LIST_SEPARATORS.split(str(value or "")))
    return tuple(role for role in roles if role in COMMANDER_ROLES)


def _function_id(function):
    return _field(function, "id", "pk")


def _rule_function_id(rule):
    function_id = _field(rule, "function_id")
    if function_id is None:
        function = _field(rule, "function")
        function_id = function if _as_int(function) is not None else _function_id(function)
    return function_id


@dataclass(frozen=True)
class ShipFunctionMap:
    """Which ship function a fleet member counts under.

    The first hit wins: an active function for the member's fleet position, a rule for
    the ship type, a rule for the ship group, then the default function. The default
    only takes hulls the SDE knows as ships (category 6); anything else is unclassified.
    Functions and rules are model instances or dicts; inactive functions and their
    rules are ignored, and the lowest ``sort_order`` wins any overlap.
    """

    by_role: dict[str, Any] = field(default_factory=dict)
    by_type: dict[int, Any] = field(default_factory=dict)
    by_group: dict[int, Any] = field(default_factory=dict)
    default: Any = None

    @classmethod
    def build(cls, functions, rules=()) -> ShipFunctionMap:
        active = [function for function in functions if _field(function, "is_active", default=True)]
        active.sort(key=lambda item: (_field(item, "sort_order") or 0, str(_field(item, "name", default=""))))
        rank, by_role, default = {}, {}, None
        for position, function in enumerate(active):
            rank.setdefault(_function_id(function), (position, function))
            for role in _stored_roles(_field(function, "fleet_roles")):
                by_role.setdefault(role, function)
            if default is None and _field(function, "is_default", default=False):
                default = function
        tables = {"type": {}, "group": {}}
        for rule in rules:
            ranked = rank.get(_rule_function_id(rule))
            table = tables.get(str(_field(rule, "kind", default="")))
            target = _unit(_field(rule, "target_id"))
            if ranked is None or table is None or target is None:
                continue
            current = table.get(target)
            if current is None or ranked[0] < current[0]:
                table[target] = ranked
        return cls(
            by_role=by_role,
            by_type={target: ranked[1] for target, ranked in tables["type"].items()},
            by_group={target: ranked[1] for target, ranked in tables["group"].items()},
            default=default,
        )

    def resolve(self, role=None, ship_type_id=None, group_id=None, category_id=None):
        """The function for one member, or None for "Unclassified"."""
        if role in self.by_role:
            return self.by_role[role]
        ship_type_id, group_id = _unit(ship_type_id), _unit(group_id)
        if ship_type_id in self.by_type:
            return self.by_type[ship_type_id]
        if group_id in self.by_group:
            return self.by_group[group_id]
        if _as_int(category_id) == SHIP_CATEGORY_ID:
            return self.default
        return None
