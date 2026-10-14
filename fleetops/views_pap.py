# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fleet page parts and actions of the PAP rules: recalculation, statistics inclusion, live status."""

from allianceauth.authentication.decorators import permissions_required
from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from fleetops import texts
from fleetops.forms import EndFleetForm, FleetAttendanceMultiplierForm, format_duration
from fleetops.models import FleetOperation, FleetOpsSettings
from fleetops.services.audit import audit
from fleetops.services.counting import operation_count_reason, set_stats_mode
from fleetops.services.multipliers import (
    allowed_multipliers,
    auto_multiplier,
    fleet_duration,
    needs_choice,
    requirements_text,
    tiers,
)
from fleetops.services.pap import compute_pap, finalize_pap, live_pap_status
from fleetops.texts import translate_stored
from fleetops.views import (
    _can_manage_attendance_operation,
    _can_manage_operation,
    _can_set_multiplier,
    _can_view_operation,
    _operation_for_user,
)

LIVE_PAP_TEMPLATE = "fleetops/partials/live_pap_status.html"
StatsMode = FleetOperation.StatsMode


def _sees_all_members(user, operation, can_manage):
    """VIS(2): while a fleet is open its member list belongs to its managers and fleet managers."""
    if operation.status not in FleetOperation.OPEN_STATUSES:
        return True
    return can_manage or user.has_perm("fleetops.view_all_fleets") or user.has_perm("fleetops.manage_fleets")


def _end_prompt(operation, settings):
    """What End Fleet shows for a running fleet: the multiplier choice, the automatic result, or nothing."""
    duration = fleet_duration(operation)
    allowed = allowed_multipliers(duration)
    prompt = {"allowed": allowed, "duration": format_duration(duration) if duration else "", "requirements": requirements_text()}
    if settings.multiplier_mode == FleetOpsSettings.MultiplierMode.AUTO:
        multiplier = auto_multiplier(duration)
        reached = [tier.threshold_minutes for tier in tiers() if tier.multiplier == multiplier]
        return {**prompt, "mode": "auto", "multiplier": multiplier, "threshold": min(reached, default=0)}
    if needs_choice(duration, settings):
        return {**prompt, "mode": "choice"}
    return {**prompt, "mode": ""}


def _preview(request, operation, show_all):
    """The PAP preview of a running fleet: everybody's rows for its managers, else the viewer's own."""
    plan = compute_pap(operation)
    rows = plan.candidates if show_all else [c for c in plan.candidates if c.identity.user.pk == request.user.pk]
    return {
        "pap_preview": sorted(rows, key=lambda c: (c.character_name.casefold(), c.character_id)),
        "pap_unregistered": plan.unregistered if show_all else [],
        "pap_minimum_seconds": plan.minimum_seconds,
        "pap_limit": plan.limit,
    }


def pap_context(request, operation):
    """What the fleet page's PAP parts need."""
    user = request.user
    settings = FleetOpsSettings.get_solo()
    can_manage = _can_manage_operation(user, operation)
    is_closed = operation.status == FleetOperation.Status.CLOSED
    context = {
        "count_reason": operation_count_reason(operation, settings),
        "can_exclude_from_stats": can_manage and operation.stats_mode != StatsMode.EXCLUDE,
        "can_include_in_stats": user.has_perm("fleetops.manage_fleets"),
        "can_recalculate_pap": is_closed and _can_manage_attendance_operation(user, operation),
        "pap_minimum_minutes": operation.pap_minimum_minutes or 0,
        "multiplier_mode": settings.multiplier_mode,
    }
    if operation.status == FleetOperation.Status.ACTIVE and can_manage:
        prompt = _end_prompt(operation, settings)
        context["end_prompt"] = prompt
        # The page shows the modal for a multiplier choice and for the automatic mode's confirmation.
        context["attendance_prompt_eligible"] = bool(prompt["mode"])
        context["end_form"] = EndFleetForm(allowed=prompt["allowed"])
    else:
        context["attendance_prompt_eligible"] = False
    if operation.status in FleetOperation.OPEN_STATUSES:
        context.update(_preview(request, operation, _sees_all_members(user, operation, can_manage)))
    if is_closed and _can_set_multiplier(user, operation):
        can_override = user.has_perm("fleetops.manage_fleets")
        form = FleetAttendanceMultiplierForm(
            operation=operation,
            can_override=can_override,
            initial={"attendance_multiplier": operation.attendance_multiplier},
        )
        context.update({"multiplier_form": form, "can_set_multiplier": True, "can_override_multiplier": can_override})
    else:
        context["can_set_multiplier"] = False
    if operation.started_at:
        duration = fleet_duration(operation, operation.ended_at if is_closed else None)
        context["multiplier_allowed"] = allowed_multipliers(duration)
        context["multiplier_requirements"] = requirements_text()
        context["fleet_duration_text"] = format_duration(duration)
    return context


@permission_required("fleetops.manage_attendance", raise_exception=True)
@require_POST
def recalculate_pap_view(request, operation_uuid):
    operation = get_object_or_404(FleetOperation, uuid=operation_uuid)
    if not _can_manage_attendance_operation(request.user, operation):
        raise Http404
    if operation.status != FleetOperation.Status.CLOSED:
        messages.error(request, _("PAP is calculated when the fleet closes."))
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    summary = finalize_pap(operation, actor=request.user, reason=texts.AUDIT_RECALCULATED())
    audit(request.user, "pap.recalculate", operation, None, summary, reason=texts.AUDIT_RECALCULATED())
    messages.success(
        request,
        _(
            "PAP recalculated: %(granted)s granted, %(capped)s over the character limit, "
            "%(below_minimum)s below the minimum time, %(unregistered)s not in Alliance Auth."
        )
        % summary,
    )
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_POST
def set_stats_mode_view(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid, manage=True)
    # Whoever manages the fleet may exclude it; counting a fleet again is up to fleet managers.
    mode = request.POST.get("mode", "")
    if mode != StatsMode.EXCLUDE and not request.user.has_perm("fleetops.manage_fleets"):
        raise PermissionDenied
    try:
        set_stats_mode(operation, mode, actor=request.user, note=request.POST.get("note", ""))
    except ValueError as exc:
        messages.error(request, translate_stored(str(exc)))
    else:
        if operation.counts_for_stats:
            messages.success(request, _("This fleet counts in statistics and incentives."))
        else:
            reason = operation_count_reason(operation)
            messages.warning(
                request,
                _("This fleet does not count in statistics and incentives (%(reason)s).") % {"reason": reason},
            )
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


def live_pap_rows(user):
    """The live PAP status rows of ``user``, with whether each fleet page may be opened."""
    rows = live_pap_status(user)
    for row in rows:
        row.can_open = _can_view_operation(user, row.operation)
    return rows


@permission_required("fleetops.basic_access", raise_exception=True)
def live_pap_partial(request):
    """The live PAP status of the user's characters, refreshed by the member pages."""
    return render(request, LIVE_PAP_TEMPLATE, {"live_pap_rows": live_pap_rows(request.user), "live_pap_fragment": True})
