# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Helpers shared by the FleetOps forms."""

from django import forms


def style_fields(fields):
    """Give each widget of ``fields`` the Bootstrap class of its kind."""
    for field in fields.values():
        widget = field.widget
        if isinstance(widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple, forms.RadioSelect)):
            widget.attrs["class"] = "form-check-input"
        else:
            widget.attrs["class"] = "form-select" if isinstance(widget, forms.Select) else "form-control"


def to_minute(value):
    """``value`` without seconds: the forms show times to the minute."""
    return value.replace(second=0, microsecond=0) if value else None


# The names the forms used before the helpers moved here.
_style = style_fields
_to_minute = to_minute
