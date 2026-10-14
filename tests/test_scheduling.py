# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Pre pings: scheduling a fleet, its page, editing, cancelling and starting it later."""

import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import AuditLog, FleetOperation, OperationAction
from fleetops.services import operations as operations_module
from fleetops.services import scheduling as scheduling_module
from fleetops.services.mentions import EVERYONE, HERE, ROLES, role_choice
from fleetops.services.messages import RenderedMessages
from fleetops.services.scheduling import cancel_scheduled_fleet, schedule_fleet, start_scheduled_fleet

from . import factories as f
from .static_files import static_source
from .test_operations_tracking import FLEET_ID, FakeESIMixin, set_scopes

UTC = dt_timezone.utc
Status = FleetOperation.Status
ActionStatus = OperationAction.Status
# The minimum FC permissions the README lists; no view_all_fleets.
README_FC_PERMS = ["fleetops.basic_access", "fleetops.start_fleet", "fleetops.manage_own_fleet"]
TOO_LONG = "The ping is 2105 characters; Discord accepts at most 2000. Shorten the additional information."


def flashed(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def steps(operation):
    return dict(OperationAction.objects.filter(operation=operation).values_list("action", "status"))


def in_two_hours():
    return (timezone.now() + timedelta(hours=2)).astimezone(UTC).replace(second=0, microsecond=0)


class SchedulingTestBase(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Plan FC")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.fleet_type = f.fleet_type("StratOp", "1.00")
        self.sig_a = f.ping_role("Sig A")
        self.channel = f.ping_channel("STR", allow_everyone=True, allow_here=True)
        self.esi.put_in_fleet(self.fc_char)
        self.esi.members[FLEET_ID] = [f.esi_member(self.fc_char)]
        f.settings(srp_auto_create=False)

    def form_data(self, when=None, **extra):
        when = when or in_two_hours()
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fleet_timing": "pre_ping",
            "fleet_date": when.strftime("%Y-%m-%d"),
            "fleet_clock": when.strftime("%H:%M"),
            "fc_character_id": str(self.fc_char.character_id),
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Jita",
            "ping_channel": str(self.channel.pk),
            "ping_mentions": [EVERYONE],
        }
        data.update(extra)
        return data

    def post_schedule(self, user=None, **extra):
        self.client.force_login(user or self.fc)
        return self.client.post(reverse("fleetops:start_fleet"), self.form_data(**extra))

    def scheduled(self, user=None, status=Status.SCHEDULED, **extra):
        """A pre ping as the start form stores it."""
        values = {
            "scheduled_at": in_two_hours(),
            "ping_channel": self.channel,
            "ping_mention_mode": EVERYONE,
            "ping_text": "Pre ping",
            "formup": "Amarr",
            "doctrine_source": "none",
        }
        values.update(extra)
        operation = f.create_scheduled_operation(user or self.fc, **values)
        if status != Status.SCHEDULED:
            FleetOperation.objects.filter(pk=operation.pk).update(status=status)
            operation.refresh_from_db()
        return operation

    def discord_payloads(self):
        return [call.kwargs["json"] for call in self.discord_post.call_args_list]


class ScheduleFleetTests(SchedulingTestBase):
    def test_pre_ping_creates_a_scheduled_fleet_without_esi(self):
        when = in_two_hours()

        response = self.post_schedule(when=when, additional_message="Bring cap boosters")

        operation = FleetOperation.objects.get()
        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False
        )
        self.assertIn("Fleet scheduled. Start it from this page when it is time.", flashed(response))
        self.assertEqual(self.esi.calls, [])
        self.assertEqual(operation.status, Status.SCHEDULED)
        self.assertEqual(operation.scheduled_at, when)
        self.assertIsNone(operation.started_at)
        self.assertIsNone(operation.esi_fleet_id)
        self.assertFalse(operation.tracking_enabled)
        self.assertTrue(operation.send_ping)
        self.assertEqual((operation.fc_user, operation.fc_character_id), (self.fc, self.fc_char.character_id))
        self.assertEqual((operation.ping_channel, operation.ping_mention_mode), (self.channel, EVERYONE))
        self.assertIn("Bring cap boosters", operation.ping_text)
        self.assertEqual(steps(operation), {"pre_ping": ActionStatus.SUCCESS})
        self.assertEqual(
            self.discord_payloads(),
            [{"content": operation.ping_text, "allowed_mentions": {"parse": ["everyone"]}, "flags": 4}],
        )
        entry = AuditLog.objects.get(action="fleet.schedule")
        self.assertEqual((entry.actor, entry.object_id), (self.fc, str(operation.pk)))
        self.assertEqual(entry.new_value["scheduled_at"], when.strftime("%Y-%m-%d %H:%M"))
        self.assertFalse(AuditLog.objects.filter(action="fleet.start").exists())

    def test_pre_ping_needs_no_fleet_token(self):
        set_scopes(self.fc.token_set.get(), [])

        self.post_schedule()

        self.assertEqual(FleetOperation.objects.get().status, Status.SCHEDULED)

    def test_pre_ping_is_rendered_as_a_pre_ping_by_the_sender(self):
        result = RenderedMessages("Pre ping text", "MOTD text", [], ping_display="Pre ping text", ping_length=13)
        with mock.patch.object(scheduling_module, "render_messages", return_value=result) as render:
            self.post_schedule()

        self.assertEqual(render.call_args.kwargs, {"actor": self.fc, "pre_ping": True})
        operation = FleetOperation.objects.get()
        self.assertEqual((operation.ping_text, operation.motd_text), ("Pre ping text", "MOTD text"))

    def test_past_fleet_time_is_rejected(self):
        response = self.post_schedule(when=timezone.now() - timedelta(minutes=10))

        self.assertEqual(response.status_code, 200)
        self.assertIn("fleet_clock", response.context["form"].errors)
        self.assertFalse(FleetOperation.objects.exists())
        self.discord_post.assert_not_called()

    def test_attendance_only_cannot_pre_ping(self):
        response = self.post_schedule(operation_mode="attendance_only")

        self.assertIn("fleet_timing", response.context["form"].errors)
        self.assertFalse(FleetOperation.objects.exists())

    def test_resubmitted_form_returns_the_same_fleet(self):
        data = self.form_data()
        self.client.force_login(self.fc)

        first = self.client.post(reverse("fleetops:start_fleet"), data)
        second = self.client.post(reverse("fleetops:start_fleet"), data)

        operation = FleetOperation.objects.get()
        for response in (first, second):
            self.assertRedirects(
                response, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False
            )
        self.assertEqual(self.discord_post.call_count, 1)

    def test_ping_over_the_discord_limit_schedules_nothing(self):
        result = RenderedMessages("x" * 2105, "MOTD", [], blocking=[TOO_LONG], ping_length=2105)
        with mock.patch("fleetops.views.render_messages", return_value=result):
            response = self.post_schedule()

        self.assertEqual(response.context["form"].non_field_errors(), [TOO_LONG])
        self.assertFalse(FleetOperation.objects.exists())
        self.discord_post.assert_not_called()

    def test_pre_ping_without_a_channel_is_kept_for_copying(self):
        self.post_schedule(ping_channel="")

        operation = FleetOperation.objects.get()
        step = operation.actions.get(action="pre_ping")
        self.assertEqual(step.status, ActionStatus.SKIPPED)
        self.assertEqual(step.error_message, "No Discord channel selected; the ping is available to copy.")
        self.discord_post.assert_not_called()

    def test_discord_failure_still_schedules_the_fleet(self):
        self.discord_post.return_value = mock.Mock(status_code=500, text="Discord is down")

        self.post_schedule()

        operation = FleetOperation.objects.get()
        self.assertEqual(operation.status, Status.SCHEDULED)
        step = operation.actions.get(action="pre_ping")
        self.assertEqual((step.status, step.error_message), (ActionStatus.FAILED, "Discord is down"))

    def test_service_is_idempotent_on_the_request_id(self):
        request_id = uuid.uuid4()
        data = {
            "fc_character_id": str(self.fc_char.character_id),
            "fleet_type": self.fleet_type,
            "formup": "Jita",
            "scheduled_at": in_two_hours(),
        }

        first = schedule_fleet(user=self.fc, cleaned_data=data, request_id=request_id)
        second = schedule_fleet(user=self.fc, cleaned_data=data, request_id=request_id)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(AuditLog.objects.filter(action="fleet.schedule").count(), 1)

    def test_character_of_another_user_cannot_schedule(self):
        other = f.main_of(f.create_user(perms=f.FC_PERMS))

        with self.assertRaises(PermissionError):
            schedule_fleet(
                user=self.fc,
                cleaned_data={
                    "fc_character_id": str(other.character_id),
                    "fleet_type": self.fleet_type,
                    "formup": "Jita",
                    "scheduled_at": in_two_hours(),
                },
            )
        self.assertFalse(FleetOperation.objects.exists())


class StartScheduledFleetTests(SchedulingTestBase):
    def post_start(self, operation, user=None, **extra):
        self.client.force_login(user or self.fc)
        data = {"fc_character_id": str(f.main_of(user or self.fc).character_id), "send_ping": "on"}
        data.update(extra)
        return self.client.post(reverse("fleetops:start_scheduled_fleet", args=[operation.uuid]), data)

    def test_start_tracks_the_fleet_from_now_with_the_scheduled_settings(self):
        operation = self.scheduled(formup="Amarr", additional_message="Bring links")
        before = timezone.now()

        response = self.post_start(operation)

        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False
        )
        self.assertIn("Scheduled fleet started. Review individual step status below.", flashed(response))
        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertGreaterEqual(operation.started_at, before)
        self.assertEqual(operation.esi_fleet_id, FLEET_ID)
        self.assertEqual(operation.fleet_boss_character_id, self.fc_char.character_id)
        self.assertTrue(operation.tracking_enabled)
        self.assertEqual((operation.formup, operation.additional_message), ("Amarr", "Bring links"))
        self.assertEqual(FleetOperation.objects.count(), 1)
        self.assertEqual(operation.member_states.get().first_seen, operation.last_esi_update)
        self.assertGreaterEqual(operation.member_states.get().first_seen, operation.started_at)
        self.assertEqual(steps(operation)["discord_ping"], ActionStatus.SUCCESS)
        self.assertEqual(steps(operation)["motd_update"], ActionStatus.SUCCESS)
        self.assertEqual(steps(operation)["tracking_start"], ActionStatus.SUCCESS)
        self.assertEqual(
            self.discord_payloads(),
            [{"content": operation.ping_text, "allowed_mentions": {"parse": ["everyone"]}, "flags": 4}],
        )

    def test_boss_check_uses_a_fresh_read(self):
        operation = self.scheduled()

        with mock.patch.object(
            operations_module, "detect_character_fleet", wraps=operations_module.detect_character_fleet
        ) as detect:
            self.post_start(operation)

        detect.assert_called_once_with(self.fc, self.fc_char.character_id, fresh=True)

    def test_character_that_is_not_the_boss_cannot_start(self):
        operation = self.scheduled()
        self.esi.put_in_fleet(self.fc_char, role="squad_member")

        response = self.post_start(operation)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "fleetops/scheduled_fleet.html")
        self.assertIn("Plan FC is not the fleet boss.", response.context["start_form"].non_field_errors()[0])
        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.SCHEDULED)

    def test_start_without_a_form_up_ping(self):
        operation = self.scheduled()

        self.post_start(operation, send_ping="")

        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.ACTIVE)
        step = operation.actions.get(action="discord_ping")
        self.assertEqual((step.status, step.error_message), (ActionStatus.SKIPPED, "Form-up ping not requested."))
        self.discord_post.assert_not_called()
        self.assertEqual(steps(operation)["motd_update"], ActionStatus.SUCCESS)

    def test_one_start_audit_entry_marked_as_from_the_schedule(self):
        operation = self.scheduled()

        self.post_start(operation)

        entry = AuditLog.objects.get(action="fleet.start")
        self.assertEqual(entry.object_id, str(operation.pk))
        self.assertEqual(entry.old_value, {"status": Status.SCHEDULED})
        self.assertTrue(entry.new_value["from_schedule"])
        self.assertEqual(entry.new_value["esi_fleet_id"], FLEET_ID)
        # The same entry lists what the start did; the earlier pre ping is not one of its steps.
        self.assertEqual(
            entry.new_value["steps"],
            {
                "fleet_detection": ActionStatus.SUCCESS,
                "discord_ping": ActionStatus.SUCCESS,
                "motd_update": ActionStatus.SUCCESS,
                "tracking_start": ActionStatus.SUCCESS,
                "srp_link": ActionStatus.SKIPPED,
            },
        )
        self.assertFalse(AuditLog.objects.filter(action="fleet.schedule_start").exists())

    def test_start_refreshes_the_doctrine_fits(self):
        operation = self.scheduled(
            doctrine_source="fittings:fittings.doctrine", doctrine_external_id="12", doctrine_name="Moa Fleet"
        )
        fits = [{"fit_id": 3, "fit": "DPS Moa", "ship_type_id": 623, "ship": "Moa"}]

        with mock.patch.object(scheduling_module, "doctrine_fits_snapshot", return_value=fits) as snapshot:
            self.post_start(operation)

        snapshot.assert_called_once_with("fittings:fittings.doctrine", "12")
        operation.refresh_from_db()
        self.assertEqual(operation.doctrine_fits, fits)

    def test_start_with_another_owned_character(self):
        alt = f.add_alt(self.fc, "Plan Alt")
        f.add_token(self.fc, alt)
        self.esi.put_in_fleet(alt)
        self.esi.members[FLEET_ID] = [f.esi_member(alt)]
        operation = self.scheduled()

        self.post_start(operation, fc_character_id=str(alt.character_id))

        operation.refresh_from_db()
        self.assertEqual((operation.status, operation.fc_character_id), (Status.ACTIVE, alt.character_id))
        self.assertEqual(operation.fc_character_name, "Plan Alt")
        self.assertEqual(operation.fc_main_character_id, self.fc_char.character_id)

    def test_esi_fleet_already_tracked_by_the_same_fc_keeps_the_schedule(self):
        running = f.create_operation(self.fc, esi_fleet_id=FLEET_ID)
        operation = self.scheduled()

        response = self.post_start(operation)

        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[running.uuid]), fetch_redirect_response=False
        )
        self.assertIn("The scheduled fleet was not started.", flashed(response)[0])
        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.SCHEDULED)
        self.assertFalse(AuditLog.objects.filter(action="fleet.start").exists())

    def test_new_boss_takes_the_fleet_over_from_another_fc(self):
        old = f.create_operation(f.create_user(perms=f.FC_PERMS, main_name="Old Boss"), esi_fleet_id=FLEET_ID)
        operation = self.scheduled()

        response = self.post_start(operation)

        self.assertIn(
            "Scheduled fleet started. Review individual step status below. The fleet boss was handed over to you, "
            "so the previous operation (FC Old Boss) was ended.",
            flashed(response),
        )
        old.refresh_from_db()
        operation.refresh_from_db()
        self.assertEqual((old.status, old.end_reason), (Status.CLOSED, FleetOperation.EndReason.BOSS_HANDOVER))
        self.assertEqual(old.ended_at, operation.started_at)
        self.assertEqual(operation.status, Status.ACTIVE)
        start = AuditLog.objects.get(action="fleet.start")
        self.assertEqual(start.new_value["handed_over_from"], [old.pk])
        self.assertTrue(start.new_value["from_schedule"])
        self.assertEqual(AuditLog.objects.get(action="fleet.boss_handover").object_id, str(old.pk))

    def test_only_the_fc_who_scheduled_it_may_start_it(self):
        operation = self.scheduled()
        lead = f.create_user(perms=f.FC_LEAD_PERMS, main_name="Lead")
        other_fc = f.create_user(perms=f.FC_PERMS, main_name="Other FC")
        for user in (lead, other_fc):
            f.add_token(user, f.main_of(user))
            self.esi.put_in_fleet(f.main_of(user))
            with self.subTest(user=user.username):
                response = self.post_start(operation, user=user)

                self.assertEqual(response.status_code, 404)
        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.SCHEDULED)
        self.assertEqual(self.esi.calls, [])

    def test_service_refuses_other_users(self):
        operation = self.scheduled()
        lead = f.create_user(perms=f.FC_LEAD_PERMS)

        with self.assertRaises(PermissionError):
            start_scheduled_fleet(operation, user=lead, character_id=f.main_of(lead).character_id, send_ping=True)

    def test_page_offers_the_start_only_to_the_fc(self):
        operation = self.scheduled()
        lead = f.create_user(perms=f.FC_LEAD_PERMS)
        self.client.force_login(self.fc)

        own = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        self.client.force_login(lead)
        other = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

        self.assertContains(own, reverse("fleetops:start_scheduled_fleet", args=[operation.uuid]))
        self.assertContains(own, "Send a form-up ping now in STR")
        self.assertNotContains(other, reverse("fleetops:start_scheduled_fleet", args=[operation.uuid]))
        self.assertContains(other, "Only Plan FC, who scheduled this fleet, can start it.")
        self.assertContains(other, reverse("fleetops:cancel_scheduled_fleet", args=[operation.uuid]))

    def test_double_start_is_safe(self):
        operation = self.scheduled()

        self.post_start(operation)
        second = self.post_start(operation)

        self.assertRedirects(
            second, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False
        )
        self.assertIn("This fleet is already active.", flashed(second))
        self.assertEqual(AuditLog.objects.filter(action="fleet.start").count(), 1)
        self.assertEqual(self.discord_post.call_count, 1)
        self.assertEqual(len(self.esi.put_calls), 1)

    def test_service_returns_a_started_fleet_unchanged(self):
        operation = self.scheduled()
        start_scheduled_fleet(operation, user=self.fc, character_id=self.fc_char.character_id, send_ping=True)
        stale = FleetOperation.objects.get(pk=operation.pk)
        stale.status = Status.SCHEDULED

        again = start_scheduled_fleet(stale, user=self.fc, character_id=self.fc_char.character_id, send_ping=True)

        self.assertEqual(again.status, Status.ACTIVE)
        self.assertEqual(AuditLog.objects.filter(action="fleet.start").count(), 1)

    def test_overdue_fleet_can_still_be_started(self):
        operation = self.scheduled()
        FleetOperation.objects.filter(pk=operation.pk).update(scheduled_at=timezone.now() - timedelta(hours=3))

        self.client.force_login(self.fc)
        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        self.assertContains(page, 'id="scheduled-overdue"')
        self.post_start(operation)

        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.ACTIVE)

    def test_interrupted_start_from_a_schedule_is_not_a_cancelled_pre_ping(self):
        operation = self.scheduled()
        with mock.patch.object(operations_module, "sync_operation", side_effect=SystemExit(1)):
            with self.assertRaises(SystemExit):
                start_scheduled_fleet(
                    operation, user=self.fc, character_id=self.fc_char.character_id, send_ping=False
                )
        FleetOperation.objects.filter(pk=operation.pk).update(updated_at=timezone.now() - timedelta(minutes=6))

        self.assertEqual(operations_module.cancel_abandoned_starts(), 1)

        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.CANCELLED)
        self.assertIsNotNone(operation.scheduled_at)
        self.assertIsNotNone(operation.started_at)
        self.assertFalse(scheduling_module.is_pre_ping_record(operation))
        self.client.force_login(self.fc)
        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        self.assertTemplateUsed(page, "fleetops/operation_detail.html")
        self.assertTemplateNotUsed(page, "fleetops/scheduled_fleet.html")
        retry = self.client.post(reverse("fleetops:retry_ping", args=[operation.uuid]))
        self.assertNotIn("This scheduled fleet was cancelled, so its pre ping is not posted again.", flashed(retry))


class EditScheduledFleetTests(SchedulingTestBase):
    def edit(self, operation, user=None, **extra):
        when = operation.scheduled_at.astimezone(UTC)
        data = {
            "fleet_type": str(self.fleet_type.pk),
            "formup": operation.formup,
            "fleet_date": when.strftime("%Y-%m-%d"),
            "fleet_clock": when.strftime("%H:%M"),
            "ping_channel": str(self.channel.pk),
            "ping_mentions": [EVERYONE],
        }
        data.update(extra)
        self.client.force_login(user or self.fc)
        return self.client.post(reverse("fleetops:edit_scheduled_fleet", args=[operation.uuid]), data)

    def test_edit_page_starts_from_the_stored_fleet(self):
        operation = self.scheduled(ping_mention_mode=ROLES, ping_roles=[{"id": str(self.sig_a.role_id), "name": "Sig A"}])
        self.client.force_login(self.fc)

        page = self.client.get(reverse("fleetops:edit_scheduled_fleet", args=[operation.uuid]))

        form = page.context["form"]
        self.assertTemplateUsed(page, "fleetops/start_fleet.html")
        self.assertEqual(form["formup"].value(), "Amarr")
        self.assertEqual(form["ping_mentions"].value(), [role_choice(self.sig_a)])
        self.assertEqual(form["fleet_clock"].value(), operation.scheduled_at.astimezone(UTC).strftime("%H:%M"))
        self.assertContains(page, "Edit Scheduled Fleet")
        self.assertContains(page, "SAVE CHANGES")
        self.assertNotContains(page, 'name="fc_character_id"')

    def test_edit_with_an_updated_pre_ping(self):
        operation = self.scheduled()
        later = in_two_hours() + timedelta(hours=1)

        response = self.edit(
            operation,
            formup="Perimeter",
            fleet_date=later.strftime("%Y-%m-%d"),
            fleet_clock=later.strftime("%H:%M"),
            ping_mentions=[HERE],
            send_update="on",
        )

        self.assertIn("Scheduled fleet updated and the updated pre ping was posted.", flashed(response))
        operation.refresh_from_db()
        self.assertEqual((operation.formup, operation.scheduled_at, operation.ping_mention_mode), ("Perimeter", later, HERE))
        self.assertIn("Perimeter", operation.ping_text)
        self.assertEqual(
            self.discord_payloads(),
            [{"content": operation.ping_text, "allowed_mentions": {"parse": ["everyone"]}, "flags": 4}],
        )
        self.assertEqual(operation.actions.get(action="pre_ping").status, ActionStatus.SUCCESS)
        entry = AuditLog.objects.get(action="fleet.schedule_edit")
        self.assertEqual(entry.old_value, {"formup": "Amarr", "ping_mention_mode": EVERYONE, "scheduled_at": entry.old_value["scheduled_at"]})
        self.assertEqual(entry.new_value["formup"], "Perimeter")
        self.assertEqual(entry.new_value["ping_mention_mode"], HERE)
        self.assertEqual(entry.new_value["scheduled_at"], later.strftime("%Y-%m-%d %H:%M"))
        self.assertTrue(entry.new_value["send_update"])

    def test_edit_without_an_update_posts_nothing(self):
        operation = self.scheduled()

        response = self.edit(operation, formup="Perimeter")

        self.assertIn("Scheduled fleet updated. No new pre ping was posted.", flashed(response))
        self.discord_post.assert_not_called()
        operation.refresh_from_db()
        self.assertIn("Perimeter", operation.ping_text)
        entry = AuditLog.objects.get(action="fleet.schedule_edit")
        self.assertEqual(entry.old_value, {"formup": "Amarr"})
        self.assertEqual(entry.new_value, {"formup": "Perimeter", "send_update": False})

    def test_editing_to_a_past_time_is_rejected(self):
        operation = self.scheduled()
        past = timezone.now() - timedelta(hours=1)

        response = self.edit(operation, fleet_date=past.strftime("%Y-%m-%d"), fleet_clock=past.strftime("%H:%M"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("fleet_clock", response.context["form"].errors)
        self.assertFalse(AuditLog.objects.filter(action="fleet.schedule_edit").exists())

    def test_overdue_fleet_is_edited_without_moving_its_time(self):
        overdue = timezone.now().astimezone(UTC).replace(second=0, microsecond=0) - timedelta(minutes=30)
        operation = self.scheduled(scheduled_at=overdue)

        response = self.edit(operation, formup="Perimeter")

        self.assertEqual(response.status_code, 302)
        operation.refresh_from_db()
        self.assertEqual((operation.formup, operation.scheduled_at), ("Perimeter", overdue))
        entry = AuditLog.objects.get(action="fleet.schedule_edit")
        self.assertEqual(entry.old_value, {"formup": "Amarr"})

    def test_overdue_fleet_cannot_move_to_another_past_time(self):
        overdue = timezone.now().astimezone(UTC).replace(second=0, microsecond=0) - timedelta(minutes=30)
        operation = self.scheduled(scheduled_at=overdue)
        earlier = overdue - timedelta(minutes=10)

        response = self.edit(operation, fleet_date=earlier.strftime("%Y-%m-%d"), fleet_clock=earlier.strftime("%H:%M"))

        self.assertEqual(response.context["form"].errors["fleet_clock"], ["The fleet time must be in the future (EVE time)."])
        self.assertFalse(AuditLog.objects.filter(action="fleet.schedule_edit").exists())

    def test_new_ping_channel_needs_the_updated_pre_ping(self):
        ops = f.ping_channel("OPS", allow_everyone=True)
        operation = self.scheduled()

        response = self.edit(operation, ping_channel=str(ops.pk), formup="Perimeter")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"].errors["send_update"], [scheduling_module.CHANNEL_CHANGE_NEEDS_POST]
        )
        operation.refresh_from_db()
        self.assertEqual((operation.ping_channel, operation.formup), (self.channel, "Amarr"))
        self.discord_post.assert_not_called()

    def test_cancellation_notice_goes_to_the_channel_of_the_latest_pre_ping(self):
        ops = f.ping_channel("OPS", allow_everyone=True)
        operation = self.scheduled()

        self.edit(operation, ping_channel=str(ops.pk), send_update="on")
        self.client.post(reverse("fleetops:cancel_scheduled_fleet", args=[operation.uuid]), {"notify": "on"})

        self.assertEqual([call.args[0] for call in self.discord_post.call_args_list], [ops.webhook_url] * 2)
        self.assertEqual(self.discord_payloads()[1]["allowed_mentions"], {"parse": []})

    def test_double_submit_posts_the_updated_pre_ping_once(self):
        operation = self.scheduled()
        self.client.force_login(self.fc)
        page = self.client.get(reverse("fleetops:edit_scheduled_fleet", args=[operation.uuid]))
        token = str(page.context["form"]["edit_token"].value())
        self.assertContains(page, f'name="edit_token" value="{token}"')

        first = self.edit(operation, formup="Perimeter", send_update="on", edit_token=token)
        second = self.edit(operation, formup="Perimeter", send_update="on", edit_token=token)

        self.assertIn("Scheduled fleet updated and the updated pre ping was posted.", flashed(first))
        self.assertIn("Scheduled fleet updated. The updated pre ping was already posted.", flashed(second))
        self.assertEqual(len(self.discord_payloads()), 1)
        self.assertFalse(AuditLog.objects.filter(action="fleet.schedule_edit").order_by("pk").last().new_value["send_update"])
        # Another edit page posts again.
        self.edit(operation, formup="Perimeter", send_update="on", edit_token=str(uuid.uuid4()))
        self.assertEqual(len(self.discord_payloads()), 2)

    def test_failed_updated_pre_ping_can_be_sent_again_from_the_same_page(self):
        operation = self.scheduled()
        token = str(uuid.uuid4())
        self.discord_post.return_value = mock.Mock(status_code=500, text="Discord is down")

        self.edit(operation, formup="Perimeter", send_update="on", edit_token=token)
        self.discord_post.return_value = mock.Mock(status_code=204, text="")
        again = self.edit(operation, formup="Perimeter", send_update="on", edit_token=token)

        self.assertIn("Scheduled fleet updated and the updated pre ping was posted.", flashed(again))
        self.assertEqual(self.discord_post.call_count, 2)
        operation.refresh_from_db()
        self.assertEqual(operation.actions.get(action="pre_ping").status, ActionStatus.SUCCESS)

    def test_edit_page_disables_save_on_submit(self):
        operation = self.scheduled()
        self.client.force_login(self.fc)

        page = self.client.get(reverse("fleetops:edit_scheduled_fleet", args=[operation.uuid]))

        self.assertContains(page, "fleetops/start_fleet.js")
        self.assertIn("form.addEventListener('submit', () => { submitButton.disabled = true; });", static_source("start_fleet.js"))

    def test_channel_rules_apply_to_the_edit(self):
        quiet = f.ping_channel("Quiet")
        operation = self.scheduled()

        response = self.edit(operation, ping_channel=str(quiet.pk))

        self.assertEqual(response.context["form"].errors["ping_mentions"], ["@everyone is not allowed in Quiet."])

    def test_ping_over_the_discord_limit_is_not_saved(self):
        operation = self.scheduled()
        result = RenderedMessages("x" * 2105, "MOTD", [], blocking=[TOO_LONG], ping_length=2105)

        with mock.patch("fleetops.views.render_messages", return_value=result):
            response = self.edit(operation, formup="Perimeter", send_update="on")

        self.assertEqual(response.context["form"].non_field_errors(), [TOO_LONG])
        operation.refresh_from_db()
        self.assertEqual(operation.formup, "Amarr")
        self.discord_post.assert_not_called()

    def test_edit_page_previews_through_its_own_url(self):
        operation = self.scheduled()
        self.client.force_login(self.fc)
        when = operation.scheduled_at.astimezone(UTC)

        payload = self.client.post(
            reverse("fleetops:edit_scheduled_fleet", args=[operation.uuid]),
            {
                "preview": "1",
                "fleet_type": str(self.fleet_type.pk),
                "formup": "Perimeter",
                "fleet_date": when.strftime("%Y-%m-%d"),
                "fleet_clock": when.strftime("%H:%M"),
            },
        ).json()

        self.assertTrue(payload["ok"])
        self.assertIn("Perimeter", payload["ping"])
        operation.refresh_from_db()
        self.assertEqual(operation.formup, "Amarr")

    def test_same_doctrine_keeps_its_announced_fits(self):
        fits = [{"fit_id": 3, "fit": "DPS Moa", "ship_type_id": 623, "ship": "Moa"}]
        operation = self.scheduled(doctrine_source="custom", doctrine_name="Moa", doctrine_fits=fits)

        self.edit(operation, custom_doctrine="Moa", formup="Perimeter")

        operation.refresh_from_db()
        self.assertEqual(operation.doctrine_fits, fits)

    def test_fleet_manager_may_edit_and_other_fcs_may_not(self):
        operation = self.scheduled()
        lead = f.create_user(perms=f.FC_LEAD_PERMS)
        other_fc = f.create_user(perms=f.FC_PERMS)

        self.assertEqual(self.edit(operation, user=other_fc, formup="Other").status_code, 404)
        self.assertEqual(self.edit(operation, user=lead, formup="Lead").status_code, 302)

        operation.refresh_from_db()
        self.assertEqual(operation.formup, "Lead")
        # The FC stays the FC of record.
        self.assertEqual(operation.fc_user, self.fc)

    def test_started_fleet_is_not_edited_here(self):
        operation = self.scheduled()
        FleetOperation.objects.filter(pk=operation.pk).update(status=Status.ACTIVE, started_at=timezone.now())

        response = self.edit(operation, formup="Late")

        self.assertEqual(response.status_code, 404)

    def test_archive_edit_of_a_scheduled_fleet_goes_to_the_scheduled_edit(self):
        operation = self.scheduled()
        self.client.force_login(self.fc)

        response = self.client.get(reverse("fleetops:edit_operation", args=[operation.uuid]))

        self.assertRedirects(
            response, reverse("fleetops:edit_scheduled_fleet", args=[operation.uuid]), fetch_redirect_response=False
        )


class CancelScheduledFleetTests(SchedulingTestBase):
    def cancel(self, operation, user=None, **data):
        self.client.force_login(user or self.fc)
        return self.client.post(reverse("fleetops:cancel_scheduled_fleet", args=[operation.uuid]), data)

    def test_cancel_with_a_notice_that_mentions_nobody(self):
        operation = self.scheduled(scheduled_at=datetime(2099, 10, 12, 19, 0, tzinfo=UTC), formup="Amarr @everyone")

        response = self.cancel(operation, notify="on")

        self.assertIn("Scheduled fleet cancelled. Cancellation notice: Success.", flashed(response))
        operation.refresh_from_db()
        self.assertEqual((operation.status, operation.tracking_enabled), (Status.CANCELLED, False))
        [payload] = self.discord_payloads()
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(payload["flags"], 4)
        self.assertTrue(payload["content"].startswith("**Cancelled:** StratOp at 2099-10-12 19:00 EVE (Amarr @\u200beveryone)"))
        self.assertIn("-# FleetOps notice from Plan FC at ", payload["content"])
        self.assertEqual(self.discord_post.call_args.args[0], self.channel.webhook_url)
        self.assertEqual(steps(operation), {"fleet_cancel": ActionStatus.SUCCESS, "cancel_notice": ActionStatus.SUCCESS})
        entry = AuditLog.objects.get(action="fleet.schedule_cancel")
        self.assertEqual((entry.old_value, entry.new_value), ({"status": "scheduled"}, {"status": "cancelled", "notify": True}))

    def test_cancel_without_a_notice(self):
        operation = self.scheduled()

        self.cancel(operation)

        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.CANCELLED)
        self.discord_post.assert_not_called()
        self.assertEqual(steps(operation), {"fleet_cancel": ActionStatus.SUCCESS})

    def test_notice_is_sent_by_the_cancelling_manager(self):
        operation = self.scheduled()
        lead = f.create_user(perms=f.FC_LEAD_PERMS, main_name="Lead Main")

        self.cancel(operation, user=lead, notify="on")

        self.assertIn("-# FleetOps notice from Lead Main at ", self.discord_payloads()[0]["content"])
        self.assertEqual(AuditLog.objects.get(action="fleet.schedule_cancel").actor, lead)

    def test_other_fcs_cannot_cancel(self):
        operation = self.scheduled()

        response = self.cancel(operation, user=f.create_user(perms=f.FC_PERMS))

        self.assertEqual(response.status_code, 404)
        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.SCHEDULED)

    def test_double_cancel_is_safe(self):
        operation = self.scheduled()

        self.cancel(operation, notify="on")
        second = self.cancel(operation, notify="on")

        self.assertIn("This fleet is already cancelled.", flashed(second))
        self.assertEqual(self.discord_post.call_count, 1)
        self.assertEqual(AuditLog.objects.filter(action="fleet.schedule_cancel").count(), 1)

    def test_started_fleet_cannot_be_cancelled_here(self):
        operation = self.scheduled()
        start_scheduled_fleet(operation, user=self.fc, character_id=self.fc_char.character_id, send_ping=False)

        response = self.cancel(operation, notify="on")

        self.assertEqual(response.status_code, 404)
        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.ACTIVE)

    def test_service_reports_whether_it_cancelled(self):
        operation = self.scheduled()

        first = cancel_scheduled_fleet(operation, actor=self.fc, notify=False)
        second = cancel_scheduled_fleet(operation, actor=self.fc, notify=False)

        self.assertTrue(first.cancelled)
        self.assertFalse(second.cancelled)

    def test_cancelled_pre_ping_page_and_retries(self):
        operation = self.scheduled()
        self.cancel(operation)

        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        retry = self.client.post(reverse("fleetops:retry_ping", args=[operation.uuid]))

        self.assertTemplateUsed(page, "fleetops/scheduled_fleet.html")
        self.assertContains(page, "This scheduled fleet was cancelled; it never started.")
        self.assertNotContains(page, reverse("fleetops:start_scheduled_fleet", args=[operation.uuid]))
        self.assertIn("This scheduled fleet was cancelled, so its pre ping is not posted again.", flashed(retry))
        self.discord_post.assert_not_called()


class ScheduledFleetPageTests(SchedulingTestBase):
    def test_page_shows_time_countdown_mentions_and_steps(self):
        operation = self.scheduled(
            scheduled_at=datetime(2099, 10, 12, 19, 0, tzinfo=UTC),
            ping_mention_mode=ROLES,
            ping_roles=[{"id": str(self.sig_a.role_id), "name": "Sig A"}],
            ping_text=f"<@&{self.sig_a.role_id}> Form up",
        )
        OperationAction.objects.create(operation=operation, action="pre_ping", status=ActionStatus.SUCCESS)
        self.client.force_login(self.fc)

        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

        self.assertTemplateUsed(page, "fleetops/scheduled_fleet.html")
        self.assertContains(page, "2099-10-12 19:00 EVE")
        self.assertContains(page, 'data-countdown-utc="2099-10-12T19:00:00Z"')
        self.assertContains(page, "@Sig A Form up")
        self.assertContains(page, "pre_ping")
        self.assertNotContains(page, 'id="scheduled-overdue"')
        self.assertContains(page, "Post a cancellation notice in STR")
        self.assertContains(page, reverse("fleetops:retry_ping", args=[operation.uuid]))

    def test_retry_pre_ping_posts_the_pre_ping_again(self):
        operation = self.scheduled()
        self.client.force_login(self.fc)

        self.client.post(reverse("fleetops:retry_ping", args=[operation.uuid]))

        self.assertEqual(operation.actions.get(action="pre_ping").status, ActionStatus.SUCCESS)
        self.assertFalse(operation.actions.filter(action="discord_ping").exists())
        self.assertEqual(self.discord_payloads()[0]["content"], "Pre ping")

    def test_motd_and_srp_retries_wait_for_the_start(self):
        operation = self.scheduled()
        self.client.force_login(self.fc)

        for name in ("retry_motd", "retry_srp"):
            with self.subTest(name=name):
                response = self.client.post(reverse(f"fleetops:{name}", args=[operation.uuid]))

                self.assertIn(
                    "This fleet has not started; the MOTD and the SRP link are set when it starts.", flashed(response)
                )
        self.assertEqual(self.esi.put_calls, [])
        self.assertEqual(steps(operation), {})


class ScheduledFleetVisibilityTests(SchedulingTestBase):
    def setUp(self):
        super().setUp()
        self.operation = self.scheduled()
        self.cancelled = self.scheduled(status=Status.CANCELLED)
        self.member = f.create_user(perms=f.MEMBER_PERMS)
        self.readme_fc = f.create_user(perms=README_FC_PERMS)
        self.viewer = f.create_user(perms=f.MEMBER_PERMS + ["fleetops.view_all_fleets"])
        self.lead = f.create_user(perms=f.FC_LEAD_PERMS)

    def test_detail_page(self):
        for user, status in (
            (self.fc, 200),
            (self.readme_fc, 200),
            (self.viewer, 200),
            (self.lead, 200),
            (self.member, 404),
        ):
            for operation in (self.operation, self.cancelled):
                with self.subTest(user=user.username, status=operation.status):
                    self.client.force_login(user)

                    response = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

                    self.assertEqual(response.status_code, status)

    def test_dashboard_lists_scheduled_fleets_for_fcs_and_managers(self):
        later = self.scheduled(scheduled_at=in_two_hours() + timedelta(hours=5), formup="Later")
        for user in (self.fc, self.readme_fc, self.viewer, self.lead):
            with self.subTest(user=user.username):
                self.client.force_login(user)

                page = self.client.get(reverse("fleetops:dashboard"))

                self.assertEqual(list(page.context["scheduled_operations"]), [self.operation, later])
                self.assertContains(page, "Scheduled Fleets")
        self.client.force_login(self.member)
        page = self.client.get(reverse("fleetops:dashboard"))
        self.assertEqual(list(page.context["scheduled_operations"]), [])
        self.assertNotContains(page, "Scheduled Fleets")

    def test_scheduled_fleets_are_not_listed_as_own_fleets(self):
        self.client.force_login(self.fc)

        page = self.client.get(reverse("fleetops:dashboard"))

        self.assertEqual(list(page.context["own_operations"]), [])

    def test_archive_files_pre_pings_under_their_fleet_time(self):
        when = datetime(2099, 3, 14, 19, 0, tzinfo=UTC)
        FleetOperation.objects.filter(pk=self.operation.pk).update(scheduled_at=when)
        for user, visible in ((self.readme_fc, True), (self.lead, True), (self.viewer, True), (self.member, False)):
            with self.subTest(user=user.username):
                self.client.force_login(user)

                page = self.client.get(reverse("fleetops:fleet_operations"), {"year": 2099, "month": 3})

                listed = list(page.context["page"].object_list)
                self.assertEqual(listed == [self.operation], visible)
        self.client.force_login(self.lead)
        march = self.client.get(reverse("fleetops:fleet_operations"), {"year": 2099, "month": 3})
        self.assertContains(march, "2099-03-14 19:00")
        self.assertContains(march, reverse("fleetops:edit_scheduled_fleet", args=[self.operation.uuid]))

    def test_archive_lists_cancelled_pre_pings_for_fcs(self):
        self.client.force_login(self.readme_fc)
        when = self.cancelled.scheduled_at.astimezone(UTC)

        page = self.client.get(reverse("fleetops:fleet_operations"), {"year": when.year, "month": when.month})

        self.assertIn(self.cancelled, list(page.context["page"].object_list))
        self.assertContains(page, "Pre ping, never started")
