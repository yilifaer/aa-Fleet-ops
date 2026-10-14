# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""PAP history pages: personal, corporation and alliance."""

from django.contrib.auth.decorators import permission_required
from django.core.paginator import Paginator
from django.db.models import Sum
from django.http import Http404
from django.utils.translation import gettext as _
from django.utils.translation import pgettext

from fleetops.services.attendance_history import history_month, history_queryset, retention_days
from fleetops.services.history import retention_cutoff
from fleetops.services.periods import current_year_month, month_bounds
from fleetops.services.stats_access import own_corporation
from fleetops.views import _can_view_all_fleets, _neighbour_period, _period_choices, _render

HISTORY_PAGE_SIZE = 200


def _month_filter_context(month, days):
    """The month selector of a history page; ``month`` is a ``(year, month)`` pair or None for the whole window.

    ``year`` and ``month`` are only in the context while a month is chosen: the export links take the
    month from the page's query string and from these two names, and without a month they export everything.
    """
    year, number = month if month else current_year_month()
    context = _period_choices(year, number)
    # The corporation tabs link to the month of the other pages; without a chosen month that is the current one.
    context.update(tab_year=year, tab_month=number)
    if month is None:
        context.update(selected_month=None, previous_period=None, next_period=None, period_label="")
        return context
    start = month_bounds(*month)[0]
    cutoff = retention_cutoff(days)
    context.update(
        year=year,
        month=number,
        previous_period=_neighbour_period(year, number, -1),
        next_period=_neighbour_period(year, number, 1),
        period_label=f"{year:04d}-{number:02d}",
        # PAP older than the retention window is deleted, so the first months of the window may be partial.
        month_may_be_incomplete=cutoff is not None and start < cutoff,
    )
    return context


def _history_context(request, scope, queryset, title, scope_label, days, month):
    page = Paginator(queryset, HISTORY_PAGE_SIZE).get_page(request.GET.get("page"))
    context = {
        "rows": page.object_list,
        "page": page,
        "title": title,
        "scope_label": scope_label,
        "total": queryset.aggregate(total=Sum("attendance_value"))["total"] or 0,
        "retention_days": days,
        "can_open_fleets": _can_view_all_fleets(request.user),
        "history_scope": scope,
        "export_params": {"scope": scope},
    }
    context.update(_month_filter_context(month, days))
    return context


def _history_page(request, scope, title, scope_label, **filters):
    month = history_month(request.GET)
    days = retention_days()
    queryset = history_queryset(scope, month=month, **filters)
    return _render(
        request,
        "fleetops/attendance_history.html",
        _history_context(request, scope, queryset, title, scope_label, days, month),
    )


@permission_required("fleetops.basic_access", raise_exception=True)
def attendance_history_me(request):
    personal = pgettext("PAP history scope", "Personal")
    return _history_page(request, "me", _("My PAP History"), personal, user=request.user)


@permission_required("fleetops.view_corp_stats", raise_exception=True)
def attendance_history_corporation(request):
    corporation = own_corporation(request.user)
    if corporation is None:
        raise Http404
    corporation_id, corporation_name = corporation
    return _history_page(
        request,
        "corporation",
        _("%(name)s PAP History") % {"name": corporation_name or pgettext("FleetOps", "Corporation")},
        pgettext("FleetOps", "Corporation"),
        corporation_id=corporation_id,
    )


@permission_required("fleetops.view_all_stats", raise_exception=True)
def attendance_history_alliance(request):
    return _history_page(request, "alliance", _("Alliance PAP History"), pgettext("FleetOps", "Alliance"))
