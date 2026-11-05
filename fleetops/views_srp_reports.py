# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FleetOps SRP: the report page, one table for any period (trend, categories, ships, members and more)."""

from django.core.paginator import Paginator
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_GET

from fleetops.decorators import fleetops_permission_required
from fleetops.services.periods import PeriodError, default_period, format_eve, parse_period, period_query
from fleetops.services.stats_access import VIEW_ALL_STATS
from fleetops.srp import access, reports, summaries
from fleetops.srp.access import MANAGE_SRP, PAY_SRP, REVIEW_SRP
from fleetops.srp.display import format_isk
from fleetops.views_errors import SRP_STAFF_REPORT_REASON, denied
from fleetops.views_srp import srp_render

TEMPLATE = "fleetops/srp/reports.html"
PRESET_CHOICES = (
    ("this_month", gettext_lazy("This month")),
    ("last_month", gettext_lazy("Last month")),
    ("last_3", gettext_lazy("Last 3 months")),
    ("last_6", gettext_lazy("Last 6 months")),
    ("last_12", gettext_lazy("Last 12 months")),
    ("this_year", gettext_lazy("This year")),
    ("all", gettext_lazy("All time")),
)
ROWS_PER_PAGE = 50
MAX_QUERY = 100


def cell_text(kind, value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        # The label of a total line stands in any column.
        return value
    if kind == reports.ISK:
        return format_isk(value)
    if kind == reports.DATETIME:
        return format_eve(value)
    return str(value)


def _display_rows(table, query):
    """The rows as cells for the page, narrowed by the search words; each cell has its text, kind and bar share."""
    columns = table.columns
    rows = []
    for line in table.rows:
        cells = [(column.kind, cell_text(column.kind, value), value) for column, value in zip(columns, line)]
        if query and not any(query in text.casefold() for kind, text, _value in cells if kind == reports.TEXT):
            continue
        rows.append(cells)
    bar_index = next((i for i, column in enumerate(columns) if column.key == table.bar), None)
    top = max((line[bar_index] or 0 for line in table.rows), default=0) if bar_index is not None else 0
    result = []
    for cells in rows:
        share = None
        if bar_index is not None and top > 0:
            share = (cells[bar_index][2] or 0) * 100 / top
        result.append(
            {
                "cells": [{"kind": kind, "text": text} for kind, text, _value in cells],
                "bar": share,
            }
        )
    return result, bar_index


@require_GET
@fleetops_permission_required((MANAGE_SRP, REVIEW_SRP, PAY_SRP, VIEW_ALL_STATS), any_perm=True)
def srp_reports(request):
    """The report page. ``report`` picks the report, ``preset`` or ``from`` and ``to`` the period (EVE days, both
    included; the last six months without them), ``q`` searches the table. The reviewer and payer reports are for SRP
    staff only; a period that cannot be read is named and the default period is shown."""
    user = request.user
    staff = access.is_srp_staff(user)
    choices = reports.available(user)
    slug = request.GET.get("report") or reports.DEFAULT_REPORT
    report = reports.REPORTS.get(slug)
    if report is None:
        report = reports.REPORTS[reports.DEFAULT_REPORT]
    elif report.staff_only and not staff:
        return denied(request, SRP_STAFF_REPORT_REASON)

    period_error = ""
    try:
        period = parse_period(request.GET, earliest=summaries.earliest_claim_time())
    except PeriodError as exc:
        period_error = str(exc)
        period = default_period()

    split = "category" if report.slug == "trend" and request.GET.get("split") == "category" else ""
    table = report.build(period, staff=staff, split=split)
    query = (request.GET.get("q") or "")[:MAX_QUERY].strip()
    rows, bar_index = _display_rows(table, query.casefold())
    page = Paginator(rows, ROWS_PER_PAGE).get_page(request.GET.get("page"))
    paginator = page.paginator
    footer = (
        [{"kind": column.kind, "text": cell_text(column.kind, value)} for column, value in zip(table.columns, table.footer)]
        if table.footer
        else None
    )
    # The links name the period shown (a preset only for "all", whose cut the file notes again); the page's own search
    # and page number do not belong in a file.
    export_params = {"preset": "", "from": "", "to": "", "q": "", "report": "", **period_query(period), "split": split}
    return srp_render(
        request,
        TEMPLATE,
        {
            "report": report,
            "report_choices": choices,
            "columns": table.columns,
            "bar_index": bar_index,
            "page": page,
            "rows": page.object_list,
            "row_count": paginator.count,
            "footer": footer,
            "notes": table.notes,
            "truncated": table.truncated,
            "period": period,
            "period_error": period_error,
            "presets": PRESET_CHOICES,
            "totals": summaries.period_totals(period.start, period.end),
            "query": query,
            "split": split,
            "export_params": export_params,
            "page_range": list(paginator.get_elided_page_range(page.number, on_each_side=2, on_ends=1)),
            "ellipsis": Paginator.ELLIPSIS,
            "summary_access": access.summary_access(user),
            "all_months": period.is_calendar_month,
        },
    )
