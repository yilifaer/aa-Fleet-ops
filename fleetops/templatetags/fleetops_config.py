# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

import functools
import json
import re

from django import template
from django.apps import apps
from django.utils.text import capfirst
from django.utils.translation import gettext_lazy, pgettext_lazy

from fleetops.texts import translate_stored

register = template.Library()


@register.filter
def is_bool(value):
    """True only for real booleans; 1, 0 and Decimal("1.00") compare equal to True/False."""
    return isinstance(value, bool)


@register.filter
def is_copyable(value):
    """True for list cells that are shown with a Copy button, such as comms join links."""
    return getattr(value, "copyable", False) is True


# The actions the audit log stores (a code such as ``srp.claim_approve``), as words for the "Action" column.
# A code that is not listed (an entry of a later or older version) is shown as stored.
AUDIT_ACTION_LABELS = {
    "approval.attendance_approve": gettext_lazy("PAP approved"),
    "approval.attendance_reject": gettext_lazy("PAP rejected"),
    "approval.operation_approve": gettext_lazy("Fleet approved"),
    "approval.operation_reject": gettext_lazy("Fleet rejected"),
    "approval.operation_resubmit": gettext_lazy("Fleet submitted again"),
    "attendance.manual_create": gettext_lazy("PAP added by hand"),
    "attendance.manual_delete": gettext_lazy("PAP removed"),
    "attendance.multiplier": gettext_lazy("PAP multiplier changed"),
    "attendance.replace_auto": gettext_lazy("Automatic PAP replaced"),
    "attendance.roster_import": gettext_lazy("PAP imported from a roster"),
    "configuration.create": gettext_lazy("Setting created"),
    "configuration.update": gettext_lazy("Setting changed"),
    "configuration.delete": gettext_lazy("Setting deleted"),
    "configuration.discord_role_sync": gettext_lazy("Discord roles synced"),
    "fleet.auto_end": gettext_lazy("Fleet ended automatically"),
    "fleet.boss_handover": gettext_lazy("Fleet boss handed over"),
    "fleet.end": gettext_lazy("Fleet ended"),
    "fleet.end_request": gettext_lazy("Fleet end requested"),
    "fleet.invite": gettext_lazy("Members invited"),
    "fleet.kick_capsules": gettext_lazy("Capsule pilots kicked"),
    "fleet.kick_member": gettext_lazy("Member kicked"),
    "fleet.kick_outside_system": gettext_lazy("Members outside a system kicked"),
    "fleet.manual_create": gettext_lazy("Fleet added by hand"),
    "fleet.member_move": gettext_lazy("Fleet member moved"),
    "fleet.motd_regenerate": gettext_lazy("MOTD made again"),
    "fleet.retry_motd": gettext_lazy("MOTD sent again"),
    "fleet.retry_ping": gettext_lazy("Ping sent again"),
    "fleet.retry_srp": gettext_lazy("SRP link made again"),
    "fleet.role_create": gettext_lazy("Special role added"),
    "fleet.role_delete": gettext_lazy("Special role removed"),
    "fleet.role_update": gettext_lazy("Special role changed"),
    "fleet.schedule": gettext_lazy("Fleet scheduled"),
    "fleet.schedule_cancel": gettext_lazy("Scheduled fleet cancelled"),
    "fleet.schedule_edit": gettext_lazy("Scheduled fleet changed"),
    "fleet.schedule_start_refused": gettext_lazy("Start of a scheduled fleet refused"),
    "fleet.squad_create": gettext_lazy("Squad created"),
    "fleet.squad_delete": gettext_lazy("Squad deleted"),
    "fleet.squad_rename": gettext_lazy("Squad renamed"),
    "fleet.start": gettext_lazy("Fleet started"),
    "fleet.start_abandoned": gettext_lazy("Fleet start abandoned"),
    "fleet.stats_mode": gettext_lazy("Counting mode changed"),
    "fleet.tracking_error": gettext_lazy("Tracking stopped by an error"),
    "fleet.tracking_recovered": gettext_lazy("Tracking recovered"),
    "fleet.wing_create": gettext_lazy("Wing created"),
    "fleet.wing_delete": gettext_lazy("Wing deleted"),
    "fleet.wing_rename": gettext_lazy("Wing renamed"),
    "fleet.invite_job": gettext_lazy("Invitations sent"),
    "fleet.kick_job": gettext_lazy("Kicks carried out"),
    "fleet.move_job": gettext_lazy("Members moved"),
    "fleet.template_job": gettext_lazy("Structure template applied"),
    "group.deleted": gettext_lazy("Alliance Auth group deleted"),
    "incentive.auto_recalculate": gettext_lazy("Incentives recalculated automatically"),
    "incentive.finalize": gettext_lazy("Incentive period finalized"),
    "incentive.period_create": gettext_lazy("Incentive period created"),
    "incentive.period_update": gettext_lazy("Incentive period changed"),
    "incentive.recalculate": gettext_lazy("Incentives recalculated"),
    "incentive.unlock": gettext_lazy("Incentive period reopened"),
    "incentive.waiver": gettext_lazy("Incentive waiver changed"),
    "operation.edit": gettext_lazy("Fleet edited"),
    "pap.limit": gettext_lazy("PAP limit changed"),
    "pap.recalculate": gettext_lazy("PAP recalculated"),
    "pap.register": gettext_lazy("PAP registered"),
    "pap_link.create": gettext_lazy("PAP link created"),
    "pap_link.revoke": gettext_lazy("PAP link revoked"),
    "ping.send": gettext_lazy("Ping sent"),
    "srp.ban_create": gettext_lazy("SRP ban created"),
    "srp.ban_lift": gettext_lazy("SRP ban lifted"),
    "srp.export": gettext_lazy("SRP export downloaded"),
    "srp.import_aasrp": gettext_lazy("aa-srp data imported"),
    "srp.import_aasrp_undo": gettext_lazy("aa-srp import undone"),
    "srp.import_enrich": gettext_lazy("Imported claims completed"),
    "srp.link_close": gettext_lazy("SRP link closed"),
    "srp.link_complete": gettext_lazy("SRP link completed"),
    "srp.link_create": gettext_lazy("SRP link created"),
    "srp.link_open": gettext_lazy("SRP link opened"),
    "srp.link_uncomplete": gettext_lazy("SRP link completion undone"),
    "srp.link_update": gettext_lazy("SRP link changed"),
    "srp.loss_ignore": gettext_lazy("Loss hidden"),
    "srp.loss_unignore": gettext_lazy("Loss shown again"),
    "srp.payment_batch": gettext_lazy("Payment batch made"),
    "statistics.export": gettext_lazy("Statistics export downloaded"),
}
# ``srp.claim_<action>``: the word of the claim event ("approve", "paid", ...), see SrpClaimEvent.Action.
AUDIT_CLAIM_PREFIX = "srp.claim_"


@register.filter
def audit_action_label(action):
    """The "Action" column of the audit log in the viewer's language; an unknown code is shown as stored."""
    action = str(action or "")
    label = AUDIT_ACTION_LABELS.get(action)
    if label is not None:
        return label
    if action.startswith(AUDIT_CLAIM_PREFIX):
        from fleetops.models import SrpClaimEvent

        word = action[len(AUDIT_CLAIM_PREFIX) :]
        if word in SrpClaimEvent.Action.values:
            return SrpClaimEvent.Action(word).label
    return action


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


_STATUS_KEY = re.compile(r"(^|_)(status|state)$")


@functools.lru_cache(maxsize=None)
def _status_words(model_name="", key="") -> dict:
    """``{stored word: name}`` of the statuses of FleetOps models (pending, approved, paid, ...).

    The field called ``key`` of the model ``model_name`` names a word first (a word reads differently on the claim
    and on the attendance); the status fields of every other model fill in what is left. The names are lazy, so
    they follow the viewer's language.
    """
    words = {}
    models = list(apps.get_app_config("fleetops").get_models())
    models.sort(key=lambda model: model.__name__ != model_name)
    for model in models:
        fields = sorted(model._meta.get_fields(), key=lambda field: field.name != key)
        for field in fields:
            if getattr(field, "choices", None) and _STATUS_KEY.search(field.name):
                for stored, name in field.flatchoices:
                    words.setdefault(str(stored), name)
    return words


def _show_text(key, text, model_name="") -> str:
    """A stored text in the viewer's language: a status word of a ``status`` or ``state`` value, or a message of
    FleetOps (an error text, ...). Text FleetOps does not know stays as stored."""
    if _STATUS_KEY.search(str(key)):
        word = _status_words(model_name, str(key)).get(text)
        if word is not None:
            return str(word)
    return translate_stored(text)


def _show(key, value, model_name="") -> str:
    """One stored value as short text, in the viewer's language; secrets are hidden."""
    if isinstance(value, bool):
        # A flag such as ``webhook_url_replaced`` is no secret; it is what tells that something was replaced.
        return "true" if value else "false"
    if _SECRET_KEY.search(str(key)):
        return "" if value in (None, "", [], {}) else HIDDEN
    if value is None or value in ({}, []):
        # An empty snapshot is "nothing", not the code "{}".
        return "—"
    text = _show_text(key, value, model_name) if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    text = _URL_USERINFO.sub(rf"\1{HIDDEN}@", _SECRET_VALUE.sub(HIDDEN, text))
    return text if len(text) <= CHANGE_VALUE_LENGTH else text[: CHANGE_VALUE_LENGTH - 1] + "…"


class FlagName(str):
    """The name of a change that is a plain statement: shown as text, without a code name and old and new value."""

    is_flag = True


# Flags the audit log shows as a statement when they became true (no secret is behind them).
AUDIT_FLAG_TEXTS = {"webhook_url_replaced": gettext_lazy("Webhook address replaced")}


def _change(name, old, new, has_old, has_new, model_name=""):
    if name in AUDIT_FLAG_TEXTS and new is True and old is not True:
        return (FlagName(AUDIT_FLAG_TEXTS[name]), "", "")
    return (
        name,
        _show(name, old, model_name) if has_old else "—",
        _show(name, new, model_name) if has_new else "—",
    )


@register.filter
def audit_changes(row):
    """The "Changes" column of the audit log: ``[(name, old, new)]`` of what an entry changed, as short text.

    Only the values that differ are listed; a created or deleted object lists all of its values. The entry's own
    JSON is read, so the page costs no query. Webhook URLs, tokens and the like are shown as ``***``.
    """
    old, new = getattr(row, "old_value", None), getattr(row, "new_value", None)
    model_name = getattr(row, "object_type", "") or ""
    if old is None and new is None:
        return []
    if isinstance(old, dict) or isinstance(new, dict):
        old = old if isinstance(old, dict) else {}
        new = new if isinstance(new, dict) else {}
        names = sorted(set(old) | set(new), key=str)
        return [
            _change(name, old.get(name), new.get(name), name in old, name in new, model_name)
            for name in names
            if name not in old or name not in new or old[name] != new[name]
        ]
    return [("", _show("", old, model_name), _show("", new, model_name))]


@register.filter
def changes_are_short(changes) -> bool:
    """Whether the changes fit in the table cell without a fold-out."""
    return len(changes) == 1 and sum(len(part) for part in changes[0]) <= CHANGES_INLINE_LENGTH
