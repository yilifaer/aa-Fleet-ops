# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from django import template

register = template.Library()


@register.filter
def is_bool(value):
    """True only for real booleans; 1, 0 and Decimal("1.00") compare equal to True/False."""
    return isinstance(value, bool)


@register.filter
def is_copyable(value):
    """True for list cells that are shown with a Copy button, such as comms join links."""
    return getattr(value, "copyable", False) is True
