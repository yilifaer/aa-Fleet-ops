"""Actions on a running fleet."""

from allianceauth.authentication.decorators import permissions_required
from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from fleetops.models import OperationAction
from fleetops.services.motd import regenerate_and_push_motd
from fleetops.views import _can_manage_operation, _operation_for_user


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_POST
def regenerate_motd(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid, manage=True)
    if not _can_manage_operation(request.user, operation):
        raise Http404
    try:
        action = regenerate_and_push_motd(operation, actor=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    if action.status == OperationAction.Status.SUCCESS:
        messages.success(request, "MOTD regenerated and pushed to the fleet.")
    elif action.status == OperationAction.Status.SKIPPED:
        messages.info(request, f"MOTD not regenerated. {action.error_message}")
    else:
        messages.error(request, f"MOTD not pushed. {action.error_message}")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
