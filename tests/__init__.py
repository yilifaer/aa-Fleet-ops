# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The FleetOps tests.

SRP looks up where a killmail happened (security status, region) and what group the ship is in. The lookups would
reach ESI from any test that stores a killmail, so they are patched for the whole run to find nothing; a loss then has
no location, which blocks nothing. A test that needs a location patches ``fleetops.services.universe`` itself, and the
tests of the service itself call the real functions kept here.
"""

from unittest import mock

from fleetops.services import universe

# The real lookups, for the tests of the universe service itself.
real_system_infos = universe.system_infos
real_type_groups = universe.type_groups

mock.patch.object(universe, "system_infos", return_value={}).start()
mock.patch.object(universe, "type_groups", return_value={}).start()
