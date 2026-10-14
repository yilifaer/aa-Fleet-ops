# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Members, inactive list, leaderboard and member detail of the statistics.

Everything is computed for one EVE month on the main character: a member is a user of the statistics
population, the PAP of all of the user's characters (alts in other corporations or alliances included) counts
for the main, and the corporation of a member is the corporation of the current main. Each function runs a
fixed number of queries whatever the number of members; members are merged in Python.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from django.db.models import Count, Max, Sum
from django.utils.translation import gettext, pgettext

from fleetops.models import AttendanceRecord, FleetOpsSettings, FleetType
from fleetops.services import population
from fleetops.services.assessment import MonthTarget, competition_ranks, member_status, month_target
from fleetops.services.counting import counted_attendance
from fleetops.services.periods import MonthRef, month_ref
from fleetops.services.statistics import (
    attendance_queryset,
    fleet_types_in,
    user_fleet_type_totals,
    user_totals,
)
from fleetops.services.stats_types import Leaderboard, MemberFleetRow, MemberRow, MemberTable

SORTS = frozenset({"-pap", "pap", "name", "-name", "-fleets", "fleets", "corporation"})
STATUS_FILTERS = ("all", "inactive", "active")
PAGE_SIZE = 100
LAST_PAP_CHUNK = 500
FLEET_ROW_LIMIT = 500
# Fleet types with a column of their own in the members table; the rest is summed in "Other".
TYPE_COLUMNS = 5
# Longest name search that is used.
QUERY_LENGTH = 100

_INACTIVE = ("below", "below_so_far")


def clean_query(text) -> str:
    """The name search as it is used: the ends trimmed and cut to ``QUERY_LENGTH``.

    The members page, the member rows and the members export all read the search through this, so a file
    is filtered by the text its page was.
    """
    return (text or "").strip()[:QUERY_LENGTH]


@dataclass(slots=True)
class CharacterUse:
    """A character of a member that earned PAP in the month."""

    character_id: int
    character_name: str
    pap: int
    fleets: int


@dataclass(slots=True)
class MemberDetail:
    """One member's month: totals, ranks, the characters used, the fleet types and the fleets.

    Ranks are competition ranks among the members with PAP; ``None`` without PAP, and for a month that ended
    before the member registered. ``fleets_truncated`` is set when the member has more fleets than ``fleets``
    lists.
    """

    row: MemberRow
    month: MonthRef
    target: MonthTarget
    alliance_rank: int | None
    alliance_ranked: int
    corporation_rank: int | None
    corporation_ranked: int
    characters: list[CharacterUse] = field(default_factory=list)
    fleet_types: list[tuple[FleetType, int]] = field(default_factory=list)
    fleets: list[MemberFleetRow] = field(default_factory=list)
    fleets_truncated: bool = False


def _name_key(row: MemberRow):
    return row.main_character_name.casefold(), row.user_id


# Display order inside a tie of the leading column is fleets, then the main's name.
_SORT_KEYS = {
    "-pap": lambda row: (-row.pap, -row.fleets, *_name_key(row)),
    "pap": lambda row: (row.pap, *_name_key(row)),
    "-fleets": lambda row: (-row.fleets, -row.pap, *_name_key(row)),
    "fleets": lambda row: (row.fleets, row.pap, *_name_key(row)),
    "name": _name_key,
    "corporation": lambda row: (row.corporation_name.casefold(), row.corporation_id, *_name_key(row)),
}


def _sorted(rows: list[MemberRow], sort: str) -> list[MemberRow]:
    if sort == "-name":
        return sorted(rows, key=_name_key, reverse=True)
    return sorted(rows, key=_SORT_KEYS[sort])


def _build_rows(people, totals, types, target: MonthTarget | None, ref: MonthRef) -> list[MemberRow]:
    """A row per member with the totals of the month, the status and the competition rank."""
    rows = []
    for person in people:
        total = totals.get(person.user_id)
        pap = total.pap if total else 0
        rows.append(
            MemberRow(
                user_id=person.user_id,
                main_character_id=person.main_character_id,
                main_character_name=person.main_character_name,
                corporation_id=person.corporation_id,
                corporation_name=person.corporation_name,
                corporation_ticker=person.corporation_ticker,
                alliance_id=person.alliance_id,
                alliance_name=person.alliance_name,
                registered_at=person.registered_at,
                pap=pap,
                fleets=total.fleets if total else 0,
                characters=total.characters if total else 0,
                by_fleet_type=dict(types.get(person.user_id, ())),
                status=member_status(pap, target, ref) if target is not None else "none",
            )
        )
    ranks = competition_ranks((row.user_id, row.pap) for row in rows)
    for row in rows:
        row.rank = ranks.get(row.user_id)
    return rows


def _scope_base(year, month, corporation_id, settings_obj):
    """The counted PAP of the month; for a corporation only the PAP of its members in that month."""
    base = attendance_queryset(year, month)
    if corporation_id is not None:
        base = base.filter(
            auth_user_id__in=population.member_user_ids_subquery(
                corporation_id=corporation_id, registered_before=month_ref(year, month).end, settings_obj=settings_obj
            )
        )
    return base


def member_rows(
    year, month, *, corporation_id=None, status="all", query="", sort=None, settings_obj=None
) -> MemberTable:
    """The members of the alliance or of one corporation with their PAP in the month.

    ``status`` is ``all``, ``inactive`` or ``active`` (not inactive); ``query`` is a case-insensitive part of
    the main's name; an unknown ``status`` or ``sort`` falls back to the default (``-pap``, ``pap`` for the
    inactive list). Ranks and ``counts`` cover the whole scope, before the status and name filters.
    ``fleet_types`` are the fleet types the members earned PAP in, in display order. ``settings_obj`` saves
    a query when the caller has the settings at hand.
    """
    settings_obj = settings_obj or FleetOpsSettings.get_solo()
    ref = month_ref(year, month)
    people = population.members(corporation_id=corporation_id, registered_before=ref.end, settings_obj=settings_obj)
    base = _scope_base(year, month, corporation_id, settings_obj)
    totals = user_totals(base)
    types = user_fleet_type_totals(base)
    target = month_target(year, month)
    rows = _build_rows(people, totals, types, target, ref)
    fleet_types = fleet_types_in({type_id for row in rows for type_id in row.by_fleet_type})

    counts = {
        "all": len(rows),
        "with_pap": sum(1 for row in rows if row.pap > 0),
        "inactive": sum(1 for row in rows if row.status in _INACTIVE),
    }
    if status == "inactive":
        rows = [row for row in rows if row.status in _INACTIVE]
    elif status == "active":
        rows = [row for row in rows if row.status not in _INACTIVE]
    else:
        status = "all"
    needle = clean_query(query).casefold()
    if needle:
        rows = [row for row in rows if needle in row.main_character_name.casefold()]
    if sort not in SORTS:
        sort = "pap" if status == "inactive" else "-pap"
    return MemberTable(
        rows=_sorted(rows, sort),
        month=ref,
        target=target,
        fleet_types=fleet_types,
        counts=counts,
        corporation_id=corporation_id,
    )


def type_columns(table: MemberTable, limit: int = TYPE_COLUMNS) -> tuple[list[FleetType], bool]:
    """The fleet types that get a column of their own, and whether a column "Other" is needed.

    The ``limit`` types with the most PAP among the rows of the table (ties in display order), shown in
    display order. "Other" is needed when more types than that earned PAP.
    """
    totals = defaultdict(int)
    for row in table.rows:
        for type_id, pap in row.by_fleet_type.items():
            totals[type_id] += pap
    present = [fleet_type for fleet_type in table.fleet_types if totals.get(fleet_type.pk)]
    leading = sorted(present, key=lambda fleet_type: -totals[fleet_type.pk])[:limit]
    keep = {fleet_type.pk for fleet_type in leading}
    return [fleet_type for fleet_type in present if fleet_type.pk in keep], len(present) > limit


def fill_last_pap(rows: list[MemberRow]) -> None:
    """Set ``last_pap_at`` on the rows from the start of the member's latest counted fleet.

    One query. Up to ``LAST_PAP_CHUNK`` members (a page of a long list) are looked up by id. A longer list
    (an export) reads the latest PAP of every member in one grouped pass, because looking the ids up in
    ``IN`` lists costs a fleet lookup for each PAP row of the members, which grows with the members and the
    retained history. Members without counted PAP get ``None``.
    """
    user_ids = [row.user_id for row in rows]
    if not user_ids:
        return
    attendance = counted_attendance()
    if len(user_ids) <= LAST_PAP_CHUNK:
        attendance = attendance.filter(auth_user_id__in=user_ids)
    else:
        attendance = attendance.filter(auth_user__isnull=False)
    found = attendance.values("auth_user_id").annotate(last=Max("operation__started_at")).order_by()
    latest = {entry["auth_user_id"]: entry["last"] for entry in found}
    for row in rows:
        row.last_pap_at = latest.get(row.user_id)


def scope_corporations(settings_obj=None) -> list[tuple[int, str]]:
    """``[(corporation id, name)]`` of the corporations with members in the statistics, by name."""
    rows = (
        population.member_profiles(settings_obj=settings_obj)
        .values_list("main_character__corporation_id", "main_character__corporation_name")
        .order_by()
        .distinct()
    )
    names = {}
    for corporation_id, name in rows:
        if corporation_id is not None and (name or corporation_id not in names):
            names[corporation_id] = name or ""
    corporations = [
        (corporation_id, name or gettext("Corporation %(id)s") % {"id": corporation_id})
        for corporation_id, name in names.items()
    ]
    corporations.sort(key=lambda item: (item[1].casefold(), item[0]))
    return corporations


def leaderboard(year, month, *, corporation_id=None, size=None, viewer=None, settings_obj=None) -> Leaderboard:
    """The members with PAP up to ``size`` places, ties on the last place included.

    ``size`` defaults to the leaderboard size of the settings. ``viewer`` (a user or user ID) sets
    ``viewer_row`` when the viewer has PAP in the scope but is outside the shown places. Ranks are
    competition ranks; display order inside a tie is fleets (more first), then the main's name.
    ``settings_obj`` saves a query when the caller has the settings at hand.
    """
    settings_obj = settings_obj or FleetOpsSettings.get_solo()
    size = max(1, int(size or settings_obj.leaderboard_size))
    ref = month_ref(year, month)
    people = population.members(corporation_id=corporation_id, registered_before=ref.end, settings_obj=settings_obj)
    totals = user_totals(_scope_base(year, month, corporation_id, settings_obj))
    ranked = _sorted([row for row in _build_rows(people, totals, {}, None, ref) if row.pap > 0], "-pap")
    shown = [row for row in ranked if row.rank <= size]
    viewer_id = getattr(viewer, "pk", viewer)
    viewer_row = None
    if viewer_id is not None:
        viewer_row = next((row for row in ranked[len(shown) :] if row.user_id == viewer_id), None)
    if corporation_id is None:
        scope_label = pgettext("FleetOps", "Alliance")
    else:
        scope_label = (
            people[0].corporation_name if people and people[0].corporation_name else pgettext("FleetOps", "Corporation")
        )
    return Leaderboard(
        rows=shown,
        size=size,
        scope_label=scope_label,
        month=ref,
        corporation_id=corporation_id,
        ranked_count=len(ranked),
        viewer_row=viewer_row,
    )


def member_fleets(user, year, month, *, limit=FLEET_ROW_LIMIT) -> list[MemberFleetRow]:
    """The fleets a member earned PAP in during the month, newest first, at most ``limit``.

    One row per fleet: ``pap`` is the sum over the member's characters, ``character_name`` and ``source``
    list the characters and the kinds of PAP that went into it. One query.
    """
    if limit <= 0:
        return []
    source_labels = {key: str(label) for key, label in AttendanceRecord.Source.choices}
    rows = (
        attendance_queryset(year, month)
        .filter(auth_user_id=getattr(user, "pk", user))
        .order_by("-operation__started_at", "-operation_id", "character_name", "pk")
        .values_list(
            "operation_id",
            "operation__uuid",
            "operation__started_at",
            "operation__ended_at",
            "operation__fleet_type__short_name",
            "operation__fleet_type__name",
            "operation__doctrine_name",
            "operation__fc_main_character_name",
            "operation__fc_character_name",
            "character_name",
            "source",
            "attendance_value",
        )
    )
    fleets = {}
    for (
        operation_id,
        operation_uuid,
        started_at,
        ended_at,
        fleet_type_short,
        fleet_type_name,
        doctrine,
        fc_main_name,
        fc_name,
        character_name,
        source,
        value,
    ) in rows:
        entry = fleets.get(operation_id)
        if entry is None:
            if len(fleets) >= limit:
                break
            entry = fleets[operation_id] = {
                "operation_uuid": str(operation_uuid),
                "started_at": started_at,
                "ended_at": ended_at,
                "fleet_type": fleet_type_short or fleet_type_name or "",
                "doctrine": doctrine or "",
                "fc_name": fc_main_name or fc_name or "",
                "characters": [],
                "sources": [],
                "pap": 0,
            }
        if character_name not in entry["characters"]:
            entry["characters"].append(character_name)
        label = source_labels.get(source, source)
        if label not in entry["sources"]:
            entry["sources"].append(label)
        entry["pap"] += value
    return [
        MemberFleetRow(
            operation_uuid=entry["operation_uuid"],
            started_at=entry["started_at"],
            ended_at=entry["ended_at"],
            fleet_type=entry["fleet_type"],
            doctrine=entry["doctrine"],
            fc_name=entry["fc_name"],
            character_name=", ".join(entry["characters"]),
            source=", ".join(entry["sources"]),
            pap=entry["pap"],
        )
        for entry in fleets.values()
    ]


def member_detail(user, year, month, *, fleet_limit=None, settings_obj=None) -> MemberDetail | None:
    """Everything the member page shows; None when the user is not in the statistics population.

    The ranks are taken among all members (alliance) and among the members of the member's current
    corporation. A member who registered after the month has no rank and no status in it. The queries do not
    depend on the number of members or fleets.
    """
    settings_obj = settings_obj or FleetOpsSettings.get_solo()
    user_id = getattr(user, "pk", user)
    ref = month_ref(year, month)
    everyone = population.members(settings_obj=settings_obj)
    people = [person for person in everyone if population.registered_by(person, ref.end)]
    target = month_target(year, month)
    base = attendance_queryset(year, month)
    totals = user_totals(base)
    rows = _build_rows(people, totals, {}, target, ref)
    row = next((candidate for candidate in rows if candidate.user_id == user_id), None)
    if row is None:
        # A member who registered after the month: the page opens, without rank and status.
        identity = next((person for person in everyone if person.user_id == user_id), None)
        if identity is None:
            return None
        row = _build_rows([identity], totals, {}, None, ref)[0]
        row.rank = None
    corporation_rows = [candidate for candidate in rows if candidate.corporation_id == row.corporation_id]
    corporation_ranks = competition_ranks((candidate.user_id, candidate.pap) for candidate in corporation_rows)

    own = base.filter(auth_user_id=user_id)
    by_type = user_fleet_type_totals(own).get(user_id, {})
    row.by_fleet_type = dict(by_type)
    fleet_types = {fleet_type.pk: fleet_type for fleet_type in fleet_types_in(by_type)}
    breakdown = sorted(
        ((fleet_types[type_id], pap) for type_id, pap in by_type.items() if type_id in fleet_types),
        key=lambda item: (-item[1], item[0].sort_order, item[0].name),
    )
    characters = [
        CharacterUse(
            character_id=entry["character_id"],
            character_name=entry["name"],
            pap=entry["pap"] or 0,
            fleets=entry["fleets"],
        )
        for entry in own.values("character_id")
        .annotate(name=Max("character_name"), pap=Sum("attendance_value"), fleets=Count("operation_id", distinct=True))
        .order_by("-pap", "name")
    ]
    fill_last_pap([row])
    fleet_limit = FLEET_ROW_LIMIT if fleet_limit is None else fleet_limit
    fleets = member_fleets(user_id, year, month, limit=fleet_limit)
    return MemberDetail(
        row=row,
        month=ref,
        target=target,
        alliance_rank=row.rank,
        alliance_ranked=sum(1 for candidate in rows if candidate.pap > 0),
        corporation_rank=corporation_ranks.get(user_id),
        corporation_ranked=len(corporation_ranks),
        characters=characters,
        fleet_types=breakdown,
        fleets=fleets,
        fleets_truncated=row.fleets > len(fleets),
    )
