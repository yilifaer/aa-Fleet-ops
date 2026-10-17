# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from django import template
from django.utils.html import format_html
from django.utils.translation import gettext as _

from fleetops.services.mentions import ROLE_MENTION_RE
from fleetops.services.periods import EVE_DATETIME_FORMAT, format_eve

# The machine-readable form of a time: ``<time datetime>`` and the ``data-relative-utc`` of fleetops.js.
ISO_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

register = template.Library()


@register.filter
def eve_time(value, fmt=EVE_DATETIME_FORMAT):
    """A datetime in EVE time (UTC), never in the site or request time zone; "" for anything else."""
    try:
        return format_eve(value, fmt)
    except TypeError:
        return ""


@register.filter
def ping_display(text, operation):
    """Show a stored ping with role mentions as ``@Name`` from the fleet's role snapshot.

    Returns plain text; the template escapes it as usual.
    """
    names = {}
    for role in getattr(operation, "ping_roles", None) or []:
        if isinstance(role, dict) and role.get("id") and role.get("name"):
            names[str(role["id"])] = str(role["name"])

    def replace(match):
        role_id = match.group(1)
        return f"@{names[role_id]}" if role_id in names else _("@role %(id)s") % {"id": role_id}

    return ROLE_MENTION_RE.sub(replace, str(text or ""))


@register.simple_tag
def eve_when(value, relative=True):
    """One time as ``2026-10-16 13:05 EVE``, with the time left or gone next to it in a quieter colour.

    ``<time class="fo-time" datetime="2026-10-16T13:05:00Z">2026-10-16 13:05 EVE</time>`` followed by
    ``<span class="fo-time-rel" data-relative-utc="2026-10-16T13:05:00Z"></span>``, which ``fleetops.js`` fills
    in ("in 30 min", "3 h 05 min ago") in the language of the page and keeps current. ``relative=False`` leaves
    the second part out. Nothing is written for a missing or invalid value.
    """
    try:
        stamp = format_eve(value, ISO_UTC_FORMAT)
        shown = format_eve(value)
    except TypeError:
        return ""
    if not shown:
        return ""
    time = format_html('<time class="fo-time" datetime="{}">{} EVE</time>', stamp, shown)
    if not relative:
        return time
    return format_html('{}<span class="fo-time-rel" data-relative-utc="{}"></span>', time, stamp)

