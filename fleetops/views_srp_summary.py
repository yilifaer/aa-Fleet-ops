# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FleetOps SRP: summaries per alliance, corporation and FC, and opening or closing a fleet's SRP link."""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from fleetops.decorators import fleetops_permission_required
from fleetops.models import SrpClaim, SrpCorporationLimit, SrpFleetLink
from fleetops.services import population
from fleetops.services.stats_access import BASIC_ACCESS, VIEW_ALL_STATS, VIEW_CORP_STATS, own_corporation
from fleetops.srp import access, summaries
from fleetops.srp.access import MANAGE_SRP
from fleetops.srp.links import set_link_open
from fleetops.views import _period_render_context, _year_month
from fleetops.views_errors import OWN_CORPORATION_STATISTICS_REASON, OWN_FLEETS_SUMMARY_REASON, denied, not_found
from fleetops.views_srp import srp_render

TEMPLATE = "fleetops/srp/summary.html"


def _corporation_name(corporation_id):
    """The name of a corporation SRP knows about, or None: it has claims, members or a limit of its own."""
    name = SrpClaim.objects.filter(corporation_id=corporation_id).order_by("-submitted_at").values_list(
        "corporation_name", flat=True
    ).first()
    if name is None:
        name = population.member_profiles(corporation_id=corporation_id).values_list(
            "main_character__corporation_name", flat=True
        ).first()
    if name is None:
        name = SrpCorporationLimit.objects.filter(corporation_id=corporation_id).values_list(
            "corporation_name", flat=True
        ).first()
    if name is None:
        return None
    return name or str(corporation_id)


def _display_name(user) -> str:
    main = getattr(getattr(user, "profile", None), "main_character", None)
    return (main.character_name if main is not None else "") or user.get_username()


def _page(request, tab, year, month, **context):
    return srp_render(
        request,
        TEMPLATE,
        _period_render_context(
            year, month, request=request, tab=tab, summary_access=access.summary_access(request.user), **context
        ),
    )


@require_GET
@fleetops_permission_required((MANAGE_SRP, VIEW_ALL_STATS), any_perm=True)
def alliance_summary(request):
    """The whole alliance: totals, in and out of fleet, the ships lost most and the other summaries."""
    year, month = _year_month(request)
    return _page(
        request,
        "alliance",
        year,
        month,
        summary=summaries.alliance(year, month),
        corporation_rows=summaries.by_corporation(year, month),
        fc_rows=summaries.by_fc(year, month),
        can_export=True,
    )


@require_GET
@fleetops_permission_required((VIEW_CORP_STATS, MANAGE_SRP, VIEW_ALL_STATS), any_perm=True)
def corporation_summary(request, corporation_id=None):
    """One corporation's summary and its members' claims.

    Without an ID it is the corporation of the user's main (``view_corp_stats`` is enough). With an ID it is
    any corporation, for SRP managers and ``view_all_stats``; a corporation SRP has never heard of is a 404.
    A corporation manager's own corporation by ID is their own page; any other ID is a 404 with the reason.
    """
    if corporation_id is None:
        corporation = own_corporation(request.user)
        if corporation is None:
            raise Http404
        corporation_id, name = corporation
        own = True
    elif not access.sees_everything(request.user):
        # A corporation manager opens their own corporation by ID too; every other ID answers the same 404.
        corporation = own_corporation(request.user)
        if corporation is None or corporation[0] != corporation_id or not request.user.has_perm(VIEW_CORP_STATS):
            return not_found(request, OWN_CORPORATION_STATISTICS_REASON)
        corporation_id, name = corporation
        own = True
    else:
        name = _corporation_name(corporation_id)
        if name is None:
            raise Http404
        own = False
    year, month = _year_month(request)
    row = summaries.corporation_row(corporation_id, year, month, name=name)
    return _page(
        request,
        "corporation",
        year,
        month,
        corporation_id=corporation_id,
        corporation_name=row.corporation_name or name,
        own_corporation=own,
        corporation=row,
        member_rows=summaries.by_member(year, month, corporation_id=corporation_id),
        claims=summaries.claims_in_month(year, month, corporation_id=corporation_id),
        claims_limit=summaries.LIST_LIMIT,
        # The files hold what the page shows: this corporation's members and claims.
        can_export=True,
        export_corporation={"corporation": corporation_id},
    )


@require_GET
@fleetops_permission_required(BASIC_ACCESS)
def fc_summary(request, user_id=None):
    """The claims and totals of the fleets one FC led.

    Without an ID it is the user's own page, for FCs, for anyone who led a fleet with an SRP link and for SRP
    managers. With an ID it is any FC, for SRP managers and ``view_all_stats``; a user that does not exist is
    a 404. Everybody else may use their own ID; any other ID is a 404 with the reason.
    """
    everything = access.sees_everything(request.user)
    if not everything and user_id is not None and user_id != request.user.pk:
        # The same answer whether the user exists or not.
        return not_found(request, OWN_FLEETS_SUMMARY_REASON)
    if user_id is None or not everything:
        if not (everything or access.leads_fleets(request.user)):
            return denied(request, OWN_FLEETS_SUMMARY_REASON)
        fc_user, own = request.user, True
    else:
        fc_user = get_object_or_404(get_user_model().objects.select_related("profile__main_character"), pk=user_id)
        own = fc_user.pk == request.user.pk
    year, month = _year_month(request)
    return _page(
        request,
        "fc",
        year,
        month,
        fc_user=fc_user,
        fc_name=_display_name(fc_user),
        own_fc=own,
        totals=summaries.month_totals(
            year, month, scope=SrpClaim.Scope.IN_FLEET, base=SrpClaim.objects.filter(fc_user=fc_user)
        ),
        fleet_rows=summaries.fc_fleets(fc_user, year, month),
        claims=summaries.claims_in_month(year, month, fc_user_id=fc_user.pk),
        claims_limit=summaries.LIST_LIMIT,
        can_export=False,
    )


@require_POST
@fleetops_permission_required(MANAGE_SRP, page=False)
def link_toggle(request, code, is_open):
    """Open or close a fleet's SRP link; the fleet page shows the result."""
    link = get_object_or_404(SrpFleetLink.objects.select_related("operation"), code=code)
    was_open = link.is_open
    set_link_open(link, actor=request.user, is_open=is_open)
    if was_open == is_open:
        if is_open:
            messages.info(request, _("The SRP link was already open."))
        else:
            messages.info(request, _("The SRP link was already closed."))
    elif is_open:
        messages.success(request, _("SRP link opened. Members can submit losses through it again."))
    else:
        messages.success(
            request,
            _("SRP link closed. Members can no longer submit losses through it; claims already made stay."),
        )
    return redirect("fleetops:operation_detail", operation_uuid=link.operation.uuid)
