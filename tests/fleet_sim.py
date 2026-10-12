"""An in-memory EVE fleet behind fake django-esi operations, for fleet management tests.

``FleetSim`` stands in for ``esi.client.Fleets``. Its operations follow django-esi's
``EsiOperation.result()``: GET responses are cached per operation and parameters (token
and body excluded), ``force_refresh`` drops that entry and its ETag, a read with a known
ETag raises ``HTTPNotModified``, and a body sent to an operation that takes none, a
missing body, unknown or missing parameters and tokens without the fleet scope raise
``ValueError`` before anything is sent. ESI errors are django-esi's ``HTTPClientError``
or ``HTTPServerError`` with an ``{"error": ...}`` body; HTTP 420 raises
``ESIErrorLimitException``.

The fleets follow EVE's rules: only the boss's token can read or change a fleet (anyone
else gets 404), new wings and squads get fresh ids, only empty units can be deleted,
each commander position holds one character, and a fleet has at most 5 wings of 5
squads and 256 members. Invitations wait for ``accept_invite()``. Responses follow
the ESI schemas of compatibility date 2025-11-06 (``fleet_boss_id`` included), as
plain dicts, pydantic models or attribute objects (``response_style``).

    sim = FleetSim()
    fleet = sim.create_fleet(boss_id=fc.character_id)
    sim.install(self)  # patches fleetops.providers.esi for this test
    sim.fail("PutFleetsFleetIdWingsWingId", status=502)
    sim.stale_reads = 1  # after each write, the next read of each fleet route is the old state
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from types import SimpleNamespace
from unittest import mock

from django.utils import timezone
from esi.exceptions import ESIErrorLimitException, HTTPClientError, HTTPNotModified, HTTPServerError
from pydantic import BaseModel

from fleetops.constants import FLEET_READ_SCOPE, FLEET_WRITE_SCOPE

ROLES = ("fleet_commander", "wing_commander", "squad_commander", "squad_member")
ROLE_NAMES = {
    "fleet_commander": "Fleet Commander",
    "wing_commander": "Wing Commander",
    "squad_commander": "Squad Commander",
    "squad_member": "Squad Member",
}
MAX_WINGS = MAX_SQUADS = 5
MAX_MEMBERS = 256
NAME_MAX = 10
NONE = -1  # ESI's wing_id/squad_id for "not applicable"

FLEET_NOT_FOUND = "The fleet does not exist or you don't have access to it!"
HEADER_PARAMS = ("Accept-Language", "If-None-Match", "X-Compatibility-Date", "X-Tenant", "If-Modified-Since")
MEMBER_DETAILS = ("ship_type_id", "solar_system_id", "station_id", "takes_fleet_warp", "join_time")


@dataclass(frozen=True)
class OperationSpec:
    method: str
    path: tuple[str, ...]
    scope: str
    body: bool = False
    status: int = 200


OPERATIONS = {
    "GetCharactersCharacterIdFleet": OperationSpec("GET", ("character_id",), FLEET_READ_SCOPE),
    "GetFleetsFleetId": OperationSpec("GET", ("fleet_id",), FLEET_READ_SCOPE),
    "PutFleetsFleetId": OperationSpec("PUT", ("fleet_id",), FLEET_WRITE_SCOPE, body=True, status=204),
    "GetFleetsFleetIdMembers": OperationSpec("GET", ("fleet_id",), FLEET_READ_SCOPE),
    "PostFleetsFleetIdMembers": OperationSpec("POST", ("fleet_id",), FLEET_WRITE_SCOPE, body=True, status=204),
    "PutFleetsFleetIdMembersMemberId": OperationSpec(
        "PUT", ("fleet_id", "member_id"), FLEET_WRITE_SCOPE, body=True, status=204
    ),
    "DeleteFleetsFleetIdMembersMemberId": OperationSpec(
        "DELETE", ("fleet_id", "member_id"), FLEET_WRITE_SCOPE, status=204
    ),
    "GetFleetsFleetIdWings": OperationSpec("GET", ("fleet_id",), FLEET_READ_SCOPE),
    "PostFleetsFleetIdWings": OperationSpec("POST", ("fleet_id",), FLEET_WRITE_SCOPE, status=201),
    "PutFleetsFleetIdWingsWingId": OperationSpec(
        "PUT", ("fleet_id", "wing_id"), FLEET_WRITE_SCOPE, body=True, status=204
    ),
    "DeleteFleetsFleetIdWingsWingId": OperationSpec("DELETE", ("fleet_id", "wing_id"), FLEET_WRITE_SCOPE, status=204),
    "PostFleetsFleetIdWingsWingIdSquads": OperationSpec("POST", ("fleet_id", "wing_id"), FLEET_WRITE_SCOPE, status=201),
    "PutFleetsFleetIdSquadsSquadId": OperationSpec(
        "PUT", ("fleet_id", "squad_id"), FLEET_WRITE_SCOPE, body=True, status=204
    ),
    "DeleteFleetsFleetIdSquadsSquadId": OperationSpec(
        "DELETE", ("fleet_id", "squad_id"), FLEET_WRITE_SCOPE, status=204
    ),
}
# ESI itself caches these for 5 seconds (x-cache-age), which ``stale_reads`` imitates.
FLEET_READS = ("GetFleetsFleetId", "GetFleetsFleetIdMembers", "GetFleetsFleetIdWings")


# Response models, named as in django-esi's stubs.


class CharactersCharacterIdFleetGet(BaseModel):
    fleet_boss_id: int
    fleet_id: int
    role: str
    squad_id: int
    wing_id: int


class FleetsFleetIdGet(BaseModel):
    is_free_move: bool
    is_registered: bool
    is_voice_enabled: bool
    motd: str


class FleetsFleetIdMembersGetItem(BaseModel):
    character_id: int
    join_time: datetime
    role: str
    role_name: str
    ship_type_id: int
    solar_system_id: int
    squad_id: int
    station_id: int | None = None
    takes_fleet_warp: bool
    wing_id: int


class FleetsFleetIdWingsGetItem_SquadsItem(BaseModel):  # noqa: N801
    id: int
    name: str


class FleetsFleetIdWingsGetItem(BaseModel):
    id: int
    name: str
    squads: list[FleetsFleetIdWingsGetItem_SquadsItem]


class FleetsFleetIdWingsPost(BaseModel):
    wing_id: int


class FleetsFleetIdWingsWingIdSquadsPost(BaseModel):
    squad_id: int


class Error(BaseModel):
    error: str


RESPONSE_MODELS = {
    "GetCharactersCharacterIdFleet": CharactersCharacterIdFleetGet,
    "GetFleetsFleetId": FleetsFleetIdGet,
    "GetFleetsFleetIdMembers": FleetsFleetIdMembersGetItem,
    "GetFleetsFleetIdWings": FleetsFleetIdWingsGetItem,
    "PostFleetsFleetIdWings": FleetsFleetIdWingsPost,
    "PostFleetsFleetIdWingsWingIdSquads": FleetsFleetIdWingsWingIdSquadsPost,
}


class SimError(Exception):
    """An ESI error answer: HTTP ``status`` with ``{"error": message}``."""

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


@dataclass(frozen=True)
class SimRequest:
    """A call that reached ESI; ``character_id`` is the owner of the token it used."""

    name: str
    method: str
    params: dict
    body: dict | None
    character_id: int | None
    etag: str | None = None


@dataclass(frozen=True)
class SimResponse:
    status_code: int
    headers: dict


@dataclass
class SimFailure:
    name: str
    status: int
    error: str | None
    exception: BaseException | None
    times: int | None
    match: dict


@dataclass
class SimMember:
    character_id: int
    role: str = "squad_member"
    wing_id: int = NONE
    squad_id: int = NONE
    ship_type_id: int = 11987
    solar_system_id: int = 30000142
    station_id: int | None = None
    takes_fleet_warp: bool = True
    join_time: datetime | None = None


def _is_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _plain_id(value):
    """-1 and None both mean "no wing/squad" in test setup calls."""
    return None if value is None or value == NONE else value


def _check_combination(role, wing_id, squad_id, *, invite=False) -> None:
    """ESI's rule for which ids go with which role."""
    if role not in ROLES:
        raise SimError(400, f"role must be one of {', '.join(ROLES)}")
    for key, value in (("wing_id", wing_id), ("squad_id", squad_id)):
        if value is not None and not _is_id(value):
            raise SimError(400, f"{key} must be an integer")
    given = (wing_id is not None, squad_id is not None)
    if role == "fleet_commander" and given != (False, False):
        raise SimError(400, "Neither wing_id nor squad_id may be set for the fleet_commander role")
    if role == "wing_commander" and given != (True, False):
        raise SimError(400, "Only wing_id must be set for the wing_commander role")
    if role == "squad_commander" and given != (True, True):
        raise SimError(400, "Both wing_id and squad_id must be set for the squad_commander role")
    if role == "squad_member" and given != (True, True) and not (invite and given == (False, False)):
        raise SimError(400, "Both wing_id and squad_id must be set for the squad_member role")


def _check_keys(body: dict, allowed: set, required: set) -> None:
    unknown = set(body) - allowed
    if unknown:
        raise SimError(400, f"Unknown field(s): {', '.join(sorted(unknown))}")
    missing = required - set(body)
    if missing:
        raise SimError(400, f"Missing field(s): {', '.join(sorted(missing))}")


def _body_name(body: dict) -> str:
    _check_keys(body, {"name"}, {"name"})
    name = body["name"]
    if not isinstance(name, str):
        raise SimError(400, "name must be a string")
    if len(name) > NAME_MAX:
        raise SimError(400, f"name must not be longer than {NAME_MAX} characters")
    return name


def _token_scopes(token) -> set | None:
    scopes = getattr(token, "scopes", None)
    if scopes is None:
        return None
    if hasattr(scopes, "all"):
        return set(scopes.all().values_list("name", flat=True))
    return {getattr(scope, "name", scope) for scope in scopes}


def _param_name(key: str) -> str:
    dashed = key.replace("_", "-").lower()
    return next((header for header in HEADER_PARAMS if header.lower() == dashed), key)


@dataclass(eq=False)
class SimFleet:
    """One fleet. Its helper methods are in-game changes: not ESI calls, not logged."""

    sim: FleetSim = field(repr=False)
    fleet_id: int
    boss_id: int
    motd: str = ""
    is_free_move: bool = False
    is_registered: bool = False
    is_voice_enabled: bool = False
    wings: list = field(default_factory=list)
    members: dict = field(default_factory=dict)
    invitations: dict = field(default_factory=dict)

    # Structure

    def wing(self, wing_id) -> dict:
        for wing in self.wings:
            if wing["id"] == wing_id:
                return wing
        raise SimError(404, "Wing not found")

    def squad(self, squad_id) -> tuple[dict, dict]:
        for wing in self.wings:
            for squad in wing["squads"]:
                if squad["id"] == squad_id:
                    return wing, squad
        raise SimError(404, "Squad not found")

    def wing_id(self, name: str) -> int:
        for wing in self.wings:
            if wing["name"] == name:
                return wing["id"]
        raise KeyError(name)

    def squad_id(self, name: str, wing=None) -> int:
        """The first squad called ``name``, optionally inside a wing given by id or name."""
        for candidate in self.wings:
            if wing is not None and wing not in (candidate["id"], candidate["name"]):
                continue
            for squad in candidate["squads"]:
                if squad["name"] == name:
                    return squad["id"]
        raise KeyError(name)

    def structure(self) -> list[dict]:
        return copy.deepcopy(self.wings)

    def add_wing(self, name: str | None = None, squads=1) -> int:
        """Create a wing with ``squads`` (a count or a list of names); returns its id."""
        if len(self.wings) >= MAX_WINGS:
            raise SimError(400, "The fleet already has the maximum number of wings")
        wing = {"id": next(self.sim._wing_ids), "name": name or f"Wing {len(self.wings) + 1}", "squads": []}
        self.wings.append(wing)
        squad_names = [None] * squads if isinstance(squads, int) else list(squads)
        for squad_name in squad_names:
            self.add_squad(wing["id"], squad_name)
        return wing["id"]

    def add_squad(self, wing_id, name: str | None = None) -> int:
        wing = self.wing(wing_id)
        if len(wing["squads"]) >= MAX_SQUADS:
            raise SimError(400, "The wing already has the maximum number of squads")
        squad = {"id": next(self.sim._squad_ids), "name": name or f"Squad {len(wing['squads']) + 1}"}
        wing["squads"].append(squad)
        return squad["id"]

    def rename_wing(self, wing_id, name: str) -> None:
        self.wing(wing_id)["name"] = name

    def rename_squad(self, squad_id, name: str) -> None:
        self.squad(squad_id)[1]["name"] = name

    def delete_wing(self, wing_id) -> None:
        """Only empty wings can go; their squads may stay as long as they are empty."""
        wing = self.wing(wing_id)
        squad_ids = {squad["id"] for squad in wing["squads"]}
        if any(member.wing_id == wing["id"] or member.squad_id in squad_ids for member in self.members.values()):
            raise SimError(400, "Only empty wings can be deleted")
        self.wings.remove(wing)

    def delete_squad(self, squad_id) -> None:
        wing, squad = self.squad(squad_id)
        if any(member.squad_id == squad["id"] for member in self.members.values()):
            raise SimError(400, "Only empty squads can be deleted")
        wing["squads"].remove(squad)

    # Members

    def member(self, character_id) -> SimMember:
        member = self.members.get(character_id)
        if member is None:
            raise SimError(404, "Fleet member not found")
        return member

    def position(self, character_id) -> tuple[str, int, int]:
        member = self.member(character_id)
        return member.role, member.wing_id, member.squad_id

    def positions(self) -> dict[int, tuple[str, int, int]]:
        return {member.character_id: (member.role, member.wing_id, member.squad_id) for member in self.members.values()}

    def _first_squad(self) -> tuple[int, int]:
        for wing in self.wings:
            if wing["squads"]:
                return wing["id"], wing["squads"][0]["id"]
        raise SimError(400, "No squad has a free position")

    def _position(self, role, wing_id, squad_id, character_id) -> tuple[str, int, int]:
        """Check a position against EVE's rules; returns it with -1 for "not applicable"."""
        wing_id, squad_id = _plain_id(wing_id), _plain_id(squad_id)
        _check_combination(role, wing_id, squad_id)
        if squad_id is not None:
            wing, _squad = self.squad(squad_id)
            if wing["id"] != wing_id:
                self.wing(wing_id)
                raise SimError(404, "Squad not found in this wing")
        elif wing_id is not None:
            self.wing(wing_id)
        if role != "squad_member":
            for other in self.members.values():
                if other.character_id == character_id or other.role != role:
                    continue
                if (
                    role == "fleet_commander"
                    or (role == "wing_commander" and other.wing_id == wing_id)
                    or (role == "squad_commander" and other.squad_id == squad_id)
                ):
                    unit = {"fleet_commander": "fleet", "wing_commander": "wing", "squad_commander": "squad"}[role]
                    raise SimError(400, f"The {unit} already has a {ROLE_NAMES[role].lower()}")
        return role, NONE if wing_id is None else wing_id, NONE if squad_id is None else squad_id

    def add_member(self, character_id, role="squad_member", wing_id=None, squad_id=None, **details) -> SimMember:
        """A character joins in game; without ids, squad positions go to the first squad."""
        if self.sim.fleet_of(character_id) is not None:
            raise SimError(400, "Character is already in a fleet")
        if len(self.members) >= MAX_MEMBERS:
            raise SimError(400, "The fleet is full")
        wing_id, squad_id = _plain_id(wing_id), _plain_id(squad_id)
        if role in ("squad_commander", "squad_member") and wing_id is None and squad_id is None:
            wing_id, squad_id = self._first_squad()
        elif role == "wing_commander" and wing_id is None and self.wings:
            wing_id = self.wings[0]["id"]
        role, wing_id, squad_id = self._position(role, wing_id, squad_id, character_id)
        details.setdefault("join_time", timezone.now())
        member = SimMember(character_id, role, wing_id, squad_id, **details)
        self.members[character_id] = member
        self.invitations.pop(character_id, None)
        return member

    def move_member(self, character_id, role, wing_id=None, squad_id=None) -> SimMember:
        member = self.member(character_id)
        member.role, member.wing_id, member.squad_id = self._position(role, wing_id, squad_id, character_id)
        return member

    def update_member(self, character_id, **details) -> SimMember:
        """Change ship, system, docking, fleet warp or join time of a member."""
        member = self.member(character_id)
        for key, value in details.items():
            if key not in MEMBER_DETAILS:
                raise TypeError(f"Unknown member detail {key!r}")
            setattr(member, key, value)
        return member

    def remove_member(self, character_id) -> None:
        """The character leaves. A leaving boss hands over to the fleet commander, else the longest member."""
        self.member(character_id)
        del self.members[character_id]
        if character_id != self.boss_id:
            return
        if not self.members:
            self.sim.disband(self.fleet_id)
            return
        members = list(self.members.values())
        successor = next((member for member in members if member.role == "fleet_commander"), members[0])
        self.boss_id = successor.character_id

    def set_boss(self, character_id) -> None:
        self.member(character_id)
        self.boss_id = character_id

    def accept_invite(self, character_id, **details) -> SimMember:
        invitation = self.invitations[character_id]
        wing_id, squad_id = invitation["wing_id"], invitation["squad_id"]
        if invitation["role"] == "squad_member" and wing_id is None:
            # Joins any squad with available positions.
            wing_id, squad_id = self._first_squad()
        return self.add_member(character_id, invitation["role"], wing_id, squad_id, **details)

    def decline_invite(self, character_id) -> None:
        del self.invitations[character_id]

    # ESI views

    def _member_row(self, member: SimMember) -> dict:
        row = {
            "character_id": member.character_id,
            "join_time": member.join_time,
            "role": member.role,
            "role_name": ROLE_NAMES[member.role] + (" (Boss)" if member.character_id == self.boss_id else ""),
            "ship_type_id": member.ship_type_id,
            "solar_system_id": member.solar_system_id,
            "squad_id": member.squad_id,
            "takes_fleet_warp": member.takes_fleet_warp,
            "wing_id": member.wing_id,
        }
        # ESI leaves optional fields out instead of sending null.
        if member.station_id is not None:
            row["station_id"] = member.station_id
        return row

    def view(self) -> dict:
        """What the three fleet GET routes would answer right now."""
        return {
            "info": {
                "is_free_move": self.is_free_move,
                "is_registered": self.is_registered,
                "is_voice_enabled": self.is_voice_enabled,
                "motd": self.motd,
            },
            "members": [self._member_row(member) for member in self.members.values()],
            "wings": copy.deepcopy(self.wings),
        }


class SimOperation:
    """A prepared call, like django-esi's ``EsiOperation`` returned by ``Fleets.<Name>(**kwargs)``."""

    def __init__(self, sim: FleetSim, name: str, kwargs: dict):
        self.sim = sim
        self.name = name
        self.kwargs = kwargs
        self.token = None

    def result(
        self,
        use_etag=True,
        return_response=False,
        force_refresh=False,
        use_cache=True,
        store_cache=True,
        last_modified=None,
        **extra,
    ):
        return self.sim._run(
            self,
            extra,
            use_etag=use_etag,
            return_response=return_response,
            force_refresh=force_refresh,
            use_cache=use_cache,
            store_cache=store_cache,
        )


class FleetSim:
    def __init__(self, *, response_style: str = "dict", new_wing_squads: int = 1, stale_reads: int = 0):
        if response_style not in ("dict", "model", "object"):
            raise ValueError(f"Unknown response style {response_style!r}")
        self.response_style = response_style
        # Squads EVE gives a wing created through ESI.
        self.new_wing_squads = new_wing_squads
        self.stale_reads = stale_reads
        self.fleets: dict[int, SimFleet] = {}
        self.requests: list[SimRequest] = []
        self.response_cache = {}
        self.etags = {}
        # django-esi caches GET responses; expire_cache() stands in for the cache window passing.
        self.cache_responses = True
        # Answer 304 even to reads that send no ETag.
        self.always_not_modified = False
        self._failures: list[SimFailure] = []
        self._stale = {}
        self._fleet_ids = itertools.count(1_045_000_000_001)
        self._wing_ids = itertools.count(2_073_700_000_001)
        self._squad_ids = itertools.count(3_129_400_000_001)
        self._request_ids = itertools.count(1)

    def __getattr__(self, name):
        if name not in OPERATIONS:
            raise AttributeError(name)
        return lambda **kwargs: SimOperation(self, name, kwargs)

    # Test setup

    def install(self, testcase=None):
        """Serve ``fleetops.providers.esi`` from this simulator until the test ends."""
        from fleetops.providers import esi as esi_provider

        patcher = mock.patch.object(esi_provider, "esi", SimpleNamespace(client=SimpleNamespace(Fleets=self)))
        patcher.start()
        if testcase is not None:
            testcase.addCleanup(patcher.stop)
        return patcher

    def create_fleet(
        self,
        boss_id: int,
        *,
        fleet_id: int | None = None,
        wings=(("Wing 1", ("Squad 1",)),),
        boss_role: str = "fleet_commander",
        **settings,
    ) -> SimFleet:
        """A fleet as EVE forms one: the boss as fleet commander, one wing with one squad.

        ``wings`` lists ``(name, squads)`` pairs, squads as names or a count.
        """
        fleet = SimFleet(self, fleet_id or next(self._fleet_ids), boss_id, **settings)
        if fleet.fleet_id in self.fleets:
            raise ValueError(f"Fleet {fleet.fleet_id} exists already.")
        if self.fleet_of(boss_id) is not None:
            raise SimError(400, "Character is already in a fleet")
        for name, squads in wings:
            fleet.add_wing(name, squads)
        self.fleets[fleet.fleet_id] = fleet
        fleet.add_member(boss_id, boss_role)
        return fleet

    def disband(self, fleet_id) -> None:
        self.fleets.pop(fleet_id, None)
        for key in [key for key in self._stale if key[0] == fleet_id]:
            del self._stale[key]

    def fleet_of(self, character_id) -> SimFleet | None:
        return next((fleet for fleet in self.fleets.values() if character_id in fleet.members), None)

    def accept_invite(self, character_id, **details) -> SimMember:
        for fleet in self.fleets.values():
            if character_id in fleet.invitations:
                return fleet.accept_invite(character_id, **details)
        raise KeyError(character_id)

    def fail(self, name: str, status: int = 500, *, error=None, times=None, exception=None, **match) -> None:
        """Answer calls of ``name`` with an error (or raise ``exception``).

        ``match`` narrows it to calls whose path parameters or body fields have these
        values; ``times`` limits how often it applies (default: until ``clear_failures``).
        """
        if name not in OPERATIONS:
            raise ValueError(f"Unknown operation {name}")
        self._failures.append(SimFailure(name, status, error, exception, times, match))

    def clear_failures(self) -> None:
        self._failures.clear()

    def expire_cache(self) -> None:
        self.response_cache.clear()

    def requests_of(self, name: str) -> list[SimRequest]:
        return [request for request in self.requests if request.name == name]

    def writes(self) -> list[SimRequest]:
        return [request for request in self.requests if request.method != "GET"]

    # Request handling

    def _run(self, operation, extra, *, use_etag, return_response, force_refresh, use_cache, store_cache):
        spec = OPERATIONS[operation.name]
        # django-esi pops token and body from the call's parameters; the token is kept for the next call.
        operation.token = operation.kwargs.pop("token", None) or operation.token
        body = operation.kwargs.pop("body", None)
        if body and not spec.body:
            raise ValueError("Request Body provided on endpoint with no request body parameter.")
        params = {_param_name(key): value for key, value in (operation.kwargs | extra).items()}
        key = (operation.name, tuple(sorted(params.items(), key=lambda item: item[0])))

        if force_refresh:
            self.response_cache.pop(key, None)
            self.etags.pop(key, None)
        known_etag = self.etags.get(key) if use_etag else None
        if use_cache and key in self.response_cache:
            data, etag = self.response_cache[key]
            if known_etag and known_etag == etag:
                raise HTTPNotModified(status_code=304, headers={"ETag": etag})
            return self._answer(operation.name, data, 200, {"ETag": etag}, return_response)

        character_id = self._check_token(spec, operation.token)
        self._check_params(spec, params)
        if spec.body:
            body = self._body_dict(body)
        else:
            body = None
        for name in spec.path:
            params[name] = int(params[name])

        self.requests.append(
            SimRequest(operation.name, spec.method, dict(params), copy.deepcopy(body), character_id, known_etag)
        )
        if self.always_not_modified and spec.method == "GET" and not force_refresh:
            raise HTTPNotModified(status_code=304, headers={})
        headers = {"x-esi-request-id": str(uuid.UUID(int=next(self._request_ids)))}

        fleet = self.fleets.get(params.get("fleet_id")) if spec.method != "GET" else None
        before = fleet.view() if fleet is not None and self.stale_reads > 0 else None
        failure = self._configured_failure(operation.name, params, body)
        if failure is not None and failure.exception is not None:
            raise failure.exception
        try:
            if failure is not None:
                raise SimError(failure.status, failure.error or self._reason(failure.status))
            status, data = getattr(self, f"_handle_{operation.name}")(params, body, character_id)
        except SimError as exc:
            error = exc
        else:
            error = None
        if error is not None:
            self._raise_http_error(error.status, error.message, headers)

        if before is not None:
            # Each route keeps the state it showed before the first of several writes in a row.
            for route in FLEET_READS:
                entry = self._stale.setdefault((fleet.fleet_id, route), {"view": before})
                entry["left"] = self.stale_reads
        if spec.method == "GET":
            etag = f'"{hashlib.md5(repr(data).encode()).hexdigest()}"'
            headers["ETag"] = etag
            if use_etag:
                self.etags[key] = etag
            if known_etag and known_etag == etag:
                raise HTTPNotModified(status_code=304, headers={"ETag": etag})
            if store_cache and self.cache_responses:
                self.response_cache[key] = (copy.deepcopy(data), etag)
        return self._answer(operation.name, data, status, headers, return_response)

    def _check_token(self, spec: OperationSpec, token) -> int:
        if token is None:
            raise ValueError("No security requirement satisfied (accepts {OAuth2})")
        scopes = _token_scopes(token)
        if scopes is not None and spec.scope not in scopes:
            raise ValueError(f"Token Missing Scopes - {[spec.scope]}")
        return int(token.character_id)

    def _check_params(self, spec: OperationSpec, params: dict) -> None:
        accepted = set(spec.path) | set(HEADER_PARAMS)
        unknown = set(params) - accepted
        if unknown:
            raise ValueError(f"Parameter {sorted(unknown)} unknown (accepted {sorted(accepted)})")
        missing = set(spec.path) - set(params)
        if missing:
            raise ValueError(f"Required Parameter {sorted(missing)} missing (provided {sorted(params)})")
        for name in spec.path:
            if not _is_id(params[name]) and not str(params[name]).isdigit():
                raise ValueError(f"Parameter {name} must be an integer, not {params[name]!r}")

    def _body_dict(self, body) -> dict:
        if body is None:
            raise ValueError("Request Body is required but none was provided.")
        if isinstance(body, BaseModel):
            return body.model_dump(mode="json")
        if isinstance(body, dict):
            return copy.deepcopy(body)
        raise TypeError(body)

    def _configured_failure(self, name: str, params: dict, body: dict | None) -> SimFailure | None:
        values = params | (body or {})
        for failure in self._failures:
            if failure.name != name or any(values.get(key) != value for key, value in failure.match.items()):
                continue
            if failure.times is not None:
                failure.times -= 1
                if failure.times <= 0:
                    self._failures.remove(failure)
            return failure
        return None

    @staticmethod
    def _reason(status: int) -> str:
        try:
            return HTTPStatus(status).phrase
        except ValueError:
            return "Error"

    def _error_data(self, message: str):
        if self.response_style == "model":
            return Error(error=message)
        if self.response_style == "object":
            return SimpleNamespace(error=message)
        return {"error": message}

    def _raise_http_error(self, status: int, message: str, headers: dict):
        data = self._error_data(message)
        if status == 420:
            # django-esi turns ESI's error-limit answer into its own exception while handling it.
            try:
                raise HTTPClientError(status_code=status, headers=headers, data=data)
            except HTTPClientError:
                raise ESIErrorLimitException(reset=60)
        error_class = HTTPServerError if status >= 500 else HTTPClientError
        raise error_class(status_code=status, headers=headers, data=data)

    def _answer(self, name: str, data, status: int, headers: dict, return_response: bool):
        rendered = self._render(name, data)
        return (rendered, SimResponse(status, dict(headers))) if return_response else rendered

    def _render(self, name: str, data):
        if data is None or self.response_style == "dict":
            return copy.deepcopy(data)
        model = RESPONSE_MODELS[name]
        if isinstance(data, list):
            items = [model.model_validate(item) for item in data]
            return items if self.response_style == "model" else [self._namespace(item.model_dump()) for item in items]
        item = model.model_validate(data)
        return item if self.response_style == "model" else self._namespace(item.model_dump())

    def _namespace(self, value):
        if isinstance(value, dict):
            return SimpleNamespace(**{key: self._namespace(item) for key, item in value.items()})
        if isinstance(value, list):
            return [self._namespace(item) for item in value]
        return value

    def _boss_fleet(self, params: dict, caller: int) -> SimFleet:
        # Only the fleet boss can see a fleet; everyone else gets the same 404 as for a missing one.
        fleet = self.fleets.get(params["fleet_id"])
        if fleet is None or fleet.boss_id != caller:
            raise SimError(404, FLEET_NOT_FOUND)
        return fleet

    def _read_view(self, fleet: SimFleet, name: str) -> dict:
        entry = self._stale.get((fleet.fleet_id, name))
        if entry is None:
            return fleet.view()
        entry["left"] -= 1
        if entry["left"] <= 0:
            del self._stale[(fleet.fleet_id, name)]
        return entry["view"]

    # One handler per operation: (params, body, caller) -> (status, data)

    def _handle_GetCharactersCharacterIdFleet(self, params, body, caller):
        if params["character_id"] != caller:
            raise SimError(403, "Token is not valid for this character")
        fleet = self.fleet_of(caller)
        if fleet is None:
            raise SimError(404, "Character is not in a fleet")
        member = fleet.members[caller]
        return 200, {
            "fleet_boss_id": fleet.boss_id,
            "fleet_id": fleet.fleet_id,
            "role": member.role,
            "squad_id": member.squad_id,
            "wing_id": member.wing_id,
        }

    def _handle_GetFleetsFleetId(self, params, body, caller):
        fleet = self._boss_fleet(params, caller)
        return 200, self._read_view(fleet, "GetFleetsFleetId")["info"]

    def _handle_PutFleetsFleetId(self, params, body, caller):
        fleet = self._boss_fleet(params, caller)
        _check_keys(body, {"motd", "is_free_move"}, set())
        if "motd" in body and not isinstance(body["motd"], str):
            raise SimError(400, "motd must be a string")
        if "is_free_move" in body and not isinstance(body["is_free_move"], bool):
            raise SimError(400, "is_free_move must be a boolean")
        fleet.motd = body.get("motd", fleet.motd)
        fleet.is_free_move = body.get("is_free_move", fleet.is_free_move)
        return 204, None

    def _handle_GetFleetsFleetIdMembers(self, params, body, caller):
        fleet = self._boss_fleet(params, caller)
        return 200, self._read_view(fleet, "GetFleetsFleetIdMembers")["members"]

    def _handle_PostFleetsFleetIdMembers(self, params, body, caller):
        fleet = self._boss_fleet(params, caller)
        _check_keys(body, {"character_id", "role", "wing_id", "squad_id"}, {"character_id", "role"})
        invitee = body["character_id"]
        if not _is_id(invitee):
            raise SimError(400, "character_id must be an integer")
        role, wing_id, squad_id = body["role"], body.get("wing_id"), body.get("squad_id")
        _check_combination(role, wing_id, squad_id, invite=True)
        if self.fleet_of(invitee) is not None:
            raise SimError(400, "Character is already in a fleet")
        if len(fleet.members) >= MAX_MEMBERS:
            raise SimError(400, "The fleet is full")
        if role != "squad_member" or wing_id is not None:
            fleet._position(role, wing_id, squad_id, invitee)
        fleet.invitations[invitee] = {"role": role, "wing_id": wing_id, "squad_id": squad_id}
        return 204, None

    def _handle_PutFleetsFleetIdMembersMemberId(self, params, body, caller):
        fleet = self._boss_fleet(params, caller)
        fleet.member(params["member_id"])
        _check_keys(body, {"role", "wing_id", "squad_id"}, {"role"})
        role, wing_id, squad_id = body["role"], body.get("wing_id"), body.get("squad_id")
        _check_combination(role, wing_id, squad_id)
        fleet.move_member(params["member_id"], role, wing_id, squad_id)
        return 204, None

    def _handle_DeleteFleetsFleetIdMembersMemberId(self, params, body, caller):
        fleet = self._boss_fleet(params, caller)
        fleet.member(params["member_id"])
        if params["member_id"] == fleet.boss_id:
            raise SimError(400, "The fleet boss cannot be kicked")
        del fleet.members[params["member_id"]]
        return 204, None

    def _handle_GetFleetsFleetIdWings(self, params, body, caller):
        fleet = self._boss_fleet(params, caller)
        return 200, self._read_view(fleet, "GetFleetsFleetIdWings")["wings"]

    def _handle_PostFleetsFleetIdWings(self, params, body, caller):
        fleet = self._boss_fleet(params, caller)
        return 201, {"wing_id": fleet.add_wing(squads=self.new_wing_squads)}

    def _handle_PutFleetsFleetIdWingsWingId(self, params, body, caller):
        fleet = self._boss_fleet(params, caller)
        wing = fleet.wing(params["wing_id"])
        wing["name"] = _body_name(body)
        return 204, None

    def _handle_DeleteFleetsFleetIdWingsWingId(self, params, body, caller):
        self._boss_fleet(params, caller).delete_wing(params["wing_id"])
        return 204, None

    def _handle_PostFleetsFleetIdWingsWingIdSquads(self, params, body, caller):
        fleet = self._boss_fleet(params, caller)
        return 201, {"squad_id": fleet.add_squad(params["wing_id"])}

    def _handle_PutFleetsFleetIdSquadsSquadId(self, params, body, caller):
        fleet = self._boss_fleet(params, caller)
        _wing, squad = fleet.squad(params["squad_id"])
        squad["name"] = _body_name(body)
        return 204, None

    def _handle_DeleteFleetsFleetIdSquadsSquadId(self, params, body, caller):
        self._boss_fleet(params, caller).delete_squad(params["squad_id"])
        return 204, None
