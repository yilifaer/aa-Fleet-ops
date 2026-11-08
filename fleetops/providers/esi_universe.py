# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Public ESI reads used by SRP to place a loss: a solar system, its constellation and the group of a ship type.

The calls go through the shared client in ``fleetops.providers.esi`` (``UNIVERSE_OPERATIONS`` is part of its
``operations`` list) and need no token. django-esi keeps each answer in its response cache for as long as ESI's
``Expires`` header says; ``fleetops.services.universe`` adds its own longer cache on top for these static data.
"""

import threading

from fleetops import texts
from fleetops.providers import esi as esi_provider
from fleetops.providers.esi import _read
from fleetops.providers.esi_killmails import _failure, to_plain

UNIVERSE_OPERATIONS = esi_provider.UNIVERSE_OPERATIONS
_calls = threading.local()


def calls_made() -> int:
    """How many reads this thread has started; the loss scan charges them to its ESI budget."""
    return getattr(_calls, "count", 0)


def _read_plain(operation) -> dict:
    _calls.count = calls_made() + 1
    try:
        payload = to_plain(_read(operation))
    except Exception as exc:  # django-esi raises its own HTTP exception classes
        raise _failure("ESI_ERROR", texts.UNIVERSE_READ_FAILED, exc) from exc
    return payload if isinstance(payload, dict) else {}


def get_system(system_id: int) -> dict:
    """``GET /universe/systems/{system_id}`` as a plain dict (``security_status``, ``constellation_id``, ...)."""
    return _read_plain(esi_provider.esi.client.Universe.GetUniverseSystemsSystemId(system_id=int(system_id)))


def get_constellation(constellation_id: int) -> dict:
    """``GET /universe/constellations/{constellation_id}`` as a plain dict (``region_id``, ...)."""
    return _read_plain(
        esi_provider.esi.client.Universe.GetUniverseConstellationsConstellationId(
            constellation_id=int(constellation_id)
        )
    )


def get_group(group_id: int) -> dict:
    """``GET /universe/groups/{group_id}`` as a plain dict (``category_id``, ``name``, ...)."""
    return _read_plain(esi_provider.esi.client.Universe.GetUniverseGroupsGroupId(group_id=int(group_id)))


def get_type(type_id: int) -> dict:
    """``GET /universe/types/{type_id}`` as a plain dict (``group_id``, ``name``, ...)."""
    return _read_plain(esi_provider.esi.client.Universe.GetUniverseTypesTypeId(type_id=int(type_id)))
