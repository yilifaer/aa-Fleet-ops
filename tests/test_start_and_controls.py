"""Capsule kick count, one record per ESI fleet, the start preview and manual fleet times."""

import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.contrib.messages import get_messages
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from fleetops.forms import ManualFleetForm
from fleetops.models import AuditLog, FleetMemberState, FleetOperation
from fleetops.services import operations as operations_module
from fleetops.services.fleet_controls import kick_all_pods, kickable_pod_members
from fleetops.services.operations import (
    START_ABANDONED_MESSAGE,
    STARTING_ABANDONED_AFTER,
    cancel_abandoned_starts,
    start_fleet,
)
from fleetops.tasks import schedule_active_fleet_tracking

from . import factories as f
from .test_operations_tracking import FLEET_ID, FakeESIMixin

UTC = dt_timezone.utc
CAPSULE = 670
RIFTER = 587
Status = FleetOperation.Status
# The minimum FC permissions the README lists; no view_all_fleets.
README_FC_PERMS = ["fleetops.basic_access", "fleetops.start_fleet", "fleetops.manage_own_fleet"]


def message_texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


def track(operation, character, ship_type_id=CAPSULE, active=True):
    now = timezone.now()
    return FleetMemberState.objects.create(
        operation=operation,
        character_id=character.character_id,
        character_name=character.character_name,
        ship_type_id=ship_type_id,
        first_seen=now,
        last_seen=now,
        is_active=active,
    )


class KickableCapsuleTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.fc_char = f.main_of(self.fc)
        self.boss_alt = f.add_alt(self.fc, "Boss Alt")
        self.operation = f.create_operation(
            self.fc, esi_fleet_id=FLEET_ID, fleet_boss_character_id=self.boss_alt.character_id
        )
        track(self.operation, self.fc_char)
        track(self.operation, self.boss_alt)
        self.pod = track(self.operation, f.create_character("Podded Pilot"))
        track(self.operation, f.create_character("Flying Pilot"), ship_type_id=RIFTER)
        track(self.operation, f.create_character("Gone Pod"), active=False)
        self.client.force_login(self.fc)

    def detail(self):
        return self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))

    def test_fc_and_fleet_boss_are_never_kickable(self):
        self.assertEqual([m.pk for m in kickable_pod_members(self.operation)], [self.pod.pk])

    def test_button_count_matches_the_kick(self):
        page = self.detail()

        self.assertEqual(page.context["pod_count"], 1)
        self.assertContains(page, "Kick 1 Capsule</button>")
        with mock.patch("fleetops.services.fleet_controls.kick_fleet_member") as kick:
            result = kick_all_pods(self.operation, actor=self.fc)

        self.assertEqual(result["target_count"], page.context["pod_count"])
        self.assertEqual([call.args[3] for call in kick.call_args_list], [self.pod.character_id])

    def test_button_is_disabled_when_only_the_fc_sits_in_a_capsule(self):
        self.pod.delete()

        page = self.detail()

        self.assertEqual(page.context["pod_count"], 0)
        self.assertContains(page, "disabled>Kick 0 Capsules</button>")

    def test_unexpected_kick_error_is_stored_without_its_text(self):
        with mock.patch(
            "fleetops.services.fleet_controls.kick_fleet_member", side_effect=RuntimeError("token=abc123secret")
        ):
            result = kick_all_pods(self.operation, actor=self.fc)

        self.assertEqual(result["failures"], [{"character": "Podded Pilot", "error": "Capsule kick failed unexpectedly (RuntimeError)."}])
        step = self.operation.actions.get(action="kick_capsules")
        self.assertNotIn("abc123secret", step.error_message)
        self.assertIn("Podded Pilot: Capsule kick failed unexpectedly (RuntimeError).", step.error_message)


class StartTestBase(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.fleet_type = f.fleet_type()
        self.esi.put_in_fleet(self.fc_char)
        self.esi.members[FLEET_ID] = [f.esi_member(self.fc_char)]
        f.settings(srp_auto_create=False)

    def start(self):
        return start_fleet(
            user=self.fc,
            cleaned_data={
                "operation_mode": "attendance_only",
                "fc_character_id": str(self.fc_char.character_id),
                "fleet_type": self.fleet_type,
                "formup": "Jita",
            },
            request_id=uuid.uuid4(),
        )

    def post_start(self, character=None):
        character = character or self.fc_char
        return self.client.post(
            reverse("fleetops:start_fleet"),
            {
                "request_id": str(uuid.uuid4()),
                "operation_mode": "attendance_only",
                "fc_character_id": str(character.character_id),
                "fleet_type": str(self.fleet_type.pk),
                "formup": "Jita",
            },
        )

    def handed_over_fleet(self, **extra):
        """An Old Boss operation on FLEET_ID whose boss role went to a README-minimum FC."""
        old_boss = f.create_user(perms=f.FC_PERMS, main_name="Old Boss")
        running = f.create_operation(
            old_boss,
            esi_fleet_id=FLEET_ID,
            missing_reason=FleetOperation.EndReason.BOSS_HANDOVER,
            missing_detail="New Boss",
            fleet_missing_count=1,
            **extra,
        )
        new_boss = f.create_user(perms=README_FC_PERMS, main_name="New Boss")
        new_boss_char = f.main_of(new_boss)
        f.add_token(new_boss, new_boss_char)
        self.esi.put_in_fleet(new_boss_char)
        self.client.force_login(new_boss)
        return running, new_boss, new_boss_char


class OneRecordPerFleetTests(StartTestBase):
    def test_open_record_is_returned_for_every_open_status(self):
        for status in (Status.STARTING, Status.ACTIVE, Status.ENDING):
            with self.subTest(status=status):
                FleetOperation.objects.all().delete()
                running = f.create_operation(self.fc, esi_fleet_id=FLEET_ID, status=status)

                operation = self.start()

                self.assertEqual(operation.pk, running.pk)
                self.assertEqual(FleetOperation.objects.count(), 1)

    def test_closed_record_does_not_block_a_new_start(self):
        closed = f.create_operation(self.fc, esi_fleet_id=FLEET_ID, status=Status.CLOSED)

        operation = self.start()

        self.assertNotEqual(operation.pk, closed.pk)
        self.assertFalse(getattr(operation, "already_tracked", False))
        self.assertEqual(FleetOperation.objects.count(), 2)

    def test_second_browser_tab_is_redirected_to_the_running_fleet(self):
        self.client.force_login(self.fc)
        first = self.post_start()
        operation = FleetOperation.objects.get()

        second = self.post_start()

        expected = reverse("fleetops:operation_detail", args=[operation.uuid])
        self.assertRedirects(first, expected, fetch_redirect_response=False)
        self.assertRedirects(second, expected, fetch_redirect_response=False)
        self.assertEqual(FleetOperation.objects.count(), 1)
        self.assertIn(
            f"ESI fleet {FLEET_ID} is already tracked by FleetOps (FC Boss Main). "
            "This is the running operation; no new fleet was started.",
            message_texts(second),
        )

    def test_start_during_a_boss_handover_explains_when_to_start_again(self):
        f.create_operation(
            f.create_user(perms=f.FC_PERMS, main_name="Old Boss"),
            esi_fleet_id=FLEET_ID,
            missing_reason=FleetOperation.EndReason.BOSS_HANDOVER,
            missing_detail="Boss Main",
            fleet_missing_count=1,
        )
        self.client.force_login(self.fc)

        response = self.post_start()

        self.assertEqual(FleetOperation.objects.count(), 1)
        self.assertIn(
            f"ESI fleet {FLEET_ID} is already tracked by FleetOps (FC Old Boss). This is the running operation; "
            "no new fleet was started. It ends automatically once the boss handover is confirmed; start the fleet "
            "again after that.",
            message_texts(response),
        )

    def test_handover_hint_names_the_manual_end_when_auto_end_is_disabled(self):
        f.settings(auto_end_enabled=False)
        f.create_operation(
            f.create_user(perms=f.FC_PERMS, main_name="Old Boss"),
            esi_fleet_id=FLEET_ID,
            missing_reason=FleetOperation.EndReason.BOSS_HANDOVER,
            missing_detail="Boss Main",
            fleet_missing_count=3,
        )
        self.client.force_login(self.fc)

        response = self.post_start()

        self.assertEqual(FleetOperation.objects.count(), 1)
        [text] = message_texts(response)
        self.assertEqual(
            text,
            f"ESI fleet {FLEET_ID} is already tracked by FleetOps (FC Old Boss). This is the running operation; "
            "no new fleet was started. Automatic end is disabled, so it stays open until its FC or a fleet manager "
            "ends it; start the fleet again after that.",
        )
        self.assertNotIn("ends automatically", text)

    def test_new_boss_who_cannot_open_the_running_record_stays_on_the_start_page(self):
        running, _new_boss, new_boss_char = self.handed_over_fleet()

        response = self.post_start(new_boss_char)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "fleetops/start_fleet.html")
        self.assertEqual(
            response.context["form"].non_field_errors(),
            [
                f"ESI fleet {FLEET_ID} is already tracked by FleetOps (FC Old Boss). No new fleet was started. "
                "It ends automatically once the boss handover is confirmed; start the fleet again after that."
            ],
        )
        self.assertEqual(message_texts(response), [])
        self.assertEqual(FleetOperation.objects.get().pk, running.pk)

    def test_new_boss_without_a_handover_is_told_whom_to_ask(self):
        running, _new_boss, new_boss_char = self.handed_over_fleet()
        FleetOperation.objects.filter(pk=running.pk).update(missing_reason="", missing_detail="", fleet_missing_count=0)

        response = self.post_start(new_boss_char)

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f"ESI fleet {FLEET_ID} is already tracked by FleetOps (FC Old Boss). No new fleet was started. "
            "Ask its FC or a fleet manager to end it, then start the fleet again.",
        )

    def test_new_boss_who_can_open_the_running_record_is_redirected_to_it(self):
        running, new_boss, new_boss_char = self.handed_over_fleet()
        f.add_attendance(running, new_boss)

        response = self.post_start(new_boss_char)

        detail = reverse("fleetops:operation_detail", args=[running.uuid])
        self.assertRedirects(response, detail)
        self.assertIn("This is the running operation; no new fleet was started.", message_texts(response)[0])

    def test_start_racing_another_request_returns_the_other_record(self):
        competing = {}

        def other_tab_commits_first(user, character_id, fresh=False):
            competing["operation"] = f.create_operation(self.fc, esi_fleet_id=FLEET_ID, status=Status.STARTING)
            return original(user, character_id)

        from fleetops.services import operations as operations_module

        original = operations_module.detect_character_fleet
        with mock.patch.object(operations_module, "detect_character_fleet", side_effect=other_tab_commits_first):
            operation = self.start()

        self.assertEqual(operation.pk, competing["operation"].pk)
        self.assertEqual(FleetOperation.objects.count(), 1)

    def test_start_locks_the_settings_row(self):
        if not connection.features.has_select_for_update:
            self.skipTest("The database has no SELECT ... FOR UPDATE.")
        with CaptureQueriesContext(connection) as queries:
            self.start()

        self.assertTrue(
            any("FOR UPDATE" in q["sql"] and "fleetops_fleetopssettings" in q["sql"] for q in queries.captured_queries)
        )


class AbandonedStartTests(StartTestBase):
    """A start whose request died after its record was created, e.g. a killed web worker."""

    def interrupted_start(self, minutes_ago, esi_fleet_id=FLEET_ID):
        operation = f.create_operation(
            self.fc, esi_fleet_id=esi_fleet_id, status=Status.STARTING, tracking_enabled=True
        )
        FleetOperation.objects.filter(pk=operation.pk).update(updated_at=timezone.now() - timedelta(minutes=minutes_ago))
        return operation

    def test_interrupted_start_no_longer_holds_the_fleet(self):
        stale = self.interrupted_start(minutes_ago=6)
        track(stale, self.fc_char, ship_type_id=RIFTER)

        operation = self.start()

        self.assertNotEqual(operation.pk, stale.pk)
        self.assertFalse(getattr(operation, "already_tracked", False))
        self.assertEqual(operation.status, Status.ACTIVE)
        stale.refresh_from_db()
        self.assertEqual((stale.status, stale.tracking_enabled), (Status.CANCELLED, False))
        self.assertIsNotNone(stale.ended_at)
        self.assertEqual(stale.last_error, START_ABANDONED_MESSAGE)
        self.assertFalse(stale.member_states.filter(is_active=True).exists())
        entry = AuditLog.objects.get(action="fleet.start_abandoned")
        self.assertEqual((entry.actor, entry.object_id), (self.fc, str(stale.pk)))
        self.assertEqual(entry.old_value, {"status": Status.STARTING, "tracking_enabled": True})
        self.assertEqual(entry.new_value, {"status": Status.CANCELLED, "tracking_enabled": False})

    def test_start_that_is_still_running_keeps_the_fleet(self):
        running = self.interrupted_start(minutes_ago=STARTING_ABANDONED_AFTER.total_seconds() / 60 - 1)
        self.client.force_login(self.fc)

        response = self.post_start()

        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[running.uuid]), fetch_redirect_response=False
        )
        self.assertEqual(FleetOperation.objects.count(), 1)
        self.assertEqual(FleetOperation.objects.get().status, Status.STARTING)
        self.assertIn(
            "Its start has not finished yet; if it was interrupted, start the fleet again in about five minutes.",
            message_texts(response)[0],
        )
        self.assertFalse(AuditLog.objects.filter(action="fleet.start_abandoned").exists())

    def test_fleet_can_be_started_again_after_a_worker_was_killed_mid_start(self):
        with mock.patch.object(operations_module, "sync_operation", side_effect=SystemExit(1)):
            with self.assertRaises(SystemExit):
                self.start()
        stale = FleetOperation.objects.get()
        self.assertEqual(stale.status, Status.STARTING)
        FleetOperation.objects.filter(pk=stale.pk).update(updated_at=timezone.now() - timedelta(minutes=6))
        self.client.force_login(self.fc)

        response = self.post_start()

        operation = FleetOperation.objects.exclude(pk=stale.pk).get()
        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False
        )
        self.assertIn("Fleet operation started. Review individual step status below.", message_texts(response))
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertEqual(FleetOperation.objects.get(pk=stale.pk).status, Status.CANCELLED)

    def test_start_cancelled_while_it_stalled_does_not_become_active(self):
        def stalled_sync(operation):
            # Meanwhile a newer start (or the beat) found the record stalled and cancelled it.
            FleetOperation.objects.filter(pk=operation.pk).update(updated_at=timezone.now() - timedelta(minutes=6))
            cancel_abandoned_starts()
            return 1

        with mock.patch.object(operations_module, "sync_operation", side_effect=stalled_sync):
            operation = self.start()

        self.assertEqual(operation.status, Status.CANCELLED)
        self.assertEqual(FleetOperation.objects.get().status, Status.CANCELLED)
        self.assertEqual(operation.last_error, START_ABANDONED_MESSAGE)

    def test_beat_cancels_interrupted_starts(self):
        stale = self.interrupted_start(minutes_ago=6)
        fresh = self.interrupted_start(minutes_ago=1, esi_fleet_id=FLEET_ID + 1)
        active = f.create_operation(self.fc, esi_fleet_id=FLEET_ID + 2)

        with mock.patch("fleetops.tasks.track_operation.delay"):
            schedule_active_fleet_tracking()

        statuses = dict(FleetOperation.objects.values_list("pk", "status"))
        self.assertEqual(
            statuses, {stale.pk: Status.CANCELLED, fresh.pk: Status.STARTING, active.pk: Status.ACTIVE}
        )
        entry = AuditLog.objects.get(action="fleet.start_abandoned")
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.reason, START_ABANDONED_MESSAGE)

    def test_resubmitted_form_of_a_cancelled_start_is_not_reported_as_started(self):
        request_id = uuid.uuid4()
        stale = self.interrupted_start(minutes_ago=6)
        FleetOperation.objects.filter(pk=stale.pk).update(start_request_id=request_id)
        cancel_abandoned_starts()
        self.client.force_login(self.fc)

        response = self.client.post(
            reverse("fleetops:start_fleet"),
            {
                "request_id": str(request_id),
                "operation_mode": "attendance_only",
                "fc_character_id": str(self.fc_char.character_id),
                "fleet_type": str(self.fleet_type.pk),
                "formup": "Jita",
            },
            follow=True,
        )

        self.assertRedirects(response, reverse("fleetops:operation_detail", args=[stale.uuid]))
        self.assertEqual(FleetOperation.objects.count(), 1)
        self.assertNotContains(response, "Fleet operation started.")
        self.assertContains(response, START_ABANDONED_MESSAGE)

    def test_cancelled_start_page_explains_what_happened(self):
        stale = self.interrupted_start(minutes_ago=6)
        cancel_abandoned_starts()
        self.client.force_login(self.fc)

        page = self.client.get(reverse("fleetops:operation_detail", args=[stale.uuid]))

        self.assertContains(page, "Cancelled")
        self.assertContains(page, START_ABANDONED_MESSAGE)


class StartPreviewTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.fc_char = f.main_of(self.fc)
        self.fleet_type = f.fleet_type()
        self.client.force_login(self.fc)

    def preview(self, **data):
        payload = {"request_id": str(uuid.uuid4()), "operation_mode": "full"}
        payload.update(data)
        return self.client.post(reverse("fleetops:preview_fleet"), payload)

    def test_incomplete_form_answers_200_with_the_missing_fields(self):
        response = self.preview()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["missing"], ["FC Character / Fleet Boss", "Fleet type", "Form Up / Staging"])
        self.assertEqual(set(payload["errors"]), {"fc_character_id", "fleet_type", "formup"})
        self.assertNotIn("ping", payload)

    def test_partly_filled_form_only_lists_what_is_missing(self):
        response = self.preview(fc_character_id=str(self.fc_char.character_id), fleet_type=str(self.fleet_type.pk))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["missing"], ["Form Up / Staging"])

    def test_invalid_value_is_reported_without_being_missing(self):
        response = self.preview(
            fc_character_id=str(self.fc_char.character_id), fleet_type="999999", formup="Jita"
        )

        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["missing"], [])
        self.assertEqual(set(payload["errors"]), {"fleet_type"})

    def test_complete_form_renders_the_preview(self):
        response = self.preview(
            fc_character_id=str(self.fc_char.character_id), fleet_type=str(self.fleet_type.pk), formup="Jita"
        )

        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("Jita", payload["motd"])

    def test_start_page_shows_missing_fields_and_debounces_the_preview(self):
        page = self.client.get(reverse("fleetops:start_fleet"))

        self.assertContains(page, 'id="preview-missing"')
        self.assertContains(page, "Missing: ${missing.join(', ')}")
        self.assertContains(page, "classList.add('is-invalid')")
        self.assertContains(page, "const PREVIEW_DELAY = 400;")
        self.assertContains(page, "setTimeout(preview,PREVIEW_DELAY)")


class ManualFleetTimeTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.fleet_type = f.fleet_type("Roam")

    def form(self, started="2026-10-11T19:00", ended="2026-10-11T21:00", **extra):
        data = {
            "fleet_type": self.fleet_type.pk,
            "formup": "Amamake",
            "started_at": started,
            "ended_at": ended,
            "attendance_multiplier": "1",
        }
        data.update(extra)
        return ManualFleetForm(data=data)

    def test_labels_name_eve_time(self):
        form = ManualFleetForm()

        self.assertEqual(form.fields["started_at"].label, "Start Time — EVE time (UTC)")
        self.assertEqual(form.fields["ended_at"].label, "End Time — EVE time (UTC)")

    def test_end_time_is_required(self):
        form = self.form(ended="")

        self.assertFalse(form.is_valid())
        self.assertIn("ended_at", form.errors)

    @override_settings(TIME_ZONE="Australia/Adelaide")
    def test_times_are_read_as_utc_whatever_the_time_zone(self):
        with timezone.override("America/Los_Angeles"):
            form = self.form()
            self.assertTrue(form.is_valid(), form.errors)

        self.assertEqual(form.cleaned_data["started_at"], datetime(2026, 10, 11, 19, 0, tzinfo=UTC))
        self.assertEqual(form.cleaned_data["ended_at"], datetime(2026, 10, 11, 21, 0, tzinfo=UTC))

    def test_explicit_offsets_are_respected(self):
        form = self.form(started="2026-10-11T21:00+02:00")

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["started_at"], datetime(2026, 10, 11, 19, 0, tzinfo=UTC))

    @override_settings(TIME_ZONE="Australia/Adelaide")
    def test_stored_times_are_shown_in_utc(self):
        form = ManualFleetForm(initial={"started_at": datetime(2026, 10, 11, 19, 0, tzinfo=UTC)})

        self.assertIn('value="2026-10-11T19:00"', str(form["started_at"]))

    def test_end_must_be_after_start(self):
        for ended in ("2026-10-11T19:00", "2026-10-11T18:59"):
            with self.subTest(ended=ended):
                form = self.form(ended=ended)
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors["ended_at"], ["End Time must be after Start Time."])

    def test_more_than_six_hours_needs_the_confirmation(self):
        form = self.form(ended="2026-10-12T08:51")

        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["confirm_long_fleet"],
            [
                "This fleet would last 13 h 51 min. Check the times, or tick this box if the fleet really ran "
                "for more than 6 hours."
            ],
        )
        self.assertTrue(self.form(ended="2026-10-12T08:51", confirm_long_fleet="on").is_valid())
        self.assertTrue(self.form(ended="2026-10-12T01:00").is_valid())

    def test_page_shows_eve_time_and_the_duration(self):
        self.client.force_login(self.fc)
        moment = datetime(2026, 10, 12, 8, 41, tzinfo=UTC)

        with mock.patch("django.utils.timezone.now", return_value=moment), override_settings(TIME_ZONE="Asia/Shanghai"):
            page = self.client.get(reverse("fleetops:manual_fleet"))

        self.assertContains(page, '<strong id="eve-time-now">2026-10-12 08:41</strong>')
        self.assertContains(page, 'id="manual-fleet-duration"')
        self.assertContains(page, "Duration: ${text}")
        self.assertEqual(page.context["form"]["ended_at"].value(), None)

    def test_long_fleet_is_saved_after_confirming(self):
        self.client.force_login(self.fc)
        data = {
            "fleet_type": self.fleet_type.pk,
            "formup": "Amamake",
            "started_at": "2026-10-11T19:21",
            "ended_at": "2026-10-12T08:51",
            "attendance_multiplier": "1",
        }

        rejected = self.client.post(reverse("fleetops:manual_fleet"), data)
        self.assertEqual(rejected.status_code, 200)
        self.assertContains(rejected, "This fleet would last 13 h 30 min.")
        self.assertFalse(FleetOperation.objects.exists())

        accepted = self.client.post(reverse("fleetops:manual_fleet"), {**data, "confirm_long_fleet": "on"})
        self.assertEqual(accepted.status_code, 302)
        operation = FleetOperation.objects.get()
        self.assertEqual(operation.started_at, datetime(2026, 10, 11, 19, 21, tzinfo=UTC))
        self.assertEqual(operation.ended_at - operation.started_at, timedelta(hours=13, minutes=30))
