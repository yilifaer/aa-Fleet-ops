# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The SRP reports of a period as plain tables: one description feeds the report page and the files.

Each report builds a :class:`ReportTable` (columns, rows and a total line) for a :class:`~fleetops.services.periods.Period`.
The page formats the cells by column kind, the export turns the same table into a CSV or Excel file, so a file always
matches its page. The numbers come from :mod:`fleetops.srp.summaries`; a report adds no query of its own.

Who may open a report: ``staff_only`` ones (reviewers and payers) are for SRP staff alone, the others also for people
with ``view_all_stats``. The names of the people who decided on or paid a claim appear only for SRP staff.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from django.db.models import Count, Sum
from django.utils.translation import gettext, gettext_lazy, ngettext, pgettext_lazy

from fleetops.services.periods import Period
from fleetops.srp import access, display
from fleetops.srp import summaries as srp_summaries
from fleetops.srp.types import SrpTotals
from fleetops.texts import translate_stored

# Column kinds: ``text``, ``int`` (a count), ``isk`` (whole ISK), ``datetime``.
TEXT = "text"
COUNT = "int"
ISK = "isk"
DATETIME = "datetime"

# Rows of a list report (rejections, payments) on the page; the files take up to their own limit.
PAGE_LIST_LIMIT = 2000
TOTAL = pgettext_lazy("FleetOps", "Total")


@dataclass(frozen=True, slots=True)
class Col:
    key: str
    label: object
    kind: str = TEXT


@dataclass(slots=True)
class ReportTable:
    columns: list[Col]
    rows: list[list]
    footer: list | None = None
    notes: list[str] = field(default_factory=list)
    # The rows were cut at the limit of the report.
    truncated: bool = False
    # The key of the column drawn as a bar (a share of the largest value).
    bar: str | None = None


@dataclass(frozen=True, slots=True)
class Report:
    slug: str
    title: object
    dataset: str
    build: Callable[..., ReportTable]
    staff_only: bool = False
    # What the report counts, in one sentence under the table.
    rule: object = ""


def _month_label(year, month) -> str:
    return f"{year:04d}-{month:02d}"


def _total_cells(totals: SrpTotals) -> list:
    return [totals.claims, totals.requested, totals.approved, totals.paid, totals.average_payout]


def _total_cols() -> list[Col]:
    return [
        Col("claims", gettext_lazy("Claims"), COUNT),
        Col("requested", gettext_lazy("Requested (ISK)"), ISK),
        Col("approved", gettext_lazy("Approved (ISK)"), ISK),
        Col("paid", gettext_lazy("Paid (ISK)"), ISK),
        Col("average", gettext_lazy("Average payout (ISK)"), ISK),
    ]


def _sum(rows) -> SrpTotals:
    return srp_summaries.add_totals(*rows)


# --- Builders --------------------------------------------------------------------------------------------


def build_trend(period: Period, *, staff: bool = False, limit: int = 0, split: str = "") -> ReportTable:
    by_category = split == "category"
    rows = srp_summaries.trend(period.start, period.end, by_category=by_category)
    columns = [Col("month", pgettext_lazy("FleetOps", "Month"))]
    if by_category:
        columns.append(Col("category", gettext_lazy("Category")))
    columns += [
        Col("claims", gettext_lazy("Claims"), COUNT),
        Col("loss_value", gettext_lazy("Loss value (ISK)"), ISK),
        Col("requested", gettext_lazy("Requested (ISK)"), ISK),
        Col("approved", gettext_lazy("Approved (ISK)"), ISK),
        Col("paid", gettext_lazy("Paid (ISK)"), ISK),
        Col("rejected", pgettext_lazy("FleetOps", "Rejected"), COUNT),
        Col("average", gettext_lazy("Average payout (ISK)"), ISK),
    ]
    lines = []
    for row in rows:
        t = row.totals
        cells = [_month_label(row.year, row.month)]
        if by_category:
            cells.append(display.category_name_label(row.category_name) or str(gettext_lazy("Uncategorised")))
        cells += [t.claims, t.loss_value, t.requested, t.approved, t.paid, t.rejected, t.average_payout]
        lines.append(cells)
    total = _sum(row.totals for row in rows)
    footer = [str(TOTAL)] + ([""] if by_category else [])
    footer += [total.claims, total.loss_value, total.requested, total.approved, total.paid, total.rejected, total.average_payout]
    return ReportTable(columns, lines, footer, bar="approved")


def build_categories(period: Period, *, staff: bool = False, limit: int = 0, split: str = "") -> ReportTable:
    rows = srp_summaries.by_category(period.start, period.end)
    columns = [
        Col("category", gettext_lazy("Category")),
        Col("members", gettext_lazy("People"), COUNT),
        *_total_cols(),
        # Approved plus paid ISK of the category in the period (the same figure as "Approved (ISK)", named for caps).
        Col("used", gettext_lazy("Used (ISK)"), ISK),
        Col("capped", gettext_lazy("Cut by a cap or limit"), COUNT),
        Col("personal_cap", gettext_lazy("Personal monthly cap (ISK)"), ISK),
        Col("corporation_cap", gettext_lazy("Corporation monthly cap (ISK)"), ISK),
        Col("remaining", gettext_lazy("Remaining (ISK)"), ISK),
    ]
    # Caps are monthly: what is left of a category-wide cap is only known for exactly one calendar month.
    month = period.is_calendar_month
    lines = [
        [
            display.category_name_label(row.category_name) or str(gettext_lazy("Uncategorised")),
            row.members,
            *_total_cells(row.totals),
            row.totals.approved,
            row.capped,
            row.personal_cap,
            row.corporation_cap,
            # The cap applies per corporation: what is left for the corporation that used most of it.
            max(row.corporation_cap - (row.corporation_peak or 0), 0) if month and row.corporation_cap is not None else None,
        ]
        for row in rows
    ]
    total = _sum(row.totals for row in rows)
    footer = [str(TOTAL), None, *_total_cells(total), total.approved, sum(row.capped for row in rows), None, None, None]
    notes = []
    if not month and any(row.corporation_cap is not None for row in rows):
        notes.append(
            gettext("Remaining is shown only when the period is exactly one calendar month, because the caps are monthly.")
        )
    if month and any(row.corporation_cap is not None for row in rows):
        notes.append(
            gettext(
                "The corporation cap applies to each corporation. Remaining is what is left for the corporation that "
                "used most of it; Used is the sum of all corporations."
            )
        )
    return ReportTable(columns, lines, footer, notes=notes, bar="approved")


def build_ships(period: Period, *, staff: bool = False, limit: int = 0, split: str = "") -> ReportTable:
    result = srp_summaries.by_ship(period.start, period.end)
    columns = [
        Col("ship", gettext_lazy("Ship")),
        Col("losses", gettext_lazy("Losses"), COUNT),
        Col("approved_claims", gettext_lazy("Approved claims"), COUNT),
        Col("rejected", pgettext_lazy("FleetOps", "Rejected"), COUNT),
        Col("approved", gettext_lazy("Approved (ISK)"), ISK),
        Col("average", gettext_lazy("Average payout (ISK)"), ISK),
    ]
    lines = [
        [
            row.ship_type_name or row.ship_type_id,
            row.losses,
            row.approved_count,
            row.rejected,
            row.approved,
            row.average_payout,
        ]
        for row in result.rows
    ]
    # The total line covers all ships, also the ones the cut leaves out.
    total = srp_summaries.period_totals(period.start, period.end)
    footer = [str(TOTAL), total.claims, total.approved_count, total.rejected, total.approved, total.average_payout]
    notes = []
    if result.truncated:
        notes.append(
            gettext("Only the %(count)s most lost ships are listed. Choose a shorter period to see the others.")
            % {"count": srp_summaries.SHIP_REPORT_LIMIT}
        )
    return ReportTable(columns, lines, footer, notes, truncated=result.truncated, bar="approved")


def build_members(period: Period, *, staff: bool = False, limit: int = 0, split: str = "") -> ReportTable:
    rows = srp_summaries.by_member(period.start, period.end)
    columns = [Col("main", pgettext_lazy("FleetOps", "Main character")), Col("corporation", pgettext_lazy("FleetOps", "Corporation")), *_total_cols()]
    lines = [[row.main_character_name or str(gettext_lazy("Unknown")), row.corporation_name, *_total_cells(row.totals)] for row in rows]
    total = _sum(row.totals for row in rows)
    return ReportTable(
        columns,
        lines,
        [str(TOTAL), "", *_total_cells(total)],
        [gettext("All characters of a player are added up under the main character.")],
        bar="approved",
    )


def build_corporations(period: Period, *, staff: bool = False, limit: int = 0, split: str = "") -> ReportTable:
    rows = srp_summaries.by_corporation(period.start, period.end)
    columns = [
        Col("corporation", pgettext_lazy("FleetOps", "Corporation")),
        Col("ticker", pgettext_lazy("FleetOps", "Ticker")),
        Col("members", gettext_lazy("People"), COUNT),
        *_total_cols(),
        Col("cap", gettext_lazy("Monthly cap (ISK)"), ISK),
    ]
    lines = [
        [
            row.corporation_name or str(gettext_lazy("Unknown corporation")),
            row.corporation_ticker,
            row.members,
            *_total_cells(row.totals),
            row.cap,
        ]
        for row in rows
    ]
    total = _sum(row.totals for row in rows)
    return ReportTable(columns, lines, [str(TOTAL), "", None, *_total_cells(total), None], bar="approved")


def build_regions(period: Period, *, staff: bool = False, limit: int = 0, split: str = "") -> ReportTable:
    rows = srp_summaries.by_region(period.start, period.end)
    columns = [Col("region", gettext_lazy("Region")), *_total_cols()]
    lines = []
    for row in rows:
        if row.region_id is None:
            name = gettext("Unknown")
        else:
            name = row.region_name or gettext("Region %(id)s") % {"id": row.region_id}
        lines.append([name, *_total_cells(row.totals)])
    total = _sum(row.totals for row in rows)
    notes = []
    if any(row.region_id is None for row in rows):
        notes.append(
            gettext(
                "Unknown: losses whose region is not stored yet. Older killmails get their region when the "
                "fleetops_srp_backfill_killmails command runs."
            )
        )
    return ReportTable(columns, lines, [str(TOTAL), *_total_cells(total)], notes, bar="approved")


def fleet_type_text(fleet_types: dict) -> str:
    return "; ".join(f"{name or gettext('Other')}: {count}" for name, count in fleet_types.items())


def build_fcs(period: Period, *, staff: bool = False, limit: int = 0, split: str = "") -> ReportTable:
    rows = srp_summaries.by_fc(period.start, period.end)
    columns = [
        Col("fc", gettext_lazy("FC (main)")),
        Col("fleets", gettext_lazy("Fleets"), COUNT),
        *_total_cols(),
        Col("fleet_types", gettext_lazy("Fleet types (fleets)")),
    ]
    lines = [
        [row.fc_name or str(gettext_lazy("Unknown FC")), row.fleets, *_total_cells(row.totals), fleet_type_text(row.fleet_types)]
        for row in rows
    ]
    total = _sum(row.totals for row in rows)
    return ReportTable(
        columns,
        lines,
        [str(TOTAL), sum(row.fleets for row in rows), *_total_cells(total), ""],
        [gettext("Only claims for losses in FleetOps fleets are grouped by FC.")],
        bar="approved",
    )


def build_reviewers(period: Period, *, staff: bool = False, limit: int = 0, split: str = "") -> ReportTable:
    rows = srp_summaries.by_reviewer(period.start, period.end)
    columns = [
        Col("reviewer", gettext_lazy("Reviewer")),
        Col("approved_claims", gettext_lazy("Approved claims"), COUNT),
        Col("approved", gettext_lazy("Approved (ISK)"), ISK),
        Col("rejected", pgettext_lazy("FleetOps", "Rejected"), COUNT),
        Col("average", gettext_lazy("Average payout (ISK)"), ISK),
    ]
    lines = [
        [row.name, row.approved_count, row.approved, row.rejected, SrpTotals(approved=row.approved, approved_count=row.approved_count).average_payout]
        for row in rows
    ]
    approved_count = sum(row.approved_count for row in rows)
    approved = sum(row.approved for row in rows)
    footer = [
        str(TOTAL),
        approved_count,
        approved,
        sum(row.rejected for row in rows),
        SrpTotals(approved=approved, approved_count=approved_count).average_payout,
    ]
    return ReportTable(columns, lines, footer, bar="approved")


def build_payers(period: Period, *, staff: bool = False, limit: int = 0, split: str = "") -> ReportTable:
    rows = srp_summaries.by_payer(period.start, period.end)
    columns = [
        Col("payer", gettext_lazy("Paid by")),
        Col("claims", gettext_lazy("Paid claims"), COUNT),
        Col("batches", gettext_lazy("Payment batches"), COUNT),
        Col("amount", gettext_lazy("Paid (ISK)"), ISK),
        Col("average", gettext_lazy("Average payout (ISK)"), ISK),
    ]

    def average(claims, amount):
        return SrpTotals(approved=amount, approved_count=claims).average_payout

    lines = [[row.name, row.claims, row.batches, row.amount, average(row.claims, row.amount)] for row in rows]
    claims = sum(row.claims for row in rows)
    amount = sum(row.amount for row in rows)
    footer = [str(TOTAL), claims, sum(row.batches for row in rows), amount, average(claims, amount)]
    return ReportTable(columns, lines, footer, [gettext("Payment batches are counted for each payer; a batch of two payers counts for both.")], bar="amount")


def _person(claim) -> str:
    return claim.main_character_name or claim.character_name


def _by(user) -> str:
    main = getattr(getattr(user, "profile", None), "main_character", None)
    return (main.character_name if main else "") or user.get_username()


def build_rejections(period: Period, *, staff: bool = False, limit: int = PAGE_LIST_LIMIT, split: str = "") -> ReportTable:
    queryset = srp_summaries.rejections(period.start, period.end)
    claims = list(queryset[: limit + 1])
    truncated = len(claims) > limit
    claims = claims[:limit]
    columns = [
        Col("loss", gettext_lazy("Loss time (EVE)"), DATETIME),
        Col("claim", gettext_lazy("Claim")),
        Col("pilot", gettext_lazy("Pilot (main)")),
        Col("ship", gettext_lazy("Ship")),
        Col("decided_by", gettext_lazy("Rejected by")),
        Col("reason", gettext_lazy("Reason")),
    ]
    lines = []
    for claim in claims:
        if claim.auto_rejected:
            decider = gettext("Automatic")
        elif claim.decided_by_id:
            decider = _by(claim.decided_by) if staff else ""
        elif claim.source == claim.Source.AASRP:
            decider = gettext("aa-srp (unknown)") if staff else ""
        else:
            decider = ""
        lines.append(
            [
                claim.killmail_time,
                claim.reference,
                _person(claim),
                claim.ship_type_name or claim.ship_type_id,
                decider,
                translate_stored(claim.decision_reason),
            ]
        )
    # The total line covers the whole period, also the claims a cut leaves out of the list.
    count = queryset.count() if truncated else len(lines)
    footer = [str(TOTAL), ngettext("%(count)s claim", "%(count)s claims", count) % {"count": count}, "", "", "", ""]
    notes = [gettext("The reviewer is shown to SRP staff only.")] if not staff else []
    return ReportTable(columns, lines, footer, notes, truncated=truncated)


def build_payments(period: Period, *, staff: bool = False, limit: int = PAGE_LIST_LIMIT, split: str = "") -> ReportTable:
    queryset = srp_summaries.payments(period.start, period.end)
    claims = list(queryset[: limit + 1])
    truncated = len(claims) > limit
    claims = claims[:limit]
    columns = [
        Col("paid_at", gettext_lazy("Paid at (EVE)"), DATETIME),
        Col("batch", gettext_lazy("Payment batch")),
        Col("claim", gettext_lazy("Claim")),
        Col("pilot", gettext_lazy("Pilot (main)")),
        Col("ship", gettext_lazy("Ship")),
        Col("paid_by", gettext_lazy("Paid by")),
        Col("amount", gettext_lazy("Paid (ISK)"), ISK),
    ]
    lines = []
    for claim in claims:
        if not staff:
            payer = ""
        elif claim.paid_by_id:
            payer = _by(claim.paid_by)
        else:
            payer = gettext("aa-srp (unknown)") if claim.source == claim.Source.AASRP else ""
        lines.append(
            [
                claim.paid_at,
                claim.payment_batch.reference if claim.payment_batch_id else "",
                claim.reference,
                _person(claim),
                claim.ship_type_name or claim.ship_type_id,
                payer,
                claim.approved_amount,
            ]
        )
    if truncated:
        # The total line covers the whole period, also the claims a cut leaves out of the list.
        whole = queryset.order_by().aggregate(count=Count("pk"), amount=Sum("approved_amount"))
        count, amount = whole["count"] or 0, int(whole["amount"] or 0)
    else:
        count, amount = len(lines), sum(line[-1] or 0 for line in lines)
    footer = [str(TOTAL), "", count, "", "", "", amount]
    notes = [gettext("The payer is shown to SRP staff only.")] if not staff else []
    return ReportTable(columns, lines, footer, notes, truncated=truncated)


def build_limits(period: Period, *, staff: bool = False, limit: int = 0, split: str = "") -> ReportTable:
    rows = srp_summaries.limits(period.start, period.end)
    columns = [
        Col("month", pgettext_lazy("FleetOps", "Month")),
        Col("member", pgettext_lazy("FleetOps", "Main character")),
        Col("limit", gettext_lazy("Limit")),
        Col("used", gettext_lazy("Used (ISK)"), ISK),
        Col("cap", gettext_lazy("Monthly cap (ISK)"), ISK),
        Col("remaining", gettext_lazy("Remaining (ISK)"), ISK),
        Col("claims", gettext_lazy("Claims"), COUNT),
        Col("over_limit", gettext_lazy("Over the limit"), COUNT),
        Col("waived", gettext_lazy("Waived"), COUNT),
    ]
    lines = [
        [
            _month_label(row.year, row.month),
            row.member_name or str(gettext_lazy("Unknown")),
            display.category_name_label(row.limit_name) if row.category_id else str(gettext_lazy("Personal monthly limit")),
            row.used,
            row.cap,
            row.remaining,
            row.claims,
            row.over_limit,
            row.waived,
        ]
        for row in rows
    ]
    notes = [
        gettext(
            "One row for the personal monthly cap and one for each category with a personal monthly cap the "
            "member claimed in. Over the limit: claims whose amount a limit cut when it was calculated. "
            "Waived: approved claims whose last approval went over a limit with the reviewer's confirmation."
        ),
        gettext("A limit is monthly: a month the period only touches in part is counted whole."),
    ]
    return ReportTable(columns, lines, None, notes)


REPORTS = {
    report.slug: report
    for report in (
        Report("trend", gettext_lazy("Trend by month"), "srp-trend", build_trend),
        Report("categories", gettext_lazy("Categories"), "srp-categories", build_categories),
        Report("ships", gettext_lazy("Ships"), "srp-ships", build_ships),
        Report("members", gettext_lazy("Members"), "srp-members", build_members),
        Report("corporations", pgettext_lazy("FleetOps", "Corporations"), "srp-corporations", build_corporations),
        Report("regions", gettext_lazy("Regions"), "srp-regions", build_regions),
        Report("fcs", gettext_lazy("FCs"), "srp-fcs", build_fcs),
        Report("reviewers", gettext_lazy("Reviewers"), "srp-reviewers", build_reviewers, staff_only=True),
        Report("payers", gettext_lazy("Payers"), "srp-payers", build_payers, staff_only=True),
        Report("rejections", gettext_lazy("Rejections"), "srp-rejections", build_rejections),
        Report("payments", gettext_lazy("Payments"), "srp-payments", build_payments),
        Report("limits", gettext_lazy("Personal limits"), "srp-limits", build_limits),
    )
}
DEFAULT_REPORT = "trend"


def available(user) -> list[Report]:
    """The reports ``user`` may open: all for SRP staff, the others for people with ``view_all_stats``."""
    staff = access.is_srp_staff(user)
    return [report for report in REPORTS.values() if staff or not report.staff_only]
