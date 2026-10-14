# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Actions on a running fleet."""

from allianceauth.authentication.decorators import permissions_required
from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from fleetops.models import OperationAction
from fleetops.services.motd import regenerate_and_push_motd
from fleetops.texts import translate_stored
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
        messages.error(request, translate_stored(str(exc)))
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    if action.status == OperationAction.Status.SUCCESS:
        messages.success(request, _("MOTD regenerated and pushed to the fleet."))
    elif action.status == OperationAction.Status.SKIPPED:
        messages.info(
            request, _("MOTD not regenerated. %(detail)s") % {"detail": translate_stored(action.error_message)}
        )
    else:
        messages.error(request, _("MOTD not pushed. %(detail)s") % {"detail": translate_stored(action.error_message)})
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
