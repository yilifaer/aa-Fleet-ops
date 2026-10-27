# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Downloads of the statistics as CSV and Excel files."""

from django.core.exceptions import PermissionDenied
from django.views.decorators.http import require_GET

from fleetops.decorators import fleetops_permission_required
from fleetops.services.export_datasets import export_for_request
from fleetops.views_errors import EXPORT_REASON, denied


@fleetops_permission_required("fleetops.basic_access")
@require_GET
def export_view(request, dataset):
    """``/statistics/export/<dataset>/?format=csv|xlsx``; each dataset checks its own permission and scope.

    A download opened from a link that is not allowed shows the FleetOps 403 page; an unknown dataset, format or
    object the FleetOps 404 page (the decorator).
    """
    try:
        return export_for_request(request, dataset, request.GET.get("format", ""))
    except PermissionDenied:
        return denied(request, EXPORT_REASON)
