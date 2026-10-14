# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from django import template
from django.utils.translation import gettext as _

from fleetops.services.mentions import ROLE_MENTION_RE
from fleetops.services.periods import EVE_DATETIME_FORMAT, format_eve

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
