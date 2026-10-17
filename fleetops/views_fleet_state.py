# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The Fleet tab of the fleet page: its context, the poller's state endpoint and the Refresh button."""

from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_GET, require_POST

from fleetops.decorators import fleet_manager_required, fleetops_permission_required
from fleetops.models import AttendanceRecord, FleetOperation, FleetOpsSettings
from fleetops.services import fleet_refresh
from fleetops.services.fleet_access import LIVE_STATUSES, can_manage_fleet, can_view_fleet_tab, can_view_live_fleet
from fleetops.services.fleet_manage import FleetActionResult
from fleetops.services.fleet_view import PAGE_FRAGMENTS, fleet_panel, state_payload
from fleetops.services.review import deletable_manual_rows
from fleetops.templatetags.fleetops_operation import owned_by
from fleetops.views import _can_manage_attendance_operation, _can_manage_operation
from fleetops.views_fleet_common import (
    fleet_response,
    fleet_tab_operation_or_404,
    json_response,
    manage_operation_or_404,
)

# The Fleet tab opens first while the fleet runs; otherwise the Members tab does.
DEFAULT_TAB_STATUSES = (FleetOperation.Status.ACTIVE, FleetOperation.Status.ENDING)
NOT_ACTIVE = gettext_lazy("The fleet is not active any more.")
LAST_REFRESH_FAILED = gettext_lazy("The last refresh failed a moment ago; try again in a few seconds.")
MEMBERS_TEMPLATE = "fleetops/operation/_members.html"
CONTROLS_TEMPLATE = "fleetops/operation/_controls_card.html"
PAP_PREVIEW_TEMPLATE = "fleetops/operation/_attendance_table.html"


def fleet_context(request, operation):
    """What the fleet page's Fleet tab needs; one of operation_detail's context providers.

    VIS(2): the tab of an open fleet is for the people who manage it and fleet managers or
    viewers of all fleets; everybody who can open the page sees the composition once it closed.
    """
    if not can_view_fleet_tab(request.user, operation):
        return {"show_fleet_tab": False, "fleet_tab_default": False}
    return {
        "show_fleet_tab": True,
        "fleet_tab_default": operation.status in DEFAULT_TAB_STATUSES,
        **fleet_panel(operation, request.user),
    }


def page_fragments(request, operation) -> dict:
    """The Members tab, the PAP preview and the Fleet Control card of an open fleet as the page renders them.

    VIS(2) holds as on the page: a viewer who may not see the fleet's members gets the hidden
    member list and only their own PAP rows; only the fleet's managers get the Fleet Control card.
    A closed fleet's parts stay as the page has them (None).
    """
    if operation.status not in LIVE_STATUSES:
        return dict.fromkeys(PAGE_FRAGMENTS)
    # Imported here: these view modules import the fleet services.
    from fleetops import views_controls, views_pap

    user = request.user
    show_all = can_view_live_fleet(user, operation)
    common = {
        "operation": operation,
        "show_composition": show_all,
        "can_manage": _can_manage_operation(user, operation),
    }
    members = operation.member_states.filter(is_active=True).order_by("character_name")
    controls = views_controls.controls_context(request, operation)
    members_html = render_to_string(MEMBERS_TEMPLATE, {**common, "members": members, **controls}, request=request)
    controls_html = (
        render_to_string(CONTROLS_TEMPLATE, {**common, **controls}, request=request)
        if controls["kick_enabled"]
        else None
    )
    attendance = list(operation.attendance_records.all())
    manual_rows = operation.attendance_records.filter(source__in=AttendanceRecord.USER_SOURCES)
    deletable = (
        set(deletable_manual_rows(user, manual_rows).values_list("pk", flat=True))
        if _can_manage_attendance_operation(user, operation)
        else set()
    )
    pap_html = render_to_string(
        PAP_PREVIEW_TEMPLATE,
        {
            **common,
            **views_pap.pap_context(request, operation),
            "attendance_rows": attendance if show_all else owned_by(attendance, user),
            "deletable_row_ids": deletable,
        },
        request=request,
    )
    return {"members_html": members_html, "pap_html": pap_html, "controls_html": controls_html}


def live_refresh(user, operation) -> bool:
    """Read an active fleet from ESI while one of its managers watches it; returns whether a read was queued.

    One read per ``fleet_live_refresh_seconds`` however many managers watch, and none when tracking
    read the fleet within that time.
    """
    if operation.status != FleetOperation.Status.ACTIVE or not can_manage_fleet(user, operation):
        return False
    seconds = FleetOpsSettings.get_solo().fleet_live_refresh_seconds
    if not seconds:
        return False
    last = operation.last_esi_update
    if last is not None and (timezone.now() - last).total_seconds() < seconds:
        return False
    return bool(fleet_refresh.schedule_refresh(operation, delays=(0,), dedupe_seconds=seconds))


@fleetops_permission_required("fleetops.basic_access", page=False)
@require_GET
def fleet_state_view(request, operation_uuid):
    """The Fleet tab's state; ``?v=<version>`` answers ``unchanged`` without HTML, ``live=1`` asks for ESI reads."""
    operation = fleet_tab_operation_or_404(request, operation_uuid)
    if request.GET.get("live") == "1" and live_refresh(request.user, operation):
        # Without a worker the read ran in this request already.
        operation.refresh_from_db()
    return json_response(state_payload(request, operation, known_version=request.GET.get("v", "")), request=request)


@fleet_manager_required()
@require_POST
def fleet_refresh_view(request, operation_uuid):
    """Read the fleet from ESI now (Refresh button)."""
    operation = manage_operation_or_404(request, operation_uuid)
    if operation.status != FleetOperation.Status.ACTIVE:
        return fleet_response(request, operation, FleetActionResult(False, NOT_ACTIVE, level="warning", status=409))
    ok, message = fleet_refresh.refresh_now(operation)
    if ok:
        result = FleetActionResult(True, message)
    elif message == fleet_refresh.REFRESHED_RECENTLY:
        problem = fleet_refresh.read_problem(operation.pk)
        if problem["boss"] or problem["read_error"]:
            # No read was made: the one a moment ago failed, and the page still shows why.
            result = FleetActionResult(False, LAST_REFRESH_FAILED, level="warning", status=429)
        else:
            result = FleetActionResult(True, message, level="info")
    elif message == fleet_refresh.NOT_OPEN:
        result = FleetActionResult(False, NOT_ACTIVE, level="warning", status=409)
    elif fleet_refresh.read_problem(operation.pk)["boss"]:
        # The FC character is not the boss any more, or the fleet is gone.
        result = FleetActionResult(False, message, level="warning", status=409)
    else:
        result = FleetActionResult(False, message, level="warning", status=502)
    return fleet_response(request, operation, result)
