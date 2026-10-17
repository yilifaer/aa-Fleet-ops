# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The PAP characters per player of one fleet.

The setting "PAP characters per player" (``FleetOpsSettings.attendance_limit``, empty = unlimited) is the default
of every new fleet; each fleet keeps its own copy in ``FleetOperation.pap_character_limit``. The FC can set it on
the Start Fleet page and, while the fleet runs, on the fleet page; the PAP engine reads the latest value when the
fleet closes. After the close only FC leads (``manage_fleets``) change it, with a written reason, and the PAP of
the fleet is calculated again.

Everybody except FC leads may only tighten it: a value must not be higher than the setting when the setting is
set (any value when it is not). FC leads may choose any value, including no limit.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from fleetops import texts
from fleetops.constants import MAX_PAP_CHARACTER_LIMIT, OVERRIDE_REASON_MIN_LENGTH
from fleetops.models import FleetOperation, FleetOpsSettings
from fleetops.services.audit import audit

Status = FleetOperation.Status
LEAD_PERMISSION = "fleetops.manage_fleets"
# The audit action of a change made after the fleet started.
AUDIT_ACTION = "pap.limit"
# Posted by the start forms.
FIELD = "pap_character_limit"


class LimitRefused(Exception):
    """A change the rules do not allow; ``str(exc)`` is the stored English text, ``status`` the HTTP status."""

    def __init__(self, message, status=403):
        super().__init__(message)
        self.status = status


@dataclass
class LimitChange:
    old: int | None
    new: int | None
    changed: bool
    # The PAP summary of a closed fleet that was calculated again, else None.
    summary: dict | None = None


def setting_limit(settings=None):
    """The default of every new fleet: the setting, None when it is empty (no limit)."""
    return (settings or FleetOpsSettings.get_solo()).attendance_limit


def is_lead(user) -> bool:
    return user is not None and user.has_perm(LEAD_PERMISSION)


def ceiling(user, settings=None):
    """The highest limit ``user`` may choose; None when any value (no limit included) is allowed."""
    if is_lead(user):
        return None
    return setting_limit(settings)


def check(user, value, settings=None):
    """The ``LimitRefused`` for a fleet limit ``value`` (None = no limit) that ``user`` must not set, else None."""
    if value is not None and not 1 <= value <= MAX_PAP_CHARACTER_LIMIT:
        return LimitRefused(texts.PAP_LIMIT_RANGE(max=MAX_PAP_CHARACTER_LIMIT), 400)
    highest = ceiling(user, settings)
    if highest is not None and (value is None or value > highest):
        return LimitRefused(texts.PAP_LIMIT_ABOVE_SETTING(max=highest), 403)
    return None


def refusal(user, value, settings=None) -> str:
    """Why ``user`` must not give a fleet the limit ``value`` (None = no limit); "" when they may."""
    problem = check(user, value, settings)
    return str(problem) if problem else ""


def parse(text):
    """The limit posted as text: None for empty (no limit); ValueError with a stored text when it is no number."""
    text = (text or "").strip()
    if not text:
        return None
    if text.isascii() and text.isdigit():
        return int(text)
    raise ValueError(texts.PAP_LIMIT_RANGE(max=MAX_PAP_CHARACTER_LIMIT))


def chosen_limit(cleaned_data, settings=None):
    """The limit a start form chose; the setting when the form carried none."""
    if FIELD in cleaned_data:
        return cleaned_data[FIELD]
    return setting_limit(settings)


def starting_value(user, stored, settings=None):
    """The limit a fleet starts with: ``stored`` (chosen when it was scheduled), else the setting.

    The setting may have been lowered since, or the fleet was scheduled before the limit could be chosen: a
    starter who may not choose the stored value gets the setting.
    """
    return setting_limit(settings) if refusal(user, stored, settings) else stored


def check_start(user, cleaned_data, settings=None) -> None:
    """Refuse (PermissionError) a start whose form chose a limit ``user`` may not set; the forms check it too.

    A start without a choice takes the setting, which everybody may have.
    """
    if FIELD in cleaned_data:
        message = refusal(user, cleaned_data[FIELD], settings)
        if message:
            raise PermissionError(message)


def change_access(user, operation) -> str:
    """How ``user`` can change the limit of a fleet that they manage: "running", "closed" or "" (not at all).

    Whether the user manages the fleet is up to the caller. The limit only matters to fleets whose PAP follows
    time in fleet.
    """
    if operation.pap_rules != FleetOperation.PapRules.TIME_IN_FLEET:
        return ""
    if operation.status == Status.ACTIVE:
        return "running"
    if operation.status == Status.CLOSED and is_lead(user):
        return "closed"
    return ""


def change_limit(operation, value, *, actor, reason="") -> LimitChange:
    """Give a fleet that runs, or one that closed, another limit; raises ``LimitRefused`` when the rules say no.

    A running fleet takes the value at once and uses it when it closes. A closed fleet (FC leads only, with a
    reason of at least ``OVERRIDE_REASON_MIN_LENGTH`` characters) has its PAP calculated again. The audit entry
    keeps the old and the new value and the reason; a change to the same value writes nothing.
    ``operation`` is brought up to date. The caller has checked that ``actor`` manages the fleet.
    """
    from fleetops.services import incentives
    from fleetops.services.pap import finalize_pap

    reason = (reason or "").strip()
    with transaction.atomic():
        current = FleetOperation.objects.select_for_update().get(pk=operation.pk)
        if current.status == Status.CLOSED and not is_lead(actor):
            raise LimitRefused(texts.PAP_LIMIT_CLOSED_LEADS())
        mode = change_access(actor, current)
        if not mode:
            if current.pap_rules != FleetOperation.PapRules.TIME_IN_FLEET:
                raise LimitRefused(texts.PAP_LIMIT_LEGACY(), 409)
            raise LimitRefused(texts.PAP_LIMIT_NOT_RUNNING(), 409)
        problem = check(actor, value)
        if problem:
            raise problem
        old = current.pap_character_limit
        if old == value:
            operation.pap_character_limit = old
            return LimitChange(old, value, False)
        if mode == "closed" and len(reason) < OVERRIDE_REASON_MIN_LENGTH:
            raise LimitRefused(texts.PAP_LIMIT_NEEDS_REASON(count=OVERRIDE_REASON_MIN_LENGTH), 400)
        current.pap_character_limit = value
        current.save(update_fields=[FIELD, "updated_at"])
        operation.pap_character_limit = value
        operation.updated_at = current.updated_at
        summary = finalize_pap(operation, actor=actor, reason=reason) if mode == "closed" else None
        new = {FIELD: value}
        if summary is not None:
            new["pap"] = summary
        audit(actor, AUDIT_ACTION, current, {FIELD: old}, new, reason=reason)
    if mode == "closed":
        # The FC points of the fleet's month are checked again like after any change to a closed fleet.
        incentives.refresh_months_after_commit(*incentives.months_of(operation), actor=actor)
    return LimitChange(old, value, True, summary)
