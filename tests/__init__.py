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


# MariaDB keeps every column dropped by an instant ALTER in the row layout of the table until the table is rebuilt.
# The migration tests go back and forth over the migrations that add and drop columns of the fleet types many times
# in one run, which fills that layout ("Row size too large") after about 15 rounds although a real upgrade or
# rollback does it once. So after every migration run by a test, the small table is rebuilt.
def _rebuild_fleet_type_table_after_migrate():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    original = MigrationExecutor.migrate

    def migrate(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        finally:
            if connection.vendor == "mysql" and "fleetops_fleettype" in connection.introspection.table_names():
                with connection.cursor() as cursor:
                    cursor.execute("ALTER TABLE fleetops_fleettype FORCE")

    MigrationExecutor.migrate = migrate


_rebuild_fleet_type_table_after_migrate()
