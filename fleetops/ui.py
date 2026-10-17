# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The look of status words: which tone every status has, and the markup of a status pill.

A pill is a short word on a tinted background with a small dot (see ``fo-pill`` in ``fleetops.css``). One
tone always means one thing, on every page:

``ok``       success, paid, reached, granted, open, enabled, live
``warn``     pending, waiting for approval, in progress, not reached yet, stale, an estimate
``bad``      an error, a failure, a rejection, a limit that was passed
``info``     neutral information: PAP only, a PAP link, scheduled, approved and waiting for payment, "You"
``neutral``  ended, closed, skipped, manual, disabled, below a minimum, replaced, a category

Colour is never the only signal: a pill always carries its words. The maps below are plain dictionaries keyed
by the stored value of a status, so models, template tags and tests share them without importing each other.
"""

import re

from django.utils.html import format_html, format_html_join

TONE_OK = "ok"
TONE_WARN = "warn"
TONE_BAD = "bad"
TONE_INFO = "info"
TONE_NEUTRAL = "neutral"
TONES = (TONE_OK, TONE_WARN, TONE_BAD, TONE_INFO, TONE_NEUTRAL)

# Fleet types are told apart by the colour of a small dot, never by a status colour. The colours come from the
# theme's palette (``--fo-type-1`` ... ``--fo-type-6``), chosen by the primary key of the type.
TYPE_TONE_COUNT = 6
_TYPE_TONE_RE = re.compile(r"type-([1-6])")
_FONT_AWESOME_NAME_RE = re.compile(r"(?:fa-)?[a-z0-9]+(?:-[a-z0-9]+)*")

# FleetOperation.Status
OPERATION_STATUS_TONES = {
    "draft": TONE_NEUTRAL,
    "scheduled": TONE_INFO,
    "starting": TONE_WARN,
    "active": TONE_OK,
    "ending": TONE_WARN,
    "closed": TONE_NEUTRAL,
    "cancelled": TONE_NEUTRAL,
    "error": TONE_BAD,
}
# OperationAction.Status (a start or end step and its attempts); "running" is shown while a step works.
STEP_STATUS_TONES = {
    "success": TONE_OK,
    "failed": TONE_BAD,
    "skipped": TONE_NEUTRAL,
    "pending": TONE_WARN,
    "running": TONE_WARN,
}
# ReviewStatus (fleets and PAP rows)
REVIEW_STATUS_TONES = {"approved": TONE_OK, "pending": TONE_WARN, "rejected": TONE_BAD}
# SrpClaim.Status; an approved claim waits for its payment, which is information, not a warning.
SRP_STATUS_TONES = {
    "pending": TONE_WARN,
    "approved": TONE_INFO,
    "paid": TONE_OK,
    "rejected": TONE_BAD,
    "withdrawn": TONE_NEUTRAL,
}
# Level of an SRP warning or of the fleet check (fleetops.srp.display.LEVEL_*)
SRP_LEVEL_TONES = {"danger": TONE_BAD, "warning": TONE_WARN, "info": TONE_INFO, "ok": TONE_OK}
# Statistics status keys of fleetops.services.assessment; anything else (no target, no members, nothing to
# assess) is neutral on purpose: a month without a target is not assessed at all.
STATISTICS_TONES = {
    "met": TONE_OK,
    "ok": TONE_OK,
    "missed": TONE_BAD,
    "below": TONE_BAD,
    "in_progress": TONE_WARN,
    "below_so_far": TONE_WARN,
}
# IncentivePeriod.Status: an open or reviewed period can still change; a finalised one never does.
PERIOD_STATUS_TONES = {"open": TONE_WARN, "review": TONE_WARN, "finalized": TONE_OK}


def tone_for(mapping, key, default=TONE_NEUTRAL):
    """The tone a status has in one of the maps above; ``default`` for a status the map does not know."""
    try:
        return mapping.get(key, default)
    except TypeError:
        return default


def bool_tone(value):
    """Yes/No columns: yes is ``ok``, no is ``neutral``."""
    return TONE_OK if value else TONE_NEUTRAL


def type_tone(fleet_type):
    """The dot number (1-6) of a fleet type: ``pk % 6 + 1``; 0 when there is no type (a neutral dot)."""
    key = getattr(fleet_type, "pk", fleet_type)
    if isinstance(key, bool):
        return 0
    try:
        return int(key) % TYPE_TONE_COUNT + 1
    except (TypeError, ValueError):
        return 0


def tone_class(tone):
    """The classes of a pill with this tone: ``fo-pill fo-pill-ok``, or the type pill for ``type-3`` / ``3``.

    An unknown tone is neutral.
    """
    if isinstance(tone, int) and not isinstance(tone, bool):
        tone = f"type-{tone}"
    tone = str(tone or "").strip().lower()
    found = _TYPE_TONE_RE.fullmatch(tone)
    if found:
        return f"fo-pill fo-pill-type fo-type-{found.group(1)}"
    if tone == "type":
        return "fo-pill fo-pill-type"
    return f"fo-pill fo-pill-{tone if tone in TONES else TONE_NEUTRAL}"


def _icon_html(icon):
    """A Font Awesome glyph for ``icon`` (``"users"`` or ``"fa-users"``); nothing for an odd name."""
    name = str(icon or "").strip()
    if not _FONT_AWESOME_NAME_RE.fullmatch(name):
        return ""
    name = name if name.startswith("fa-") else f"fa-{name}"
    return format_html('<i class="fa-solid {}" aria-hidden="true"></i>', name)


def _attribute_name(name):
    """``data_state`` -> ``data-state``, ``aria_label`` -> ``aria-label``; ``None`` for a name that is not allowed."""
    name = str(name)
    if name == "class_":
        return "class"
    if name in {"id", "title", "class", "role"}:
        return name
    for prefix in ("data", "aria"):
        rest = name[len(prefix) + 1 :]
        if name.startswith(prefix + "_") and re.fullmatch(r"[a-z0-9_]+", rest):
            return f"{prefix}-{rest.replace('_', '-')}"
    return None


def pill_html(tone, text, *, icon="", **attrs):
    """A status pill as safe HTML: ``<span class="fo-pill fo-pill-ok">Active</span>``.

    ``tone`` is one of ``TONES``, ``"type-N"`` or the number N of a fleet type dot; ``text`` is escaped. ``icon``
    is a Font Awesome name that takes the place of the dot. ``attrs`` may hold ``id``, ``title``, ``role``,
    ``class`` (or ``class_``; added to the pill classes), and ``data_*`` / ``aria_*`` (underscores become
    hyphens). Other names, and values that are ``None``, are left out.
    """
    classes = tone_class(tone)
    glyph = _icon_html(icon)
    if glyph:
        classes += " fo-pill-icon"
    values = []
    for name, value in attrs.items():
        attribute = _attribute_name(name)
        if value is None or value is False or attribute is None:
            continue
        if attribute == "class":
            classes += f" {value}"
        else:
            values.append((attribute, value))
    attributes = format_html_join("", ' {}="{}"', values)
    return format_html('<span class="{}"{}>{}{}</span>', classes, attributes, glyph, text)
