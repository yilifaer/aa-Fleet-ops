# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

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
from django.core.exceptions import BadRequest, PermissionDenied
from django.db.models import Sum
from django.http import Http404, HttpRequest
from django.utils.translation import gettext, gettext_lazy, ngettext, pgettext, pgettext_lazy

from fleetops.models import (
    MAX_DATABASE_ID,
    AttendanceRecord,
    FleetOpsSettings,
    FleetType,
    IncentivePeriod,
    SrpCategory,
    SrpClaim,
    SrpSettings,
)
from fleetops.services import exports, stats_access
from fleetops.services import members as member_service
from fleetops.services import statistics as statistics_service
from fleetops.services.assessment import status_label
from fleetops.services.attendance_history import SCOPES as ATTENDANCE_SCOPES
from fleetops.services.attendance_history import history_month, history_queryset, retention_days
from fleetops.services.audit import audit_event
from fleetops.services.exports import Column, Table, export_filename, export_response
from fleetops.services.periods import (
    MAX_REPORT_DAYS,
    Period,
    PeriodError,
    eve_now,
    parse_period,
    parse_year_month,
    period_query,
)
from fleetops.srp import access as srp_access
from fleetops.srp import display as srp_display
from fleetops.srp import payments as srp_payments
from fleetops.srp import policy as srp_rules
from fleetops.srp import reports as srp_reports
from fleetops.srp import summaries as srp_summaries
from fleetops.srp.types import warnings_from_json
from fleetops.texts import translate_stored

BASIC_ACCESS = "fleetops.basic_access"
VIEW_CORP_STATS = "fleetops.view_corp_stats"
VIEW_ALL_STATS = "fleetops.view_all_stats"
MANAGE_INCENTIVES = "fleetops.manage_incentives"

PARAM_TEXT_LIMIT = 100
ATTENDANCE_CHUNK_ROWS = 2000
POINTS_PLACES = Decimal("0.01")
OTHER_CORPORATIONS = gettext_lazy("Other corporations")
ALLIANCE_TOTAL = gettext_lazy("Alliance total")
COUNTING_RULE = gettext_lazy(
    "PAP of closed, approved fleets that count in the statistics, in the month the fleet started (EVE time). "
    "The PAP of all characters of a person is added up on the main character."
)
# What the totals and averages of the corporations overview count (FB11.1); the page prints the same sentences.
ALLIANCE_ROW_NOTE = gettext_lazy(
    "Alliance row: the total is every counted PAP of the month, the Other corporations row included; "
    "the average is the PAP of the current mains divided by the mains."
)
CORPORATION_ROWS_NOTE = gettext_lazy(
    "Corporation rows: PAP is every PAP flown for the corporation that month, including pilots who registered "
    "later; Mains and the average count only members registered before the month ended."
)
FC_RULE = gettext_lazy(
    "Fleets that count in the statistics, for the FC and for special roles that give FC credit. "
    "FC points are the fleet type weight times the PAP multiplier."
)


@dataclass(slots=True)
class ExportScope:
    """What a download covers, as the check of a dataset decided it.

    ``label`` names the scope in the file name and the audit entry (``alliance``, ``corporation-98000001``,
    ``me``); ``year`` and ``month`` are None for "all retained" PAP and for an SRP period that is not one calendar
    month. ``window`` is the period of an SRP download (any range of EVE days). ``params`` holds the validated
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
    window: Period | None = None

    @property
    def period_label(self) -> str:
        """The period in the file name: ``YYYY-MM-DD_YYYY-MM-DD`` for every SRP period (a whole calendar month
        too), ``YYYY-MM`` for one month of retained PAP, ``all`` for all retained PAP."""
        if self.window is not None:
            return self.window.file_label
        return f"{self.year:04d}-{self.month:02d}" if self.year and self.month else "all"

    @property
    def period_text(self) -> str:
        """The period for the About sheet."""
        if self.window is not None:
            return self.window.label
        return self.period_label if self.year else gettext("All retained")

    @property
    def object_id(self) -> str:
        return f"{self.dataset}:{self.label}:{self.period_label}"


@dataclass(frozen=True, slots=True)
class Dataset:
    slug: str
    title: str
    check: Callable[[HttpRequest], ExportScope]
    build: Callable[[HttpRequest, ExportScope], tuple[Table, str]]
    # The audit entry of a download: its action and the object type it names.
    audit_action: str = "statistics.export"
    audit_object: str = "Statistics"


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
        (gettext("Dataset"), title),
        (gettext("Scope"), scope_text),
        # SRP files cover any period; the other statistics files are by month and keep their label.
        (gettext("Period (EVE time)") if scope.window is not None else gettext("Month (EVE time)"), scope.period_text),
        (gettext("Exported at (EVE)"), eve_now()),
        (gettext("Exported by"), _display_name(request.user)),
    ]
    about.extend((label, value) for label, value in details if value not in (None, ""))
    table = Table(name=title, columns=columns, rows=rows, about=about)
    return table, export_filename(scope.dataset, scope.label, scope.period_label)


def _target_details(target):
    average = gettext("No target") if target.average is None else target.average
    # A month without any target row is not assessed: there is no member minimum either (a minimum of 0).
    minimum = gettext("No target") if target.rule is None and not target.minimum else target.minimum
    return [
        (gettext("Corporation average target (PAP per main)"), average),
        (gettext("Member minimum PAP"), minimum),
    ]


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
        ("excluded", gettext("%(count)s excluded")),
        ("pending", gettext("%(count)s waiting for approval")),
        ("rejected", gettext("%(count)s rejected")),
        ("running", gettext("%(count)s still running")),
    )
    return "; ".join(text % {"count": summary[key]} for key, text in labels if summary.get(key))


def build_corporations(request, scope):
    overview = statistics_service.corporation_overview(scope.year, scope.month, sort=scope.params.get("sort") or "name")
    columns = [
        Column("corporation", pgettext("FleetOps", "Corporation")),
        Column("ticker", pgettext("FleetOps", "Ticker")),
        Column("alliance", pgettext("FleetOps", "Alliance")),
        Column("mains", pgettext("FleetOps", "Mains"), "int"),
        Column("mains_with_pap", gettext("Mains with PAP"), "int"),
        Column("mains_below_minimum", gettext("Mains below minimum"), "int"),
        Column("total_pap", gettext("Total PAP"), "int"),
        Column("average", gettext("Average PAP per main"), "decimal"),
        Column("target", gettext("Target"), "decimal"),
        Column("status", gettext("Status")),
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
                str(OTHER_CORPORATIONS),
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
            str(ALLIANCE_TOTAL),
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
            (
                gettext("Corporations meeting the target"),
                gettext("%(met)s of %(total)s")
                % {"met": overview.corporations_met, "total": overview.corporations_with_target},
            )
        )
    details.append((gettext("Fleets not counted"), _not_counted_text(overview.not_counted)))
    details.append((gettext("Counting rule"), str(COUNTING_RULE)))
    details.append((gettext("Note"), str(ALLIANCE_ROW_NOTE)))
    details.append((gettext("Note"), str(CORPORATION_ROWS_NOTE)))
    return _table(
        request,
        scope,
        pgettext("FleetOps", "Corporations"),
        columns + _type_columns(fleet_types),
        lines,
        scope_text=pgettext("FleetOps", "Alliance"),
        details=details,
    )


def _members_scope_text(table, scope) -> str:
    if scope.corporation_id is None:
        return pgettext("FleetOps", "Alliance")
    name = table.rows[0].corporation_name if table.rows else ""
    return gettext("Corporation %(name)s") % {"name": name or scope.corporation_id}


def _members_filters(params) -> str:
    parts = []
    if params.get("status"):
        parts.append(gettext("Status: %(value)s") % {"value": params["status"]})
    if params.get("q"):
        parts.append(gettext("Name contains: %(value)s") % {"value": params["q"]})
    if params.get("sort"):
        parts.append(gettext("Sort: %(value)s") % {"value": params["sort"]})
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
        Column("rank", gettext("Rank"), "int"),
        Column("main", pgettext("FleetOps", "Main character")),
        Column("corporation", pgettext("FleetOps", "Corporation")),
        Column("ticker", pgettext("FleetOps", "Ticker")),
        Column("alliance", pgettext("FleetOps", "Alliance")),
        Column("pap", "PAP", "int"),
        Column("fleets", gettext("Fleets"), "int"),
        Column("characters", gettext("Characters used"), "int"),
        Column("status", gettext("Status")),
        Column("last_pap", gettext("Last PAP (EVE)"), "datetime"),
        Column("registered", gettext("Registered (EVE)"), "datetime"),
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
        (pgettext("FleetOps", "Filters"), _members_filters(params)),
        *_target_details(table.target),
        (gettext("Counting rule"), str(COUNTING_RULE)),
    ]
    scope_text = _members_scope_text(table, scope)
    return _table(request, scope, gettext("Members"), columns, lines, scope_text=scope_text, details=details)


def build_member_fleets(request, scope):
    # The page lists the latest fleets only; the file holds every fleet of the month.
    fleets = member_service.member_fleets(scope.user, scope.year, scope.month, limit=exports.MAX_EXPORT_ROWS)
    columns = [
        Column("started", gettext("Fleet start (EVE)"), "datetime"),
        Column("ended", gettext("Fleet end (EVE)"), "datetime"),
        Column("fleet_type", gettext("Fleet type")),
        Column("group", pgettext("FleetOps", "Group")),
        Column("doctrine", pgettext("FleetOps", "Doctrine")),
        Column("fc", "FC"),
        Column("character", gettext("Character")),
        Column("source", gettext("Source")),
        Column("pap", "PAP", "int"),
    ]
    lines = [
        [
            fleet.started_at,
            fleet.ended_at,
            fleet.fleet_type,
            fleet.group_name,
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
        gettext("Member fleets"),
        columns,
        lines,
        scope_text=gettext("Member %(name)s") % {"name": _display_name(scope.user)},
        details=[(gettext("Counting rule"), str(COUNTING_RULE))],
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
    columns = [
        Column("fc", gettext("FC (main)")),
        Column("fleets", gettext("Fleets"), "int"),
        Column("points", gettext("FC points"), "decimal"),
    ]
    for index, name in enumerate(type_names):
        columns.append(Column(f"fleets_{index}", gettext("%(name)s fleets") % {"name": name}, "int"))
        columns.append(Column(f"points_{index}", gettext("%(name)s points") % {"name": name}, "decimal"))
    lines = []
    for row in rows:
        line = [row.main_name, row.fleets, _points(row.points)]
        for name in type_names:
            part = row.breakdown.get(name) or {}
            line.extend((part.get("count", 0), _points(part.get("points"))))
        lines.append(line)
    return _table(
        request,
        scope,
        gettext("FCs"),
        columns,
        lines,
        scope_text=pgettext("FleetOps", "Alliance"),
        details=[(gettext("Counting rule"), str(FC_RULE))],
    )


ATTENDANCE_FIELDS = (
    "operation__started_at",
    "operation__ended_at",
    "operation__fleet_type__short_name",
    "operation__fleet_type__name",
    "operation__group_name",
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
        started, ended, short_name, type_name, group, doctrine, fc = values[:7]
        character, main, corporation, source, pap, uuid = values[7:]
        yield [
            started,
            ended,
            short_name or type_name,
            group,
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
        scope_text = gettext("Personal PAP of %(name)s") % {"name": _display_name(request.user)}
    elif scope.label == "corporation":
        corporation = stats_access.own_corporation(request.user)
        scope_text = gettext("Corporation %(name)s (at PAP time)") % {"name": corporation[1] or corporation[0]}
    else:
        scope_text = pgettext("FleetOps", "Alliance")
    columns = [
        Column("started", gettext("Fleet start (EVE)"), "datetime"),
        Column("ended", gettext("Fleet end (EVE)"), "datetime"),
        Column("fleet_type", gettext("Fleet type")),
        Column("group", pgettext("FleetOps", "Group")),
        Column("doctrine", pgettext("FleetOps", "Doctrine")),
        Column("fc", "FC"),
        Column("character", gettext("Character")),
        Column("main", pgettext("FleetOps", "Main character")),
        Column("corporation", gettext("Corporation (at PAP time)")),
        Column("source", gettext("Source")),
        Column("pap", "PAP", "int"),
        Column("fleet_id", gettext("Fleet ID")),
    ]
    details = [
        (gettext("History window"), gettext("The last %(days)s days") % {"days": retention_days()}),
        (gettext("Counting rule"), str(COUNTING_RULE)),
    ]
    return _table(
        request,
        scope,
        gettext("PAP history"),
        columns,
        _attendance_rows(queryset),
        scope_text=scope_text,
        details=details,
    )


def build_incentives(request, scope):
    period = scope.period
    statistics = period.fc_statistics.select_related("fc_user__profile__main_character")
    status = period.get_status_display()
    columns = [
        Column("fc", "FC"),
        Column("fleets", gettext("Fleets"), "int"),
        Column("points", gettext("FC points"), "decimal"),
        Column("eligible", gettext("Eligible"), "bool"),
        Column("waived", gettext("Waived"), "bool"),
        Column("calculated", gettext("Calculated payout"), "int"),
        Column("final", gettext("Final payout"), "int"),
        Column("status", gettext("Period status")),
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
        (gettext("Budget (ISK)"), period.budget),
        (gettext("Minimum fleets"), period.minimum_fleets),
        (gettext("Period status"), status),
        (gettext("Finalized at (EVE)"), period.finalized_at),
        (gettext("Counting rule"), str(FC_RULE)),
    ]
    return _table(
        request, scope, gettext("FC incentives"), columns, lines, scope_text=pgettext("FleetOps", "Alliance"), details=details
    )


# SRP

SRP_RULE = gettext_lazy(
    "Claims count in the EVE month of the loss (killmail time). Withdrawn claims are left out. "
    "Approved counts approved and paid claims; outstanding is approved minus paid."
)
SRP_PAYABLE_RULE = gettext_lazy(
    "Approved claims that are not paid yet, with the filters of the payments page. Only payers can export this list."
)
SRP_AUDIT = {"audit_action": "srp.export", "audit_object": "SrpSummary"}
SRP_STATUS_FILTERS = (
    SrpClaim.Status.PENDING,
    SrpClaim.Status.APPROVED,
    SrpClaim.Status.PAID,
    SrpClaim.Status.REJECTED,
)
SRP_TOTAL_ROW = gettext_lazy("Total")


def _srp_corporation(request):
    """``(corporation_id or None, scope label)`` of an SRP download; raises when it is not allowed.

    SRP managers and ``view_all_stats`` download the alliance or any corporation, ``view_corp_stats`` its own
    corporation.
    """
    corporation_id = _integer(request, "corporation")
    if corporation_id is None:
        if not srp_access.sees_everything(request.user):
            raise PermissionDenied
        return None, "alliance"
    if not srp_access.sees_everything(request.user):
        _require(request, VIEW_CORP_STATS)
        if not srp_access.can_view_corporation_summary(request.user, corporation_id):
            raise Http404
    return corporation_id, f"corporation-{corporation_id}"


def _srp_everything(request):
    if not srp_access.sees_everything(request.user):
        raise PermissionDenied


def _srp_window(request, *, optional: bool = False) -> Period | None:
    """The period of an SRP download from ``from`` and ``to``, ``preset`` or ``year`` and ``month``; the current month
    without them (as in 0.2.x). With ``optional`` and none of them None. An unusable period is a bad request."""
    if optional and not any(name in request.GET for name in ("from", "to", "preset", "year", "month")):
        return None
    try:
        return parse_period(request.GET, default="this_month", earliest=srp_summaries.earliest_claim_time())
    except PeriodError as exc:
        raise BadRequest(str(exc)) from None


def _window_params(window: Period | None) -> dict:
    """The period for the audit entry: the month as ``year`` and ``month`` like before, else the days or the preset."""
    if window is None:
        return {}
    if window.is_calendar_month:
        return {"year": window.start.year, "month": window.start.month}
    return period_query(window)


def _window_scope(dataset, label, window, *, params, corporation_id=None) -> ExportScope:
    year, month = window.year_month or (None, None)
    return ExportScope(
        dataset,
        label,
        year,
        month,
        corporation_id=corporation_id,
        params={**_window_params(window), **params},
        window=window,
    )


def check_srp_claims(request) -> ExportScope:
    window = _srp_window(request)
    corporation_id, label = _srp_corporation(request)
    status = request.GET.get("status", "")
    claim_scope = request.GET.get("scope", "")
    category = request.GET.get("category", "")
    if category and not SrpCategory.objects.filter(key=category).exists():
        raise Http404
    params = _params(
        corporation=corporation_id,
        status=status if status in SRP_STATUS_FILTERS else "",
        scope=claim_scope if claim_scope in SrpClaim.Scope.values else "",
        category=category,
    )
    return _window_scope("srp-claims", label, window, params=params, corporation_id=corporation_id)


def check_srp_members(request) -> ExportScope:
    window = _srp_window(request)
    corporation_id, label = _srp_corporation(request)
    return _window_scope(
        "srp-members", label, window, params=_params(corporation=corporation_id), corporation_id=corporation_id
    )


def check_srp_corporations(request) -> ExportScope:
    _srp_everything(request)
    return _window_scope("srp-corporations", "alliance", _srp_window(request), params={})


def check_srp_fcs(request) -> ExportScope:
    _srp_everything(request)
    return _window_scope("srp-fcs", "alliance", _srp_window(request), params={})


def _check_srp_report(slug, *, staff_only: bool = False):
    """The check of a report dataset (``srp-trend`` ...): everyone who sees all SRP summaries, SRP staff only for the
    reviewer and payer reports."""

    def check(request) -> ExportScope:
        if staff_only:
            if not srp_access.is_srp_staff(request.user):
                raise PermissionDenied
        else:
            _srp_everything(request)
        split = request.GET.get("split", "") if slug == "srp-trend" else ""
        return _window_scope(
            slug, "alliance", _srp_window(request), params=_params(split="category" if split == "category" else "")
        )

    return check


def check_srp_payable(request) -> ExportScope:
    """The claims waiting for payment, with the filters of the payments page; payers only (``manage_srp`` or
    ``pay_srp``), and nobody else even when they may see all statistics."""
    if not srp_access.is_payer(request.user):
        raise PermissionDenied
    filters = srp_payments.parse_filters(request.GET)
    group = "player" if request.GET.get("group") == "player" else ""
    # The period of the loss narrows the page's list only once picked; the file follows the page.
    window = _srp_window(request, optional=True)
    year, month = (window.year_month or (None, None)) if window is not None else (None, None)
    params = _params(group=group, **filters)
    params.update(_window_params(window))
    return ExportScope("srp-payable", "alliance", year, month, params=params, window=window)


def _label(choices, value) -> str:
    return dict(choices).get(value, value or "")


def _srp_claim_lines(claims, *, show_reviewers: bool = False) -> Iterator[list]:
    """The claim rows one at a time; the query is cut one row past the export limit so the writer can note the cut.

    The names of the people who decided on and paid a claim are written for ``show_reviewers`` only: the claim
    page shows them to SRP managers alone, and the file must not tell more than the page.
    """
    claims = claims.select_related("category", "link", "killmail")
    if show_reviewers:
        claims = claims.select_related(
            "decided_by__profile__main_character", "paid_by__profile__main_character", "payment_batch"
        )
    rows = claims.order_by("killmail_time", "pk")[: exports.MAX_EXPORT_ROWS + 1]
    value_sources = SrpSettings.ValueSource.choices
    for claim in rows.iterator(chunk_size=ATTENDANCE_CHUNK_ROWS):
        yield [
            claim.reference,
            claim.killmail_time,
            claim.main_character_name or claim.character_name,
            claim.character_name,
            claim.corporation_name,
            claim.ship_type_name or claim.ship_type_id,
            claim.solar_system_name,
            claim.get_scope_display(),
            claim.fleet_label,
            claim.fc_main_character_name,
            claim.loss_value,
            _label(value_sources, claim.value_source),
            claim.get_payout_basis_display(),
            claim.suggested_amount,
            claim.limited_amount,
            claim.approved_amount,
            claim.get_status_display(),
            _display_name(claim.decided_by) if show_reviewers and claim.decided_by_id else "",
            claim.decided_at,
            translate_stored(claim.decision_reason),
            claim.paid_at,
            _display_name(claim.paid_by) if show_reviewers and claim.paid_by_id else "",
            srp_display.presence_text(claim.fleet_check, claim.fleet_check_params) if claim.fleet_check else "",
            "; ".join(warning.text for warning in warnings_from_json(claim.warnings)),
            # Appended at the end (decision 26). Like the names of the deciders, only staff see the batch.
            claim.payment_batch.reference if show_reviewers and claim.payment_batch_id else "",
            # Appended at the end as well, so that downstream sheets that read by column number keep working. The
            # security status is part of the staff block of the claim page, so only staff get it here.
            srp_display.category_label(claim.category) if claim.category_id else "",
            srp_display.link_name(claim),
            claim.get_source_display(),
            claim.killmail.solar_system_security if show_reviewers else None,
        ]


def build_srp_payable(request, scope):
    """The approved claims to pay (the same ones as the payments page for these filters): one row per claim, or one
    row per player with the claims and the sum. The footer is the total of the approved amounts."""
    params = scope.params
    filters = {key: params[key] for key in srp_payments.FILTER_NAMES if key in params}
    claims = srp_payments.apply_filters(SrpClaim.objects.filter(status=SrpClaim.Status.APPROVED), filters)
    if scope.window is not None:
        claims = claims.filter(killmail_time__gte=scope.window.start, killmail_time__lt=scope.window.end)
    claims = claims.select_related("user__profile__main_character", "category", "link").order_by("killmail_time", "pk")
    # One row past the limit shows a cut; the file then says so (a payer must not take a short list for the whole one).
    rows = list(claims[: exports.MAX_EXPORT_ROWS + 1])
    cut = len(rows) > exports.MAX_EXPORT_ROWS
    rows = rows[: exports.MAX_EXPORT_ROWS]
    total = sum(claim.approved_amount or 0 for claim in rows)
    if params.get("group") == "player":
        columns = [
            Column("pay_to", gettext("Pay to")),
            Column("character_id", gettext("Character ID"), "int"),
            Column("claims", gettext("Claims"), "int"),
            Column("total", gettext("Approved (ISK)"), "int"),
        ]
        lines = [
            [row.main_character_name, row.main_character_id, len(row.claims), row.total]
            for row in srp_payments.pay_rows(rows)
        ]
        footer = [str(SRP_TOTAL_ROW), None, len(rows), total]
    else:
        columns = [
            Column("pay_to", gettext("Pay to")),
            Column("character_id", gettext("Character ID"), "int"),
            Column("claim", gettext("Claim")),
            Column("loss", gettext("Loss time (EVE)"), "datetime"),
            Column("ship", gettext("Ship")),
            Column("category", gettext("Category")),
            Column("link", gettext("Link")),
            Column("approved", gettext("Approved (ISK)"), "int"),
        ]
        lines = []
        for claim in rows:
            character_id, name = srp_rules.pay_to(claim)
            lines.append(
                [
                    name,
                    character_id,
                    claim.reference,
                    claim.killmail_time,
                    claim.ship_type_name or claim.ship_type_id,
                    srp_display.category_label(claim.category) if claim.category_id else "",
                    (claim.link.name or claim.link.code) if claim.link_id else "",
                    claim.approved_amount,
                ]
            )
        footer = [str(SRP_TOTAL_ROW), None, len(rows), None, None, None, None, total]
    filter_text = "; ".join(f"{key}: {value}" for key, value in filters.items())
    table, filename = _table(
        request,
        scope,
        gettext("SRP to pay"),
        columns,
        lines,
        scope_text=pgettext("FleetOps", "Alliance"),
        details=[
            (pgettext("FleetOps", "Filters"), filter_text),
            (gettext("Counting rule"), str(SRP_PAYABLE_RULE)),
        ],
    )
    if cut:
        # A payer must not take a short list for the whole one: the note stands in the file, above the total.
        table.rows.append([exports.truncation_note()] + [None] * (len(columns) - 1))
    table.footer.append(footer)
    return table, filename


def _srp_scope_text(scope, name="") -> str:
    if scope.corporation_id is None:
        return pgettext("FleetOps", "Alliance")
    return gettext("Corporation %(name)s") % {"name": name or scope.corporation_id}


def build_srp_claims(request, scope):
    params = scope.params
    claims = srp_summaries.period_claims(scope.window.start, scope.window.end)
    if scope.corporation_id is not None:
        claims = claims.filter(corporation_id=scope.corporation_id)
    if params.get("status"):
        claims = claims.filter(status=params["status"])
    if params.get("scope"):
        claims = claims.filter(scope=params["scope"])
    if params.get("category"):
        claims = claims.filter(category__key=params["category"])
    columns = [
        Column("claim", gettext("Claim")),
        Column("loss", gettext("Loss time (EVE)"), "datetime"),
        Column("pilot", gettext("Pilot (main)")),
        Column("character", gettext("Character")),
        Column("corporation", pgettext("FleetOps", "Corporation")),
        Column("ship", gettext("Ship")),
        Column("system", gettext("System")),
        Column("scope", gettext("Scope")),
        Column("fleet", gettext("Fleet")),
        Column("fc", "FC"),
        Column("value", gettext("Value (ISK)"), "decimal"),
        Column("value_source", gettext("Value source")),
        Column("basis", gettext("Payout basis")),
        Column("suggested", gettext("Suggested (ISK)"), "int"),
        Column("limited", gettext("Within limit (ISK)"), "int"),
        Column("approved", gettext("Approved (ISK)"), "int"),
        Column("status", gettext("Status")),
        Column("decided_by", gettext("Decided by")),
        Column("decided_at", gettext("Decided at (EVE)"), "datetime"),
        Column("reason", gettext("Reason")),
        Column("paid_at", gettext("Paid at (EVE)"), "datetime"),
        Column("paid_by", gettext("Paid by")),
        Column("fleet_check", gettext("Fleet check")),
        Column("warnings", gettext("Warnings")),
        Column("payment_batch", gettext("Payment batch")),
        Column("category", gettext("Category")),
        Column("link", gettext("Link")),
        Column("source", gettext("Source")),
        Column("security", gettext("Security status"), "decimal"),
    ]
    filters = "; ".join(
        text
        for text in (
            gettext("Status: %(value)s") % {"value": _label(SrpClaim.Status.choices, params["status"])}
            if params.get("status")
            else "",
            gettext("Scope: %(value)s") % {"value": _label(SrpClaim.Scope.choices, params["scope"])}
            if params.get("scope")
            else "",
            gettext("Category: %(value)s") % {"value": params["category"]} if params.get("category") else "",
        )
        if text
    )
    table, filename = _table(
        request,
        scope,
        gettext("SRP claims"),
        columns,
        _srp_claim_lines(claims, show_reviewers=srp_access.is_srp_staff(request.user)),
        scope_text=_srp_scope_text(scope),
        details=[(pgettext("FleetOps", "Filters"), filters), (gettext("Counting rule"), str(SRP_RULE))],
    )
    # The total covers every filtered claim, also the ones a cut export leaves out.
    table.footer.append(_srp_claims_total_line(claims, columns))
    return table, filename


def _srp_claims_total_line(claims, columns) -> list:
    """The "Total" row of the claims export: the figures of the summary page for the filtered claims.

    The pilot column counts the claims, the value column adds up the loss values, "Suggested" is the requested
    total and "Approved" the amount of approved and paid claims; the other cells stay empty.
    """
    totals = srp_summaries.claims_totals(claims)
    loss_value = claims.aggregate(total=Sum("loss_value"))["total"] or 0
    filled = {
        "claim": str(SRP_TOTAL_ROW),
        "pilot": ngettext("%(count)s claim", "%(count)s claims", totals.claims) % {"count": totals.claims},
        "value": loss_value,
        "suggested": totals.requested,
        "approved": totals.approved,
    }
    return [filled.get(column.key) for column in columns]


def _totals_cells(totals) -> list:
    return [
        totals.claims,
        totals.pending,
        totals.approved_count,
        totals.paid_count,
        totals.rejected,
        totals.requested,
        totals.approved,
        totals.paid,
        totals.outstanding,
    ]


def _totals_columns() -> list[Column]:
    return [
        Column("claims", gettext("Claims"), "int"),
        Column("pending", pgettext("FleetOps", "Pending"), "int"),
        Column("approved_count", gettext("Approved claims"), "int"),
        Column("paid_count", gettext("Paid claims"), "int"),
        Column("rejected", pgettext("FleetOps", "Rejected"), "int"),
        Column("requested", gettext("Requested (ISK)"), "int"),
        Column("approved", gettext("Approved (ISK)"), "int"),
        Column("paid", gettext("Paid (ISK)"), "int"),
        Column("outstanding", gettext("Outstanding (ISK)"), "int"),
    ]


def build_srp_members(request, scope):
    rows = srp_summaries.by_member(scope.window.start, scope.window.end, corporation_id=scope.corporation_id)
    columns = [
        Column("main", pgettext("FleetOps", "Main character")),
        Column("corporation", pgettext("FleetOps", "Corporation")),
        *_totals_columns(),
        Column("average", gettext("Average payout (ISK)"), "int"),
    ]
    lines = [
        [row.main_character_name, row.corporation_name, *_totals_cells(row.totals), row.totals.average_payout]
        for row in rows
    ]
    name = rows[0].corporation_name if rows else ""
    table, filename = _table(
        request,
        scope,
        gettext("SRP members"),
        columns,
        lines,
        scope_text=_srp_scope_text(scope, name),
        details=[(gettext("Counting rule"), str(SRP_RULE))],
    )
    total = srp_summaries.add_totals(*(row.totals for row in rows))
    table.footer.append([str(SRP_TOTAL_ROW), "", *_totals_cells(total), total.average_payout])
    return table, filename


def build_srp_corporations(request, scope):
    rows = srp_summaries.by_corporation(scope.window.start, scope.window.end)
    columns = [
        Column("corporation", pgettext("FleetOps", "Corporation")),
        Column("ticker", pgettext("FleetOps", "Ticker")),
        *_totals_columns(),
        Column("cap", gettext("Monthly cap (ISK)"), "int"),
        Column("used", gettext("Used of the cap (ISK)"), "int"),
        Column("remaining", gettext("Remaining (ISK)"), "int"),
        # Appended at the end (decision 26).
        Column("members", gettext("People"), "int"),
        Column("average", gettext("Average payout (ISK)"), "int"),
    ]
    # The cap is monthly: what a corporation used of it is only known for exactly one calendar month.
    month = scope.window.is_calendar_month
    lines = [
        [
            row.corporation_name,
            row.corporation_ticker,
            *_totals_cells(row.totals),
            row.cap,
            row.used if month else None,
            row.remaining if month else None,
            row.members,
            row.totals.average_payout,
        ]
        for row in rows
    ]
    total = srp_summaries.add_totals(*(row.totals for row in rows))
    # The total stays the last row of this older file (consumers read it there); the new datasets use the footer.
    lines.append([str(SRP_TOTAL_ROW), "", *_totals_cells(total), None, None, None, None, total.average_payout])
    return _table(
        request,
        scope,
        gettext("SRP corporations"),
        columns,
        lines,
        scope_text=pgettext("FleetOps", "Alliance"),
        details=[(gettext("Counting rule"), str(SRP_RULE))]
        + (
            []
            if month
            else [
                (
                    gettext("Note"),
                    gettext(
                        "Used and Remaining are shown only when the period is exactly one calendar month, because the cap is monthly."
                    ),
                )
            ]
        ),
    )


def build_srp_fcs(request, scope):
    rows = srp_summaries.by_fc(scope.window.start, scope.window.end)
    columns = [
        Column("fc", gettext("FC (main)")),
        Column("fleets", gettext("Fleets"), "int"),
        *_totals_columns(),
        Column("average", gettext("Average payout (ISK)"), "int"),
        Column("fleet_types", gettext("Fleet types (fleets)")),
    ]
    lines = [
        [
            row.fc_name,
            row.fleets,
            *_totals_cells(row.totals),
            row.totals.average_payout,
            srp_reports.fleet_type_text(row.fleet_types),
        ]
        for row in rows
    ]
    total = srp_summaries.add_totals(*(row.totals for row in rows))
    lines.append([str(SRP_TOTAL_ROW), sum(row.fleets for row in rows), *_totals_cells(total), total.average_payout, ""])
    return _table(
        request,
        scope,
        gettext("SRP FCs"),
        columns,
        lines,
        scope_text=pgettext("FleetOps", "Alliance"),
        details=[
            (
                gettext("Counting rule"),
                f"{SRP_RULE} " + gettext("Only claims for losses in FleetOps fleets are grouped by FC."),
            )
        ],
    )


_REPORT_KINDS = {srp_reports.TEXT: "text", srp_reports.COUNT: "int", srp_reports.ISK: "int", srp_reports.DATETIME: "datetime"}


def _build_srp_report(slug, title):
    """The builder of a report dataset: the table of the report page, written as a file (the total in the footer)."""
    report = srp_reports.REPORTS[slug]

    def build(request, scope):
        window = scope.window
        table = report.build(
            window,
            staff=srp_access.is_srp_staff(request.user),
            limit=exports.MAX_EXPORT_ROWS,
            split=scope.params.get("split", ""),
        )
        columns = [Column(col.key, str(col.label), _REPORT_KINDS[col.kind]) for col in table.columns]
        details = [(gettext("Counting rule"), str(SRP_RULE))]
        details.extend((gettext("Note"), note) for note in table.notes)
        if window.cut:
            details.append(
                (
                    gettext("Note"),
                    gettext("All time is cut to the last %(days)s days.") % {"days": MAX_REPORT_DAYS},
                )
            )
        built, filename = _table(
            request, scope, str(title), columns, table.rows, scope_text=pgettext("FleetOps", "Alliance"), details=details
        )
        if table.truncated and report.slug in ("rejections", "payments"):
            # A short list must not pass for the whole one: the note stands in the file, above the total.
            built.rows.append([exports.truncation_note()] + [None] * (len(columns) - 1))
        if table.footer:
            built.footer.append(table.footer)
        return built, filename

    return build


def _report_dataset(slug, title, *, staff_only=False):
    return Dataset(
        slug, title, _check_srp_report(slug, staff_only=staff_only), _build_srp_report(slug.removeprefix("srp-"), title), **SRP_AUDIT
    )


DATASETS = {
    dataset.slug: dataset
    for dataset in (
        Dataset("corporations", pgettext_lazy("FleetOps", "Corporations"), check_corporations, build_corporations),
        Dataset("members", gettext_lazy("Members"), check_members, build_members),
        Dataset("member-fleets", gettext_lazy("Member fleets"), check_member_fleets, build_member_fleets),
        Dataset("fcs", gettext_lazy("FCs"), check_fcs, build_fcs),
        Dataset("attendance", gettext_lazy("PAP history"), check_attendance, build_attendance),
        Dataset("incentives", gettext_lazy("FC incentives"), check_incentives, build_incentives),
        Dataset("srp-claims", gettext_lazy("SRP claims"), check_srp_claims, build_srp_claims, **SRP_AUDIT),
        Dataset("srp-members", gettext_lazy("SRP members"), check_srp_members, build_srp_members, **SRP_AUDIT),
        Dataset(
            "srp-corporations",
            gettext_lazy("SRP corporations"),
            check_srp_corporations,
            build_srp_corporations,
            **SRP_AUDIT,
        ),
        Dataset("srp-fcs", gettext_lazy("SRP FCs"), check_srp_fcs, build_srp_fcs, **SRP_AUDIT),
        Dataset("srp-payable", gettext_lazy("SRP to pay"), check_srp_payable, build_srp_payable, **SRP_AUDIT),
        _report_dataset("srp-trend", gettext_lazy("SRP trend")),
        _report_dataset("srp-categories", gettext_lazy("SRP categories")),
        _report_dataset("srp-ships", gettext_lazy("SRP ships")),
        _report_dataset("srp-regions", gettext_lazy("SRP regions")),
        _report_dataset("srp-reviewers", gettext_lazy("SRP reviewers"), staff_only=True),
        _report_dataset("srp-payers", gettext_lazy("SRP payers"), staff_only=True),
        _report_dataset("srp-rejections", gettext_lazy("SRP rejections")),
        _report_dataset("srp-payments", gettext_lazy("SRP payments")),
        _report_dataset("srp-limits", gettext_lazy("SRP limits")),
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
        dataset.audit_action,
        dataset.audit_object,
        scope.object_id,
        new_value={"format": fmt, "params": scope.params},
    )
    return export_response(table, fmt, filename)
