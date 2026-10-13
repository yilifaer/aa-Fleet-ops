"""Statistics pages: personal, corporation, alliance and FC figures."""

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import permission_required
from django.http import Http404
from django.shortcuts import get_object_or_404

from fleetops.services import population
from fleetops.services.assessment import corporation_status, member_status, month_target
from fleetops.services.history import configured_alliance_ids, month_retention_days
from fleetops.services.periods import month_ref
from fleetops.services.statistics import (
    attendance_queryset,
    corporation_overview,
    corporation_statistics,
    fc_operations_queryset,
    fc_rows,
    fc_statistics,
    member_rank,
    member_statistics,
)
from fleetops.services.stats_access import leaderboard_access, own_corporation
from fleetops.views import _can_view_all_fleets, _period_render_context, _render, _year_month

# What the corporation overview keeps when the month changes.
OVERVIEW_FILTERS = ("sort",)
# The columns of the corporation overview that can be sorted, and the order a first click gives.
OVERVIEW_COLUMNS = {"name": "name", "mains": "-mains", "total": "-total", "average": "-average", "status": "status"}


def _rank_card(user, year, month, total):
    """Where the user stands among the members, when they may see a leaderboard.

    The alliance board comes first; a user who may only see their own corporation's board is ranked there.
    """
    if total <= 0 or month_ref(year, month).is_future:
        return None
    access = leaderboard_access(user)
    if access["alliance"]:
        rank, ranked = member_rank(user, year, month)
        scope = "alliance"
    elif access["own_corporation"] is not None:
        rank, ranked = member_rank(user, year, month, corporation_id=access["own_corporation"][0])
        scope = "corporation"
    else:
        return None
    if rank is None:
        return None
    return {"rank": rank, "of": ranked, "scope": scope}


@permission_required("fleetops.basic_access", raise_exception=True)
def my_statistics_view(request):
    from fleetops.views_pap import live_pap_rows

    year, month = _year_month(request)
    stats = member_statistics(request.user, year, month)
    target = month_target(year, month)
    return _render(
        request,
        "fleetops/my_statistics.html",
        _period_render_context(
            year,
            month,
            stats=stats,
            live_pap_rows=live_pap_rows(request.user),
            minimum=target.minimum,
            minimum_missing=max(target.minimum - stats["total"], 0),
            minimum_status=member_status(stats["total"], target, month_ref(year, month)),
            rank_card=_rank_card(request.user, year, month, stats["total"]),
        ),
    )


def _corporation_page(request, stats, *, year, month, corporation_id, corporation_name, own, tab_corporation_id):
    stats["corporation_name"] = corporation_name
    return _render(
        request,
        "fleetops/corporation_statistics.html",
        _period_render_context(
            year,
            month,
            stats=stats,
            corporation_name=corporation_name,
            own_corporation=own,
            active_tab="overview",
            tab_corporation_id=tab_corporation_id,
            export_params={"corporation": corporation_id},
            alliance_filter_configured=bool(configured_alliance_ids()),
            retention_days=month_retention_days(month_ref(year, month)),
        ),
    )


@permission_required("fleetops.view_corp_stats", raise_exception=True)
def corporation_statistics_view(request):
    corporation = own_corporation(request.user)
    if corporation is None:
        raise Http404
    year, month = _year_month(request)
    stats = corporation_statistics(corporation[0], year, month)
    return _corporation_page(
        request,
        stats,
        year=year,
        month=month,
        corporation_id=corporation[0],
        corporation_name=corporation[1],
        own=True,
        tab_corporation_id=None,
    )


def _corporation_name(corporation_id, year, month):
    """The corporation's name: its current members' corporation, else the name stored with its PAP of the month.

    None when the corporation has neither members in scope nor PAP in the month.
    """
    names = list(
        population.member_profiles(corporation_id=corporation_id).values_list(
            "main_character__corporation_name", flat=True
        )[:1]
    )
    if not names:
        names = list(
            attendance_queryset(year, month)
            .filter(corporation_id=corporation_id)
            .order_by("-created_at")
            .values_list("corporation_name", flat=True)[:1]
        )
    if not names:
        return None
    return names[0] or str(corporation_id)


@permission_required("fleetops.view_all_stats", raise_exception=True)
def corporation_statistics_detail_view(request, corporation_id):
    year, month = _year_month(request)
    name = _corporation_name(corporation_id, year, month)
    if name is None:
        raise Http404
    stats = corporation_statistics(corporation_id, year, month)
    return _corporation_page(
        request,
        stats,
        year=year,
        month=month,
        corporation_id=corporation_id,
        corporation_name=name,
        own=False,
        tab_corporation_id=corporation_id,
    )


def _overview_table(overview):
    """The overview rows for the template: PAP per fleet type in column order and the PAP the target asks for."""
    average = overview.target.average
    table = []
    for row in [*overview.rows, *([overview.other] if overview.other else [])]:
        table.append(
            {
                "row": row,
                "types": [(fleet_type, row.by_fleet_type.get(fleet_type.pk, 0)) for fleet_type in overview.fleet_types],
                "target_pap": average * row.mains if average is not None and not row.is_other else None,
            }
        )
    return table


def _sort_links(current):
    """The sort value each column header sets: the first order on a click, the opposite order on the sorted column."""
    return {
        column: (first[1:] if first.startswith("-") else f"-{first}") if current == first else first
        for column, first in OVERVIEW_COLUMNS.items()
    }


@permission_required("fleetops.view_all_stats", raise_exception=True)
def all_corporation_statistics_view(request):
    year, month = _year_month(request)
    overview = corporation_overview(year, month, sort=request.GET.get("sort", ""))
    return _render(
        request,
        "fleetops/all_corporation_statistics.html",
        _period_render_context(
            year,
            month,
            request=request,
            preserve=OVERVIEW_FILTERS,
            overview=overview,
            table=_overview_table(overview),
            # The average per main of all members against the monthly target, with the corporation rule.
            alliance_status=corporation_status(overview.member_pap, overview.mains, overview.target, overview.month),
            below_total=sum(row.mains_below_minimum for row in overview.rows),
            not_counted_total=sum(overview.not_counted.values()),
            sort=overview.sort,
            sort_links=_sort_links(overview.sort),
            retention_days=month_retention_days(overview.month),
        ),
    )


@permission_required("fleetops.view_all_stats", raise_exception=True)
def all_fc_statistics_view(request):
    year, month = _year_month(request)
    rows = fc_rows(year, month)
    return _render(
        request,
        "fleetops/all_fc_statistics.html",
        _period_render_context(
            year,
            month,
            rows=rows,
            total_points=sum((row.points for row in rows), 0),
            # A fleet credited to its FC and to a special role is one fleet, however many rows it is in.
            total_fleets=len(frozenset().union(*(row.operation_ids for row in rows))),
        ),
    )


@permission_required("fleetops.basic_access", raise_exception=True)
def fc_statistics_detail_view(request, user_id):
    if request.user.pk != user_id and not request.user.has_perm("fleetops.view_all_stats"):
        raise Http404
    user = get_object_or_404(get_user_model().objects.select_related("profile__main_character"), pk=user_id)
    year, month = _year_month(request)
    stat = fc_statistics(user, year, month)
    operations = fc_operations_queryset(user, year, month).select_related("fleet_type").order_by("-started_at")
    return _render(
        request,
        "fleetops/fc_statistics_detail.html",
        _period_render_context(
            year,
            month,
            stat=stat,
            fc_user=user,
            operations=operations,
            can_open_fleets=user.pk == request.user.pk or _can_view_all_fleets(request.user),
        ),
    )
