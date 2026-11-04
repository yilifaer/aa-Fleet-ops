# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Where a loss happened and what kind of ship it was (RUL-1): the lookup service (django-eveonline-sde first, then
ESI, cached) and the fields a stored killmail gets from it.

ESI is never reached: the three reads of ``providers/esi_universe`` are patched, and the SDE is a stand-in model.
"""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from fleetops.models import SrpKillmail
from fleetops.providers import esi_universe
from fleetops.providers.esi import FleetESIError
from fleetops.services import sde, universe
from fleetops.srp import killmails

from . import factories as f
from . import real_system_infos, real_type_groups
from .test_srp_killmails import HASH, KillmailTestCase

JITA = 30_000_142
JITA_ESI = {"constellation_id": 20_000_020, "name": "Jita", "security_status": 0.94591, "system_id": JITA}
KIMOTORO = {"constellation_id": 20_000_020, "name": "Kimotoro", "region_id": 10_000_002}
DOWN = FleetESIError("ESI_ERROR", "Could not read game data: ESI returned HTTP 503.", 503)


class FakeSdeModel:
    """The part of a django-eveonline-sde model the lookup reads."""

    def __init__(self, columns, rows):
        self._columns = columns
        self.rows = rows
        self._meta = SimpleNamespace(get_fields=lambda: [SimpleNamespace(name=name) for name in columns])
        self.objects = self
        self.queries = 0

    def filter(self, pk__in):
        self.pks = set(pk__in)
        return self

    def values_list(self, *fields):
        self.queries += 1
        return [self._row(row, fields) for row in self.rows if row["pk"] in self.pks]

    @staticmethod
    def _row(row, fields):
        return tuple(row[field] for field in fields)


class UniverseCase(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.system = self.patch(esi_universe, "get_system", return_value=dict(JITA_ESI))
        self.constellation = self.patch(esi_universe, "get_constellation", return_value=dict(KIMOTORO))
        self.type = self.patch(esi_universe, "get_type", return_value={"group_id": 25, "name": "Rifter"})

    def patch(self, obj, name, **kwargs):
        patcher = mock.patch.object(obj, name, **kwargs)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def with_sde(self, model):
        self.patch(sde, "sde_installed", return_value=True)
        self.patch(universe.apps, "get_model", return_value=model)


class SystemLookupTests(UniverseCase):
    def test_without_the_sde_the_system_and_its_region_come_from_esi(self):
        found = real_system_infos([JITA])

        info = found[JITA]
        self.assertEqual(info.security, Decimal("0.9459"))
        self.assertEqual((info.constellation_id, info.region_id), (20_000_020, 10_000_002))
        self.system.assert_called_once_with(JITA)
        self.constellation.assert_called_once_with(20_000_020)

    def test_the_second_lookup_comes_from_the_cache(self):
        real_system_infos([JITA])
        again = real_system_infos([JITA])

        self.assertEqual(again[JITA].region_id, 10_000_002)
        self.assertEqual((self.system.call_count, self.constellation.call_count), (1, 1))

    def test_the_security_is_stored_with_four_places_rounded_half_up(self):
        for raw, stored in ((0.94591, "0.9459"), (0.43876, "0.4388"), (0.45, "0.4500"), (-0.99999, "-1.0000"), (1.0, "1.0000")):
            self.assertEqual(universe.security_value(raw), Decimal(stored))
        for bad in (None, True, "x", float("nan"), 7):
            self.assertIsNone(universe.security_value(bad))

    def test_a_system_the_sde_knows_needs_no_esi_request(self):
        model = FakeSdeModel(
            {"id", "name", "security_status", "constellation"},
            [{"pk": JITA, "security_status": 0.94591, "constellation_id": 20_000_020, "constellation__region_id": 10_000_002}],
        )
        self.with_sde(model)

        info = real_system_infos([JITA])[JITA]

        self.assertEqual((info.security, info.region_id), (Decimal("0.9459"), 10_000_002))
        self.assertEqual(model.queries, 1)
        for call in (self.system, self.constellation, self.type):
            call.assert_not_called()

    def test_the_older_security_column_name_is_found_too(self):
        model = FakeSdeModel(
            {"id", "security", "constellation"},
            [{"pk": JITA, "security": 0.5, "constellation_id": 1, "constellation__region_id": 2}],
        )
        self.with_sde(model)

        self.assertEqual(real_system_infos([JITA])[JITA].security, Decimal("0.5000"))

    def test_an_sde_without_a_security_column_falls_back_to_esi(self):
        model = FakeSdeModel({"id", "name"}, [])
        self.with_sde(model)

        self.assertEqual(real_system_infos([JITA])[JITA].security, Decimal("0.9459"))
        self.system.assert_called_once()

    def test_an_sde_without_the_constellation_takes_the_region_from_esi(self):
        model = FakeSdeModel({"id", "security_status"}, [{"pk": JITA, "security_status": 0.9}])
        self.with_sde(model)
        self.patch(universe, "_esi_system", side_effect=AssertionError("ESI must not be asked for the system"))

        info = real_system_infos([JITA])[JITA]

        self.assertEqual(info.security, Decimal("0.9000"))
        self.assertIsNone(info.region_id)  # no constellation to ask for
        self.constellation.assert_not_called()

    def test_a_system_the_sde_does_not_know_is_asked_from_esi(self):
        model = FakeSdeModel({"id", "security_status", "constellation"}, [])
        self.with_sde(model)

        self.assertEqual(real_system_infos([JITA])[JITA].constellation_id, 20_000_020)
        self.system.assert_called_once()

    def test_a_broken_sde_is_swallowed_and_esi_answers(self):
        self.patch(sde, "sde_installed", return_value=True)
        self.patch(universe.apps, "get_model", side_effect=LookupError("no such model"))

        self.assertEqual(real_system_infos([JITA])[JITA].security, Decimal("0.9459"))

    def test_an_esi_failure_gives_nothing_and_is_remembered_for_an_hour(self):
        self.system.side_effect = DOWN

        self.assertEqual(real_system_infos([JITA]), {})
        self.assertEqual(real_system_infos([JITA]), {})

        self.assertEqual(self.system.call_count, 1)

    def test_a_failed_constellation_gives_a_system_without_a_region_that_is_asked_again_later(self):
        self.constellation.side_effect = DOWN

        info = real_system_infos([JITA])[JITA]

        self.assertEqual((info.security, info.region_id), (Decimal("0.9459"), None))
        with mock.patch.object(universe.cache, "set") as put:
            cache.clear()
            real_system_infos([JITA])
        stored = [call for call in put.call_args_list if call.args[0] == universe._system_key(JITA)]
        self.assertEqual(stored[0].args[2], universe.FAILURE_SECONDS)

    def test_a_complete_system_is_kept_for_seven_days(self):
        with mock.patch.object(universe.cache, "set") as put:
            real_system_infos([JITA])
        stored = [call for call in put.call_args_list if call.args[0] == universe._system_key(JITA)]
        self.assertEqual(stored[0].args[2], 7 * 24 * 3600)

    def test_an_answer_without_data_is_a_failure(self):
        self.system.return_value = {}

        self.assertEqual(real_system_infos([JITA]), {})

    def test_nonsense_ids_are_left_out_without_a_request(self):
        self.assertEqual(real_system_infos([None, 0, -5, "x", True]), {})
        self.assertEqual(real_system_infos([]), {})
        self.system.assert_not_called()

    def test_an_unexpected_error_in_the_cache_does_not_break_the_lookup(self):
        with mock.patch.object(universe.cache, "get_many", side_effect=ConnectionError("cache down")), mock.patch.object(
            universe.cache, "set", side_effect=ConnectionError("cache down")
        ):
            self.assertEqual(real_system_infos([JITA])[JITA].security, Decimal("0.9459"))


class TypeGroupTests(UniverseCase):
    def test_without_the_sde_the_group_comes_from_esi_and_is_cached(self):
        self.assertEqual(real_type_groups([f.RIFTER]), {f.RIFTER: 25})
        self.assertEqual(real_type_groups([f.RIFTER]), {f.RIFTER: 25})

        self.assertEqual(self.type.call_count, 1)

    def test_the_sde_answers_first(self):
        self.patch(sde, "ship_type_details", return_value={f.RIFTER: SimpleNamespace(group_id=25)})

        self.assertEqual(real_type_groups([f.RIFTER]), {f.RIFTER: 25})
        self.type.assert_not_called()

    def test_a_type_nobody_knows_is_left_out_and_remembered_for_an_hour(self):
        self.type.side_effect = DOWN

        self.assertEqual(real_type_groups([f.RIFTER]), {})
        self.assertEqual(real_type_groups([f.RIFTER]), {})

        self.assertEqual(self.type.call_count, 1)

    def test_a_broken_sde_does_not_stop_the_lookup(self):
        self.patch(sde, "ship_type_details", side_effect=RuntimeError("broken"))

        self.assertEqual(real_type_groups([f.RIFTER]), {f.RIFTER: 25})


# --- The fields of a stored killmail -------------------------------------------------------------------------------


class StoredLocationTests(KillmailTestCase):
    """The killmail row gets the location fields when it is stored, and an old row once, outside the quota lock."""

    def store(self):
        payload = f.esi_killmail_payload(killmail_id=131_000_777, time=self.now - timedelta(days=1))
        return killmails.store_killmail(payload, HASH)

    def test_a_stored_killmail_gets_the_location_and_the_ship_group(self):
        info = universe.SystemInfo(JITA, Decimal("0.9459"), 20_000_020, 10_000_002)
        with mock.patch.object(universe, "system_infos", return_value={JITA: info}) as systems, mock.patch.object(
            universe, "type_groups", return_value={f.RIFTER: 25}
        ):
            payload = f.esi_killmail_payload(killmail_id=131_000_778, time=self.now - timedelta(days=1), solar_system_id=JITA)
            km = killmails.store_killmail(payload, HASH)

        km.refresh_from_db()
        systems.assert_called_once_with([JITA])
        self.assertEqual(km.solar_system_security, Decimal("0.9459"))
        self.assertEqual((km.constellation_id, km.region_id, km.ship_group_id), (20_000_020, 10_000_002, 25))
        self.assertIsNotNone(km.location_checked_at)
        self.assertEqual(km.origin, SrpKillmail.Origin.ESI)

    def test_when_the_lookups_find_nothing_the_fields_stay_empty_but_the_check_is_noted(self):
        km = self.store()

        km.refresh_from_db()
        self.assertEqual((km.solar_system_security, km.region_id, km.ship_group_id), (None, None, None))
        self.assertIsNotNone(km.location_checked_at)

    def test_a_lookup_that_raises_never_stops_the_killmail_from_being_stored(self):
        with mock.patch.object(universe, "system_infos", side_effect=RuntimeError("boom")):
            km = self.store()

        self.assertTrue(SrpKillmail.objects.filter(pk=km.pk).exists())
        self.assertIsNotNone(SrpKillmail.objects.get(pk=km.pk).location_checked_at)

    def test_an_old_killmail_is_looked_up_once_when_it_is_fetched(self):
        user = f.create_user("pilot", perms=f.SRP_MEMBER_PERMS)
        old = f.srp_killmail(f.main_of(user), fitted="1000000")
        self.assertIsNone(old.location_checked_at)
        info = universe.SystemInfo(old.solar_system_id, Decimal("0.4388"), 1, 2)

        with mock.patch.object(universe, "system_infos", return_value={old.solar_system_id: info}) as systems, mock.patch.object(
            universe, "type_groups", return_value={}
        ):
            first = killmails.fetch_killmail(
                killmails.KillmailRef(old.pk, None, killmails.SOURCE_ID_HASH), zkill_ok=False
            )
            killmails.fetch_killmail(killmails.KillmailRef(old.pk, None, killmails.SOURCE_ID_HASH), zkill_ok=False)

        self.assertEqual(first.solar_system_security, Decimal("0.4388"))
        self.assertEqual(systems.call_count, 1)

    def test_submitting_a_claim_never_looks_anything_up(self):
        # The lookups call ESI, so they must not run while the quota lock is held: the claim check reads the stored
        # fields only.
        from fleetops.srp import claims

        user = f.create_user("lookup-pilot", perms=f.SRP_MEMBER_PERMS)
        f.srp_settings(enabled=True, percentage=Decimal("80"))
        km = f.srp_killmail(f.main_of(user), fitted="1000000", location_checked_at=timezone.now())
        token = claims.signing.dumps({"km": km.pk, "link": None, "u": user.pk, "cat": None}, salt=claims.PREVIEW_SALT)

        with mock.patch.object(universe, "system_infos", side_effect=AssertionError("lookup under the lock")), mock.patch.object(
            universe, "type_groups", side_effect=AssertionError("lookup under the lock")
        ):
            claim = claims.submit_claim(user, token)

        self.assertEqual(claim.status, "pending")
