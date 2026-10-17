# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Send Ping: a Discord ping without a fleet, for FCs and the leaders of configured groups."""

from django.http import JsonResponse
from django.views.decorators.http import require_POST

from fleetops.decorators import ping_sender_required
from fleetops.views import _render


@ping_sender_required
def send_ping_view(request):
    """The Send Ping page. Not available yet: the page only has its header."""
    return _render(request, "fleetops/send_ping.html", {})


@ping_sender_required(page=False)
@require_POST
def preview_ping(request):
    """The preview of a ping without a fleet, as JSON. Not available yet."""
    return JsonResponse({"ok": False})
