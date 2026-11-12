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
# A fleet type may carry a chosen colour (FleetType.color): "" (automatic), "theme-N" (one of the six theme colours) or
# a custom "#rrggbb". Only these shapes are ever written into a page; anything else is treated as "not set".
_THEME_COLOR_RE = re.compile(r"theme-([1-6])")
_HEX_COLOR_RE = re.compile(r"#(?:[0-9a-f]{3}|[0-9a-f]{6})", re.IGNORECASE)
_STORED_HEX_RE = re.compile(r"#[0-9a-f]{6}")
# The page backgrounds of the two themes the plugin is tested in (Flatly and Darkly) and the contrast a coloured
# dot should have against them. The dot never carries meaning alone (the pill always has the type's name), so the
# limit is 2:1, below the 3:1 WCAG asks of a graphic that is needed to understand the page; it still catches the
# colours that vanish (white on the light theme, near black on the dark theme).
THEME_BACKGROUNDS = (("light", "#ffffff"), ("dark", "#222222"))
MIN_DOT_CONTRAST = 2.0
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


def normalize_type_color(value):
    """The stored form of a fleet type colour: ``""``, ``"theme-N"`` (N 1-6) or ``"#rrggbb"`` in lower case.

    ``#rgb`` becomes ``#rrggbb``. Raises ``ValueError`` for anything else (names, ``rgb()``, ``url(...)``, extra text).
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if _THEME_COLOR_RE.fullmatch(text):
        return text
    if _HEX_COLOR_RE.fullmatch(text):
        digits = text[1:].lower()
        if len(digits) == 3:
            digits = "".join(char * 2 for char in digits)
        return f"#{digits}"
    raise ValueError(text)


def _stored_type_color(fleet_type):
    """The valid stored colour of a fleet type object (``""`` for none, a number, or a value that is not valid)."""
    value = getattr(fleet_type, "color", "")
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if _THEME_COLOR_RE.fullmatch(value) or _STORED_HEX_RE.fullmatch(value):
        return value
    return ""


def type_custom_color(fleet_type):
    """``"#rrggbb"`` when the type has a valid custom colour, else ``""``."""
    value = _stored_type_color(fleet_type)
    return value if value.startswith("#") else ""


def type_tone(fleet_type):
    """The dot number (1-6) of a fleet type; 0 when there is no type or it has a custom colour (a neutral dot).

    A theme colour set on the type wins; without one the number is ``pk % 6 + 1``.
    """
    stored = _stored_type_color(fleet_type)
    if stored.startswith("#"):
        return 0
    if stored:
        return int(stored[-1])
    key = getattr(fleet_type, "pk", fleet_type)
    if isinstance(key, bool):
        return 0
    try:
        return int(key) % TYPE_TONE_COUNT + 1
    except (TypeError, ValueError):
        return 0


def type_auto_tone(fleet_type):
    """The dot number (1-6) a type gets when it has no colour (``pk % 6 + 1``), whatever colour is stored; 0 for a new type."""
    key = getattr(fleet_type, "pk", None)
    if key is None or isinstance(key, bool):
        return 0
    try:
        return int(key) % TYPE_TONE_COUNT + 1
    except (TypeError, ValueError):
        return 0


def type_css_color(fleet_type):
    """The CSS colour of a fleet type for ``--fo-dot`` / ``--fo-bar``: ``var(--fo-type-N)``, ``#rrggbb`` or ``""``."""
    custom = type_custom_color(fleet_type)
    if custom:
        return custom
    number = type_tone(fleet_type)
    return f"var(--fo-type-{number})" if number else ""


def _channels(color):
    digits = color[1:]
    if len(digits) == 3:
        digits = "".join(char * 2 for char in digits)
    return [int(digits[index : index + 2], 16) / 255 for index in (0, 2, 4)]


def relative_luminance(color):
    """WCAG relative luminance (0 black, 1 white) of ``#rgb`` / ``#rrggbb``."""
    red, green, blue = (value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4 for value in _channels(color))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first, second):
    """WCAG contrast ratio (1 to 21) of two ``#rrggbb`` colours."""
    light, dark = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def ink_for(color):
    """The text colour for a pill filled with ``color``: ``#000000`` or ``#ffffff``, whichever reads better."""
    return "#000000" if contrast_ratio(color, "#000000") >= contrast_ratio(color, "#ffffff") else "#ffffff"


def poor_contrast_themes(color):
    """The themes (``"light"`` / ``"dark"``) whose page background the dot colour hardly stands out from."""
    return [name for name, background in THEME_BACKGROUNDS if contrast_ratio(color, background) < MIN_DOT_CONTRAST]


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


def pill_html(tone, text, *, icon="", dot="", **attrs):
    """A status pill as safe HTML: ``<span class="fo-pill fo-pill-ok">Active</span>``.

    ``tone`` is one of ``TONES``, ``"type-N"`` or the number N of a fleet type dot; ``text`` is escaped. ``icon``
    is a Font Awesome name that takes the place of the dot. ``attrs`` may hold ``id``, ``title``, ``role``,
    ``class`` (or ``class_``; added to the pill classes), and ``data_*`` / ``aria_*`` (underscores become
    hyphens). Other names, and values that are ``None``, are left out. ``dot`` is a custom ``#rrggbb`` colour for
    the dot (a fleet type's own colour); any other value is ignored.
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
    if isinstance(dot, str) and _STORED_HEX_RE.fullmatch(dot):
        attributes += format_html(' style="--fo-dot: {}"', dot)
    return format_html('<span class="{}"{}>{}{}</span>', classes, attributes, glyph, text)
