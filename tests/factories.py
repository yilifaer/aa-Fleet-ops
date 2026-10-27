# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Shared helpers for building FleetOps test data.

Test modules should build their fixtures through these helpers so that users,
characters, ownerships, tokens and operations are created consistently.
"""

from __future__ import annotations

import itertools
import secrets
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from allianceauth.tests.auth_utils import AuthUtils
from django.contrib.auth.models import Group
from django.utils import timezone
from esi.models import Scope, Token

from fleetops.constants import DEFAULT_MOTD_TEMPLATE, DEFAULT_PING_TEMPLATE, FLEET_READ_SCOPE, FLEET_WRITE_SCOPE
from fleetops.models import (
    AttendanceRecord,
    DiscordWebhook,
    FleetMemberState,
    FleetOperation,
    FleetOpsSettings,
    FleetStructureTemplate,
    FleetType,
    GroupAccess,
    MessageTemplate,
    MultiplierTier,
    PapLink,
    PapRegistration,
    PingRole,
    ShipFunction,
    ShipFunctionRule,
    SpecialRole,
    SrpClaim,
    SrpClaimEvent,
    SrpFleetLink,
    SrpKillmail,
    SrpPayoutRule,
    SrpSettings,
    StandalonePing,
    StatisticsTarget,
)
from fleetops.services.fleet_rules import normalize_wings
from fleetops.srp.types import Calculation, PolicySnapshot

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
    "fleetops.request_srp",
    "fleetops.manage_srp",
]
# Members get request_srp from the upgrade; tests ask for it explicitly so pages do not pay for the SRP menu.
SRP_MEMBER_PERMS = MEMBER_PERMS + ["fleetops.request_srp"]
SRP_MANAGER_PERMS = SRP_MEMBER_PERMS + ["fleetops.manage_srp"]

DEFAULT_CORP = (2_000_001, "Test Corp One")
OTHER_CORP = (2_000_002, "Test Corp Two")
# Characters in this corporation were biomassed.
DOOMHEIM_CORP = (1_000_001, "Doomheim")
THIRD_CORP = (2_000_003, "Test Corp Three")
DEFAULT_ALLIANCE = (3_000_001, "Test Alliance")
FOREIGN_ALLIANCE = (3_000_999, "Foreign Alliance")


# Test users registered long before any month the tests look at, so every month counts them.
DEFAULT_JOINED = datetime(2020, 1, 1, tzinfo=dt_timezone.utc)


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
    user.date_joined = DEFAULT_JOINED
    user.save(update_fields=["date_joined"])
    if with_main:
        main = create_character(main_name or f"{username} Main", corporation=corporation, alliance=alliance)
        user.profile.main_character = main
        user.profile.save()
        add_ownership(user, main)
    if perms:
        user = AuthUtils.add_permissions_to_user_by_name(perms, user)
    return user


def member(
    username: str | None = None,
    *,
    corporation: tuple[int, str] = DEFAULT_CORP,
    alliance: tuple[int, str] | None = DEFAULT_ALLIANCE,
    active: bool = True,
    perms: list[str] | None = None,
    main_name: str | None = None,
    with_main: bool = True,
    joined=None,
):
    """A user as the statistics see members: main character in a corporation and alliance, account active or not.

    ``corporation=DOOMHEIM_CORP`` makes a biomassed main, ``with_main=False`` a user without any main.
    """
    user = create_user(
        username, perms=perms, main_name=main_name, corporation=corporation, alliance=alliance, with_main=with_main
    )
    changed = []
    if not active:
        user.is_active = False
        changed.append("is_active")
    if joined is not None:
        user.date_joined = joined
        changed.append("date_joined")
    if changed:
        user.save(update_fields=changed)
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


def builtin_default_templates():
    """Make the stored "Default Ping" and "Default MOTD" templates the built-in ones from constants.py."""
    MessageTemplate.objects.filter(template_type=MessageTemplate.TemplateType.PING, name="Default Ping").update(
        content=DEFAULT_PING_TEMPLATE
    )
    MessageTemplate.objects.filter(template_type=MessageTemplate.TemplateType.MOTD, name="Default MOTD").update(
        content=DEFAULT_MOTD_TEMPLATE
    )


def ping_role(name: str = "Sig A", *, role_id: int | None = None, **extra) -> PingRole:
    return PingRole.objects.create(name=name, role_id=role_id or next_id(), **extra)


def ping_channel(
    name: str = "STR",
    *,
    allow_everyone: bool = False,
    allow_here: bool = False,
    url: str | None = None,
    roles: list[PingRole] | None = None,
    **extra,
) -> DiscordWebhook:
    """A ping channel (Discord webhook); ``roles`` restricts the roles it allows."""
    webhook_id = next_id()
    channel = DiscordWebhook.objects.create(
        name=name,
        webhook_url=url or f"https://discord.com/api/webhooks/{webhook_id}/factory-token-{webhook_id}",
        allow_everyone=allow_everyone,
        allow_here=allow_here,
        **extra,
    )
    if roles:
        channel.allowed_roles.set(roles)
    return channel


def create_operation(
    fc_user,
    *,
    fc_character: EveCharacter | None = None,
    status: str = FleetOperation.Status.ACTIVE,
    type_obj: FleetType | None = None,
    started_at=None,
    ended_at=None,
    esi_fleet_id: int | None = None,
    ping_channel: DiscordWebhook | None = None,
    ping_mention_mode: str = FleetOperation.MentionMode.NONE,
    ping_roles: list[dict] | None = None,
    doctrine_fits: list[dict] | None = None,
    **extra,
) -> FleetOperation:
    fc_character = fc_character or main_of(fc_user)
    type_obj = type_obj or fleet_type()
    scheduled = status == FleetOperation.Status.SCHEDULED
    # A scheduled fleet has not started and has no ESI fleet yet.
    if not scheduled:
        started_at = started_at or (timezone.now() - timedelta(minutes=30))
    if ended_at is None and status == FleetOperation.Status.CLOSED:
        ended_at = started_at + timedelta(hours=1)
    if esi_fleet_id is None and not scheduled:
        esi_fleet_id = next_id()
    defaults = dict(
        created_by=fc_user,
        fc_user=fc_user,
        fc_character_id=fc_character.character_id,
        fc_character_name=fc_character.character_name,
        fc_main_character_id=main_of(fc_user).character_id if main_of(fc_user) else None,
        fc_main_character_name=main_of(fc_user).character_name if main_of(fc_user) else "",
        fleet_boss_character_id=fc_character.character_id,
        esi_fleet_id=esi_fleet_id,
        fleet_type=type_obj,
        fleet_point_weight_snapshot=type_obj.point_weight,
        formup="Jita",
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        tracking_enabled=status == FleetOperation.Status.ACTIVE,
        ping_channel=ping_channel,
        ping_channel_name=ping_channel.name if ping_channel else "",
        ping_mention_mode=ping_mention_mode,
        ping_roles=ping_roles or [],
        doctrine_fits=doctrine_fits or [],
    )
    defaults.update(extra)
    return FleetOperation.objects.create(**defaults)


def closed_operation(fc_user, started_at=None, **extra) -> FleetOperation:
    """A closed fleet that counts in the statistics; it ends an hour after it started unless ``ended_at`` is given."""
    return create_operation(fc_user, status=FleetOperation.Status.CLOSED, started_at=started_at, **extra)


def create_scheduled_operation(fc_user, scheduled_at=None, **extra) -> FleetOperation:
    """A pre-pinged fleet waiting for its FC to start it (``scheduled_at`` defaults to an hour ahead)."""
    return create_operation(
        fc_user,
        status=FleetOperation.Status.SCHEDULED,
        scheduled_at=scheduled_at or (timezone.now() + timedelta(hours=1)),
        **extra,
    )


def add_attendance(
    operation: FleetOperation,
    user,
    character: EveCharacter | None = None,
    *,
    value: int = 1,
    source: str = AttendanceRecord.Source.AUTOMATIC,
    granted: bool = True,
    capped: bool = False,
    **extra,
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
        **extra,
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


def multiplier_tiers(*tiers: tuple[int, int]) -> list[MultiplierTier]:
    """Replace the multiplier tiers with ``(threshold minutes, multiplier)`` pairs (90 -> 2x, 180 -> 3x by default)."""
    MultiplierTier.objects.all().delete()
    return [
        MultiplierTier.objects.create(threshold_minutes=minutes, multiplier=multiplier)
        for minutes, multiplier in (tiers or ((90, 2), (180, 3)))
    ]


def special_role(name: str | None = None, *, key: str = "", grants_fc_credit: bool = False, **extra) -> SpecialRole:
    """A special role. A ``key`` of an existing role, such as the seeded ``backseat_fc``, updates and returns it."""
    role = SpecialRole.objects.filter(key=key).first() if key else None
    if role is None:
        return SpecialRole.objects.create(
            key=key, name=name or f"Role {next_id()}", grants_fc_credit=grants_fc_credit, **extra
        )
    role.grants_fc_credit = grants_fc_credit
    if name:
        role.name = name
    for field, value in extra.items():
        setattr(role, field, value)
    role.save()
    return role


def statistics_target(
    year: int, month: int, average: str | Decimal | None = None, *, minimum: int = 1, note: str = ""
) -> StatisticsTarget:
    """A monthly target that applies from ``year``-``month``; ``average`` None = no corporation target."""
    return StatisticsTarget.objects.create(
        year=year,
        month=month,
        corporation_average_target=None if average is None else Decimal(str(average)),
        member_minimum_pap=minimum,
        note=note,
    )


def owner_of(character_id: int):
    ownership = (
        CharacterOwnership.objects.filter(character__character_id=character_id).select_related("user").first()
    )
    return ownership.user if ownership else None


def _character(character: EveCharacter | int, name: str | None):
    """``(character_id, name, EveCharacter or None)``; an int is a character Alliance Auth does not know."""
    if isinstance(character, EveCharacter):
        return character.character_id, name or character.character_name, character
    return int(character), name or f"Pilot {character}", None


def member_state(
    operation: FleetOperation,
    character: EveCharacter | int,
    *,
    accumulated_seconds: int = 0,
    active: bool = True,
    user=None,
    name: str | None = None,
    role: str | None = None,
    **extra,
) -> FleetMemberState:
    """A tracked fleet member with the character's own corporation, its owner and the owner's main.

    ``character`` may be a bare character ID for a pilot without Alliance Auth account. ``role`` is the
    fleet position (``fleet_role``); ``wing_id``, ``squad_id``, ``ship_type_id``, ``solar_system_id``,
    ``station_id`` and the other fields pass through.
    """
    if role is not None:
        extra["fleet_role"] = role
    character_id, character_name, eve_character = _character(character, name)
    user = user or owner_of(character_id)
    main = main_of(user) if user else None
    first_seen = operation.started_at or timezone.now()
    last_seen = timezone.now() if active else (operation.ended_at or timezone.now())
    values = dict(
        character_name=character_name,
        main_character_id=main.character_id if main else None,
        main_character_name=main.character_name if main else "",
        auth_user=user,
        corporation_id=eve_character.corporation_id if eve_character else None,
        corporation_name=eve_character.corporation_name if eve_character else "",
        alliance_id=eve_character.alliance_id if eve_character else None,
        alliance_name=eve_character.alliance_name if eve_character else "",
        first_seen=first_seen,
        last_seen=last_seen,
        left_at=None if active else last_seen,
        is_active=active,
        accumulated_seconds=accumulated_seconds,
    )
    values.update(extra)
    return FleetMemberState.objects.create(operation=operation, character_id=character_id, **values)


def pap_link(
    operation: FleetOperation,
    *,
    created_by=None,
    minutes: int = 30,
    require_online: bool = True,
    **extra,
) -> PapLink:
    values = dict(
        code=secrets.token_urlsafe(24),
        created_by=created_by or operation.fc_user,
        expires_at=timezone.now() + timedelta(minutes=minutes),
        require_online=require_online,
    )
    values.update(extra)
    return PapLink.objects.create(operation=operation, **values)


def registration(
    operation: FleetOperation,
    character: EveCharacter | int,
    *,
    link: PapLink | None = None,
    user=None,
    name: str | None = None,
    **extra,
) -> PapRegistration:
    """A character registered for PAP through a PAP link of the fleet."""
    character_id, character_name, _eve_character = _character(character, name)
    values = dict(
        link=link,
        character_name=character_name,
        auth_user=user or owner_of(character_id),
        online=True,
    )
    values.update(extra)
    return PapRegistration.objects.create(operation=operation, character_id=character_id, **values)


_unit_ids = itertools.count(4_100_000_001)


def wings(*spec) -> list[dict]:
    """A normalised wings list, as ``FleetOperation.fleet_wings`` stores it.

    Each wing is ``(name, squads)`` or ``(wing_id, name, squads)``; squads are names or
    ``(squad_id, name)`` pairs. Missing ids are allocated. ``wings(("Main", ("Alpha", "Bravo")), ("Logi", ("Logi",)))``
    """
    result = []
    for wing in spec:
        if isinstance(wing, dict):
            result.append(wing)
            continue
        wing_id, name, squads = wing if len(wing) == 3 else (next(_unit_ids), *wing)
        result.append(
            {
                "id": wing_id,
                "name": name,
                "squads": [
                    {"id": squad[0], "name": squad[1]}
                    if isinstance(squad, (tuple, list))
                    else {"id": next(_unit_ids), "name": squad}
                    for squad in squads
                ],
            }
        )
    return normalize_wings(result)


def clear_ship_functions() -> None:
    """Remove the seeded ship functions and their rules."""
    ShipFunction.objects.all().delete()


def ship_function(
    name: str,
    *,
    rules=(("group", 832),),
    fleet_roles: str = "",
    is_default: bool = False,
    color: str = "#0d6efd",
    **extra,
) -> ShipFunction:
    """A ship function with ``(kind, target_id)`` or ``(kind, target_id, label)`` rules.

    An existing function of that name (such as a seeded one) is updated; rules for the same ship type
    or group move to this function.
    """
    function, _created = ShipFunction.objects.update_or_create(
        name=name, defaults={"fleet_roles": fleet_roles, "is_default": is_default, "color": color, **extra}
    )
    for rule in rules:
        kind, target_id, label = (*rule, "")[:3]
        ShipFunctionRule.objects.update_or_create(
            kind=kind, target_id=target_id, defaults={"function": function, "label": label}
        )
    return function


def structure_template(name: str = "Standard", layout: str = "Main: Alpha, Bravo", **extra) -> FleetStructureTemplate:
    template, _created = FleetStructureTemplate.objects.update_or_create(
        name=name, defaults={"layout": layout, **extra}
    )
    return template


# --- FleetOps SRP ------------------------------------------------------------------------------------

RIFTER = 587
CAPSULE = 670
SRP_SYSTEM = (30_002_187, "Amamake")
_killmail_ids = itertools.count(131_000_001)


def srp_settings(**values) -> SrpSettings:
    """The SRP settings, enabled unless ``enabled=False`` is passed; other fields are set as given."""
    obj = SrpSettings.get_solo()
    values.setdefault("enabled", True)
    for key, value in values.items():
        setattr(obj, key, value)
    obj.save()
    return obj


def payout_rule(
    ship_type_id: int = 0,
    amount: int = 50_000_000,
    *,
    doctrine=None,
    scope: str = SrpPayoutRule.Scope.ANY,
    ship_name: str | None = None,
    **extra,
) -> SrpPayoutRule:
    """A payout rule. ``doctrine`` is a name, or ``(source, external_id, name)`` for a doctrine of a doctrine app."""
    values = dict(
        ship_type_id=ship_type_id,
        ship_type_name=ship_name if ship_name is not None else (f"Ship {ship_type_id}" if ship_type_id else ""),
        scope=scope,
        amount=amount,
    )
    if isinstance(doctrine, str):
        values["doctrine_name"] = doctrine
    elif doctrine:
        values["doctrine_source"], values["doctrine_external_id"], values["doctrine_name"] = doctrine
    values.update(extra)
    return SrpPayoutRule.objects.create(**values)


def _srp_time(value=None):
    return value or (timezone.now() - timedelta(hours=1))


def esi_killmail_payload(
    killmail_id: int | None = None,
    *,
    victim: EveCharacter | int | None = None,
    ship_type_id: int = RIFTER,
    time=None,
    items=None,
    solar_system_id: int = SRP_SYSTEM[0],
    **overrides,
) -> dict:
    """A killmail shaped like ESI's GetKillmailsKillmailIdKillmailHash answer, with nested items allowed.

    ``victim`` is the victim character (or its ID); without one the victim is a structure-like loss.
    ``items`` are ESI item dicts (``flag``, ``item_type_id``, ``quantity_destroyed``, ``singleton``, ``items``).
    """
    killmail_id = killmail_id or next(_killmail_ids)
    when = _srp_time(time).astimezone(dt_timezone.utc)
    victim_row = {
        "ship_type_id": ship_type_id,
        "damage_taken": 2_345,
        "position": {"x": 1.0, "y": 2.0, "z": 3.0},
        "items": list(items if items is not None else [{"flag": 27, "item_type_id": 2_873, "quantity_destroyed": 1}]),
    }
    if isinstance(victim, EveCharacter):
        victim_row.update(
            character_id=victim.character_id,
            corporation_id=victim.corporation_id,
            alliance_id=victim.alliance_id,
        )
    elif victim:
        victim_row["character_id"] = int(victim)
    payload = {
        "killmail_id": killmail_id,
        "killmail_time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "solar_system_id": solar_system_id,
        "moon_id": None,
        "war_id": None,
        "attackers": [
            {"character_id": 95_000_001, "corporation_id": 98_000_001, "ship_type_id": 17_738, "final_blow": True},
            {"character_id": None, "corporation_id": 1_000_125, "ship_type_id": None, "final_blow": False},
        ],
        "victim": victim_row,
    }
    payload.update(overrides)
    return payload


def zkill_payload(
    killmail_id: int,
    *,
    killmail_hash: str | None = None,
    fitted=10_234_567.89,
    total=12_500_000.25,
    victim: EveCharacter | int | None = None,
    **zkb,
) -> list[dict]:
    """The answer of zKillboard's ``/api/killID/<id>/``: a list with the ESI killmail and its ``zkb`` block.

    ``fitted`` or ``total`` None leaves that value out; ``zkb`` fields override the block.
    """
    block = {
        "locationID": 40_139_385,
        "hash": killmail_hash or secrets.token_hex(20),
        "droppedValue": 120_000.5,
        "destroyedValue": 9_800_000,
        "points": 1,
        "npc": False,
        "solo": True,
        "awox": False,
        "labels": ["cat:6", "solo", "pvp", "loc:lowsec"],
    }
    if fitted is not None:
        block["fittedValue"] = fitted
    if total is not None:
        block["totalValue"] = total
    block.update(zkb)
    return [dict(esi_killmail_payload(killmail_id, victim=victim), zkb=block)]


def srp_killmail(
    character: EveCharacter | int | None = None,
    *,
    killmail_id: int | None = None,
    ship_type_id: int = RIFTER,
    ship_name: str | None = None,
    time=None,
    fitted=None,
    total=None,
    items=None,
    zkill_status: str | None = None,
    **extra,
) -> SrpKillmail:
    """A stored killmail of ``character`` (an EveCharacter or an ID; None = no pilot).

    ``fitted`` and ``total`` are the zKillboard values; with either the zKillboard state is ``ok``,
    otherwise ``pending`` unless ``zkill_status`` says otherwise.
    """
    killmail_id = killmail_id or next(_killmail_ids)
    has_value = fitted is not None or total is not None
    values = dict(
        killmail_id=killmail_id,
        killmail_hash=secrets.token_hex(20),
        killmail_time=_srp_time(time),
        solar_system_id=SRP_SYSTEM[0],
        solar_system_name=SRP_SYSTEM[1],
        ship_type_id=ship_type_id,
        ship_type_name=ship_name if ship_name is not None else f"Ship {ship_type_id}",
        attacker_count=2,
        items=list(items if items is not None else [{"flag": 27, "item_type_id": 2_873, "quantity_destroyed": 1}]),
        zkill_status=zkill_status or (SrpKillmail.ZkillStatus.OK if has_value else SrpKillmail.ZkillStatus.PENDING),
        zkill_fitted_value=None if fitted is None else Decimal(str(fitted)),
        zkill_total_value=None if total is None else Decimal(str(total)),
    )
    if isinstance(character, EveCharacter):
        values.update(
            victim_character_id=character.character_id,
            victim_corporation_id=character.corporation_id,
            victim_alliance_id=character.alliance_id,
        )
    elif character:
        values["victim_character_id"] = int(character)
    values.update(extra)
    return SrpKillmail.objects.create(**values)


def fleet_link(operation: FleetOperation, *, code: str | None = None, **extra) -> SrpFleetLink:
    """The SRP link of a fleet, led by the fleet's FC."""
    values = dict(
        code=code or secrets.token_urlsafe(12),
        fc_user=operation.fc_user,
        fc_main_character_id=operation.fc_main_character_id,
        fc_main_character_name=operation.fc_main_character_name,
        created_by=operation.fc_user,
    )
    values.update(extra)
    return SrpFleetLink.objects.create(operation=operation, **values)


def srp_claim(
    user,
    *,
    killmail: SrpKillmail | None = None,
    character: EveCharacter | None = None,
    scope: str = SrpClaim.Scope.OUT_OF_FLEET,
    status: str = SrpClaim.Status.PENDING,
    amount: int = 10_000_000,
    approved_amount: int | None = None,
    link: SrpFleetLink | None = None,
    decided_by=None,
    time=None,
    **extra,
) -> SrpClaim:
    """A claim of ``user`` for a loss of ``character`` (the user's main by default).

    ``amount`` is the base, suggested and limited amount. Approved and paid claims get ``approved_amount``
    (``amount`` by default) and the decision and payment fields; ``link`` makes it an in-fleet claim of that
    fleet. Any claim field can be passed to override the defaults.
    """
    character = character or main_of(user)
    killmail = killmail or srp_killmail(character, time=time)
    main = main_of(user)
    now = timezone.now()
    values = dict(
        killmail=killmail,
        scope=scope,
        user=user,
        character_id=character.character_id,
        character_name=character.character_name,
        main_character_id=main.character_id if main else None,
        main_character_name=main.character_name if main else "",
        corporation_id=main.corporation_id if main else None,
        corporation_name=main.corporation_name if main else "",
        corporation_ticker=main.corporation_ticker if main else "",
        alliance_id=main.alliance_id if main else None,
        alliance_name=main.alliance_name if main else "",
        killmail_time=killmail.killmail_time,
        ship_type_id=killmail.ship_type_id,
        ship_type_name=killmail.ship_type_name,
        solar_system_name=killmail.solar_system_name,
        loss_value=killmail.zkill_fitted_value,
        value_source="zkill_fitted" if killmail.zkill_fitted_value is not None else "",
        payout_basis=SrpClaim.PayoutBasis.NONE,
        policy=PolicySnapshot().as_json(),
        calculation=Calculation(base=amount, suggested=amount, limited=amount).as_json(),
        base_amount=amount,
        suggested_amount=amount,
        limited_amount=amount,
        status=status,
    )
    if link is not None:
        operation = link.operation
        values.update(
            scope=SrpClaim.Scope.IN_FLEET,
            link=link,
            operation=operation,
            fleet_label=f"{operation.fleet_type} — {operation.fc_character_name}",
            fc_user=link.fc_user,
            fc_main_character_name=link.fc_main_character_name,
        )
    if status in (SrpClaim.Status.APPROVED, SrpClaim.Status.PAID):
        values.update(
            approved_amount=amount if approved_amount is None else approved_amount,
            decided_by=decided_by,
            decided_at=now,
        )
    elif approved_amount is not None:
        values["approved_amount"] = approved_amount
    if status == SrpClaim.Status.PAID:
        values.update(paid_by=decided_by, paid_at=now)
    if status == SrpClaim.Status.REJECTED:
        values.update(decided_by=decided_by, decided_at=now, decision_reason="Not covered")
    values.update(extra)
    return SrpClaim.objects.create(**values)


def claim_event(claim: SrpClaim, action: str = SrpClaimEvent.Action.NOTE, *, actor=None, **extra) -> SrpClaimEvent:
    return SrpClaimEvent.objects.create(claim=claim, action=action, actor=actor, **extra)


def aa_group(name: str | None = None, *, leaders=(), leader_groups=()) -> Group:
    """An Alliance Auth group with its group leaders and leader groups.

    Leaders are added by primary key: on Alliance Auth 5.4 and later the leader fields point to proxy models
    and refuse plain ``User``/``Group`` instances.
    """
    group = Group.objects.create(name=name or f"Group {next_id()}")
    group.authgroup.group_leaders.add(*(user.pk for user in leaders))
    group.authgroup.group_leader_groups.add(*(leader_group.pk for leader_group in leader_groups))
    return group


def group_access(group: Group, mode: str = GroupAccess.Mode.PING, types=(), **extra) -> GroupAccess:
    """The FleetOps access row of ``group``: ``mode`` and, for fleets, the allowed fleet types."""
    access = GroupAccess.objects.create(group=group, mode=mode, **extra)
    if types:
        access.fleet_types.set(types)
    return access


def group_channel(name: str = "Group Pings", *, groups=(), fcs_allowed: bool = False, **extra) -> DiscordWebhook:
    """A ping channel assigned to ``groups``; by default reserved for them (alliance FCs may not use it)."""
    channel = ping_channel(name, fcs_allowed=fcs_allowed, **extra)
    if groups:
        channel.allowed_groups.set(groups)
    return channel


def standalone_ping(**fields) -> StandalonePing:
    """A ping without a fleet as the Send Ping page stores it; ``sender`` and ``channel`` fill their snapshots."""
    channel = fields.get("channel")
    sender = fields.get("sender")
    group = fields.get("group")
    values = {
        "request_id": uuid.uuid4(),
        "channel_name": channel.name if channel is not None else "Example Channel",
        "sender_name": main_of(sender).character_name if sender is not None and main_of(sender) else "",
        "group_name": group.name if group is not None else "",
        "additional_message": "Example message",
        "status": StandalonePing.Status.SENT,
    }
    values.update(fields)
    return StandalonePing.objects.create(**values)
