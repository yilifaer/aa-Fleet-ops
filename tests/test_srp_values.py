# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Loss values on stored killmails: zKillboard lookups and their retry schedule, and the ESI estimate.

The pure parts (value order, schedule maths, estimate arithmetic) are tested in test_srp_intake.py.
zKillboard and ESI are never reached: ``zkillboard.fetch_kill`` and ``get_market_prices`` are patched.
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from fleetops.models import SrpKillmail
from fleetops.providers import zkillboard
from fleetops.providers.esi import FleetESIError
from fleetops.srp import values
from fleetops.srp.types import ZkillResult

from . import factories as f

HASH = "0123456789abcdef0123456789abcdef01234567"
PRICES = {f.RIFTER: Decimal("400000"), 2_873: Decimal("12000")}
ESTIMATE = Decimal("412000.00")
MARKET_DOWN = FleetESIError("ESI_ERROR", "Could not read market prices: ESI returned HTTP 503.")


def answer(**fields):
    fields.setdefault("killmail_hash", HASH)
    return ZkillResult("ok", **fields)


class ValuesTestCase(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.now = timezone.now().replace(microsecond=0)
        self.old_kill = self.now - timedelta(days=1)

    def patch(self, obj, name, **kwargs):
        patcher = mock.patch.object(obj, name, **kwargs)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def lookup_returns(self, *results):
        """Patch zKillboard's lookup to answer with ``results`` in turn."""
        return self.patch(zkillboard, "fetch_kill", side_effect=list(results))

    def prices(self, prices=PRICES):
        return self.patch(values, "get_market_prices", return_value=prices)

    def waiting_killmail(self, **extra):
        extra.setdefault("time", self.old_kill)
        extra.setdefault("zkill_next_attempt_at", self.now)
        return f.srp_killmail(f.main_of(f.create_user()), **extra)


class RefreshZkillTests(ValuesTestCase):
    def test_a_value_is_stored_with_everything_zkillboard_knows(self):
        km = self.waiting_killmail()
        fetch = self.lookup_returns(
            answer(
                fitted_value=Decimal("10234567.89"),
                total_value=Decimal("12500000.25"),
                destroyed_value=Decimal("9800000"),
                dropped_value=Decimal("120000.50"),
                npc=False,
                solo=True,
                awox=False,
                labels=["cat:6", "solo"],
            )
        )

        changed = values.refresh_zkill(km, now=self.now)

        self.assertTrue(changed)
        fetch.assert_called_once_with(km.killmail_id, killmail_time=km.killmail_time, now=self.now)
        km = SrpKillmail.objects.get(pk=km.pk)
        self.assertEqual(
            (km.zkill_status, km.zkill_attempts, km.zkill_next_attempt_at, km.zkill_checked_at),
            ("ok", 1, None, self.now),
        )
        self.assertEqual(
            (km.zkill_fitted_value, km.zkill_total_value, km.zkill_destroyed_value, km.zkill_dropped_value),
            (Decimal("10234567.89"), Decimal("12500000.25"), Decimal("9800000.00"), Decimal("120000.50")),
        )
        self.assertEqual((km.zkill_npc, km.zkill_solo, km.zkill_awox), (False, True, False))
        self.assertEqual(km.zkill_labels, ["cat:6", "solo"])
        self.assertEqual(values.loss_value(km, "zkill_fitted"), (Decimal("10234567.89"), "zkill_fitted"))

    def test_the_object_given_is_updated_too(self):
        km = self.waiting_killmail()
        self.lookup_returns(answer(total_value=Decimal("7")))

        values.refresh_zkill(km, now=self.now)

        self.assertEqual((km.zkill_status, km.zkill_total_value), ("ok", Decimal("7")))

    def test_no_value_yet_is_tried_again_after_65_minutes(self):
        km = self.waiting_killmail()
        self.lookup_returns(ZkillResult("missing"))

        changed = values.refresh_zkill(km, now=self.now)

        self.assertFalse(changed)
        km.refresh_from_db()
        self.assertEqual(
            (km.zkill_status, km.zkill_attempts, km.zkill_next_attempt_at),
            ("missing", 1, self.now + timedelta(minutes=65)),
        )

    def test_the_whole_ladder_ends_in_unavailable_with_an_estimate(self):
        km = self.waiting_killmail()
        self.lookup_returns(*[ZkillResult("missing")] * 5)
        self.prices()
        delays = []
        results = []
        for _attempt in range(5):
            when = km.zkill_next_attempt_at
            results.append(values.refresh_zkill(km, now=when))
            delays.append((km.zkill_next_attempt_at - when) if km.zkill_next_attempt_at else None)

        self.assertEqual(results, [False, False, False, False, True])
        self.assertEqual(delays, [timedelta(minutes=65), timedelta(hours=3), timedelta(hours=12), timedelta(hours=24), None])
        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.zkill_attempts), ("unavailable", 5))
        self.assertEqual(km.esi_estimate, ESTIMATE)
        self.assertEqual(values.loss_value(km, "zkill_fitted"), (ESTIMATE, "esi_estimate"))

    def test_an_answer_with_the_hash_only_counts_as_missing(self):
        km = self.waiting_killmail()
        self.lookup_returns(answer(labels=["pvp"]))

        self.assertFalse(values.refresh_zkill(km, now=self.now))

        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.zkill_attempts, km.zkill_labels), ("missing", 1, ["pvp"]))
        self.assertIsNone(km.zkill_fitted_value)

    def test_errors_use_up_attempts_like_missing_answers(self):
        km = self.waiting_killmail()
        self.lookup_returns(ZkillResult("unavailable"))

        values.refresh_zkill(km, now=self.now)

        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.zkill_attempts), ("pending", 1))
        self.assertEqual(km.zkill_next_attempt_at, self.now + timedelta(minutes=65))

    def test_an_answer_that_was_not_requested_is_not_an_attempt(self):
        km = self.waiting_killmail(zkill_status="missing", zkill_attempts=2)
        self.lookup_returns(ZkillResult("throttled", retry_after=2, requested=False))

        self.assertFalse(values.refresh_zkill(km, now=self.now))

        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.zkill_attempts), ("missing", 2))
        self.assertEqual(km.zkill_next_attempt_at, self.now + timedelta(seconds=2))
        self.assertIsNone(km.zkill_checked_at)

    def test_a_kill_younger_than_six_minutes_is_not_requested(self):
        young = self.now - timedelta(minutes=2)
        km = self.waiting_killmail(time=young, zkill_next_attempt_at=young + timedelta(minutes=6))
        with mock.patch("fleetops.providers.zkillboard.requests.get") as get:
            changed = values.refresh_zkill(km, now=self.now)

        get.assert_not_called()
        self.assertFalse(changed)
        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.zkill_attempts), ("pending", 0))
        self.assertEqual(km.zkill_next_attempt_at, young + timedelta(minutes=6))

    def test_killmails_that_are_not_waiting_are_left_alone(self):
        fetch = self.lookup_returns()
        for status in ("ok", "unavailable", "disabled"):
            with self.subTest(status=status):
                km = self.waiting_killmail(zkill_status=status)

                self.assertFalse(values.refresh_zkill(km, now=self.now))

        fetch.assert_not_called()

    def test_a_lookup_that_was_taken_by_someone_else_is_not_repeated(self):
        km = self.waiting_killmail()
        stale = SrpKillmail.objects.get(pk=km.pk)
        fetch = self.lookup_returns(answer(fitted_value=Decimal("5")))

        self.assertTrue(values.refresh_zkill(km, now=self.now))
        # The second caller still holds the old state: the sweep and a web request met.
        self.assertFalse(values.refresh_zkill(stale, now=self.now))

        fetch.assert_called_once()

    def test_the_killmail_is_taken_while_zkillboard_is_asked(self):
        km = self.waiting_killmail()
        seen = []

        def fetch(*args, **kwargs):
            seen.append(SrpKillmail.objects.get(pk=km.pk).zkill_next_attempt_at)
            return ZkillResult("missing")

        self.patch(zkillboard, "fetch_kill", side_effect=fetch)

        values.refresh_zkill(km, now=self.now)

        self.assertEqual(seen, [self.now + values.ZKILL_LEASE])

    def test_a_value_found_earlier_is_never_replaced_by_nothing(self):
        km = self.waiting_killmail(fitted=1000, total=2000, zkill_status="missing")

        values.apply_zkill_result(km, answer(labels=["a"]), now=self.now)

        km.refresh_from_db()
        self.assertEqual((km.zkill_fitted_value, km.zkill_total_value), (Decimal("1000.00"), Decimal("2000.00")))

    def test_a_failed_answer_keeps_the_stored_labels_and_flags(self):
        km = self.waiting_killmail(zkill_labels=["pvp"], zkill_solo=True)

        values.apply_zkill_result(km, ZkillResult("unavailable"), now=self.now)

        km.refresh_from_db()
        self.assertEqual((km.zkill_labels, km.zkill_solo), (["pvp"], True))


class EstimateTests(ValuesTestCase):
    def test_the_estimate_is_stored_and_returned(self):
        km = self.waiting_killmail()
        self.prices()

        self.assertEqual(values.esi_estimate(km), ESTIMATE)

        km.refresh_from_db()
        self.assertEqual(km.esi_estimate, ESTIMATE)
        self.assertLessEqual(abs(km.esi_estimate_at - timezone.now()), timedelta(seconds=30))

    def test_only_fitted_items_count(self):
        km = self.waiting_killmail(
            items=[
                {"flag": 27, "item_type_id": 2_873, "quantity_destroyed": 1, "singleton": 0},
                {"flag": 5, "item_type_id": 2_873, "quantity_dropped": 500, "singleton": 0},
            ]
        )
        self.prices()

        self.assertEqual(values.esi_estimate(km), ESTIMATE)

    def test_without_market_prices_nothing_is_stored_and_the_old_estimate_stays(self):
        km = self.waiting_killmail(esi_estimate=Decimal("5"))
        self.patch(values, "get_market_prices", side_effect=MARKET_DOWN)

        self.assertIsNone(values.esi_estimate(km))

        km.refresh_from_db()
        self.assertEqual(km.esi_estimate, Decimal("5.00"))

    def test_a_ship_without_a_price_has_no_estimate(self):
        km = self.waiting_killmail()
        self.prices({2_873: Decimal("12000")})

        self.assertIsNone(values.esi_estimate(km))

        km.refresh_from_db()
        self.assertIsNone(km.esi_estimate)


class SettleTests(ValuesTestCase):
    def test_without_zkillboard_the_estimate_is_the_value(self):
        km = self.waiting_killmail()
        self.prices()

        changed = values.settle_without_zkill(km, "disabled", now=self.now)

        self.assertTrue(changed)
        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.zkill_next_attempt_at, km.esi_estimate), ("disabled", None, ESTIMATE))
        self.assertEqual(km.esi_estimate_at, self.now)

    def test_an_estimate_that_exists_is_not_made_again(self):
        km = self.waiting_killmail(esi_estimate=Decimal("9"))
        prices = self.prices()

        changed = values.settle_without_zkill(km, "unavailable", now=self.now)

        prices.assert_not_called()
        self.assertTrue(changed)  # the killmail is settled now
        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.esi_estimate), ("unavailable", Decimal("9.00")))
        self.assertFalse(values.settle_without_zkill(km, "unavailable", now=self.now))

    def test_missing_market_prices_are_tried_again_in_an_hour(self):
        km = self.waiting_killmail()
        self.patch(values, "get_market_prices", side_effect=MARKET_DOWN)

        changed = values.settle_without_zkill(km, "disabled", now=self.now)

        self.assertTrue(changed)  # settled, even though there is no number yet
        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.esi_estimate), ("disabled", None))
        self.assertEqual(km.zkill_next_attempt_at, self.now + timedelta(hours=1))

    def test_the_retry_works_and_ends_the_waiting(self):
        km = self.waiting_killmail(zkill_status="disabled")
        self.prices()

        self.assertTrue(values.settle_without_zkill(km, "disabled", now=self.now))

        km.refresh_from_db()
        self.assertEqual((km.esi_estimate, km.zkill_next_attempt_at), (ESTIMATE, None))

    def test_retries_stop_after_two_days(self):
        km = self.waiting_killmail(fetched_at=self.now - timedelta(days=2, minutes=1))
        self.patch(values, "get_market_prices", side_effect=MARKET_DOWN)

        values.settle_without_zkill(km, "unavailable", now=self.now)

        km.refresh_from_db()
        self.assertIsNone(km.zkill_next_attempt_at)

    def test_a_ship_without_a_price_is_not_retried(self):
        km = self.waiting_killmail()
        self.prices({})

        values.settle_without_zkill(km, "disabled", now=self.now)

        km.refresh_from_db()
        self.assertEqual((km.esi_estimate, km.zkill_next_attempt_at), (None, None))
