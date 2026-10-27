# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Template helpers for the look of the pages: status pills, fleet type dots and the notices of a page.

``{% pill "ok" "Active" %}`` writes a pill; the filters write the pill of a status with its translated words.
The tones and their meaning are described in ``fleetops.ui``.
"""

from django import template
from django.utils.html import json_script

from fleetops.models import FleetOperation, OperationAction
from fleetops.ui import (
    OPERATION_STATUS_TONES,
    STEP_STATUS_TONES,
    pill_html,
    tone_class as ui_tone_class,
    tone_for,
    type_tone as ui_type_tone,
)
from fleetops.ui_notices import pop_notices

register = template.Library()

OPERATION_STATUS_LABELS = dict(FleetOperation.Status.choices)
STEP_STATUS_LABELS = dict(OperationAction.Status.choices)


def _status_of(value):
    """The status key of a string, or of an object that has a ``status``."""
    return getattr(value, "status", value)


@register.simple_tag
def pill(tone, text, **attrs):
    """A status pill: ``{% pill "warn" "Pending" id="badge-x" title="…" class="ms-1" icon="users" %}``.

    ``tone`` is ``ok``, ``warn``, ``bad``, ``info``, ``neutral`` or ``type-1`` ... ``type-6``. ``icon`` is the
    name of a Font Awesome glyph that replaces the dot; ``class`` adds classes. The text is escaped.
    """
    return pill_html(tone, text, **attrs)


@register.filter
def op_status_pill(value):
    """The pill of a fleet's status, for the status key or for the fleet itself."""
    status = _status_of(value)
    label = OPERATION_STATUS_LABELS.get(status)
    return pill_html(tone_for(OPERATION_STATUS_TONES, status), label if label is not None else str(status or ""))


@register.filter
def step_status_pill(value):
    """The pill of a start or end step (or one of its attempts): the status key or the object."""
    status = _status_of(value)
    label = STEP_STATUS_LABELS.get(status)
    if label is None:
        label = str(status or "").replace("_", " ").capitalize()
    return pill_html(tone_for(STEP_STATUS_TONES, status), label)


@register.filter
def type_tone(fleet_type):
    """The dot number (1-6) of a fleet type, for ``fo-type-{{ fleet_type|type_tone }}``; 0 without a type."""
    return ui_type_tone(fleet_type)


@register.filter
def type_pill(fleet_type):
    """The pill of a fleet type: its name, with the dot colour of the type."""
    number = ui_type_tone(fleet_type)
    return pill_html(f"type-{number}" if number else "type", str(fleet_type or ""))


@register.filter
def tone_class(tone):
    """The classes of a pill for a tone name (``fo-pill fo-pill-ok``); an unknown tone is neutral."""
    return ui_tone_class(tone)


@register.simple_tag(takes_context=True)
def fleetops_notices(context):
    """The notices waiting for the user (``fleetops.ui_notices``) as a JSON block that ``fleetops.js`` shows.

    Nothing is written when none is waiting. A notice is shown once: reading it removes it from the stored notices.
    """
    notices = pop_notices(context.get("request"))
    if not notices:
        return ""
    return json_script(notices, "fo-notices")
