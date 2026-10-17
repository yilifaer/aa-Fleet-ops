# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Keep the clickable MOTD links (FC character, form-up system) off the network and off the shared cache.

Rendering the default MOTD looks up the bloodline of the FC character and, when the form-up text is a solar
system name, the system. A test module that renders messages imports ``setUpModule`` and ``tearDownModule`` from
here (unittest runs them around the module):

    from .link_fakes import setUpModule, tearDownModule  # noqa: F401

Every character then has the bloodline ``DEFAULT_BLOODLINE`` (character type 1378) and the only known solar
systems are the ones in ``SYSTEMS`` (the test project has no SDE, so ESI answers); the lookups use a private
in-memory cache. A test that needs other answers changes ``FAKE.bloodlines`` or ``FAKE.systems`` (``start_fakes``
resets both; ``test_eve_links.py`` starts fresh fakes for every test).
"""

from unittest import mock

from django.core.cache.backends.locmem import LocMemCache

from fleetops.providers import esi as esi_provider
from fleetops.services import eve_links

DEFAULT_BLOODLINE = 8
DEFAULT_CHARACTER_TYPE = eve_links.BLOODLINE_CHARACTER_TYPES[DEFAULT_BLOODLINE]
SYSTEMS = {"jita": (30000142, "Jita"), "amarr": (30002187, "Amarr"), "dodixie": (30002659, "Dodixie")}


class FakeLookups:
    """The answers of the ESI calls behind the links; ``calls`` records what was asked."""

    def __init__(self):
        self.bloodlines = {}
        self.systems = dict(SYSTEMS)
        self.calls = []
        self.fail = False

    def character_bloodline(self, character_id):
        self.calls.append(("character", int(character_id)))
        if self.fail:
            return None
        return self.bloodlines.get(int(character_id), DEFAULT_BLOODLINE)

    def universe_system_ids(self, names):
        self.calls.append(("systems", tuple(names)))
        if self.fail:
            raise esi_provider.FleetESIError("SYSTEM_IDS_ERROR", "ESI is down.")
        return {name.casefold(): self.systems[name.casefold()] for name in names if name.casefold() in self.systems}


FAKE = FakeLookups()
_patchers = []


def start_fakes():
    """Install the fake lookups and a private cache; returns the fake. Called by ``setUpModule``."""
    FAKE.__init__()
    cache = LocMemCache("fleetops-eve-links", {})
    cache.clear()
    _patchers.extend(
        [
            mock.patch.object(eve_links, "cache", cache),
            mock.patch.object(esi_provider, "character_bloodline", FAKE.character_bloodline),
            mock.patch.object(esi_provider, "universe_system_ids", FAKE.universe_system_ids),
        ]
    )
    for patcher in _patchers:
        patcher.start()
    return FAKE


def stop_fakes():
    while _patchers:
        _patchers.pop().stop()


def setUpModule():  # noqa: N802 - the name unittest looks for
    start_fakes()


def tearDownModule():  # noqa: N802
    stop_fakes()


def character_link(character_id, name, type_id=DEFAULT_CHARACTER_TYPE):
    """The MOTD link of a character as the fakes make it."""
    return f'<a href="showinfo:{type_id}//{character_id}">{name}</a>'


def system_link(system_id, name):
    return f'<loc><a href="showinfo:5//{system_id}">{name}</a></loc>'
