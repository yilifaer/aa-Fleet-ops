"""Downloads of the statistics as CSV and Excel files."""

from django.contrib.auth.decorators import permission_required
from django.views.decorators.http import require_GET

from fleetops.services.export_datasets import export_for_request


@permission_required("fleetops.basic_access", raise_exception=True)
@require_GET
def export_view(request, dataset):
    """``/statistics/export/<dataset>/?format=csv|xlsx``; each dataset checks its own permission and scope."""
    return export_for_request(request, dataset, request.GET.get("format", ""))
