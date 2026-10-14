"""Reading killmails for SRP: the stored copy, ESI as the source, zKillboard for hashes and values.

Neither ESI nor zKillboard is reached: ``get_killmail`` and ``zkillboard.fetch_kill`` are patched, or
``requests.get`` where the whole zKillboard client is part of the test.
"""

import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.test import TestCase
from django.utils import timezone
from requests.structures import CaseInsensitiveDict

from fleetops.models import SrpKillmail, SrpSettings
from fleetops.providers import zkillboard
from fleetops.providers.esi import FleetESIError
from fleetops.providers.esi_killmails import KILLMAIL_NOT_FOUND
from fleetops.srp import killmails, values
from fleetops.srp.killmails import parse_killmail_reference
from fleetops.srp.types import KillmailRef, SrpError, ZkillResult

from . import factories as f

HASH = "0123456789abcdef0123456789abcdef01234567"
OTHER_HASH = "fedcba9876543210fedcba9876543210fedcba98"
PRICES = {f.RIFTER: Decimal("400000"), 2_873: Decimal("12000")}
ESTIMATE = Decimal("412000.00")
MARKET_DOWN = FleetESIError("ESI_ERROR", "Could not read market prices: ESI returned HTTP 503.")
NAMES = {f.RIFTER: "Rifter", f.SRP_SYSTEM[0]: f.SRP_SYSTEM[1]}
IN_GAME_HINT = "Paste the in-game killmail link instead."


class FakeResponse:
    def __init__(self, body, status=200):
        self.status_code = status
        self.headers = CaseInsensitiveDict()
        self.text = json.dumps(body)

    def json(self):
        return json.loads(self.text)


class KillmailTestCase(TestCase):
    """ESI, the names, the market prices and zKillboard's lookup are patched; zKillboard fails the test if asked."""

    real_zkillboard = False

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.now = timezone.now().replace(microsecond=0)
        self.old = self.now - timedelta(hours=3)
        self.user = f.create_user()
        self.pilot = f.main_of(self.user)
        self.payload = f.esi_killmail_payload(victim=self.pilot, time=self.old)
        self.killmail_id = self.payload["killmail_id"]
        self.esi = self.patch(killmails, "get_killmail", return_value=self.payload)
        if self.real_zkillboard:
            self.get = self.patch(zkillboard.requests, "get")
        else:
            self.zkill = self.patch(zkillboard, "fetch_kill", side_effect=AssertionError("zKillboard was asked"))
        self.entity_names = self.patch(killmails.names, "entity_names", return_value=NAMES)
        self.prices = self.patch(values, "get_market_prices", return_value=PRICES)

    def patch(self, obj, name, **kwargs):
        patcher = mock.patch.object(obj, name, **kwargs)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def ref(self, killmail_hash=HASH, source="ingame"):
        return KillmailRef(self.killmail_id, killmail_hash, source)

    def killboard_ref(self):
        return parse_killmail_reference(f"https://zkillboard.com/kill/{self.killmail_id}/")

    def zkill_answers(self, *results):
        self.zkill.side_effect = list(results)

    def fetch(self, ref=None, *, zkill_ok=True):
        return killmails.fetch_killmail(ref or self.ref(), zkill_ok=zkill_ok)

    def stored(self, **extra):
        extra.setdefault("killmail_id", self.killmail_id)
        extra.setdefault("killmail_hash", HASH)
        extra.setdefault("time", self.old)
        return f.srp_killmail(self.pilot, **extra)


class FetchNewKillmailTests(KillmailTestCase):
    def test_in_game_link_reads_esi_once_and_stores_the_killmail(self):
        self.zkill_answers(ZkillResult("missing"))

        km = self.fetch()

        self.esi.assert_called_once_with(self.killmail_id, HASH)
        self.assertEqual(SrpKillmail.objects.get(), km)
        self.assertEqual(
            (km.killmail_id, km.killmail_hash, km.victim_character_id, km.victim_corporation_id, km.ship_type_id),
            (self.killmail_id, HASH, self.pilot.character_id, self.pilot.corporation_id, f.RIFTER),
        )
        self.assertEqual((km.ship_type_name, km.solar_system_name), ("Rifter", "Amamake"))
        self.assertEqual(km.killmail_time, self.old)
        self.assertEqual(km.attacker_count, 2)

    def test_the_hash_is_sent_to_esi_in_lower_case(self):
        self.zkill_answers(ZkillResult("missing"))

        km = self.fetch(self.ref(HASH.upper()))

        self.esi.assert_called_once_with(self.killmail_id, HASH)
        self.assertEqual(km.killmail_hash, HASH)

    def test_old_killmails_get_their_zkillboard_value_at_once(self):
        self.zkill_answers(
            ZkillResult("ok", killmail_hash=HASH, fitted_value=Decimal("5000000"), total_value=Decimal("6000000"))
        )

        km = self.fetch()

        self.zkill.assert_called_once()
        self.assertEqual(self.zkill.call_args.args, (self.killmail_id,))
        self.assertEqual(self.zkill.call_args.kwargs["killmail_time"], self.old)
        km.refresh_from_db()
        self.assertEqual(
            (km.zkill_status, km.zkill_fitted_value, km.zkill_total_value, km.zkill_next_attempt_at),
            ("ok", Decimal("5000000.00"), Decimal("6000000.00"), None),
        )

    def test_the_killmail_waits_when_zkillboard_has_no_value_yet(self):
        self.zkill_answers(ZkillResult("missing"))

        km = self.fetch()

        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.zkill_attempts), ("missing", 1))
        self.assertGreater(km.zkill_next_attempt_at, self.now + timedelta(minutes=60))

    def test_young_killmails_are_scheduled_but_not_looked_up(self):
        young = timezone.now() - timedelta(minutes=2)
        self.payload.update(f.esi_killmail_payload(self.killmail_id, victim=self.pilot, time=young))

        km = self.fetch()

        self.zkill.assert_not_called()
        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.zkill_attempts), ("pending", 0))
        self.assertEqual(km.zkill_next_attempt_at, km.killmail_time + timedelta(minutes=6))

    def test_a_killmail_one_second_short_of_six_minutes_waits(self):
        when = timezone.now() - timedelta(minutes=5, seconds=59)
        self.payload.update(f.esi_killmail_payload(self.killmail_id, victim=self.pilot, time=when))

        km = self.fetch()

        self.zkill.assert_not_called()
        self.assertGreater(km.zkill_next_attempt_at, timezone.now())

    def test_unknown_names_are_looked_up_once_for_the_new_killmail(self):
        self.zkill_answers(ZkillResult("missing"))

        self.fetch()

        self.entity_names.assert_called_once_with([f.RIFTER, f.SRP_SYSTEM[0]])

    def test_names_of_the_sde_are_used_first(self):
        self.zkill_answers(ZkillResult("missing"))
        self.patch(killmails.sde, "item_type_names", return_value={f.RIFTER: "Rifter (SDE)"})
        self.patch(killmails.sde, "solar_system_names", return_value={f.SRP_SYSTEM[0]: "Amamake (SDE)"})

        km = self.fetch()

        self.entity_names.assert_not_called()
        self.assertEqual((km.ship_type_name, km.solar_system_name), ("Rifter (SDE)", "Amamake (SDE)"))

    def test_one_missing_sde_name_is_filled_in_from_esi(self):
        self.zkill_answers(ZkillResult("missing"))
        self.patch(killmails.sde, "item_type_names", return_value={f.RIFTER: "Rifter (SDE)"})
        self.patch(killmails.sde, "solar_system_names", return_value={})

        km = self.fetch()

        self.entity_names.assert_called_once_with([f.SRP_SYSTEM[0]])
        self.assertEqual((km.ship_type_name, km.solar_system_name), ("Rifter (SDE)", "Amamake"))

    def test_a_failing_name_lookup_never_stops_the_claim(self):
        self.zkill_answers(ZkillResult("missing"))
        self.entity_names.side_effect = RuntimeError("names-secret")

        with self.assertLogs("extensions", "ERROR") as logs:
            km = self.fetch()

        self.assertEqual((km.ship_type_name, km.solar_system_name), ("", ""))
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("names-secret", "\n".join(logs.output))

    def test_long_names_are_cut_to_the_column_size(self):
        self.zkill_answers(ZkillResult("missing"))
        self.entity_names.return_value = {f.RIFTER: "S" * 400, f.SRP_SYSTEM[0]: "Y" * 400}

        km = self.fetch()

        self.assertEqual((len(km.ship_type_name), len(km.solar_system_name)), (255, 100))

    def test_a_structure_loss_is_stored_without_a_character(self):
        self.payload.update(f.esi_killmail_payload(self.killmail_id, time=self.old))
        self.zkill_answers(ZkillResult("missing"))

        km = self.fetch()

        self.assertIsNone(km.victim_character_id)

    def test_items_are_stored_as_esi_returned_them(self):
        items = [{"flag": 11, "item_type_id": 2_873, "quantity_destroyed": 1, "items": [{"flag": 5, "item_type_id": 34}]}]
        self.payload.update(f.esi_killmail_payload(self.killmail_id, victim=self.pilot, time=self.old, items=items))
        self.zkill_answers(ZkillResult("missing"))

        km = self.fetch()

        self.assertEqual(km.items[0]["flag"], 11)
        self.assertEqual(km.items[0]["items"][0]["item_type_id"], 34)


class FetchFromKillboardLinkTests(KillmailTestCase):
    def test_zkillboard_gives_the_hash_and_the_value_in_one_answer(self):
        self.zkill_answers(
            ZkillResult("ok", killmail_hash=OTHER_HASH, fitted_value=Decimal("8000000"), total_value=Decimal("9000000"))
        )

        km = self.fetch(self.killboard_ref())

        self.zkill.assert_called_once_with(self.killmail_id)
        self.esi.assert_called_once_with(self.killmail_id, OTHER_HASH)
        km.refresh_from_db()
        self.assertEqual((km.killmail_hash, km.zkill_status, km.zkill_attempts), (OTHER_HASH, "ok", 1))
        self.assertEqual(km.zkill_fitted_value, Decimal("8000000.00"))
        self.assertIsNone(km.zkill_next_attempt_at)

    def test_an_answer_with_the_hash_only_leaves_the_value_for_later(self):
        self.zkill_answers(ZkillResult("ok", killmail_hash=OTHER_HASH))

        km = self.fetch(self.killboard_ref())

        self.zkill.assert_called_once()  # no second request for the value
        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.zkill_attempts), ("missing", 1))
        self.assertGreater(km.zkill_next_attempt_at, self.now + timedelta(minutes=60))

    def test_a_killmail_zkillboard_does_not_have_yet(self):
        self.zkill_answers(ZkillResult("missing"))

        with self.assertRaises(SrpError) as ctx:
            self.fetch(self.killboard_ref())

        self.assertEqual(ctx.exception.code, "no_hash")
        self.assertEqual(
            ctx.exception.message,
            "zKillboard does not have this killmail yet (new killmails appear after about 5 minutes). "
            + IN_GAME_HINT,
        )
        self.esi.assert_not_called()
        self.assertFalse(SrpKillmail.objects.exists())

    def test_zkillboard_busy_asks_to_try_again(self):
        self.zkill_answers(ZkillResult("throttled", retry_after=2, requested=False, message=zkillboard.MESSAGES["throttled"]))

        with self.assertRaises(SrpError) as ctx:
            self.fetch(self.killboard_ref())

        self.assertEqual(
            (ctx.exception.code, ctx.exception.message), ("zkill_busy", "zKillboard is busy, try again in a few seconds.")
        )

    def test_zkillboard_down_or_blocked_points_to_the_in_game_link(self):
        for status in ("unavailable", "blocked"):
            with self.subTest(status=status):
                self.zkill_answers(ZkillResult(status, message=zkillboard.MESSAGES[status]))

                with self.assertRaises(SrpError) as ctx:
                    self.fetch(self.killboard_ref())

                self.assertEqual(ctx.exception.code, "zkill_unavailable")
                self.assertEqual(ctx.exception.message, f"{zkillboard.MESSAGES[status]} {IN_GAME_HINT}")
        self.esi.assert_not_called()

    def test_killboard_links_are_refused_while_zkillboard_is_off(self):
        with self.assertRaises(SrpError) as ctx:
            self.fetch(self.killboard_ref(), zkill_ok=False)

        self.assertEqual(ctx.exception.code, "zkill_off")
        self.zkill.assert_not_called()
        self.esi.assert_not_called()

    def test_every_killboard_link_form_works_the_same(self):
        for link in (
            f"https://eve-kill.com/kill/{self.killmail_id}",
            f"https://kb.evetools.org/kill/{self.killmail_id}",
            f"zkillboard.com/kill/{self.killmail_id}",
        ):
            with self.subTest(link=link):
                SrpKillmail.objects.all().delete()
                self.zkill_answers(ZkillResult("ok", killmail_hash=HASH, fitted_value=Decimal("1")))

                km = self.fetch(parse_killmail_reference(link))

                self.assertEqual(km.killmail_hash, HASH)


class FetchErrorTests(KillmailTestCase):
    def test_an_unknown_killmail_or_a_wrong_hash(self):
        self.esi.side_effect = FleetESIError("KILLMAIL_NOT_FOUND", KILLMAIL_NOT_FOUND, 422)

        with self.assertRaises(SrpError) as ctx:
            self.fetch()

        self.assertEqual((ctx.exception.code, ctx.exception.message), ("not_found", "Killmail not found. Check the link or the hash."))
        self.assertFalse(SrpKillmail.objects.exists())

    def test_other_esi_failures_keep_their_short_text(self):
        self.esi.side_effect = FleetESIError("ESI_ERROR", "Could not read the killmail: ESI error limit reached; try again in a minute.", 420)

        with self.assertRaises(SrpError) as ctx:
            self.fetch()

        self.assertEqual(ctx.exception.code, "esi_unavailable")
        self.assertEqual(ctx.exception.message, "Could not read the killmail: ESI error limit reached; try again in a minute.")

    def test_an_incomplete_esi_killmail(self):
        del self.payload["victim"]["ship_type_id"]

        with self.assertRaises(SrpError) as ctx:
            self.fetch()

        self.assertEqual(ctx.exception.code, "invalid_killmail")
        self.assertFalse(SrpKillmail.objects.exists())

    def test_an_esi_killmail_with_another_id_is_refused(self):
        self.payload["killmail_id"] = self.killmail_id + 1

        with self.assertRaises(SrpError) as ctx:
            self.fetch()

        self.assertEqual(ctx.exception.code, "invalid_killmail")
        self.assertFalse(SrpKillmail.objects.exists())


class FetchStoredKillmailTests(KillmailTestCase):
    def test_a_stored_killmail_needs_neither_esi_nor_zkillboard(self):
        km = self.stored(fitted=1_000_000)

        for ref in (self.ref(), self.ref(None, "zkillboard"), self.killboard_ref()):
            with self.subTest(source=ref.source):
                self.assertEqual(self.fetch(ref), km)

        self.esi.assert_not_called()
        self.zkill.assert_not_called()

    def test_killboard_links_work_for_stored_killmails_even_without_zkillboard(self):
        km = self.stored(fitted=1_000_000)

        self.assertEqual(self.fetch(self.killboard_ref(), zkill_ok=False), km)

    def test_a_hash_in_capitals_matches_the_stored_one(self):
        km = self.stored(fitted=1_000_000)

        self.assertEqual(self.fetch(self.ref(HASH.upper())), km)

    def test_a_wrong_hash_is_refused_like_an_unknown_killmail(self):
        self.stored(fitted=1_000_000)

        with self.assertRaises(SrpError) as ctx:
            self.fetch(self.ref(OTHER_HASH))

        self.assertEqual((ctx.exception.code, ctx.exception.message), ("not_found", KILLMAIL_NOT_FOUND))
        self.assertNotIn(HASH, ctx.exception.message)
        self.esi.assert_not_called()

    def test_a_due_value_lookup_is_made_while_the_member_waits(self):
        km = self.stored(zkill_next_attempt_at=self.now - timedelta(minutes=1))
        self.zkill_answers(ZkillResult("ok", killmail_hash=HASH, fitted_value=Decimal("3000000")))

        result = self.fetch()

        self.zkill.assert_called_once()
        self.assertEqual((result.pk, result.zkill_status, result.zkill_fitted_value), (km.pk, "ok", Decimal("3000000")))

    def test_a_lookup_that_is_not_due_yet_is_left_to_the_sweep(self):
        self.stored(zkill_next_attempt_at=timezone.now() + timedelta(minutes=30))

        self.fetch()

        self.zkill.assert_not_called()

    def test_a_killmail_without_a_schedule_is_not_asked(self):
        self.stored()

        self.fetch()

        self.zkill.assert_not_called()

    def test_a_killmail_with_a_value_is_not_asked_again(self):
        self.stored(fitted=1_000_000, zkill_next_attempt_at=self.now - timedelta(days=1))

        self.fetch()

        self.zkill.assert_not_called()

    def test_zkillboard_that_is_on_again_is_asked_for_a_killmail_read_without_it(self):
        self.stored(zkill_status="disabled", esi_estimate=Decimal("5"))
        self.zkill_answers(ZkillResult("missing"))

        km = self.fetch()

        self.zkill.assert_called_once()
        self.assertEqual((km.zkill_status, km.zkill_attempts), ("missing", 1))

    def test_a_young_killmail_read_without_zkillboard_waits_for_it_again(self):
        young = timezone.now() - timedelta(minutes=2)
        km = self.stored(time=young, zkill_status="disabled", esi_estimate=Decimal("5"))

        result = self.fetch()

        self.zkill.assert_not_called()
        self.assertEqual((result.zkill_status, result.zkill_next_attempt_at), ("pending", km.killmail_time + timedelta(minutes=6)))

    def test_a_value_lookup_problem_never_stops_the_check(self):
        km = self.stored(zkill_next_attempt_at=self.now - timedelta(minutes=1))
        self.zkill.side_effect = RuntimeError("lookup-secret")

        with self.assertLogs("extensions", "ERROR") as logs:
            result = self.fetch()

        self.assertEqual(result.pk, km.pk)
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("lookup-secret", "\n".join(logs.output))


class EstimateAtFetchTests(KillmailTestCase):
    def test_without_zkillboard_the_estimate_is_made_and_the_wait_ends(self):
        km = self.fetch(zkill_ok=False)

        self.zkill.assert_not_called()
        self.prices.assert_called_once_with()
        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.esi_estimate, km.zkill_next_attempt_at), ("disabled", ESTIMATE, None))

    def test_an_estimate_that_failed_is_left_to_the_sweep(self):
        self.prices.side_effect = MARKET_DOWN

        km = self.fetch(zkill_ok=False)

        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.esi_estimate), ("disabled", None))
        self.assertGreater(km.zkill_next_attempt_at, timezone.now() + timedelta(minutes=30))

    def test_a_stored_waiting_killmail_stops_waiting_when_zkillboard_is_off(self):
        self.stored(zkill_next_attempt_at=self.now + timedelta(hours=1))

        km = self.fetch(zkill_ok=False)

        self.zkill.assert_not_called()
        self.assertEqual((km.zkill_status, km.esi_estimate, km.zkill_next_attempt_at), ("disabled", ESTIMATE, None))

    def test_an_estimate_that_exists_is_not_made_again(self):
        self.stored(zkill_status="disabled", esi_estimate=Decimal("7"))

        km = self.fetch(zkill_ok=False)

        self.prices.assert_not_called()
        self.assertEqual(km.esi_estimate, Decimal("7"))

    def test_an_estimate_value_source_gets_its_estimate_with_zkillboard_on(self):
        f.srp_settings(value_source="esi_estimate")
        self.zkill_answers(ZkillResult("ok", killmail_hash=HASH, fitted_value=Decimal("5000000")))

        km = self.fetch()

        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.esi_estimate), ("ok", ESTIMATE))
        self.assertEqual(values.loss_value(km, "esi_estimate"), (ESTIMATE, "esi_estimate"))

    def test_the_default_value_source_makes_no_estimate(self):
        f.srp_settings()
        self.zkill_answers(ZkillResult("ok", killmail_hash=HASH, fitted_value=Decimal("5000000")))

        km = self.fetch()

        self.prices.assert_not_called()
        self.assertIsNone(km.esi_estimate)


class StoreKillmailTests(KillmailTestCase):
    def test_a_killmail_is_stored_once(self):
        first = killmails.store_killmail(self.payload, HASH)
        again = killmails.store_killmail(self.payload, HASH)

        self.assertEqual(first, again)
        self.assertEqual(SrpKillmail.objects.count(), 1)
        self.entity_names.assert_called_once()  # the second call finds the row first

    def test_the_zkillboard_lookup_is_scheduled(self):
        km = killmails.store_killmail(self.payload, HASH)

        self.assertEqual((km.zkill_status, km.zkill_attempts), ("pending", 0))
        # The kill is old enough: due as soon as it is stored.
        self.assertGreaterEqual(km.zkill_next_attempt_at, self.now)
        self.assertLessEqual(km.zkill_next_attempt_at, timezone.now())

    def test_a_concurrent_create_returns_the_row_that_won(self):
        winner = self.stored()
        real_filter = SrpKillmail.objects.filter
        calls = []

        def filter_(*args, **kwargs):
            calls.append(kwargs)
            # The first look finds nothing: the other request stores its row only after it.
            return SrpKillmail.objects.none() if len(calls) == 1 else real_filter(*args, **kwargs)

        with (
            mock.patch.object(SrpKillmail.objects, "filter", side_effect=filter_),
            mock.patch.object(killmails, "schedule_zkill") as schedule,
        ):
            km = killmails.store_killmail(self.payload, HASH)

        self.assertEqual(km.pk, winner.pk)
        self.assertEqual(SrpKillmail.objects.count(), 1)
        schedule.assert_not_called()

    def test_an_integrity_error_without_a_row_is_not_hidden(self):
        with (
            mock.patch.object(SrpKillmail.objects, "create", side_effect=IntegrityError("boom")),
            self.assertRaises(IntegrityError),
        ):
            killmails.store_killmail(self.payload, HASH)

    def test_the_hash_is_stored_in_lower_case(self):
        km = killmails.store_killmail(self.payload, HASH.upper())

        self.assertEqual(km.killmail_hash, HASH)

    def test_the_hash_must_look_like_one(self):
        for bad in ("", "xyz", HASH[:-1], HASH + "0", None):
            with self.subTest(bad=bad), self.assertRaises(SrpError) as ctx:
                killmails.store_killmail(self.payload, bad)
            self.assertEqual(ctx.exception.code, "invalid_hash")
        self.assertFalse(SrpKillmail.objects.exists())

    def test_the_payload_is_checked(self):
        with self.assertRaises(SrpError) as ctx:
            killmails.store_killmail({"killmail_id": 5}, HASH)

        self.assertEqual(ctx.exception.code, "invalid_killmail")


class ScheduleTests(KillmailTestCase):
    def test_a_young_killmail_waits_until_it_is_six_minutes_old(self):
        young = self.now - timedelta(minutes=1)
        km = self.stored(time=young)

        killmails.schedule_zkill(km, now=self.now)

        km.refresh_from_db()
        self.assertEqual(km.zkill_next_attempt_at, young + timedelta(minutes=6))

    def test_an_old_killmail_is_due_now(self):
        km = self.stored()

        killmails.schedule_zkill(km, now=self.now)

        km.refresh_from_db()
        self.assertEqual(km.zkill_next_attempt_at, self.now)

    def test_a_killmail_that_zkillboard_is_done_with_is_left_alone(self):
        for offset, status in enumerate(("ok", "unavailable"), 100):
            with self.subTest(status=status):
                km = self.stored(killmail_id=self.killmail_id + offset, zkill_status=status)

                killmails.schedule_zkill(km, now=self.now)

                km.refresh_from_db()
                self.assertIsNone(km.zkill_next_attempt_at)

    def test_a_disabled_killmail_waits_for_zkillboard_again(self):
        km = self.stored(zkill_status="disabled")

        killmails.schedule_zkill(km, now=self.now)

        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.zkill_next_attempt_at), ("pending", self.now))


class PruneTests(KillmailTestCase):
    def test_old_killmails_without_a_claim_are_deleted(self):
        old = self.stored(killmail_id=1_001, fetched_at=self.now - timedelta(days=8))
        recent = self.stored(killmail_id=1_002, fetched_at=self.now - timedelta(days=6))
        claimed = self.stored(killmail_id=1_003, fetched_at=self.now - timedelta(days=30))
        f.srp_claim(self.user, killmail=claimed, character=self.pilot)

        deleted = killmails.prune_unclaimed(7)

        self.assertEqual(deleted, 1)
        self.assertEqual(
            set(SrpKillmail.objects.values_list("pk", flat=True)), {recent.pk, claimed.pk}
        )
        self.assertFalse(SrpKillmail.objects.filter(pk=old.pk).exists())

    def test_the_age_is_a_parameter(self):
        self.stored(killmail_id=1_001, fetched_at=self.now - timedelta(days=2))

        self.assertEqual(killmails.prune_unclaimed(3), 0)
        self.assertEqual(killmails.prune_unclaimed(1), 1)

    def test_nothing_to_delete(self):
        self.assertEqual(killmails.prune_unclaimed(), 0)

    def test_a_claim_that_appears_during_the_delete_saves_its_killmail(self):
        self.stored(killmail_id=1_001, fetched_at=self.now - timedelta(days=8))
        with mock.patch(
            "django.db.models.query.QuerySet.delete",
            side_effect=[ProtectedError("claimed", set()), (1, {"fleetops.SrpKillmail": 1})],
        ) as delete:
            self.assertEqual(killmails.prune_unclaimed(), 1)

        self.assertEqual(delete.call_count, 2)

    def test_it_gives_up_when_the_delete_keeps_failing(self):
        with mock.patch("django.db.models.query.QuerySet.delete", side_effect=ProtectedError("claimed", set())):
            self.assertEqual(killmails.prune_unclaimed(), 0)


class RealZkillboardClientTests(KillmailTestCase):
    """The same intake with the real zKillboard client over a patched ``requests.get``."""

    real_zkillboard = True

    def test_a_killboard_link_reads_hash_and_value_from_the_one_answer(self):
        self.get.return_value = FakeResponse(
            f.zkill_payload(
                self.killmail_id, killmail_hash=OTHER_HASH, victim=self.pilot, fitted=7_500_000.5, total=9_000_000
            )
        )

        km = self.fetch(self.killboard_ref())

        self.get.assert_called_once()
        self.assertEqual(self.get.call_args.args[0], f"https://zkillboard.com/api/killID/{self.killmail_id}/")
        self.esi.assert_called_once_with(self.killmail_id, OTHER_HASH)
        km.refresh_from_db()
        self.assertEqual(
            (km.zkill_status, km.zkill_fitted_value, km.zkill_total_value),
            ("ok", Decimal("7500000.50"), Decimal("9000000.00")),
        )
        self.assertEqual(km.zkill_labels, ["cat:6", "solo", "pvp", "loc:lowsec"])
        self.assertEqual((km.zkill_npc, km.zkill_solo, km.zkill_awox), (False, True, False))

    def test_an_in_game_link_gets_the_value_from_zkillboard_too(self):
        self.get.return_value = FakeResponse(
            f.zkill_payload(self.killmail_id, killmail_hash=HASH, victim=self.pilot, fitted=1_234_567, total=None)
        )

        km = self.fetch()

        self.esi.assert_called_once_with(self.killmail_id, HASH)
        self.assertEqual((km.zkill_status, km.zkill_fitted_value, km.zkill_total_value), ("ok", Decimal("1234567.00"), None))

    def test_an_empty_answer_for_a_killboard_link(self):
        self.get.return_value = FakeResponse([])

        with self.assertRaises(SrpError) as ctx:
            self.fetch(self.killboard_ref())

        self.assertEqual(ctx.exception.code, "no_hash")

    def test_the_second_request_inside_two_seconds_is_busy(self):
        self.get.return_value = FakeResponse([])
        with self.assertRaises(SrpError):
            self.fetch(self.killboard_ref())

        with self.assertRaises(SrpError) as ctx:
            self.fetch(self.killboard_ref())

        self.assertEqual(ctx.exception.code, "zkill_busy")
        self.assertEqual(self.get.call_count, 1)

    def test_zkillboard_blocking_us_does_not_stop_in_game_links(self):
        self.get.return_value = FakeResponse({}, status=403)
        with self.assertLogs("fleetops.providers.zkillboard", "WARNING"), self.assertRaises(SrpError) as ctx:
            self.fetch(self.killboard_ref())
        self.assertEqual(ctx.exception.code, "zkill_unavailable")
        self.assertIn(IN_GAME_HINT, ctx.exception.message)

        km = self.fetch(self.ref())

        self.assertEqual(km.killmail_hash, HASH)
        self.assertEqual(self.get.call_count, 1)  # paused, so no second request
        km.refresh_from_db()
        self.assertEqual(km.zkill_status, "pending")
        self.assertGreater(km.zkill_next_attempt_at, timezone.now() + timedelta(minutes=30))
