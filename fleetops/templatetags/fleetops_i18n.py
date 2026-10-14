# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Template helpers for translated text: stored messages, step codes, fleet positions and the version."""

from django import template

import fleetops
from fleetops.texts import FLEET_ROLE_LABELS, STEP_LABELS, translate_stored

register = template.Library()


@register.filter
def stored_text(value):
    """A message stored in English, in the language of the viewer. Other text is shown unchanged."""
    return translate_stored(value)


@register.filter
def step_label(code):
    """The name of a fleet step such as ``discord_ping``; a code without a label is made readable."""
    code = "" if code is None else str(code)
    label = STEP_LABELS.get(code)
    return str(label) if label is not None else code.replace("_", " ").capitalize()


@register.filter
def fleet_role_label(code):
    """The name of a fleet position such as ``squad_commander``; an unknown value is shown as it is."""
    code = "" if code is None else str(code)
    label = FLEET_ROLE_LABELS.get(code)
    return str(label) if label is not None else code


@register.simple_tag
def fleetops_version():
    """The installed version; static files and the JavaScript catalog use it to refresh browser caches."""
    return fleetops.__version__
