"""Shared helpers for building FleetOps test data.

Test modules should build their fixtures through these helpers so that users,
characters, ownerships, tokens and operations are created consistently.
"""

from __future__ import annotations

import itertools
from datetime import timedelta
from decimal import Decimal

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from allianceauth.tests.auth_utils import AuthUtils
from django.utils import timezone
from esi.models import Scope, Token

from fleetops.constants import FLEET_READ_SCOPE, FLEET_WRITE_SCOPE
from fleetops.models import AttendanceRecord, FleetOperation, FleetOpsSettings, FleetType

_ids = itertools.count(90_000_001)

MEMBER_PERMS = ["fleetops.basic_access"]
CORP_MANAGEMENT_PERMS = MEMBER_PERMS + ["fleetops.view_corp_stats"]
FC_PERMS = MEMBER_PERMS + [
    "fleetops.start_fleet",
    "fleetops.manage_own_fleet",
    "fleetops.view_all_fleets",
    "fleetops.create_manual_fleet",
    "fleetops.manage_attendance",
]
FC_LEAD_PERMS = [
    "fleetops.basic_access",
    "fleetops.start_fleet",
    "fleetops.manage_own_fleet",
    "fleetops.view_all_fleets",
    "fleetops.create_manual_fleet",
    "fleetops.manage_fleets",
    "fleetops.view_corp_stats",
    "fleetops.view_all_stats",
    "fleetops.manage_attendance",
    "fleetops.manage_incentives",
    "fleetops.manage_configuration",
    "fleetops.view_audit_log",
]

DEFAULT_CORP = (2_000_001, "Test Corp One")
OTHER_CORP = (2_000_002, "Test Corp Two")
DEFAULT_ALLIANCE = (3_000_001, "Test Alliance")
FOREIGN_ALLIANCE = (3_000_999, "Foreign Alliance")


def next_id() -> int:
    return next(_ids)


def create_character(
    name: str | None = None,
    *,
    character_id: int | None = None,
    corporation: tuple[int, str] = DEFAULT_CORP,
    alliance: tuple[int, str] | None = DEFAULT_ALLIANCE,
) -> EveCharacter:
    character_id = character_id or next_id()
    return EveCharacter.objects.create(
        character_id=character_id,
        character_name=name or f"Pilot {character_id}",
        corporation_id=corporation[0],
        corporation_name=corporation[1],
        corporation_ticker=corporation[1][:5].upper(),
        alliance_id=alliance[0] if alliance else None,
        alliance_name=alliance[1] if alliance else "",
    )


def add_ownership(user, character: EveCharacter) -> CharacterOwnership:
    return CharacterOwnership.objects.create(
        user=user,
        character=character,
        owner_hash=f"hash-{character.character_id}",
    )


def create_user(
    username: str | None = None,
    *,
    perms: list[str] | None = None,
    main_name: str | None = None,
    corporation: tuple[int, str] = DEFAULT_CORP,
    alliance: tuple[int, str] | None = DEFAULT_ALLIANCE,
    with_main: bool = True,
):
    """Create an Auth user with an owned main character and the given permissions."""
    username = username or f"user{next_id()}"
    user = AuthUtils.create_user(username)
    if with_main:
        main = create_character(main_name or f"{username} Main", corporation=corporation, alliance=alliance)
        user.profile.main_character = main
        user.profile.save()
        add_ownership(user, main)
    if perms:
        user = AuthUtils.add_permissions_to_user_by_name(perms, user)
    return user


def main_of(user) -> EveCharacter:
    return user.profile.main_character


def add_alt(user, name: str | None = None, **kwargs) -> EveCharacter:
    character = create_character(name, **kwargs)
    add_ownership(user, character)
    return character


def add_token(user, character: EveCharacter, scopes: list[str] | None = None) -> Token:
    """Attach a refreshable ESI token with the given scopes (fleet read+write by default)."""
    scopes = scopes if scopes is not None else [FLEET_READ_SCOPE, FLEET_WRITE_SCOPE]
    token = Token.objects.create(
        user=user,
        character_id=character.character_id,
        character_name=character.character_name,
        character_owner_hash=f"hash-{character.character_id}",
        access_token=f"access-{character.character_id}",
        refresh_token=f"refresh-{character.character_id}",
        token_type="character",
    )
    for name in scopes:
        scope, _ = Scope.objects.get_or_create(name=name, defaults={"help_text": name})
        token.scopes.add(scope)
    return token


def fleet_type(name: str = "StratOp", weight: str = "1.00") -> FleetType:
    obj, _ = FleetType.objects.get_or_create(name=name, defaults={"point_weight": Decimal(weight)})
    return obj


def settings(**values) -> FleetOpsSettings:
    obj = FleetOpsSettings.get_solo()
    for key, value in values.items():
        setattr(obj, key, value)
    obj.save()
    return obj


def create_operation(
    fc_user,
    *,
    fc_character: EveCharacter | None = None,
    status: str = FleetOperation.Status.ACTIVE,
    type_obj: FleetType | None = None,
    started_at=None,
    ended_at=None,
    esi_fleet_id: int | None = None,
    **extra,
) -> FleetOperation:
    fc_character = fc_character or main_of(fc_user)
    type_obj = type_obj or fleet_type()
    started_at = started_at or (timezone.now() - timedelta(minutes=30))
    if ended_at is None and status == FleetOperation.Status.CLOSED:
        ended_at = started_at + timedelta(hours=1)
    defaults = dict(
        created_by=fc_user,
        fc_user=fc_user,
        fc_character_id=fc_character.character_id,
        fc_character_name=fc_character.character_name,
        fc_main_character_id=main_of(fc_user).character_id if main_of(fc_user) else None,
        fc_main_character_name=main_of(fc_user).character_name if main_of(fc_user) else "",
        fleet_boss_character_id=fc_character.character_id,
        esi_fleet_id=esi_fleet_id if esi_fleet_id is not None else next_id(),
        fleet_type=type_obj,
        fleet_point_weight_snapshot=type_obj.point_weight,
        formup="Jita",
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        tracking_enabled=status == FleetOperation.Status.ACTIVE,
    )
    defaults.update(extra)
    return FleetOperation.objects.create(**defaults)


def add_attendance(
    operation: FleetOperation,
    user,
    character: EveCharacter | None = None,
    *,
    value: int = 1,
    source: str = AttendanceRecord.Source.AUTOMATIC,
    granted: bool = True,
    capped: bool = False,
) -> AttendanceRecord:
    character = character or main_of(user)
    main = main_of(user)
    return AttendanceRecord.objects.create(
        operation=operation,
        character_id=character.character_id,
        character_name=character.character_name,
        main_character_id=main.character_id if main else None,
        main_character_name=main.character_name if main else "",
        auth_user=user,
        corporation_id=main.corporation_id if main else None,
        corporation_name=main.corporation_name if main else "",
        source=source,
        attendance_value=value,
        granted=granted,
        capped=capped,
        first_seen=operation.started_at,
        last_seen=operation.started_at,
    )


def esi_member(character: EveCharacter, **overrides) -> dict:
    """A fleet member row shaped like the ESI GET /fleets/{id}/members/ response."""
    row = {
        "character_id": character.character_id,
        "join_time": timezone.now() - timedelta(minutes=5),
        "role": "squad_member",
        "role_name": "Squad Member",
        "ship_type_id": 11987,
        "solar_system_id": 30000142,
        "squad_id": 1,
        "station_id": None,
        "takes_fleet_warp": True,
        "wing_id": 1,
    }
    row.update(overrides)
    return row
