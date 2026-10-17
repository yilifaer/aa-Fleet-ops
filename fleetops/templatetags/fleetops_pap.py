# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from datetime import timedelta

from django import template

from fleetops.forms import format_duration
from fleetops.services.multipliers import multiplier_label as label_of_multiplier

register = template.Library()


@register.filter
def minutes(seconds):
    """Seconds as time in fleet: "37 min", "1 h 05 min"; "" when unknown."""
    if seconds in (None, ""):
        return ""
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return ""
    return format_duration(timedelta(seconds=seconds))


@register.filter
def joined_eve(value):
    """The EVE time (HH:MM) of the join time a JOIN or REJOIN event stored, or ""."""
    from fleetops.services.tracking import stored_join_time

    join_time = stored_join_time(value)
    return join_time.strftime("%H:%M") if join_time else ""


@register.filter
def multiplier_label(value):
    """A PAP multiplier as the page's language writes it: "2x", Chinese "2 倍"."""
    return label_of_multiplier(value)
