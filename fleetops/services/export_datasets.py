"""The statistics exports: who may download which dataset and what a file contains.

Each dataset has a ``check`` that reads the request, applies the permission rules and returns the scope of
the download, and a ``build`` that turns the scope into a :class:`~fleetops.services.exports.Table` and a
file name. The builders call the same service functions as the pages, so a file matches its page.

Permission failures raise ``PermissionDenied``; a corporation, member or period the user may not see, or that
does not exist, raises ``Http404`` like the object checks of the pages.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest

from fleetops.models import MAX_DATABASE_ID, AttendanceRecord, FleetOpsSettings, FleetType, IncentivePeriod
from fleetops.services import exports, stats_access
from fleetops.services import members as member_service
from fleetops.services import statistics as statistics_service
from fleetops.services.assessment import status_label
from fleetops.services.attendance_history import SCOPES as ATTENDANCE_SCOPES
from fleetops.services.attendance_history import history_month, history_queryset, retention_days
from fleetops.services.audit import audit_event
from fleetops.services.exports import Column, Table, export_filename, export_response
from fleetops.services.periods import eve_now, parse_year_month

BASIC_ACCESS = "fleetops.basic_access"
VIEW_CORP_STATS = "fleetops.view_corp_stats"
VIEW_ALL_STATS = "fleetops.view_all_stats"
MANAGE_INCENTIVES = "fleetops.manage_incentives"

PARAM_TEXT_LIMIT = 100
ATTENDANCE_CHUNK_ROWS = 2000
POINTS_PLACES = Decimal("0.01")
OTHER_CORPORATIONS = "Other corporations"
ALLIANCE_TOTAL = "Alliance total"
COUNTING_RULE = (
    "PAP of closed, approved fleets that count in the statistics, in the month the fleet started (EVE time). "
    "The PAP of all characters of a person is added up on the main character."
)
FC_RULE = (
    "Fleets that count in the statistics, for the FC and for special roles that give FC credit. "
    "FC points are the fleet type weight times the PAP multiplier."
)


@dataclass(slots=True)
class ExportScope:
    """What a download covers, as the check of a dataset decided it.

    ``label`` names the scope in the file name and the audit entry (``alliance``, ``corporation-98000001``,
    ``me``); ``year`` and ``month`` are None for "all retained" attendance. ``params`` holds the validated
    query parameters and is stored in the audit entry, so it carries no secrets.
    """

    dataset: str
    label: str
    year: int | None = None
    month: int | None = None
    corporation_id: int | None = None
    user: object | None = None
    period: IncentivePeriod | None = None
    params: dict = field(default_factory=dict)

    @property
    def period_label(self) -> str:
        return f"{self.year:04d}-{self.month:02d}" if self.year and self.month else "all"

    @property
    def object_id(self) -> str:
        return f"{self.dataset}:{self.label}:{self.period_label}"


@dataclass(frozen=True, slots=True)
class Dataset:
    slug: str
    title: str
    check: Callable[[HttpRequest], ExportScope]
    build: Callable[[HttpRequest, ExportScope], tuple[Table, str]]


def _require(request, permission):
    if not request.user.has_perm(permission):
        raise PermissionDenied


def _integer(request, name) -> int | None:
    """A positive database id from the query string; None when it is missing, unreadable or out of range."""
    value = request.GET.get(name, "")
    if not (value.isascii() and value.isdigit()):
        return None
    number = int(value)
    return number if 0 < number <= MAX_DATABASE_ID else None


def _params(**values) -> dict:
    """The validated query parameters for the audit entry; free text is cut so an entry stays small."""
    return {
        key: value[:PARAM_TEXT_LIMIT] if isinstance(value, str) else value
        for key, value in values.items()
        if value not in (None, "")
    }


# Checks


def check_corporations(request) -> ExportScope:
    _require(request, VIEW_ALL_STATS)
    year, month = parse_year_month(request.GET)
    sort = request.GET.get("sort", "")
    return ExportScope(
        "corporations",
        "alliance",
        year,
        month,
        params=_params(
            year=year, month=month, sort=sort if sort in statistics_service.OVERVIEW_SORTS else ""
        ),
    )


def check_members(request) -> ExportScope:
    """The alliance or one corporation; own corporation for ``view_corp_stats``, any for ``view_all_stats``."""
    year, month = parse_year_month(request.GET)
    corporation_id = _integer(request, "corporation")
    if corporation_id is None:
        _require(request, VIEW_ALL_STATS)
        label = "alliance"
    else:
        if not request.user.has_perm(VIEW_ALL_STATS):
            _require(request, VIEW_CORP_STATS)
            if not stats_access.can_view_corporation(request.user, corporation_id):
                raise Http404
        label = f"corporation-{corporation_id}"
    # The filters of the members page; a value the page would ignore is not exported either.
    status = request.GET.get("status", "")
    sort = request.GET.get("sort", "")
    params = _params(
        year=year,
        month=month,
        corporation=corporation_id,
        status=status if status in member_service.STATUS_FILTERS else "",
        q=member_service.clean_query(request.GET.get("q", "")),
        sort=sort if sort in member_service.SORTS else "",
    )
    return ExportScope("members", label, year, month, corporation_id=corporation_id, params=params)


def check_member_fleets(request) -> ExportScope:
    user_id = _integer(request, "user")
    if user_id is None or not stats_access.can_view_member(request.user, user_id):
        raise Http404
    user = get_user_model().objects.filter(pk=user_id).first()
    if user is None:
        raise Http404
    year, month = parse_year_month(request.GET)
    return ExportScope(
        "member-fleets", f"user-{user_id}", year, month, user=user, params=_params(user=user_id, year=year, month=month)
    )


def check_fcs(request) -> ExportScope:
    _require(request, VIEW_ALL_STATS)
    year, month = parse_year_month(request.GET)
    return ExportScope("fcs", "alliance", year, month, params=_params(year=year, month=month))


def check_attendance(request) -> ExportScope:
    """``scope`` is ``me``, ``corporation`` (the user's own, as on the history page) or ``alliance``.

    ``year`` and ``month`` together choose a month; without a valid pair the whole retention window is
    exported, as the history pages show it.
    """
    scope = request.GET.get("scope", "me")
    if scope not in ATTENDANCE_SCOPES:
        raise Http404
    _require(request, {"me": BASIC_ACCESS, "corporation": VIEW_CORP_STATS, "alliance": VIEW_ALL_STATS}[scope])
    corporation_id = None
    if scope == "corporation":
        corporation = stats_access.own_corporation(request.user)
        if corporation is None:
            raise Http404
        corporation_id = corporation[0]
    year, month = history_month(request.GET) or (None, None)
    return ExportScope(
        "attendance",
        scope,
        year,
        month,
        corporation_id=corporation_id,
        user=request.user if scope == "me" else None,
        params=_params(scope=scope, year=year, month=month),
    )


def check_incentives(request) -> ExportScope:
    _require(request, MANAGE_INCENTIVES)
    if not FleetOpsSettings.get_solo().incentive_enabled:
        raise Http404
    period_id = _integer(request, "period")
    period = IncentivePeriod.objects.filter(pk=period_id).first() if period_id is not None else None
    if period is None:
        raise Http404
    return ExportScope(
        "incentives", "alliance", period.year, period.month, period=period, params=_params(period=period.pk)
    )


# Builders


def _display_name(user) -> str:
    main = getattr(getattr(user, "profile", None), "main_character", None)
    return (main.character_name if main else "") or user.get_username()


def _table(request, scope, title, columns, rows, *, scope_text, details=()) -> tuple[Table, str]:
    """The table of a dataset with its About sheet, and the file name for the scope."""
    about = [
        ("Dataset", title),
        ("Scope", scope_text),
        ("Month (EVE time)", scope.period_label if scope.year else "All retained"),
        ("Exported at (EVE)", eve_now()),
        ("Exported by", _display_name(request.user)),
    ]
    about.extend((label, value) for label, value in details if value not in (None, ""))
    table = Table(name=title, columns=columns, rows=rows, about=about)
    return table, export_filename(scope.dataset, scope.label, scope.period_label)


def _target_details(target):
    average = "No target" if target.average is None else target.average
    return [("Corporation average target (PAP per main)", average), ("Member minimum PAP", target.minimum)]


def _fleet_types(known: Iterable[FleetType], mappings) -> list[FleetType]:
    """The fleet types that get a column: the given ones and any type a row has PAP in that they lack."""
    types = list(known)
    present = {fleet_type.pk for fleet_type in types}
    missing = {type_id for mapping in mappings for type_id in mapping} - present
    if missing:
        types.extend(statistics_service.fleet_types_in(missing))
    return types


def _type_columns(fleet_types) -> list[Column]:
    return [Column(f"type_{fleet_type.pk}", str(fleet_type), "int") for fleet_type in fleet_types]


def _by_type(mapping, fleet_types) -> list[int]:
    return [mapping.get(fleet_type.pk, 0) for fleet_type in fleet_types]


def _points(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(POINTS_PLACES)


def _not_counted_text(summary) -> str:
    labels = (
        ("excluded", "excluded"),
        ("pending", "waiting for approval"),
        ("rejected", "rejected"),
        ("running", "still running"),
    )
    return "; ".join(f"{summary[key]} {text}" for key, text in labels if summary.get(key))


def build_corporations(request, scope):
    overview = statistics_service.corporation_overview(scope.year, scope.month, sort=scope.params.get("sort") or "name")
    columns = [
        Column("corporation", "Corporation"),
        Column("ticker", "Ticker"),
        Column("alliance", "Alliance"),
        Column("mains", "Mains", "int"),
        Column("mains_with_pap", "Mains with PAP", "int"),
        Column("mains_below_minimum", "Mains below minimum", "int"),
        Column("total_pap", "Total PAP", "int"),
        Column("average", "Average PAP per main", "decimal"),
        Column("target", "Target", "decimal"),
        Column("status", "Status"),
    ]
    rows = overview.rows
    other = overview.other
    mappings = [row.by_fleet_type for row in rows] + ([other.by_fleet_type] if other else [])
    fleet_types = _fleet_types(overview.fleet_types, mappings)
    target = overview.target.average

    lines = [
        [
            row.corporation_name,
            row.corporation_ticker,
            row.alliance_name,
            row.mains,
            row.mains_with_pap,
            row.mains_below_minimum,
            row.total_pap,
            row.average,
            target,
            status_label(row.status),
            *_by_type(row.by_fleet_type, fleet_types),
        ]
        for row in rows
    ]
    if other is not None:
        # Snapshot totals of corporations without members in scope; they make the columns add up.
        lines.append(
            [
                OTHER_CORPORATIONS,
                "",
                "",
                None,
                None,
                None,
                other.total_pap,
                None,
                None,
                "",
                *_by_type(other.by_fleet_type, fleet_types),
            ]
        )
    type_totals = [sum(mapping.get(fleet_type.pk, 0) for mapping in mappings) for fleet_type in fleet_types]
    lines.append(
        [
            ALLIANCE_TOTAL,
            "",
            "",
            overview.mains,
            overview.mains_with_pap,
            sum(row.mains_below_minimum for row in rows),
            overview.alliance_pap,
            overview.average,
            target,
            "",
            *type_totals,
        ]
    )
    details = _target_details(overview.target)
    if overview.corporations_with_target:
        details.append(
            ("Corporations meeting the target", f"{overview.corporations_met} of {overview.corporations_with_target}")
        )
    details.append(("Fleets not counted", _not_counted_text(overview.not_counted)))
    details.append(("Counting rule", COUNTING_RULE))
    return _table(
        request,
        scope,
        "Corporations",
        columns + _type_columns(fleet_types),
        lines,
        scope_text="Alliance",
        details=details,
    )


def _members_scope_text(table, scope) -> str:
    if scope.corporation_id is None:
        return "Alliance"
    name = table.rows[0].corporation_name if table.rows else ""
    return f"Corporation {name or scope.corporation_id}"


def _members_filters(params) -> str:
    parts = []
    if params.get("status"):
        parts.append(f"Status: {params['status']}")
    if params.get("q"):
        parts.append(f"Name contains: {params['q']}")
    if params.get("sort"):
        parts.append(f"Sort: {params['sort']}")
    return "; ".join(parts)


def build_members(request, scope):
    params = scope.params
    table = member_service.member_rows(
        scope.year,
        scope.month,
        corporation_id=scope.corporation_id,
        status=params.get("status") or "all",
        query=params.get("q", ""),
        sort=params.get("sort") or None,
    )
    rows = table.rows
    # The file has the last PAP of everybody, read in one grouped query.
    member_service.fill_last_pap(rows)
    fleet_types = _fleet_types(table.fleet_types, (row.by_fleet_type for row in rows))
    columns = [
        Column("rank", "Rank", "int"),
        Column("main", "Main character"),
        Column("corporation", "Corporation"),
        Column("ticker", "Ticker"),
        Column("alliance", "Alliance"),
        Column("pap", "PAP", "int"),
        Column("fleets", "Fleets", "int"),
        Column("characters", "Characters used", "int"),
        Column("status", "Status"),
        Column("last_pap", "Last PAP (EVE)", "datetime"),
        Column("registered", "Registered (EVE)", "datetime"),
        *_type_columns(fleet_types),
    ]
    lines = [
        [
            row.rank,
            row.main_character_name,
            row.corporation_name,
            row.corporation_ticker,
            row.alliance_name,
            row.pap,
            row.fleets,
            row.characters,
            status_label(row.status),
            row.last_pap_at,
            row.registered_at,
            *_by_type(row.by_fleet_type, fleet_types),
        ]
        for row in rows
    ]
    details = [
        ("Filters", _members_filters(params)),
        *_target_details(table.target),
        ("Counting rule", COUNTING_RULE),
    ]
    scope_text = _members_scope_text(table, scope)
    return _table(request, scope, "Members", columns, lines, scope_text=scope_text, details=details)


def build_member_fleets(request, scope):
    # The page lists the latest fleets only; the file holds every fleet of the month.
    fleets = member_service.member_fleets(scope.user, scope.year, scope.month, limit=exports.MAX_EXPORT_ROWS)
    columns = [
        Column("started", "Fleet start (EVE)", "datetime"),
        Column("ended", "Fleet end (EVE)", "datetime"),
        Column("fleet_type", "Fleet type"),
        Column("doctrine", "Doctrine"),
        Column("fc", "FC"),
        Column("character", "Character"),
        Column("source", "Source"),
        Column("pap", "PAP", "int"),
    ]
    lines = [
        [
            fleet.started_at,
            fleet.ended_at,
            fleet.fleet_type,
            fleet.doctrine,
            fleet.fc_name,
            fleet.character_name,
            fleet.source,
            fleet.pap,
        ]
        for fleet in fleets
    ]
    return _table(
        request,
        scope,
        "Member fleets",
        columns,
        lines,
        scope_text=f"Member {_display_name(scope.user)}",
        details=[("Counting rule", COUNTING_RULE)],
    )


def _fc_type_names(rows) -> list[str]:
    """Fleet type names with FC credit in the rows, in the display order of the fleet types."""
    present = {name for row in rows for name in row.breakdown}
    if not present:
        return []
    ordered = [name for name in map(str, FleetType.objects.all()) if name in present]
    return ordered + sorted(present - set(ordered))


def build_fcs(request, scope):
    rows = statistics_service.fc_rows(scope.year, scope.month)
    type_names = _fc_type_names(rows)
    columns = [Column("fc", "FC (main)"), Column("fleets", "Fleets", "int"), Column("points", "FC points", "decimal")]
    for index, name in enumerate(type_names):
        columns.append(Column(f"fleets_{index}", f"{name} fleets", "int"))
        columns.append(Column(f"points_{index}", f"{name} points", "decimal"))
    lines = []
    for row in rows:
        line = [row.main_name, row.fleets, _points(row.points)]
        for name in type_names:
            part = row.breakdown.get(name) or {}
            line.extend((part.get("count", 0), _points(part.get("points"))))
        lines.append(line)
    return _table(
        request, scope, "FCs", columns, lines, scope_text="Alliance", details=[("Counting rule", FC_RULE)]
    )


ATTENDANCE_FIELDS = (
    "operation__started_at",
    "operation__ended_at",
    "operation__fleet_type__short_name",
    "operation__fleet_type__name",
    "operation__doctrine_name",
    "operation__fc_character_name",
    "character_name",
    "main_character_name",
    "corporation_name",
    "source",
    "attendance_value",
    "operation__uuid",
)


def _attendance_rows(queryset) -> Iterator[list]:
    """The PAP rows one at a time; the query is cut one row past the export limit so the writer can note the cut."""
    sources = dict(AttendanceRecord.Source.choices)
    rows = queryset.values_list(*ATTENDANCE_FIELDS)[: exports.MAX_EXPORT_ROWS + 1]
    for values in rows.iterator(chunk_size=ATTENDANCE_CHUNK_ROWS):
        started, ended, short_name, type_name, doctrine, fc, character, main, corporation, source, pap, uuid = values
        yield [
            started,
            ended,
            short_name or type_name,
            doctrine,
            fc,
            character,
            main,
            corporation,
            sources.get(source, source),
            pap,
            str(uuid),
        ]


def build_attendance(request, scope):
    month = (scope.year, scope.month) if scope.year else None
    queryset = history_queryset(
        scope.label, user=scope.user, corporation_id=scope.corporation_id, month=month
    )
    if scope.label == "me":
        scope_text = f"Personal PAP of {_display_name(request.user)}"
    elif scope.label == "corporation":
        corporation = stats_access.own_corporation(request.user)
        scope_text = f"Corporation {corporation[1] or corporation[0]} (at PAP time)"
    else:
        scope_text = "Alliance"
    columns = [
        Column("started", "Fleet start (EVE)", "datetime"),
        Column("ended", "Fleet end (EVE)", "datetime"),
        Column("fleet_type", "Fleet type"),
        Column("doctrine", "Doctrine"),
        Column("fc", "FC"),
        Column("character", "Character"),
        Column("main", "Main character"),
        Column("corporation", "Corporation (at PAP time)"),
        Column("source", "Source"),
        Column("pap", "PAP", "int"),
        Column("fleet_id", "Fleet ID"),
    ]
    details = [
        ("History window", f"The last {retention_days()} days"),
        ("Counting rule", COUNTING_RULE),
    ]
    return _table(
        request, scope, "PAP history", columns, _attendance_rows(queryset), scope_text=scope_text, details=details
    )


def build_incentives(request, scope):
    period = scope.period
    statistics = period.fc_statistics.select_related("fc_user__profile__main_character")
    status = period.get_status_display()
    columns = [
        Column("fc", "FC"),
        Column("fleets", "Fleets", "int"),
        Column("points", "FC points", "decimal"),
        Column("eligible", "Eligible", "bool"),
        Column("waived", "Waived", "bool"),
        Column("calculated", "Calculated payout", "int"),
        Column("final", "Final payout", "int"),
        Column("status", "Period status"),
    ]
    lines = [
        [
            _display_name(row.fc_user),
            row.fleet_count,
            row.total_points,
            row.eligible,
            row.waived,
            row.calculated_payout,
            row.final_payout,
            status,
        ]
        for row in statistics
    ]
    details = [
        ("Budget (ISK)", period.budget),
        ("Minimum fleets", period.minimum_fleets),
        ("Period status", status),
        ("Finalized at (EVE)", period.finalized_at),
        ("Counting rule", FC_RULE),
    ]
    return _table(request, scope, "FC incentives", columns, lines, scope_text="Alliance", details=details)


DATASETS = {
    dataset.slug: dataset
    for dataset in (
        Dataset("corporations", "Corporations", check_corporations, build_corporations),
        Dataset("members", "Members", check_members, build_members),
        Dataset("member-fleets", "Member fleets", check_member_fleets, build_member_fleets),
        Dataset("fcs", "FCs", check_fcs, build_fcs),
        Dataset("attendance", "PAP history", check_attendance, build_attendance),
        Dataset("incentives", "FC incentives", check_incentives, build_incentives),
    )
}


def get_dataset(slug: str) -> Dataset | None:
    return DATASETS.get(slug)


def export_for_request(request, slug: str, fmt: str):
    """The download response for a dataset, after the permission check, with its audit entry.

    Raises ``Http404`` for an unknown dataset or format, ``PermissionDenied`` without the permission.
    """
    dataset = get_dataset(slug)
    if dataset is None or fmt not in exports.FORMATS:
        raise Http404
    scope = dataset.check(request)
    table, filename = dataset.build(request, scope)
    audit_event(
        request.user,
        "statistics.export",
        "Statistics",
        scope.object_id,
        new_value={"format": fmt, "params": scope.params},
    )
    return export_response(table, fmt, filename)
