# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fleet positions, member events, fleet changes and composition numbers as the fleet page shows them.

Stored values keep ESI's raw wing and squad ids and role codes; the names come from the fleet's
latest wings snapshot when the page is rendered.
"""

import re

from django import template
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from fleetops.models import FleetMemberEvent
from fleetops.services.fleet_rules import (
    FLEET_COMMAND,
    normalize_wings,
    position_labels,
    squad_path_label,
    wing_index,
)
from fleetops.texts import FLEET_ROLE_LABELS, translate_stored

register = template.Library()

Event = FleetMemberEvent.EventType
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
ROLE_MARKERS = {"fleet_commander": "FC", "wing_commander": "WC", "squad_commander": "SC"}
# Fleet changes whose audit entry has no summary of its own.
CHANGE_LABELS = {
    "fleet.kick_member": gettext_lazy("Kicked a member"),
    "fleet.kick_outside_system": gettext_lazy("Kicked members outside a system"),
    "fleet.kick_capsules": gettext_lazy("Kicked capsule pilots"),
}


def _wings(operation):
    """The fleet's wings snapshot, normalised once per rendered operation."""
    if operation is None:
        return []
    cached = getattr(operation, "_fleetops_wings", None)
    if cached is None or cached[0] is not operation.fleet_wings:
        cached = (operation.fleet_wings, normalize_wings(operation.fleet_wings or []))
        operation._fleetops_wings = cached
    return cached[1]


def _number(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@register.simple_tag
def member_position(member, operation):
    """``(wing, squad)`` names of a tracked member's position, such as ("Main", "Alpha").

    The fleet commander reads ("Fleet Command", ""), a wing commander ("Logi", "Wing Command");
    a member whose position was never read shows dashes.
    """
    role = getattr(member, "fleet_role", "") or ""
    wing_id, squad_id = getattr(member, "wing_id", None), getattr(member, "squad_id", None)
    if wing_id is None and squad_id is None and role != "fleet_commander":
        return ("—", "—")
    wing, squad = position_labels(role, wing_id, squad_id, _wings(operation))
    return (wing, squad or "—")


@register.simple_tag
def fleet_event_change(event, operation):
    """``(old, new)`` of a member event as people read them.

    Wing changes show the names of the latest wings snapshot ("Wing <id>" for wings that are gone; -1 is
    the fleet command position), squad changes "Wing / Squad" ("Squad <id>" for squads that are gone, so
    two squads with the same name in different wings can be told apart), role changes the position names.
    """
    values = (event.old_value, event.new_value)
    if event.event_type == Event.ROLE_CHANGE:
        return tuple(str(FLEET_ROLE_LABELS.get(value, value)) for value in values)
    if event.event_type not in (Event.WING_CHANGE, Event.SQUAD_CHANGE):
        return values
    wings = _wings(operation)
    labels = []
    for value in values:
        number = _number(value)
        if number is None:
            labels.append(value or "—")
        elif event.event_type == Event.WING_CHANGE:
            wing = wing_index(wings).get(number)
            labels.append(
                FLEET_COMMAND if number == -1 else (wing and wing["name"]) or _("Wing %(id)s") % {"id": number}
            )
        else:
            labels.append(_("No squad") if number == -1 else squad_path_label(number, wings))
    return tuple(labels)


@register.filter
def role_label(role):
    """``squad_commander`` -> "Squad Commander"."""
    return str(FLEET_ROLE_LABELS.get(role, role or ""))


@register.filter
def role_marker(role):
    """The short position badge of a commander (FC, WC, SC); "" for squad members."""
    return ROLE_MARKERS.get(role, "")


@register.filter
def change_text(entry):
    """What a fleet change audit entry did, from its summary."""
    value = entry.new_value if isinstance(entry.new_value, dict) else {}
    summary = value.get("summary")
    if summary:
        return translate_stored(str(summary))
    return CHANGE_LABELS.get(entry.action) or entry.action.removeprefix("fleet.").replace("_", " ").capitalize()


@register.filter
def change_failed(entry):
    value = entry.new_value if isinstance(entry.new_value, dict) else {}
    return value.get("result") == "failed"


@register.filter
def actor_name(user):
    """A user as FleetOps names people: the main character, else the user name."""
    if user is None:
        return "—"
    profile = getattr(user, "profile", None)
    main = getattr(profile, "main_character", None)
    return getattr(main, "character_name", "") or user.get_username()


@register.filter
def kilograms(value):
    """1234567000.4 -> "1,234,567,000 kg"."""
    try:
        return f"{round(float(value)):,} kg"
    except (TypeError, ValueError):
        return ""


@register.filter
def bar_width(percent):
    """A percentage as a CSS width that never leaves the bar: "37.5%"."""
    try:
        number = min(max(float(percent), 0.0), 100.0)
    except (TypeError, ValueError):
        number = 0.0
    return f"{number:.1f}%"


@register.filter
def hex_color(value):
    """A ship function colour such as "#0d6efd" for a style attribute; "" for anything else."""
    text = str(value or "")
    return text if HEX_COLOR.match(text) else ""
