# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fleet tab tools: inviting characters, structure templates and quick ship classification."""

from allianceauth.authentication.decorators import permissions_required
from django.contrib.auth.decorators import permission_required
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from fleetops.models import FleetStructureTemplate
from fleetops.services import composition_config, fleet_invites, fleet_templates
from fleetops.services.fleet_manage import FleetActionResult
from fleetops.views import _database_id, _operation_for_user
from fleetops.views_fleet_common import (
    MANAGE_PERMISSIONS,
    fleet_response,
    invalid,
    json_response,
    manage_operation_or_404,
    posted_ids,
    posted_unit,
    run_fleet_action,
)

# What a ticked checkbox posts (browsers send "on" without a value attribute).
CHECKED = ("1", "on", "true")


@permissions_required(MANAGE_PERMISSIONS, raise_exception=True)
@require_GET
def invite_candidates_view(request, operation_uuid):
    """Characters to invite matching ``?q=``; without a query the user's own characters."""
    operation = manage_operation_or_404(request, operation_uuid)
    query = request.GET.get("q", "").strip()
    candidates = fleet_invites.invite_candidates(query, actor=request.user, operation=operation)
    return json_response({"query": query, "candidates": candidates})


@permissions_required(MANAGE_PERMISSIONS, raise_exception=True)
@require_POST
def invite_view(request, operation_uuid):
    """Invite the posted ``character_ids`` as squad members, into ``squad_id`` or any squad with room."""
    operation = manage_operation_or_404(request, operation_uuid)
    character_ids = posted_ids(request)
    if not character_ids:
        return fleet_response(request, operation, invalid(_("Choose the characters to invite.")))
    try:
        wing_id = posted_unit(request, "wing_id", "wing")
        squad_id = posted_unit(request, "squad_id", "squad")
    except ValueError as exc:
        return fleet_response(request, operation, invalid(str(exc)))
    return run_fleet_action(
        request,
        operation,
        fleet_invites.invite_characters,
        operation,
        character_ids,
        actor=request.user,
        wing_id=wing_id,
        squad_id=squad_id,
    )


@permissions_required(MANAGE_PERMISSIONS, raise_exception=True)
@require_POST
def apply_template_view(request, operation_uuid):
    """Apply ``template_id``; ``delete_extra`` also deletes empty wings and squads beyond the template."""
    operation = manage_operation_or_404(request, operation_uuid)
    template_id = _database_id(request.POST.get("template_id", "").strip())
    template = FleetStructureTemplate.objects.filter(pk=template_id, is_active=True).first() if template_id else None
    if template is None:
        gone = FleetActionResult(False, fleet_templates.TEMPLATE_GONE, level="error", status=404)
        return fleet_response(request, operation, gone)
    return run_fleet_action(
        request,
        operation,
        fleet_templates.apply_template,
        operation,
        template,
        actor=request.user,
        delete_extra=request.POST.get("delete_extra", "") in CHECKED,
    )


@permission_required("fleetops.manage_configuration", raise_exception=True)
@require_POST
def classify_view(request, operation_uuid):
    """Count ``type_id`` under the ship function ``function_id`` (quick classify in the composition)."""
    operation = _operation_for_user(request, operation_uuid)
    type_id = _database_id(request.POST.get("type_id", "").strip())
    function_id = _database_id(request.POST.get("function_id", "").strip())
    if type_id is None or function_id is None:
        return fleet_response(request, operation, invalid(_("Choose a ship type and a ship function.")))
    return run_fleet_action(
        request, operation, composition_config.classify_type, type_id, function_id, actor=request.user
    )
