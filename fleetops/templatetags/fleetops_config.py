# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from django import template
from django.apps import apps
from django.utils.text import capfirst
from django.utils.translation import gettext_lazy, pgettext_lazy

register = template.Library()


@register.filter
def is_bool(value):
    """True only for real booleans; 1, 0 and Decimal("1.00") compare equal to True/False."""
    return isinstance(value, bool)


@register.filter
def is_copyable(value):
    """True for list cells that are shown with a Copy button, such as comms join links."""
    return getattr(value, "copyable", False) is True


# What the audit log stores for something that is not a FleetOps model: the exports and the Alliance Auth group.
AUDIT_OBJECT_NAMES = {
    "Statistics": gettext_lazy("Statistics"),
    "SrpSummary": gettext_lazy("SRP Summary"),
    "Group": pgettext_lazy("FleetOps", "Group"),
}


@register.filter
def audit_object_label(row):
    """The "Object" column of the audit log: the name of what was changed and its ID, in the viewer's language.

    Entries store the class name (``FleetOperation``); the page shows the model's name (``Fleet operation #77``).
    Names of things that are no FleetOps model are translated when FleetOps knows them and shown as stored
    otherwise.
    """
    object_type = getattr(row, "object_type", "") or ""
    object_id = getattr(row, "object_id", "")
    try:
        name = apps.get_app_config("fleetops").get_model(object_type)._meta.verbose_name
    except (LookupError, ValueError):
        name = AUDIT_OBJECT_NAMES.get(object_type)
    label = capfirst(str(name)) if name is not None else object_type
    return f"{label} #{object_id}" if object_id not in (None, "") else label
