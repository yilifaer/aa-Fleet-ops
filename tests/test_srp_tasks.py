"""The SRP value sweep and its queueing.

zKillboard and ESI are never reached; ``requests.get`` and the market prices are patched.
"""

import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from requests.structures import CaseInsensitiveDict

from fleetops import tasks as fleet_tasks
from fleetops.models import SrpKillmail
from fleetops.providers import zkillboard
from fleetops.providers.esi import FleetESIError
from fleetops.srp import tasks as srp_tasks, values
from fleetops.srp.types import ZkillResult

from . import factories as f

HASH = "0123456789abcdef0123456789abcdef01234567"
PRICES = {f.RIFTER: Decimal("400000"), 2_873: Decimal("12000")}
ESTIMATE = Decimal("412000.00")
MARKET_DOWN = FleetESIError("ESI_ERROR", "Could not read market prices: ESI returned HTTP 503.")


class FakeResponse:
    def __init__(self, body, status=200):
        self.status_code = status
        self.headers = CaseInsensitiveDict()
        self.text = json.dumps(body)

    def json(self):
        return json.loads(self.text)


class SweepTestCase(TestCase):
    real_zkillboard = False

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.now = timezone.now().replace(microsecond=0)
        self.user = f.create_user()
        self.pilot = f.main_of(self.user)
        self.sleep = self.patch(srp_tasks.time, "sleep")
        if self.real_zkillboard:
            self.get = self.patch(zkillboard.requests, "get")
        else:
            self.fetch = self.patch(zkillboard, "fetch_kill")
        self.on_updated = self.patch_claims("on_value_updated")
        self.prices = self.patch(values, "get_market_prices", return_value=PRICES)

    def patch(self, obj, name, **kwargs):
        patcher = mock.patch.object(obj, name, **kwargs)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def patch_claims(self, name):
        patcher = mock.patch(f"fleetops.srp.claims.{name}")
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def due(self, minutes_ago=1, **extra):
        """A killmail zKillboard is due to be asked about."""
        extra.setdefault("time", self.now - timedelta(days=1))
        extra.setdefault("zkill_next_attempt_at", self.now - timedelta(minutes=minutes_ago))
        return f.srp_killmail(self.pilot, **extra)

    def claimed(self, **extra):
        km = self.due(**extra)
        claim = f.srp_claim(self.user, killmail=km, character=self.pilot)
        return km, claim

    def sweep(self):
        return srp_tasks.refresh_srp_values(self.now)


class SelectionTests(SweepTestCase):
    def test_nothing_due_costs_one_query(self):
        with self.assertNumQueries(1):
            self.assertEqual(self.sweep(), 0)

        self.fetch.assert_not_called()

    def test_only_due_killmails_waiting_for_zkillboard_are_looked_up(self):
        waiting = self.due(zkill_status="pending")
        missing = self.due(zkill_status="missing", zkill_attempts=2)
        self.due(zkill_next_attempt_at=self.now + timedelta(minutes=1))  # not yet
        self.due(zkill_next_attempt_at=None)  # no schedule
        self.due(zkill_status="ok", fitted=100)
        self.due(zkill_status="unavailable", esi_estimate=Decimal("5"))
        self.due(zkill_status="disabled", esi_estimate=Decimal("5"))
        self.fetch.return_value = ZkillResult("missing")

        handled = self.sweep()

        self.assertEqual(handled, 2)
        self.assertEqual(
            [call.args[0] for call in self.fetch.call_args_list], [waiting.pk, missing.pk]
        )

    def test_the_oldest_due_killmails_come_first(self):
        newer = self.due(minutes_ago=5)
        oldest = self.due(minutes_ago=60)
        middle = self.due(minutes_ago=30)
        self.fetch.return_value = ZkillResult("missing")

        self.sweep()

        self.assertEqual([call.args[0] for call in self.fetch.call_args_list], [oldest.pk, middle.pk, newer.pk])

    def test_a_sweep_handles_twenty_killmails(self):
        for minutes in range(1, 26):
            self.due(minutes_ago=minutes)
        self.fetch.return_value = ZkillResult("missing")

        self.assertEqual(self.sweep(), 20)

        self.assertEqual(self.fetch.call_count, 20)
        # The five most recent ones wait for the next sweep.
        self.assertEqual(SrpKillmail.objects.filter(zkill_attempts=0).count(), 5)

    def test_an_estimate_that_failed_is_tried_again(self):
        gave_up = self.due(zkill_status="unavailable")
        off = self.due(zkill_status="disabled")

        self.assertEqual(self.sweep(), 2)

        self.fetch.assert_not_called()
        for km in (gave_up, off):
            km.refresh_from_db()
            self.assertEqual((km.esi_estimate, km.zkill_next_attempt_at), (ESTIMATE, None))
        self.assertEqual(self.prices.call_count, 2)


class ZkillboardOnTests(SweepTestCase):
    def test_a_value_updates_the_claim_waiting_for_it(self):
        km, claim = self.claimed()
        self.fetch.return_value = ZkillResult("ok", killmail_hash=HASH, fitted_value=Decimal("5000000"))

        self.assertEqual(self.sweep(), 1)

        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.zkill_fitted_value), ("ok", Decimal("5000000.00")))
        self.on_updated.assert_called_once()
        self.assertEqual(self.on_updated.call_args.args[0].pk, km.pk)
        self.assertEqual(self.on_updated.call_args.args[0].zkill_fitted_value, Decimal("5000000.00"))

    def test_the_request_is_made_with_the_age_of_the_kill(self):
        km = self.due()
        self.fetch.return_value = ZkillResult("missing")

        self.sweep()

        self.fetch.assert_called_once_with(km.pk, killmail_time=km.killmail_time, now=self.now)

    def test_a_killmail_without_a_claim_gets_its_value_without_a_claim_update(self):
        km = self.due()
        self.fetch.return_value = ZkillResult("ok", killmail_hash=HASH, total_value=Decimal("7"))

        self.sweep()

        km.refresh_from_db()
        self.assertEqual(km.zkill_status, "ok")
        self.on_updated.assert_not_called()

    def test_no_value_yet_changes_no_claim(self):
        self.claimed()
        self.fetch.return_value = ZkillResult("missing")

        self.sweep()

        self.on_updated.assert_not_called()

    def test_giving_up_makes_the_estimate_and_updates_the_claim(self):
        km, claim = self.claimed(zkill_status="missing", zkill_attempts=4)
        self.fetch.return_value = ZkillResult("missing")

        self.sweep()

        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.zkill_attempts, km.esi_estimate), ("unavailable", 5, ESTIMATE))
        self.on_updated.assert_called_once()

    def test_a_claim_that_cannot_be_updated_does_not_stop_the_others(self):
        first, _claim = self.claimed(minutes_ago=10)
        second, _claim = self.claimed(minutes_ago=5)
        self.on_updated.side_effect = [RuntimeError("claim-secret"), None]
        self.fetch.return_value = ZkillResult("ok", killmail_hash=HASH, fitted_value=Decimal("1"))

        with self.assertLogs("extensions", "ERROR") as logs:
            handled = self.sweep()

        self.assertEqual(handled, 2)
        self.assertEqual(self.on_updated.call_count, 2)
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("claim-secret", "\n".join(logs.output))
        self.assertEqual(SrpKillmail.objects.filter(zkill_status="ok").count(), 2)

    def test_a_killmail_that_fails_is_left_alone_for_a_while(self):
        bad = self.due(minutes_ago=10)
        good = self.due(minutes_ago=5)
        self.fetch.side_effect = [RuntimeError("zkill-secret"), ZkillResult("missing")]

        with self.assertLogs("extensions", "ERROR") as logs:
            handled = self.sweep()

        self.assertEqual(handled, 1)
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("zkill-secret", "\n".join(logs.output))
        bad.refresh_from_db()
        good.refresh_from_db()
        self.assertEqual(bad.zkill_next_attempt_at, self.now + timedelta(minutes=15))
        self.assertEqual(good.zkill_status, "missing")
        # The next sweep does not look at the failing one again.
        self.assertEqual(list(srp_tasks.due_killmails(self.now + timedelta(minutes=14))), [])

    def test_the_two_second_slot_is_waited_out_after_a_request(self):
        for minutes in (3, 2, 1):
            self.due(minutes_ago=minutes)
        self.fetch.return_value = ZkillResult("missing")

        self.sweep()

        self.assertEqual(self.sleep.call_args_list, [mock.call(2), mock.call(2)])

    def test_nothing_is_waited_for_when_zkillboard_was_not_asked(self):
        for minutes in (3, 2, 1):
            self.due(minutes_ago=minutes)
        self.fetch.return_value = ZkillResult("unavailable", retry_after=600, requested=False)

        self.assertEqual(self.sweep(), 3)

        self.sleep.assert_not_called()
        # Not requested, so no attempt was used up and the next try is after the pause.
        for km in SrpKillmail.objects.all():
            self.assertEqual((km.zkill_attempts, km.zkill_next_attempt_at), (0, self.now + timedelta(seconds=600)))

    def test_a_killmail_taken_by_a_web_request_is_skipped(self):
        km = self.due()
        selected = list(SrpKillmail.objects.filter(pk=km.pk))
        # A web request asked zKillboard between the sweep's query and its turn.
        SrpKillmail.objects.filter(pk=km.pk).update(zkill_next_attempt_at=self.now + timedelta(hours=1))
        with mock.patch.object(srp_tasks, "due_killmails", return_value=selected):
            self.assertEqual(self.sweep(), 1)

        self.fetch.assert_not_called()
        self.on_updated.assert_not_called()


class ZkillboardOffTests(SweepTestCase):
    def setUp(self):
        super().setUp()
        f.srp_settings(zkillboard_enabled=False, value_source="esi_estimate")

    def test_waiting_killmails_get_the_estimate_and_zkillboard_is_never_asked(self):
        km, claim = self.claimed()

        self.assertEqual(self.sweep(), 1)

        self.fetch.assert_not_called()
        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.esi_estimate, km.zkill_next_attempt_at), ("disabled", ESTIMATE, None))
        self.on_updated.assert_called_once()

    def test_an_estimate_that_fails_is_tried_again_in_an_hour(self):
        km = self.due()
        self.prices.side_effect = MARKET_DOWN

        self.sweep()

        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.esi_estimate), ("disabled", None))
        self.assertEqual(km.zkill_next_attempt_at, self.now + timedelta(hours=1))
        self.assertEqual(self.sweep(), 0)  # not due yet
        self.on_updated.assert_not_called()

    def test_the_later_retry_updates_the_claim(self):
        km, claim = self.claimed()
        self.prices.side_effect = MARKET_DOWN
        self.sweep()
        self.on_updated.reset_mock()
        self.prices.side_effect = None

        self.assertEqual(srp_tasks.refresh_srp_values(self.now + timedelta(hours=1, seconds=1)), 1)

        km.refresh_from_db()
        self.assertEqual(km.esi_estimate, ESTIMATE)
        self.on_updated.assert_called_once()


class EndToEndTests(SweepTestCase):
    """The sweep with the real zKillboard client over a patched ``requests.get``."""

    real_zkillboard = True

    def test_values_arrive_and_the_claim_is_updated(self):
        km, claim = self.claimed()
        self.get.return_value = FakeResponse(
            f.zkill_payload(km.pk, killmail_hash=HASH, victim=self.pilot, fitted=4_321_000.5, total=5_000_000)
        )

        self.assertEqual(self.sweep(), 1)

        self.assertEqual(self.get.call_args.args[0], f"https://zkillboard.com/api/killID/{km.pk}/")
        km.refresh_from_db()
        self.assertEqual((km.zkill_status, km.zkill_fitted_value), ("ok", Decimal("4321000.50")))
        self.on_updated.assert_called_once()

    def test_a_block_pauses_the_rest_of_the_sweep(self):
        asked, second, third = (self.due(minutes_ago=minutes) for minutes in (3, 2, 1))
        self.get.return_value = FakeResponse({}, status=403)

        with self.assertLogs("fleetops.providers.zkillboard", "WARNING"):
            self.assertEqual(self.sweep(), 3)

        self.assertEqual(self.get.call_count, 1)
        for km, attempts in ((asked, 1), (second, 0), (third, 0)):
            km.refresh_from_db()
            self.assertEqual((km.zkill_status, km.zkill_attempts), ("pending", attempts))
            self.assertGreater(km.zkill_next_attempt_at, timezone.now() + timedelta(minutes=30))


class QueueTests(TestCase):
    def setUp(self):
        super().setUp()
        cache.delete(srp_tasks.SWEEP_KEY)
        self.addCleanup(cache.delete, srp_tasks.SWEEP_KEY)
        patcher = mock.patch.object(fleet_tasks.refresh_srp_values, "delay")
        self.delay = patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_sweep_is_queued(self):
        self.assertTrue(srp_tasks.queue_srp_sweep(timezone.now()))

        self.delay.assert_called_once_with()

    def test_the_clock_is_optional(self):
        self.assertTrue(srp_tasks.queue_srp_sweep())

    def test_only_one_sweep_is_queued_in_five_minutes(self):
        self.assertTrue(srp_tasks.queue_srp_sweep())
        self.assertFalse(srp_tasks.queue_srp_sweep())
        self.assertFalse(srp_tasks.queue_srp_sweep())

        self.delay.assert_called_once()

    def test_the_next_sweep_may_be_queued_when_the_interval_is_over(self):
        srp_tasks.queue_srp_sweep()
        cache.delete(srp_tasks.SWEEP_KEY)  # what the five minutes running out does

        self.assertTrue(srp_tasks.queue_srp_sweep())

        self.assertEqual(self.delay.call_count, 2)

    def test_the_interval_is_five_minutes(self):
        with mock.patch.object(srp_tasks.cache, "add", return_value=True) as add:
            srp_tasks.queue_srp_sweep()

        add.assert_called_once_with(srp_tasks.SWEEP_KEY, "1", timeout=300)

    def test_a_broker_error_is_logged_without_its_text_and_the_next_beat_tries_again(self):
        self.delay.side_effect = ConnectionError("amqp://user:broker-secret@host//")

        with self.assertLogs("extensions", "ERROR") as logs:
            queued = srp_tasks.queue_srp_sweep()

        self.assertFalse(queued)
        self.assertIn("ConnectionError", logs.output[0])
        self.assertNotIn("broker-secret", "\n".join(logs.output))
        self.delay.side_effect = None
        self.assertTrue(srp_tasks.queue_srp_sweep())

    def test_a_cache_error_is_logged_and_nothing_is_queued(self):
        with (
            mock.patch.object(srp_tasks.cache, "add", side_effect=ConnectionError("redis-secret")),
            self.assertLogs("extensions", "ERROR") as logs,
        ):
            queued = srp_tasks.queue_srp_sweep()

        self.assertFalse(queued)
        self.assertNotIn("redis-secret", "\n".join(logs.output))
        self.delay.assert_not_called()

    def test_fleetops_tasks_is_imported_only_when_a_sweep_is_queued(self):
        # fleetops.tasks imports this module at the top; a top-level import back would be circular.
        self.assertFalse(hasattr(srp_tasks, "tasks"))
        self.assertFalse(hasattr(srp_tasks, "fleet_tasks"))

    def test_the_beat_wiring_runs_the_queueing(self):
        with mock.patch.object(fleet_tasks, "queue_srp_sweep", wraps=srp_tasks.queue_srp_sweep) as queue:
            fleet_tasks.schedule_active_fleet_tracking()

        queue.assert_called_once()
        self.delay.assert_called_once_with()
