# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Members, inactive members, leaderboard and member details of the statistics."""

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import permission_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _

from fleetops.models import FleetOpsSettings
from fleetops.services import members as member_stats
from fleetops.services import population
from fleetops.services.history import month_retention_days
from fleetops.services.periods import month_ref
from fleetops.services.stats_access import (
    VIEW_ALL_STATS,
    VIEW_CORP_STATS,
    can_view_member,
    leaderboard_access,
    leaderboard_visible,
    own_corporation,
)
from fleetops.views import _can_view_all_fleets, _database_id, _period_render_context, _render, _year_month

# Filters the member tables keep when the month changes.
MEMBER_FILTERS = ("corporation", "status", "q", "sort")
MEMBER_STATUSES = member_stats.STATUS_FILTERS
# Columns of the members table that sort, and the sort a first click chooses (PAP and fleets: most first).
SORT_COLUMNS = (("name", "name"), ("pap", "-pap"), ("fleets", "-fleets"), ("corporation", "corporation"))


def _status_filter(request):
    status = request.GET.get("status", "")
    return status if status in MEMBER_STATUSES else "all"


def _sort_headers(effective):
    """How each sortable header shows the current sort (``aria-sort`` values) and what a click sorts by."""
    state, following = {}, {}
    for column, first in SORT_COLUMNS:
        descending = f"-{column}"
        if effective == column:
            state[column] = "ascending"
            following[column] = descending if descending in member_stats.SORTS else column
        elif effective == descending:
            state[column] = "descending"
            following[column] = column
        else:
            state[column] = "none"
            following[column] = first
    return state, following


def _members_page(request, *, scope, corporation_id, corporation_name):
    year, month = _year_month(request)
    status = _status_filter(request)
    query = member_stats.clean_query(request.GET.get("q", ""))
    sort = request.GET.get("sort", "")
    if sort not in member_stats.SORTS:
        sort = ""
    settings_obj = FleetOpsSettings.get_solo()
    corporations = member_stats.scope_corporations(settings_obj) if scope == "alliance" else []
    if scope == "alliance" and corporation_id is not None:
        corporation_name = dict(corporations).get(corporation_id, "")

    table = member_stats.member_rows(
        year, month, corporation_id=corporation_id, status=status, query=query, sort=sort or None,
        settings_obj=settings_obj,
    )
    paginator = Paginator(table.rows, member_stats.PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    page_rows = list(page.object_list)
    # The latest PAP is looked up for the rows of this page only.
    member_stats.fill_last_pap(page_rows)
    shown_types, has_other = member_stats.type_columns(table)
    items = []
    for row in page_rows:
        cells = [row.by_fleet_type.get(fleet_type.pk, 0) for fleet_type in shown_types]
        items.append(
            {"row": row, "cells": cells, "other": row.pap - sum(cells), "is_viewer": row.user_id == request.user.pk}
        )
    effective_sort = sort or ("pap" if status == "inactive" else "-pap")
    sort_state, sort_next = _sort_headers(effective_sort)
    configured = scope == "own_corporation" and not table.counts["all"] and population.scope_alliances()[0]
    return _render(
        request,
        "fleetops/statistics/members.html",
        _period_render_context(
            year,
            month,
            request=request,
            preserve=MEMBER_FILTERS if scope == "alliance" else ("status", "q", "sort"),
            scope=scope,
            status=status,
            title=_("Inactive members") if status == "inactive" else _("Members"),
            active_tab="inactive" if status == "inactive" else "members",
            own_corporation=scope == "own_corporation",
            tab_corporation_id=None if scope == "own_corporation" else corporation_id,
            corporation_id=corporation_id,
            corporation_name=corporation_name,
            corporations=corporations,
            table=table,
            target=table.target,
            counts=table.counts,
            items=items,
            page_obj=page,
            page_range=list(paginator.get_elided_page_range(page.number, on_each_side=2, on_ends=1)),
            ellipsis=Paginator.ELLIPSIS,
            shown_types=shown_types,
            has_other=has_other,
            query=query,
            sort=sort,
            sort_state=sort_state,
            sort_next=sort_next,
            show_corporation_column=corporation_id is None,
            # A corporation page names its corporation in the export; the alliance page passes ?corporation= on.
            export_params={} if scope == "alliance" else {"corporation": corporation_id},
            outside_member_alliances=bool(configured),
            retention_days=month_retention_days(table.month, settings_obj),
        ),
    )


@permission_required("fleetops.view_all_stats", raise_exception=True)
def alliance_members_view(request):
    """Every member of the alliance (view_all_stats); ``?corporation=`` narrows the list."""
    corporation_id = _database_id(request.GET.get("corporation", ""))
    return _members_page(request, scope="alliance", corporation_id=corporation_id, corporation_name="")


def corporation_members_view(request, corporation_id=None):
    """The members of one corporation: the viewer's own (view_corp_stats) or any (view_all_stats)."""
    if corporation_id is None:
        if not request.user.has_perm(VIEW_CORP_STATS):
            raise PermissionDenied
        own = own_corporation(request.user)
        if own is None:
            raise Http404
        return _members_page(request, scope="own_corporation", corporation_id=own[0], corporation_name=own[1])
    if not request.user.has_perm(VIEW_ALL_STATS):
        raise PermissionDenied
    name = (
        population.member_profiles(corporation_id=corporation_id)
        .values_list("main_character__corporation_name", flat=True)
        .first()
    )
    if name is None:
        raise Http404
    return _members_page(
        request,
        scope="corporation",
        corporation_id=corporation_id,
        corporation_name=name or _("Corporation %(id)s") % {"id": corporation_id},
    )


def _detail_check(user):
    """Whether ``user`` may open the page of a member shown in a list, without a query per row.

    The lists only hold members of the statistics population, so this is the rule of ``can_view_member``.
    """
    if user.has_perm(VIEW_ALL_STATS):
        return lambda row: True
    own = own_corporation(user) if user.has_perm(VIEW_CORP_STATS) else None
    return lambda row: row.user_id == user.pk or (own is not None and row.corporation_id == own[0])


def _board(request, board, title, can_open):
    return {
        "board": board,
        "title": title,
        "items": [
            {"row": row, "link": can_open(row), "is_viewer": row.user_id == request.user.pk} for row in board.rows
        ],
        "viewer_link": board.viewer_row is not None and can_open(board.viewer_row),
    }


@permission_required("fleetops.basic_access", raise_exception=True)
def member_detail_view(request, user_id):
    """One member's month: themselves (their own page), a corporation manager's members, or anyone."""
    member = get_object_or_404(get_user_model().objects.select_related("profile__main_character"), pk=user_id)
    year, month = _year_month(request)
    if member.pk == request.user.pk:
        return redirect(f"{reverse('fleetops:my_statistics')}?year={year}&month={month}")
    if not can_view_member(request.user, member):
        raise Http404
    settings_obj = FleetOpsSettings.get_solo()
    detail = member_stats.member_detail(member, year, month, settings_obj=settings_obj)
    if detail is None:
        raise Http404
    back = "alliance_members" if request.user.has_perm(VIEW_ALL_STATS) else "corporation_members"
    return _render(
        request,
        "fleetops/statistics/member_detail.html",
        _period_render_context(
            year,
            month,
            request=request,
            member=member,
            detail=detail,
            row=detail.row,
            back_url=f"{reverse(f'fleetops:{back}')}?year={year}&month={month}",
            # Fleet pages only open for the viewers of all fleets (or the member, who has their own page).
            can_open_fleets=_can_view_all_fleets(request.user),
            export_params={"user": member.pk},
            retention_days=month_retention_days(detail.month, settings_obj),
        ),
    )


@permission_required("fleetops.basic_access", raise_exception=True)
def leaderboard_view(request):
    """The PAP leaderboard of the alliance and of one corporation, as far as the settings let the viewer see it."""
    if not leaderboard_visible(request.user):
        raise PermissionDenied
    access = leaderboard_access(request.user)
    own = access["own_corporation"]
    # Only viewers of every corporation choose the corporation; everybody else sees their own.
    corporation_id = own[0] if own else None
    if access["any_corporation"]:
        corporation_id = _database_id(request.GET.get("corporation", "")) or corporation_id
    year, month = _year_month(request)
    settings_obj = FleetOpsSettings.get_solo()
    corporations = member_stats.scope_corporations(settings_obj) if access["any_corporation"] else []
    can_open = _detail_check(request.user)
    boards = []
    if access["alliance"]:
        board = member_stats.leaderboard(year, month, viewer=request.user, settings_obj=settings_obj)
        boards.append(_board(request, board, _("Alliance: top %(size)s") % {"size": board.size}, can_open))
    if corporation_id is not None:
        board = member_stats.leaderboard(
            year, month, corporation_id=corporation_id, viewer=request.user, settings_obj=settings_obj
        )
        boards.append(
            _board(
                request,
                board,
                _("%(scope)s: top %(size)s") % {"scope": board.scope_label, "size": board.size},
                can_open,
            )
        )
    # The corporation tabs belong to the statistics managers; their by-id pages need view_all_stats.
    managers = request.user.has_perm(VIEW_ALL_STATS) or request.user.has_perm(VIEW_CORP_STATS)
    tab_corporation_id = corporation_id if request.user.has_perm(VIEW_ALL_STATS) else None
    return _render(
        request,
        "fleetops/statistics/leaderboard.html",
        _period_render_context(
            year,
            month,
            request=request,
            preserve=("corporation",),
            access=access,
            corporation_id=corporation_id,
            corporations=corporations,
            boards=boards,
            active_tab="leaderboard",
            show_tabs=managers and corporation_id is not None,
            tab_corporation_id=tab_corporation_id,
            retention_days=month_retention_days(month_ref(year, month), settings_obj),
        ),
    )
