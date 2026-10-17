# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Wing, squad and member position changes from the Fleet tab.

Every view answers through ``fleet_response``: JSON for the tab's scripts, a message and the
fleet page for plain form posts.
"""

from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from fleetops.decorators import fleet_manager_required
from fleetops.services import fleet_manage
from fleetops.services.fleet_manage import MoveRequest
from fleetops.services.fleet_rules import FLEET_ROLES
from fleetops.views_fleet_common import (
    fleet_response,
    invalid,
    manage_operation_or_404,
    posted_ids,
    posted_unit,
    run_fleet_action,
    unit_or_404,
)


def _name(request):
    return request.POST.get("name", "")


@fleet_manager_required()
@require_POST
def wing_add_view(request, operation_uuid):
    operation = manage_operation_or_404(request, operation_uuid)
    return run_fleet_action(
        request, operation, fleet_manage.add_wing, operation, actor=request.user, name=_name(request)
    )


@fleet_manager_required()
@require_POST
def wing_rename_view(request, operation_uuid, wing_id):
    operation = manage_operation_or_404(request, operation_uuid)
    wing_id = unit_or_404(wing_id)
    return run_fleet_action(
        request, operation, fleet_manage.rename_wing, operation, wing_id, _name(request), actor=request.user
    )


@fleet_manager_required()
@require_POST
def wing_delete_view(request, operation_uuid, wing_id):
    operation = manage_operation_or_404(request, operation_uuid)
    wing_id = unit_or_404(wing_id)
    return run_fleet_action(request, operation, fleet_manage.delete_wing, operation, wing_id, actor=request.user)


@fleet_manager_required()
@require_POST
def squad_add_view(request, operation_uuid, wing_id):
    operation = manage_operation_or_404(request, operation_uuid)
    wing_id = unit_or_404(wing_id)
    return run_fleet_action(
        request, operation, fleet_manage.add_squad, operation, wing_id, actor=request.user, name=_name(request)
    )


@fleet_manager_required()
@require_POST
def squad_rename_view(request, operation_uuid, squad_id):
    operation = manage_operation_or_404(request, operation_uuid)
    squad_id = unit_or_404(squad_id)
    return run_fleet_action(
        request, operation, fleet_manage.rename_squad, operation, squad_id, _name(request), actor=request.user
    )


@fleet_manager_required()
@require_POST
def squad_delete_view(request, operation_uuid, squad_id):
    operation = manage_operation_or_404(request, operation_uuid)
    squad_id = unit_or_404(squad_id)
    return run_fleet_action(request, operation, fleet_manage.delete_squad, operation, squad_id, actor=request.user)


@fleet_manager_required()
@require_POST
def members_move_view(request, operation_uuid):
    """Move the posted ``character_ids`` to ``role`` in ``wing_id`` / ``squad_id`` (as the role needs)."""
    operation = manage_operation_or_404(request, operation_uuid)
    character_ids = posted_ids(request)
    role = request.POST.get("role", "")
    if not character_ids:
        return fleet_response(request, operation, invalid(_("Choose the members to move.")))
    if role not in FLEET_ROLES:
        return fleet_response(request, operation, invalid(_("Choose a fleet position.")))
    try:
        wing_id = posted_unit(request, "wing_id", "wing")
        squad_id = posted_unit(request, "squad_id", "squad")
    except ValueError as exc:
        return fleet_response(request, operation, invalid(str(exc)))
    moves = [MoveRequest(character_id, role, wing_id, squad_id) for character_id in character_ids]
    return run_fleet_action(request, operation, fleet_manage.move_members, operation, moves, actor=request.user)
