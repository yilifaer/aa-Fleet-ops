# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from django import template
from django.http import QueryDict
from django.urls import reverse

from fleetops.services.assessment import status_label as label_for_status
from fleetops.ui import STATISTICS_TONES, tone_class, tone_for

register = template.Library()

# Colour is never the only signal: every pill shows the label of its status as text too.
ROW_CLASSES = {
    "met": "fleetops-status-row-met",
    "ok": "fleetops-status-row-met",
    "missed": "fleetops-status-row-missed",
    "below": "fleetops-status-row-missed",
    "in_progress": "fleetops-status-row-progress",
    "below_so_far": "fleetops-status-row-progress",
}


@register.filter
def status_label(status):
    """The words for a status key of ``services.assessment``; empty for ``none``."""
    return label_for_status(status)


@register.filter
def status_badge_class(status):
    """The classes of the pill of a status: green when reached, amber while open, red when missed, grey otherwise."""
    return tone_class(tone_for(STATISTICS_TONES, status))


@register.filter
def status_row_class(status):
    """The class that marks a table row with the colour of its status (left edge)."""
    return ROW_CLASSES.get(status, "")


@register.simple_tag(takes_context=True)
def export_url(context, dataset, fmt, params=None):
    """Link to an export of what the page shows.

    The page's own query string (filters, sort, month) is passed on without the page number. The month
    shown is added when the URL does not name one. ``params`` overrides single parameters, such as a
    scope that the page has in its path; a ``None`` or empty value removes the parameter.
    """
    request = context.get("request")
    query = request.GET.copy() if request is not None else QueryDict(mutable=True)
    query.pop("page", None)
    for name in ("year", "month"):
        if name not in query and context.get(name):
            query[name] = str(context[name])
    query["format"] = fmt
    for name, value in (params or {}).items():
        if value is None or value == "":
            query.pop(name, None)
        else:
            query[name] = str(value)
    return f"{reverse('fleetops:export', args=[dataset])}?{query.urlencode()}"

