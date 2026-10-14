# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Read-only check of the SRP systems FleetOps replaces, so a loss claimed there is flagged."""

import re

from allianceauth.services.hooks import get_extension_logger
from django.apps import apps

logger = get_extension_logger(__name__)

# (label, app that must be installed, app label and model of the requests, name of the status field)
SYSTEMS = (
    ("aa-srp", "aasrp", "aasrp", "SrpRequest", "request_status"),
    ("Alliance Auth SRP", "allianceauth.srp", "srp", "SrpUserRequest", "srp_status"),
)
MAX_REQUESTS = 20
MAX_STATUS_LENGTH = 30


def _requests_for(killmail_id: int, installed: str, app_label: str, model_name: str, status_field: str) -> list[str]:
    if not apps.is_installed(installed):
        return []
    model = apps.get_model(app_label, model_name)
    # The links are free text ("…/kill/123/", "…/kill/123", sometimes with a query), so the database narrows
    # them down and the exact ID match is checked here: /kill/123 must not match /kill/1234.
    exact = re.compile(rf"/kill/{killmail_id}(?!\d)")
    rows = model.objects.filter(killboard_link__contains=f"/kill/{killmail_id}").values_list(
        "killboard_link", status_field
    )[:MAX_REQUESTS]
    return [str(status or "")[:MAX_STATUS_LENGTH] for link, status in rows if exact.search(str(link or ""))]


def external_claims(killmail_id: int) -> list[str]:
    """Where else the killmail is claimed, such as ``["aa-srp: Pending"]`` or ``["Alliance Auth SRP: Approved"]``.

    Looks at aa-srp and Alliance Auth SRP when they are installed, by the killboard link of their requests
    (``/kill/<id>/``, with or without the trailing slash). An error while reading one of them leaves that one
    out, so a broken or unknown schema gives an empty list.
    """
    found = []
    for label, installed, app_label, model_name, status_field in SYSTEMS:
        try:
            statuses = _requests_for(int(killmail_id), installed, app_label, model_name, status_field)
        except Exception as exc:  # another app's schema or database problem must not stop a claim check
            logger.debug("FleetOps: reading %s requests failed (%s).", label, type(exc).__name__)
            continue
        found.extend(f"{label}: {status}" if status else label for status in statuses)
    return list(dict.fromkeys(found))
