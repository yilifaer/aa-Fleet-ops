"""Regenerate & push MOTD on a running fleet, and the fleet page's Ping / MOTD display."""

from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from fleetops.models import (
    AuditLog,
    CommsPreset,
    FleetOperation,
    MessageTemplate,
    OperationAction,
    OperationActionAttempt,
)
from fleetops.providers import esi as esi_provider
from fleetops.providers.doctrines import FITTINGS_DOCTRINE_SOURCE
from fleetops.services.motd import NOT_ACTIVE, regenerate_and_push_motd
from fleetops.services.operations import set_action

from . import factories as f
from .test_operations_tracking import FLEET_ID, FakeESIMixin, http_error

Status = FleetOperation.Status
ActionStatus = OperationAction.Status

OLD_FITS = [{"fit_id": 1, "fit": "Old Fit", "ship_type_id": 623, "ship": "Moa"}]
NEW_FITS = [
    {"fit_id": 1, "fit": "DPS Moa", "ship_type_id": 623, "ship": "Moa"},
    {"fit_id": 2, "fit": "Logi Osprey", "ship_type_id": 620, "ship": "Osprey"},
]


class RegenerateTestBase(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Motd Boss")
        self.fc_char = f.main_of(self.fc)
        self.token = f.add_token(self.fc, self.fc_char)
        self.template = MessageTemplate.objects.create(
            name="Test MOTD", template_type=MessageTemplate.TemplateType.MOTD, content="Form up {{ formup }}"
        )
        self.operation = f.create_operation(
            self.fc,
            esi_fleet_id=FLEET_ID,
            send_ping=True,
            motd_template=self.template,
            motd_text="Form up Jita",
            formup="Jita",
        )
        set_action(self.operation, "motd_update", True)
        self.esi.fleet_info[FLEET_ID] = {"is_free_move": True, "motd": "Form up Jita"}

    def regenerate(self, operation=None):
        return regenerate_and_push_motd(operation or self.operation, actor=self.fc)


class RegenerateServiceTests(RegenerateTestBase):
    def test_current_template_and_settings_are_rendered_and_pushed(self):
        self.template.content = "Form up {{ formup }} on {{ comms }}"
        self.template.save()
        self.operation.comms = CommsPreset.objects.create(name="Alliance Mumble", voice_url="https://voice.example.com")
        self.operation.save()

        action = self.regenerate()

        self.operation.refresh_from_db()
        self.assertEqual(action.status, ActionStatus.SUCCESS)
        self.assertEqual(self.operation.motd_text, "Form up Jita on Alliance Mumble")
        [put] = self.esi.put_calls
        self.assertEqual(put["fleet_id"], FLEET_ID)
        self.assertEqual(put["body"], {"motd": "Form up Jita on Alliance Mumble", "is_free_move": True})
        self.assertEqual(put["token"], self.token)

    def test_push_is_a_new_attempt_of_the_motd_step(self):
        self.regenerate()
        action = self.regenerate()

        self.assertEqual(action.attempts, 3)
        self.assertEqual(OperationActionAttempt.objects.filter(action=action).count(), 3)
        self.assertEqual(OperationAction.objects.filter(operation=self.operation, action="motd_update").count(), 1)

    def test_regeneration_is_audited_with_the_lengths(self):
        self.template.content = "Form up {{ formup }} now"
        self.template.save()

        self.regenerate()

        entry = AuditLog.objects.get(action="fleet.motd_regenerate")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.object_id, str(self.operation.pk))
        self.assertEqual(entry.new_value, {"old_length": 12, "new_length": 16, "status": ActionStatus.SUCCESS})
        self.assertEqual(entry.reason, "")

    def test_doctrine_snapshot_of_a_fittings_doctrine_is_refreshed(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            doctrine_source=FITTINGS_DOCTRINE_SOURCE, doctrine_external_id="7", doctrine_fits=OLD_FITS
        )
        with mock.patch("fleetops.services.motd.doctrine_fits_snapshot", return_value=NEW_FITS) as snapshot:
            self.regenerate()

        snapshot.assert_called_once_with(FITTINGS_DOCTRINE_SOURCE, "7")
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.doctrine_fits, NEW_FITS)

    def test_unreadable_doctrine_keeps_its_last_snapshot(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            doctrine_source=FITTINGS_DOCTRINE_SOURCE, doctrine_external_id="7", doctrine_fits=OLD_FITS
        )

        with mock.patch("fleetops.services.motd.doctrine_fits_snapshot", return_value=[]):
            action = self.regenerate()

        self.operation.refresh_from_db()
        self.assertEqual(action.status, ActionStatus.SUCCESS)
        self.assertEqual(self.operation.doctrine_fits, OLD_FITS)

    def test_custom_doctrine_is_not_looked_up(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(doctrine_source="custom", doctrine_fits=OLD_FITS)

        with mock.patch("fleetops.services.motd.doctrine_fits_snapshot") as snapshot:
            self.regenerate()

        snapshot.assert_not_called()
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.doctrine_fits, OLD_FITS)

    def test_esi_failure_is_stored_as_the_short_esi_text(self):
        self.esi.put_outcome = http_error(404)
        self.template.content = "New MOTD"
        self.template.save()

        with self.assertLogs(esi_provider.logger, "WARNING"):
            action = self.regenerate()

        self.operation.refresh_from_db()
        self.assertEqual(action.status, ActionStatus.FAILED)
        self.assertEqual(action.error_message, "Could not update fleet MOTD: ESI returned HTTP 404 (Not found).")
        # The new text is kept, so Retry MOTD pushes it.
        self.assertEqual(self.operation.motd_text, "New MOTD")
        entry = AuditLog.objects.get(action="fleet.motd_regenerate")
        self.assertEqual(entry.new_value["status"], ActionStatus.FAILED)
        self.assertEqual(entry.reason, action.error_message)

    def test_missing_write_scope_is_reported(self):
        f.add_token(self.fc, self.fc_char, scopes=[])
        self.token.delete()

        action = self.regenerate()

        self.assertEqual(action.status, ActionStatus.FAILED)
        self.assertIn("No valid ESI token", action.error_message)
        self.assertEqual(self.esi.put_calls, [])

    def test_template_fallback_is_recorded_and_the_motd_still_pushed(self):
        MessageTemplate.objects.filter(pk=self.template.pk).update(content="{% if %}")

        action = self.regenerate()

        self.assertEqual(action.status, ActionStatus.SUCCESS)
        render = OperationAction.objects.get(operation=self.operation, action="message_render")
        self.assertEqual(render.status, ActionStatus.FAILED)
        self.assertIn('MOTD template "Test MOTD" could not be rendered', render.error_message)
        self.assertEqual(len(self.esi.put_calls), 1)

    def test_render_failure_pushes_nothing(self):
        with mock.patch("fleetops.services.motd.render_messages", side_effect=RuntimeError("secret detail")):
            with self.assertLogs("extensions", "ERROR"):
                action = self.regenerate()

        self.operation.refresh_from_db()
        self.assertEqual(action.status, ActionStatus.FAILED)
        self.assertEqual(action.error_message, "Not sent because the fleet messages could not be rendered.")
        self.assertEqual(self.operation.motd_text, "Form up Jita")
        render = OperationAction.objects.get(operation=self.operation, action="message_render")
        self.assertEqual(render.error_message, "Message rendering failed unexpectedly (RuntimeError).")
        self.assertEqual(self.esi.put_calls, [])

    def test_attendance_only_and_manual_fleets_are_skipped(self):
        attendance_only = f.create_operation(self.fc, send_ping=False, motd_text="Old")
        manual = f.create_operation(self.fc, status=Status.CLOSED, is_manual=True, send_ping=False)
        for operation, text in (
            (attendance_only, "Attendance-only mode: MOTD update intentionally skipped."),
            (manual, "Manual fleet: MOTD update intentionally skipped."),
        ):
            with self.subTest(operation=text):
                action = self.regenerate(operation)

                self.assertEqual(action.status, ActionStatus.SKIPPED)
                self.assertEqual(action.error_message, text)
        self.assertEqual(self.esi.put_calls, [])
        self.assertFalse(AuditLog.objects.filter(action="fleet.motd_regenerate").exists())

    def test_only_an_active_fleet_is_regenerated(self):
        for status in (Status.STARTING, Status.ENDING, Status.CLOSED, Status.CANCELLED):
            with self.subTest(status=status):
                FleetOperation.objects.filter(pk=self.operation.pk).update(status=status)

                with self.assertRaisesMessage(ValueError, NOT_ACTIVE):
                    self.regenerate()
        self.assertEqual(self.esi.put_calls, [])


class RegenerateViewTests(RegenerateTestBase):
    def url(self, operation=None):
        return reverse("fleetops:regenerate_motd", args=[(operation or self.operation).uuid])

    def post_as(self, user, operation=None):
        self.client.force_login(user)
        return self.client.post(self.url(operation))

    def flash(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    def test_fc_regenerates_and_returns_to_the_fleet_page(self):
        response = self.post_as(self.fc)

        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[self.operation.uuid]), fetch_redirect_response=False
        )
        self.assertEqual(self.flash(response), ["MOTD regenerated and pushed to the fleet."])
        self.assertEqual(len(self.esi.put_calls), 1)

    def test_failed_push_shows_the_reason(self):
        self.esi.put_outcome = http_error(403)

        with self.assertLogs(esi_provider.logger, "WARNING"):
            response = self.post_as(self.fc)

        self.assertEqual(
            self.flash(response),
            ["MOTD not pushed. ESI denied permission to update the fleet MOTD: ESI returned HTTP 403 (Forbidden)."],
        )

    def test_closed_fleet_is_refused_with_a_message(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.CLOSED)

        response = self.post_as(self.fc)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.flash(response), [NOT_ACTIVE])
        self.assertEqual(self.esi.put_calls, [])

    def test_fleet_manager_may_regenerate_any_fleet(self):
        lead = f.create_user(perms=f.FC_LEAD_PERMS)

        self.post_as(lead)

        self.assertEqual(len(self.esi.put_calls), 1)
        self.assertEqual(AuditLog.objects.get(action="fleet.motd_regenerate").actor, lead)

    def test_other_users_cannot_regenerate(self):
        member = f.create_user(perms=f.MEMBER_PERMS)
        other_fc = f.create_user(perms=f.FC_PERMS)
        self.assertEqual(self.post_as(member).status_code, 403)
        self.assertEqual(self.post_as(other_fc).status_code, 404)
        self.assertEqual(self.esi.put_calls, [])

    def test_get_is_not_allowed(self):
        self.client.force_login(self.fc)

        self.assertEqual(self.client.get(self.url()).status_code, 405)


# ---------------------------------------------------------------------------
# Fleet page: Ping / MOTD and the message actions
# ---------------------------------------------------------------------------


class FleetPageMessagesTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.role = f.ping_role("Sig A")
        self.channel = f.ping_channel("Strategic Ops")

    def page(self, operation, user=None):
        self.client.force_login(user or self.fc)
        response = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_ping_shows_role_names_and_copies_the_real_mention(self):
        operation = f.create_operation(
            self.fc,
            send_ping=True,
            ping_channel=self.channel,
            ping_mention_mode=FleetOperation.MentionMode.ROLES,
            ping_roles=[{"id": str(self.role.role_id), "name": "Sig A"}],
            ping_text=f"<@&{self.role.role_id}> Form up <@&42>",
        )

        content = self.page(operation)

        self.assertIn('<pre id="ping-text" class="preview-box">@Sig A Form up @role 42</pre>', content)
        self.assertIn(f'<pre id="ping-copy" class="d-none">&lt;@&amp;{self.role.role_id}&gt; Form up &lt;@&amp;42&gt;</pre>', content)
        self.assertIn('data-id="ping-copy">Copy Ping</button>', content)
        self.assertIn("Ping channel: Strategic Ops · Mention: @Sig A", content)

    def test_plain_ping_is_copied_as_shown(self):
        operation = f.create_operation(
            self.fc, send_ping=True, ping_mention_mode=FleetOperation.MentionMode.EVERYONE, ping_text="@everyone Form up"
        )

        content = self.page(operation)

        self.assertIn('data-id="ping-text">Copy Ping</button>', content)
        self.assertNotIn('id="ping-copy"', content)
        self.assertIn("Ping channel: None (copy only) · Mention: @everyone", content)

    def test_no_mention_is_shown_as_no_ping(self):
        operation = f.create_operation(self.fc, send_ping=True, ping_channel=self.channel, ping_text="Form up")

        self.assertIn("Ping channel: Strategic Ops · Mention: No ping", self.page(operation))

    def test_retry_ping_needs_a_channel(self):
        with_channel = f.create_operation(self.fc, send_ping=True, ping_channel=self.channel)
        copy_only = f.create_operation(self.fc, send_ping=True)

        self.assertIn("Retry Ping", self.page(with_channel))
        content = self.page(copy_only)
        self.assertNotIn("Retry Ping", content)
        self.assertIn("Retry MOTD", content)

    def test_regenerate_button_on_an_active_full_fleet(self):
        operation = f.create_operation(self.fc, send_ping=True)

        content = self.page(operation)

        self.assertIn(f'action="{reverse("fleetops:regenerate_motd", args=[operation.uuid])}" id="regenerate-motd"', content)
        self.assertIn("return confirm(", content)
        self.assertIn('<button class="btn btn-sm btn-outline-primary">Regenerate &amp; Push MOTD</button>', content)

    def test_regenerate_button_is_disabled_after_a_boss_handover(self):
        operation = f.create_operation(
            self.fc, send_ping=True, missing_reason=FleetOperation.EndReason.BOSS_HANDOVER, fleet_missing_count=1
        )

        content = self.page(operation)

        self.assertIn(
            '<button class="btn btn-sm btn-outline-primary" disabled title="The fleet boss was handed over; '
            'only the boss can change the MOTD.">Regenerate &amp; Push MOTD</button>',
            content,
        )

    def test_regenerate_button_is_hidden_where_it_cannot_be_used(self):
        member = f.create_user(perms=f.MEMBER_PERMS)
        visible = f.create_operation(self.fc, send_ping=True)
        f.add_attendance(visible, member)
        cases = (
            (f.create_operation(self.fc, send_ping=False), self.fc),
            (f.create_operation(self.fc, status=Status.CLOSED, is_manual=True, send_ping=False), self.fc),
            (f.create_operation(self.fc, status=Status.CLOSED, send_ping=True), self.fc),
            (visible, member),
        )
        for operation, user in cases:
            with self.subTest(operation=operation.pk):
                self.assertNotIn("Regenerate &amp; Push MOTD", self.page(operation, user))
