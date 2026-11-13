# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

import json
import re

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

    Entries store the class name (``FleetOperation``); the page shows the model's name (``Fleet operation #12``).
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


# Keys whose values are never shown, whatever an entry holds: the audit log keeps webhook URLs out of its snapshots,
# and this is the second wall in front of the page.
_SECRET_KEY = re.compile(r"webhook|token|secret|password|passwd|authorization|api_?key", re.IGNORECASE)
_SECRET_VALUE = re.compile(r"/api/(?:v\d+/)?webhooks/\d+/[\w-]+", re.IGNORECASE)
# Login data in a link (``mumble://user:password@host``): old entries of 0.1.x kept voice links as they were typed.
_URL_USERINFO = re.compile(r"(\b[a-z][\w+.-]*://)[^/@\s]+@", re.IGNORECASE)
HIDDEN = "***"
CHANGE_VALUE_LENGTH = 160
CHANGES_INLINE_LENGTH = 90


def _show(key, value) -> str:
    """One stored value as short text; secrets are hidden."""
    if isinstance(value, bool):
        # A flag such as ``webhook_url_replaced`` is no secret; it is what tells that something was replaced.
        return "true" if value else "false"
    if _SECRET_KEY.search(str(key)):
        return "" if value in (None, "", [], {}) else HIDDEN
    if value is None:
        return "—"
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    text = _URL_USERINFO.sub(rf"\1{HIDDEN}@", _SECRET_VALUE.sub(HIDDEN, text))
    return text if len(text) <= CHANGE_VALUE_LENGTH else text[: CHANGE_VALUE_LENGTH - 1] + "…"


@register.filter
def audit_changes(row):
    """The "Changes" column of the audit log: ``[(name, old, new)]`` of what an entry changed, as short text.

    Only the values that differ are listed; a created or deleted object lists all of its values. The entry's own
    JSON is read, so the page costs no query. Webhook URLs, tokens and the like are shown as ``***``.
    """
    old, new = getattr(row, "old_value", None), getattr(row, "new_value", None)
    if old is None and new is None:
        return []
    if isinstance(old, dict) or isinstance(new, dict):
        old = old if isinstance(old, dict) else {}
        new = new if isinstance(new, dict) else {}
        names = sorted(set(old) | set(new), key=str)
        return [
            (name, _show(name, old.get(name)) if name in old else "—", _show(name, new.get(name)) if name in new else "—")
            for name in names
            if name not in old or name not in new or old[name] != new[name]
        ]
    return [("", _show("", old), _show("", new))]


@register.filter
def changes_are_short(changes) -> bool:
    """Whether the changes fit in the table cell without a fold-out."""
    return len(changes) == 1 and sum(len(part) for part in changes[0]) <= CHANGES_INLINE_LENGTH
