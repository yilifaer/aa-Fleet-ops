# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Clickable links for the in-game MOTD: the FC's character and the form-up solar system.

The game opens an information window for ``<a href="showinfo:<type>//<id>">``. A character's link uses the
type of the character's bloodline (1373 to 1386); a solar system is ``showinfo:5//<system id>`` inside
``<loc>``. Both come from ESI (a system from the SDE when it is installed) and are remembered, because the
preview of the start page renders the MOTD for every change of the form.

Everything here is best effort and never raises: when a lookup fails the character gets the default type and
the form-up place stays the text as it was typed. A failed lookup is remembered for a short time, so a broken
ESI is not asked again on every preview.
"""

import hashlib
import html
from datetime import timedelta

from allianceauth.services.hooks import get_extension_logger
from django.core.cache import cache

from fleetops.providers import esi as esi_provider
from fleetops.services import sde

logger = get_extension_logger(__name__)

# Bloodline ID -> character type ID. Verified against ESI and the game client for the Minmatar Brutor (4 -> 1380)
# and a Gallente-line character (8 -> 1378); the rest follows the long-standing community table.
BLOODLINE_CHARACTER_TYPES = {
    5: 1373,
    6: 1374,
    2: 1375,
    1: 1376,
    7: 1377,
    8: 1378,
    3: 1379,
    4: 1380,
    9: 1381,
    10: 1382,
    11: 1383,
    12: 1384,
    13: 1385,
    14: 1386,
}
# The "CharacterType" of ESI: used for bloodlines outside the table and whenever a lookup fails.
DEFAULT_CHARACTER_TYPE = 1373
# The type of a solar system in a showinfo link.
SOLAR_SYSTEM_TYPE = 5

# A bloodline never changes; a failed lookup is tried again after a few minutes.
BLOODLINE_SECONDS = int(timedelta(days=30).total_seconds())
LOOKUP_FAILED_SECONDS = int(timedelta(minutes=10).total_seconds())
SYSTEM_FOUND_SECONDS = int(timedelta(days=30).total_seconds())
SYSTEM_MISSING_SECONDS = int(timedelta(days=1).total_seconds())
# An ESI error for a system name is not a statement about the name: asked again soon, but not on every preview.
SYSTEM_ERROR_SECONDS = 60
# Form-up text this long or longer, or with other characters, is ordinary text and never looked up.
MAX_SYSTEM_NAME_LENGTH = 40
_SYSTEM_NAME_EXTRA = frozenset(" -'")


def escaped(text) -> str:
    """Text for the MOTD: only ``&``, ``<`` and ``>`` are escaped, so a name such as O'Brien reads as typed."""
    return html.escape(str(text or ""), quote=False)


def _character_key(character_id: int) -> str:
    return f"fleetops:eve:chartype:{character_id}"


def _system_key(name: str) -> str:
    digest = hashlib.sha1(name.casefold().encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"fleetops:eve:system:{digest}"


def _normalise(name) -> str:
    return " ".join(str(name or "").split())


def looks_like_system_name(name: str) -> bool:
    """Whether ``name`` could be a solar system: 1 to 40 letters, digits, spaces, hyphens and apostrophes."""
    if not 0 < len(name) <= MAX_SYSTEM_NAME_LENGTH:
        return False
    return all(char.isalnum() or char in _SYSTEM_NAME_EXTRA for char in name)


def character_type_id(character_id) -> int:
    """The type ID of the character's bloodline for ``showinfo`` links; 1373 when it cannot be found out."""
    try:
        character_id = int(character_id)
    except (TypeError, ValueError):
        return DEFAULT_CHARACTER_TYPE
    try:
        remembered = cache.get(_character_key(character_id))
        if remembered is not None:
            return int(remembered) or DEFAULT_CHARACTER_TYPE
        bloodline = esi_provider.character_bloodline(character_id)
        if bloodline is None:
            cache.set(_character_key(character_id), 0, LOOKUP_FAILED_SECONDS)
            return DEFAULT_CHARACTER_TYPE
        type_id = BLOODLINE_CHARACTER_TYPES.get(bloodline, DEFAULT_CHARACTER_TYPE)
        cache.set(_character_key(character_id), type_id, BLOODLINE_SECONDS)
        return type_id
    except Exception as exc:
        logger.warning("FleetOps could not work out the character type of a link (%s).", type(exc).__name__)
        return DEFAULT_CHARACTER_TYPE


def character_link(character_id, name) -> str:
    """``<a href="showinfo:<type>//<id>">name</a>``; just the name when the character has no ID."""
    if not character_id:
        return escaped(name)
    return f'<a href="showinfo:{character_type_id(character_id)}//{int(character_id)}">{escaped(name)}</a>'


def remember_character_type(character_id, type_id) -> None:
    """Store what a successful lookup stores, for tests and for the seed of the interface checks."""
    cache.set(_character_key(int(character_id)), int(type_id), BLOODLINE_SECONDS)


def solar_system(name):
    """``(system_id, English name)`` of the solar system called ``name``, or None.

    Only text that looks like a system name is tried; anything else is ordinary form-up text and costs
    nothing. The SDE answers when it is installed, otherwise ESI does. Never raises.
    """
    name = _normalise(name)
    if not looks_like_system_name(name):
        return None
    key = _system_key(name)
    try:
        remembered = cache.get(key)
        if remembered is not None:
            return tuple(remembered) or None
        if sde.sde_installed():
            found = sde.find_solar_system(name)
            cache.set(key, found or (), SYSTEM_FOUND_SECONDS if found else SYSTEM_MISSING_SECONDS)
            return found
        try:
            answer = esi_provider.universe_system_ids([name])
        except Exception as exc:
            logger.warning("FleetOps could not look up a solar system name (%s).", type(exc).__name__)
            cache.set(key, (), SYSTEM_ERROR_SECONDS)
            return None
        found = answer.get(name.casefold())
        cache.set(key, found or (), SYSTEM_FOUND_SECONDS if found else SYSTEM_MISSING_SECONDS)
        return found
    except Exception as exc:
        logger.warning("FleetOps could not look up a solar system name (%s).", type(exc).__name__)
        return None


def system_link(name, system=None) -> str:
    """``<loc><a href="showinfo:5//<id>">English name</a></loc>``, or the text as typed.

    ``system`` is the result of :func:`solar_system` when the caller has it already.
    """
    found = system if system is not None else solar_system(name)
    if not found:
        return escaped(name)
    system_id, english_name = found
    return f'<loc><a href="showinfo:{SOLAR_SYSTEM_TYPE}//{int(system_id)}">{escaped(english_name)}</a></loc>'


def remember_solar_system(name, system_id, english_name) -> None:
    """Store what a successful lookup stores, for tests and for the seed of the interface checks."""
    cache.set(_system_key(_normalise(name)), (int(system_id), str(english_name)), SYSTEM_FOUND_SECONDS)
