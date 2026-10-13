"""Ending a fleet: background end task, recovery, end reasons and the state left behind."""

from contextlib import nullcontext
from datetime import timedelta
from unittest import mock

from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    FleetMemberEvent,
    FleetMemberState,
    FleetOperation,
    OperationAction,
)
from fleetops.providers.esi import FleetESIError
from fleetops.services import tracking as tracking_module
from fleetops.services.operations import end_fleet, finish_requested_end, request_fleet_end
from fleetops.services.tracking import sync_operation
from fleetops.tasks import (
    END_LOCK_TIMEOUT,
    ENDING_RECOVERY_AFTER,
    finish_fleet_end,
    schedule_active_fleet_tracking,
    track_operation,
)

from . import factories as f
from .test_operations_tracking import FLEET_ID, FakeESIMixin, http_error

Status = FleetOperation.Status
EndReason = FleetOperation.EndReason


def message_texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class EndTestBase(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.pilot = f.create_user(main_name="Line Pilot")
        self.pilot_char = f.main_of(self.pilot)
        # Long enough for every seeded multiplier (90 -> 2x, 180 -> 3x), so the FC's choice stands.
        self.operation = f.create_operation(
            self.fc, esi_fleet_id=FLEET_ID, started_at=timezone.now() - timedelta(minutes=200)
        )
        self.esi.put_in_fleet(self.fc_char)
        self.esi.members[FLEET_ID] = [f.esi_member(self.fc_char), f.esi_member(self.pilot_char)]
        sync_operation(self.operation)
        self.client.force_login(self.fc)

    def end_url(self):
        return reverse("fleetops:end_fleet", args=[self.operation.uuid])

    def detail(self):
        return self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))

    def refresh(self):
        self.operation.refresh_from_db()
        return self.operation


class BackgroundEndTests(EndTestBase):
    def test_end_request_only_records_the_request_and_queues_the_task(self):
        reads = len(self.esi.calls_of("members"))
        with mock.patch("fleetops.tasks.finish_fleet_end.delay") as delay:
            response = self.client.post(self.end_url(), {"attendance_multiplier": "2"})

        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[self.operation.uuid]), fetch_redirect_response=False
        )
        delay.assert_called_once_with(self.operation.pk)
        operation = self.refresh()
        self.assertEqual(operation.status, Status.ENDING)
        self.assertEqual(operation.end_requested_by, self.fc)
        self.assertEqual(operation.end_requested_multiplier, 2)
        self.assertIsNotNone(operation.end_requested_at)
        self.assertEqual(operation.end_reason, EndReason.MANUAL)
        # Nothing slow ran in the request: no ESI read, no multiplier, no close.
        self.assertEqual(len(self.esi.calls_of("members")), reads)
        self.assertEqual(operation.attendance_multiplier, 1)
        self.assertIsNone(operation.ended_at)
        self.assertIn(
            "Ending the fleet. The final sync and attendance are being finished; this page updates when the fleet is closed.",
            message_texts(response),
        )
        entry = AuditLog.objects.get(action="fleet.end_request")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.old_value, {"status": Status.ACTIVE})
        self.assertEqual(entry.new_value, {"status": Status.ENDING, "attendance_multiplier": 2})

    def test_page_shows_ending_until_the_task_closes_the_fleet(self):
        with mock.patch("fleetops.tasks.finish_fleet_end.delay"):
            self.client.post(self.end_url(), {"attendance_multiplier": "2"})

        page = self.detail()
        self.assertContains(page, "Ending…")
        self.assertContains(page, 'id="fleet-ending"')
        self.assertContains(page, "setTimeout(()=>location.reload(),5000)")
        self.assertNotContains(page, 'class="end-fleet-form"')

        self.assertEqual(finish_fleet_end(self.operation.pk), "closed")

        operation = self.refresh()
        self.assertEqual(operation.status, Status.CLOSED)
        self.assertEqual(operation.attendance_multiplier, 2)
        page = self.detail()
        self.assertNotContains(page, 'id="fleet-ending"')
        self.assertContains(page, "Ended manually")

    def test_task_runs_the_final_sync_applies_the_multiplier_and_closes(self):
        late_char = f.main_of(f.create_user(main_name="Late Pilot"))
        self.esi.members[FLEET_ID] = [f.esi_member(self.fc_char), f.esi_member(late_char)]

        response = self.client.post(self.end_url(), {"attendance_multiplier": "3"})

        operation = self.refresh()
        self.assertEqual(operation.status, Status.CLOSED)
        self.assertEqual(operation.attendance_multiplier, 3)
        self.assertFalse(operation.tracking_enabled)
        self.assertIn("Fleet ended. Attendance finalized at 3x.", message_texts(response))
        values = dict(
            AttendanceRecord.objects.filter(operation=operation).values_list("character_id", "attendance_value")
        )
        self.assertEqual(values[late_char.character_id], 3)
        self.assertEqual(values[self.pilot_char.character_id], 3)
        entry = AuditLog.objects.get(action="fleet.end")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.reason, "Ended manually")
        # The status before the end request, not the passing Ending state.
        self.assertEqual(entry.old_value, {"status": Status.ACTIVE, "tracking_enabled": True})

    def test_end_of_a_requested_start_records_the_status_it_had(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.STARTING)
        with mock.patch("fleetops.tasks.finish_fleet_end.delay"):
            request_fleet_end(self.refresh(), actor=self.fc)

        finish_fleet_end(self.operation.pk)

        self.assertEqual(AuditLog.objects.get(action="fleet.end").old_value["status"], Status.STARTING)

    def test_double_submit_ends_the_fleet_once(self):
        with mock.patch("fleetops.tasks.finish_fleet_end.delay") as delay:
            self.client.post(self.end_url(), {"attendance_multiplier": "2"})
            second = self.client.post(self.end_url(), {"attendance_multiplier": "3"})

        delay.assert_called_once()
        self.assertIn("This fleet is already ending.", message_texts(second))
        self.assertEqual(self.refresh().end_requested_multiplier, 2)
        self.assertEqual(AuditLog.objects.filter(action="fleet.end_request").count(), 1)

        finish_fleet_end(self.operation.pk)
        third = self.client.post(self.end_url(), {"attendance_multiplier": "3"})

        self.assertIn("This fleet is already closed.", message_texts(third))
        self.assertEqual(self.refresh().attendance_multiplier, 2)
        self.assertEqual(AuditLog.objects.filter(action="fleet.end").count(), 1)
        self.assertEqual(OperationAction.objects.get(operation=self.operation, action="fleet_end").attempts, 1)

    def test_request_on_a_closed_fleet_is_a_no_op(self):
        end_fleet(self.operation, actor=self.fc)

        operation, requested = request_fleet_end(self.operation, actor=self.fc, attendance_multiplier=3)

        self.assertFalse(requested)
        self.assertEqual(operation.status, Status.CLOSED)
        self.assertFalse(AuditLog.objects.filter(action="fleet.end_request").exists())

    def test_cancelled_fleet_is_not_ended(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.CANCELLED)

        response = self.client.post(self.end_url())

        self.assertIn("This fleet is already cancelled.", message_texts(response))
        self.assertEqual(self.refresh().status, Status.CANCELLED)

    def test_task_ignores_fleets_that_are_not_ending(self):
        self.assertEqual(finish_fleet_end(self.operation.pk), "not-ending")
        self.assertEqual(finish_fleet_end(999_999_999), "missing")
        self.assertEqual(self.refresh().status, Status.ACTIVE)

    def test_task_lock_keeps_a_second_worker_out(self):
        with mock.patch("fleetops.tasks.finish_fleet_end.delay"):
            request_fleet_end(self.operation, actor=self.fc)
        cache.add(f"fleetops:end:{self.operation.pk}", "1", timeout=60)
        self.addCleanup(cache.delete, f"fleetops:end:{self.operation.pk}")

        self.assertEqual(finish_fleet_end(self.operation.pk), "already-running")
        self.assertEqual(self.refresh().status, Status.ENDING)

    def test_failing_task_leaves_the_fleet_ending_for_the_recovery(self):
        with mock.patch("fleetops.tasks.finish_fleet_end.delay"):
            request_fleet_end(self.operation, actor=self.fc)

        with mock.patch("fleetops.tasks.finish_requested_end", side_effect=RuntimeError("db gone")):
            self.assertEqual(finish_fleet_end(self.operation.pk), "error")

        self.assertEqual(self.refresh().status, Status.ENDING)
        self.assertIsNone(cache.get(f"fleetops:end:{self.operation.pk}"))

    def test_unreachable_queue_finishes_the_end_in_the_request(self):
        from kombu.exceptions import OperationalError

        with mock.patch("fleetops.tasks.finish_fleet_end.delay", side_effect=OperationalError("broker down")):
            response = self.client.post(self.end_url(), {"attendance_multiplier": "2"})

        self.assertEqual(self.refresh().status, Status.CLOSED)
        self.assertIn("Fleet ended. Attendance finalized at 2x.", message_texts(response))

    def test_end_buttons_disable_themselves_after_the_click(self):
        page = self.detail()

        self.assertContains(page, 'class="end-fleet-form"')
        self.assertContains(page, "button.disabled=true; button.textContent='Ending…';")


class ConcurrentEndTests(EndTestBase):
    def stale_copy(self):
        return FleetOperation.objects.get(pk=self.operation.pk)

    def test_automatic_end_backs_off_while_a_requested_end_is_running(self):
        stale = self.stale_copy()
        with mock.patch("fleetops.tasks.finish_fleet_end.delay"):
            request_fleet_end(self.operation, actor=self.fc, attendance_multiplier=2)

        result = end_fleet(stale, automatic=True, reason=EndReason.BOSS_HANDOVER, detail="New Boss")

        self.assertEqual(result.status, Status.ENDING)
        self.assertEqual(result.end_reason, EndReason.MANUAL)
        self.assertFalse(AuditLog.objects.filter(action="fleet.auto_end").exists())

        finish_fleet_end(self.operation.pk)
        operation = self.refresh()
        self.assertEqual((operation.status, operation.end_reason, operation.attendance_multiplier), (Status.CLOSED, EndReason.MANUAL, 2))

    def test_stale_copy_of_a_closed_fleet_is_not_ended_again(self):
        stale = self.stale_copy()
        end_fleet(self.operation, actor=self.fc, attendance_multiplier=2)
        ended_at = self.refresh().ended_at

        result = end_fleet(stale, automatic=True)

        self.assertEqual(result.status, Status.CLOSED)
        operation = self.refresh()
        self.assertEqual((operation.ended_at, operation.end_reason), (ended_at, EndReason.MANUAL))
        self.assertEqual(AuditLog.objects.filter(action__in=["fleet.end", "fleet.auto_end"]).count(), 1)


class EndingRecoveryTests(EndTestBase):
    def request_end(self, minutes_ago, **extra):
        with mock.patch("fleetops.tasks.finish_fleet_end.delay"):
            request_fleet_end(self.operation, actor=self.fc, attendance_multiplier=2)
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            end_requested_at=timezone.now() - timedelta(minutes=minutes_ago), **extra
        )

    def test_beat_finishes_an_end_whose_task_was_lost(self):
        self.request_end(minutes_ago=6)

        schedule_active_fleet_tracking()

        operation = self.refresh()
        self.assertEqual(operation.status, Status.CLOSED)
        self.assertEqual(operation.attendance_multiplier, 2)
        self.assertEqual(AuditLog.objects.get(action="fleet.end").actor, self.fc)

    def test_recent_end_is_left_to_its_own_task(self):
        self.request_end(minutes_ago=1)

        with mock.patch("fleetops.tasks.finish_fleet_end.delay") as delay:
            schedule_active_fleet_tracking()

        delay.assert_not_called()
        self.assertEqual(self.refresh().status, Status.ENDING)

    def test_recovery_window(self):
        self.assertEqual(ENDING_RECOVERY_AFTER, timedelta(minutes=5))

    def test_lock_of_a_killed_end_task_expires_before_the_recovery(self):
        # A worker killed during the end task never releases its lock; the beat's recovery
        # would only get "already-running" until the lock expires.
        self.assertLess(END_LOCK_TIMEOUT, ENDING_RECOVERY_AFTER.total_seconds())
        lock_key = f"fleetops:end:{self.operation.pk}"
        self.request_end(minutes_ago=0)
        seen = {}

        def killed_worker(operation):
            seen["ttl"] = cache.ttl(lock_key)
            raise SystemExit(1)

        with mock.patch("fleetops.tasks.finish_requested_end", side_effect=killed_worker), mock.patch.object(
            cache, "delete"
        ):
            with self.assertRaises(SystemExit):
                finish_fleet_end(self.operation.pk)
        self.addCleanup(cache.delete, lock_key)

        self.assertLessEqual(seen["ttl"], END_LOCK_TIMEOUT)
        self.assertLess(cache.ttl(lock_key), ENDING_RECOVERY_AFTER.total_seconds())
        self.assertEqual(self.refresh().status, Status.ENDING)

    def test_ending_fleet_without_a_request_time_is_recovered_by_its_last_update(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            status=Status.ENDING, updated_at=timezone.now() - timedelta(minutes=10)
        )

        with mock.patch("fleetops.tasks.finish_fleet_end.delay") as delay:
            schedule_active_fleet_tracking()

        delay.assert_called_once_with(self.operation.pk)
        self.assertIsNone(finish_requested_end(self.refresh()).end_requested_by)
        self.assertEqual(self.refresh().status, Status.CLOSED)
        self.assertEqual(self.refresh().end_reason, EndReason.MANUAL)

    def test_interrupted_automatic_end_is_finished_as_automatic(self):
        self.request_end(minutes_ago=6, end_reason=EndReason.BOSS_HANDOVER, end_detail="New Boss", end_requested_by=None)
        reads = len(self.esi.calls_of("members"))

        schedule_active_fleet_tracking()

        operation = self.refresh()
        self.assertEqual(operation.status, Status.CLOSED)
        self.assertEqual(operation.last_error, "Ended automatically: fleet boss handed over to New Boss")
        self.assertEqual(len(self.esi.calls_of("members")), reads)
        self.assertTrue(AuditLog.objects.filter(action="fleet.auto_end").exists())


class LatePollTests(EndTestBase):
    """A tracking poll whose ESI read was still running when the fleet was ended."""

    def setUp(self):
        super().setUp()
        self.late_char = f.main_of(f.create_user(main_name="Late Joiner"))
        self.members_after_the_end = [
            f.esi_member(self.fc_char),
            f.esi_member(self.pilot_char),
            f.esi_member(self.late_char),
        ]

    def end_during_the_read(self, outcome):
        """The first member read ends the fleet (Celery runs eagerly), then answers with ``outcome``."""
        real_read = tracking_module.get_fleet_members
        reads = []

        def read(user, character_id, fleet_id, fresh=False):
            reads.append(fleet_id)
            if len(reads) > 1:
                # The end's own final sync.
                return real_read(user, character_id, fleet_id, fresh=fresh)
            request_fleet_end(self.refresh(), actor=self.fc, attendance_multiplier=2)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return mock.patch.object(tracking_module, "get_fleet_members", side_effect=read)

    def assert_closed_as_the_end_left_it(self):
        operation = self.refresh()
        self.assertEqual(operation.status, Status.CLOSED)
        self.assertEqual(operation.attendance_multiplier, 2)
        self.assertEqual(operation.last_error, "")
        self.assertEqual((operation.fleet_missing_count, operation.missing_reason), (0, ""))
        self.assertLessEqual(operation.last_esi_update, operation.ended_at)
        states = FleetMemberState.objects.filter(operation=operation)
        self.assertFalse(states.filter(is_active=True).exists())
        self.assertFalse(states.filter(character_id=self.late_char.character_id).exists())
        self.assertEqual(set(states.values_list("left_at", flat=True)), {operation.ended_at})
        self.assertFalse(
            FleetMemberEvent.objects.filter(
                operation=operation, event_type__in=[FleetMemberEvent.EventType.JOIN, FleetMemberEvent.EventType.REJOIN]
            )
            .exclude(character_id__in=[self.fc_char.character_id, self.pilot_char.character_id])
            .exists()
        )
        self.assertFalse(
            FleetMemberEvent.objects.filter(operation=operation, event_type=FleetMemberEvent.EventType.REJOIN).exists()
        )
        values = dict(AttendanceRecord.objects.filter(operation=operation).values_list("character_name", "attendance_value"))
        self.assertEqual(values, {"Boss Main": 2, "Line Pilot": 2})

    def test_poll_that_read_the_fleet_before_the_end_writes_nothing(self):
        poll_copy = FleetOperation.objects.select_related("fc_user").get(pk=self.operation.pk)
        request_fleet_end(self.operation, actor=self.fc, attendance_multiplier=2)
        self.esi.members[FLEET_ID] = self.members_after_the_end

        self.assertIsNone(sync_operation(poll_copy))

        self.assert_closed_as_the_end_left_it()

    def test_tracking_task_reports_the_ended_fleet_as_inactive(self):
        with self.end_during_the_read(self.members_after_the_end):
            self.assertEqual(track_operation(self.operation.pk), "inactive")

        self.assert_closed_as_the_end_left_it()

    def test_unreadable_fleet_is_not_counted_on_an_ended_fleet(self):
        f.settings(auto_end_enabled=True, auto_end_missing_count=1)
        gone = FleetESIError("FLEET_NOT_FOUND", "Could not read fleet members: ESI returned HTTP 404 (Not found).", 404)

        with self.end_during_the_read(gone):
            self.assertEqual(track_operation(self.operation.pk), "inactive")

        self.assert_closed_as_the_end_left_it()
        self.assertEqual(self.refresh().end_reason, EndReason.MANUAL)
        self.assertFalse(AuditLog.objects.filter(action="fleet.auto_end").exists())

    def test_failed_poll_does_not_store_its_error_on_an_ended_fleet(self):
        failures = (
            (FleetESIError("ESI_ERROR", "Could not read fleet members: ESI returned HTTP 503 (Service unavailable).", 503), nullcontext()),
            (ValueError("bad payload"), self.assertLogs("extensions", "ERROR")),
        )
        for failure, logs in failures:
            with self.subTest(failure=type(failure).__name__):
                self.operation = f.create_operation(self.fc, esi_fleet_id=FLEET_ID)
                with self.end_during_the_read(failure), logs:
                    self.assertEqual(track_operation(self.operation.pk), "inactive")

                operation = self.refresh()
                self.assertEqual((operation.status, operation.last_error), (Status.CLOSED, ""))

    def test_poll_that_writes_first_is_still_closed_by_the_end(self):
        self.esi.members[FLEET_ID] = self.members_after_the_end

        self.assertEqual(sync_operation(self.operation), 3)
        request_fleet_end(self.operation, actor=self.fc, attendance_multiplier=2)

        operation = self.refresh()
        self.assertEqual(operation.status, Status.CLOSED)
        self.assertFalse(FleetMemberState.objects.filter(operation=operation, is_active=True).exists())
        values = dict(AttendanceRecord.objects.filter(operation=operation).values_list("character_name", "attendance_value"))
        self.assertEqual(values, {"Boss Main": 2, "Line Pilot": 2, "Late Joiner": 2})


class EndStateCleanupTests(EndTestBase):
    def test_automatic_end_persists_the_count_error_and_member_states(self):
        f.settings(auto_end_enabled=True, auto_end_missing_count=3)
        self.esi.character_fleets.clear()
        self.esi.members[FLEET_ID] = http_error(404)

        for _ in range(3):
            track_operation(self.operation.pk)

        operation = self.refresh()
        self.assertEqual(operation.status, Status.CLOSED)
        self.assertEqual(operation.fleet_missing_count, 3)
        self.assertEqual(operation.last_error, "Ended automatically: the fleet no longer exists or the FC left it")
        states = FleetMemberState.objects.filter(operation=operation)
        self.assertEqual(states.count(), 2)
        self.assertFalse(states.filter(is_active=True).exists())
        self.assertEqual(set(states.values_list("left_at", flat=True)), {operation.ended_at})

    def test_manual_end_clears_the_error_and_marks_members_as_left(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            last_error="Could not read fleet members: ESI returned HTTP 503 (Service unavailable)."
        )
        self.esi.members[FLEET_ID] = http_error(502)

        end_fleet(self.refresh(), actor=self.fc)

        operation = self.refresh()
        self.assertEqual(operation.last_error, "")
        self.assertEqual(operation.end_reason, EndReason.MANUAL)
        self.assertFalse(FleetMemberState.objects.filter(operation=operation, is_active=True).exists())

    def test_closed_fleet_page_has_no_active_fleet_warning(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            last_esi_update=timezone.now() - timedelta(hours=2)
        )
        self.esi.members[FLEET_ID] = http_error(404)
        self.esi.character_fleets.clear()
        f.settings(auto_end_enabled=True, auto_end_missing_count=2)
        track_operation(self.operation.pk)
        track_operation(self.operation.pk)
        self.assertEqual(self.refresh().status, Status.CLOSED)

        page = self.detail()

        self.assertFalse(page.context["stale"])
        self.assertNotContains(page, "STALE DATA")
        # The only warning left asks the FC for the multiplier the automatic end could not choose.
        self.assertContains(page, 'class="alert alert-warning"', count=1)
        self.assertContains(page, 'id="multiplier-pending"')
        self.assertContains(page, "Ended automatically: the fleet no longer exists or the FC left it")

    def test_closed_fleet_lists_everyone_who_was_tracked(self):
        end_fleet(self.operation, actor=self.fc)

        page = self.detail()

        self.assertEqual(len(page.context["members"]), 2)
        self.assertContains(page, "Line Pilot")

    def test_archive_shows_the_end_reason(self):
        other = f.create_operation(self.fc)
        end_fleet(self.operation, actor=self.fc)
        end_fleet(other, automatic=True, reason=EndReason.BOSS_HANDOVER, detail="New Boss")
        self.client.force_login(f.create_user(perms=f.FC_LEAD_PERMS))

        # Both fleets started 30 minutes ago; the archive lists one year at a time.
        page = self.client.get(reverse("fleetops:fleet_operations"), {"year": self.operation.started_at.year})

        self.assertContains(page, "Ended manually")
        self.assertContains(page, "Ended automatically: fleet boss handed over to New Boss")

    def test_audit_log_page_shows_the_end_reason(self):
        end_fleet(self.operation, automatic=True)
        self.client.force_login(f.create_user(perms=f.FC_LEAD_PERMS))

        page = self.client.get(reverse("fleetops:audit_log"))

        self.assertContains(page, "fleet.auto_end")
        self.assertContains(page, "Ended automatically: the fleet no longer exists or the FC left it")

    def test_open_fleets_have_no_end_reason(self):
        self.assertEqual(self.refresh().end_reason, "")
        self.assertEqual(self.operation.end_reason_text, "")
