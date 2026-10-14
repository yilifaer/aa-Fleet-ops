# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Pages that work without an Alliance Auth login when the site allows public FleetOps views."""

from django.http import Http404
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from fleetops.models import CommsPreset
from fleetops.services.links import public_voice_url
from fleetops.validators import VOICE_URL_SCHEMES, voice_url_scheme


@require_GET
@never_cache
def comms_join(request, token):
    preset = CommsPreset.objects.filter(join_token=token, is_active=True).first()
    voice_url = public_voice_url(preset)
    if not voice_url or voice_url_scheme(voice_url) not in VOICE_URL_SCHEMES:
        raise Http404
    response = render(request, "fleetops/comms_join.html", {"preset": preset, "voice_url": voice_url})
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex"
    return response
