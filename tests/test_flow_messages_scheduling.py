# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""End-to-end paths of the messages and scheduling release through the web pages.

ESI goes to the in-memory fleet fake and Discord to a mocked ``requests.post``; nothing leaves the
test process.
"""

import uuid
from datetime import timedelta, timezone as dt_timezone
from urllib.parse import urlsplit

from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from fleetops.models import AuditLog, CommsPreset, DiscordWebhook, FleetOperation, OperationAction, PingRole
from fleetops.services.mentions import role_choice

from . import factories as f
from .test_operations_tracking import FLEET_ID, FakeESIMixin
from .test_public_urls import build_urlconf

UTC = dt_timezone.utc
Status = FleetOperation.Status
ActionStatus = OperationAction.Status
ROLE_ID = 876543210987654321
WEBHOOK_URL = "https://discord.com/api/webhooks/4242/flow-secret-token"


def flashed(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def steps(operation):
    return dict(OperationAction.objects.filter(operation=operation).values_list("action", "status"))


def in_two_hours():
    return (timezone.now() + timedelta(hours=2)).astimezone(UTC).replace(second=0, microsecond=0)


class FlowTestBase(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        f.builtin_default_templates()
        f.settings(srp_auto_create=False)
        self.admin = f.create_user(perms=f.FC_LEAD_PERMS, main_name="Fleet Lead")
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Plan FC")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.fleet_type = f.fleet_type("StratOp", "1.00")
        self.comms = CommsPreset.objects.create(name="Main Comms", voice_url="mumble://voice.example.com/Ops")

    def discord_payloads(self):
        return [call.kwargs["json"] for call in self.discord_post.call_args_list]

    def start_data(self, character, **extra):
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fleet_timing": "now",
            "fc_character_id": str(character.character_id),
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Jita",
            "comms": str(self.comms.pk),
        }
        data.update(extra)
        return data


class PrePingToClosedFleetTests(FlowTestBase):
    def configure_channel_and_role(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("fleetops:configuration_add", args=["ping-roles"]),
            {"name": "@Sig A", "role_id": f"<@&{ROLE_ID}>", "is_active": "on", "sort_order": "1"},
        )
        self.assertRedirects(response, reverse("fleetops:configuration_list", args=["ping-roles"]))
        role = PingRole.objects.get()
        response = self.client.post(
            reverse("fleetops:configuration_add", args=["ping-channels"]),
            {
                "name": "STR Pings",
                "webhook_url": WEBHOOK_URL,
                "is_active": "on",
                "sort_order": "1",
                "allowed_roles": [str(role.pk)],
            },
        )
        self.assertRedirects(response, reverse("fleetops:configuration_list", args=["ping-channels"]))
        return role, DiscordWebhook.objects.get()

    def schedule(self, channel, role, when, **extra):
        self.client.force_login(self.fc)
        data = self.start_data(
            self.fc_char,
            fleet_timing="pre_ping",
            fleet_date=when.strftime("%Y-%m-%d"),
            fleet_clock=when.strftime("%H:%M"),
            ping_channel=str(channel.pk),
            ping_mentions=[role_choice(role)],
            **extra,
        )
        response = self.client.post(reverse("fleetops:start_fleet"), data)
        operation = FleetOperation.objects.get(start_request_id=data["request_id"])
        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False
        )
        return operation

    def test_pre_ping_schedule_start_regenerate_and_end(self):
        role, channel = self.configure_channel_and_role()
        self.assertEqual((role.name, role.role_id), ("Sig A", ROLE_ID))
        when = in_two_hours()

        # Pre ping: no ESI call, a scheduled record and a Discord post that notifies only the role.
        operation = self.schedule(channel, role, when, additional_message="Bring @everyone and @here")
        self.assertEqual(self.esi.calls, [])
        self.assertEqual(operation.status, Status.SCHEDULED)
        self.assertEqual(operation.scheduled_at, when)
        self.assertEqual(operation.ping_roles, [{"id": str(ROLE_ID), "name": "Sig A"}])
        self.assertEqual(steps(operation), {"pre_ping": ActionStatus.SUCCESS})
        [pre_ping] = self.discord_payloads()
        self.assertEqual(pre_ping["allowed_mentions"], {"parse": [], "roles": [str(ROLE_ID)]})
        self.assertEqual(pre_ping["flags"], 4)
        self.assertEqual(self.discord_post.call_args.args[0], WEBHOOK_URL)
        content = pre_ping["content"]
        self.assertTrue(content.startswith(f"<@&{ROLE_ID}>\n"))
        self.assertIn("Bring @​everyone and @​here", content)
        unix = int(when.timestamp())
        self.assertIn(f"**Fleet Time:** {when:%Y-%m-%d %H:%M} EVE · <t:{unix}:t> (<t:{unix}:R>)", content)
        self.assertIn("-# This was a FleetOps broadcast from Plan FC to Sig A at ", content)
        self.assertEqual(AuditLog.objects.filter(action="fleet.schedule").count(), 1)

        # The scheduled page shows the start card to its FC only.
        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        self.assertTemplateUsed(page, "fleetops/scheduled_fleet.html")
        self.assertContains(page, reverse("fleetops:start_scheduled_fleet", args=[operation.uuid]))

        other_fc = f.create_user(perms=f.FC_PERMS, main_name="Other FC")
        other_char = f.main_of(other_fc)
        f.add_token(other_fc, other_char)
        self.esi.put_in_fleet(other_char)
        self.client.force_login(other_fc)
        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, reverse("fleetops:start_scheduled_fleet", args=[operation.uuid]))
        response = self.client.post(
            reverse("fleetops:start_scheduled_fleet", args=[operation.uuid]),
            {"fc_character_id": str(other_char.character_id), "send_ping": "on"},
        )
        self.assertIn(response.status_code, (403, 404))
        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.SCHEDULED)
        self.assertEqual(self.esi.calls, [])

        # A fleet manager cannot start it either, but can cancel a scheduled fleet with a quiet notice.
        self.client.force_login(self.admin)
        response = self.client.post(reverse("fleetops:start_scheduled_fleet", args=[operation.uuid]), {})
        self.assertIn(response.status_code, (403, 404))
        spare = self.schedule(channel, role, when + timedelta(hours=1))
        self.discord_post.reset_mock()
        self.client.force_login(self.admin)
        response = self.client.post(reverse("fleetops:cancel_scheduled_fleet", args=[spare.uuid]), {"notify": "on"})
        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[spare.uuid]), fetch_redirect_response=False
        )
        spare.refresh_from_db()
        self.assertEqual(spare.status, Status.CANCELLED)
        [notice] = self.discord_payloads()
        self.assertEqual(notice["allowed_mentions"], {"parse": []})
        self.assertNotIn(str(ROLE_ID), notice["content"])
        self.assertTrue(notice["content"].startswith("**Cancelled:** StratOp at "))
        self.assertIn("-# FleetOps notice from Fleet Lead at ", notice["content"])
        self.assertEqual(AuditLog.objects.get(action="fleet.schedule_cancel").actor, self.admin)

        # The scheduling FC starts the fleet with a form-up ping; ESI is read fresh for the boss check.
        self.esi.put_in_fleet(self.fc_char)
        self.esi.members[FLEET_ID] = [f.esi_member(self.fc_char, role="fleet_commander", wing_id=-1, squad_id=-1)]
        self.discord_post.reset_mock()
        self.client.force_login(self.fc)
        response = self.client.post(
            reverse("fleetops:start_scheduled_fleet", args=[operation.uuid]),
            {"fc_character_id": str(self.fc_char.character_id), "send_ping": "on"},
        )
        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False
        )
        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertEqual(operation.esi_fleet_id, FLEET_ID)
        self.assertIsNotNone(operation.started_at)
        self.assertTrue(operation.tracking_enabled)
        [form_up] = self.discord_payloads()
        self.assertEqual(form_up["allowed_mentions"], {"parse": [], "roles": [str(ROLE_ID)]})
        self.assertEqual(form_up["content"], operation.ping_text)
        # A form-up ping is not a pre ping, so it has no fleet time line.
        self.assertNotIn("**Fleet Time:**", form_up["content"])
        self.assertEqual(steps(operation)["discord_ping"], ActionStatus.SUCCESS)
        self.assertEqual(steps(operation)["motd_update"], ActionStatus.SUCCESS)
        self.assertEqual(len(self.esi.put_calls), 1)
        self.assertIn('<a href="http://localhost:8000/fleetops/comms/', self.esi.put_calls[0]["body"]["motd"])
        start = AuditLog.objects.get(action="fleet.start")
        self.assertTrue(start.new_value["from_schedule"])
        self.assertEqual(start.object_id, str(operation.pk))

        # The fleet page shows the ping as people read it and offers Regenerate MOTD.
        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        self.assertTemplateUsed(page, "fleetops/operation_detail.html")
        self.assertContains(page, "@Sig A")
        self.assertContains(page, reverse("fleetops:regenerate_motd", args=[operation.uuid]))

        # Regenerate MOTD after the comms preset was renamed.
        self.comms.name = "Backup Comms"
        self.comms.save()
        response = self.client.post(reverse("fleetops:regenerate_motd", args=[operation.uuid]))
        self.assertIn("MOTD regenerated and pushed to the fleet.", flashed(response))
        self.assertEqual(len(self.esi.put_calls), 2)
        self.assertIn(">Backup Comms</a>", self.esi.put_calls[1]["body"]["motd"])
        operation.refresh_from_db()
        self.assertIn("Backup Comms", operation.motd_text)
        regenerate = AuditLog.objects.get(action="fleet.motd_regenerate")
        self.assertEqual((regenerate.actor, regenerate.new_value["status"]), (self.fc, ActionStatus.SUCCESS))

        # End the fleet; the background end runs at once in tests.
        response = self.client.post(reverse("fleetops:end_fleet", args=[operation.uuid]), {"attendance_multiplier": "1"})
        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False
        )
        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.CLOSED)
        self.assertEqual(operation.end_reason, FleetOperation.EndReason.MANUAL)
        self.assertEqual(AuditLog.objects.filter(action="fleet.start").count(), 1)
        # Discord was only ever called with the configured webhook.
        self.assertEqual({call.args[0] for call in self.discord_post.call_args_list}, {WEBHOOK_URL})

    def test_ping_over_the_discord_limit_is_refused_before_anything_is_posted(self):
        role, channel = self.configure_channel_and_role()
        when = in_two_hours()
        self.client.force_login(self.fc)
        data = self.start_data(
            self.fc_char,
            fleet_timing="pre_ping",
            fleet_date=when.strftime("%Y-%m-%d"),
            fleet_clock=when.strftime("%H:%M"),
            ping_channel=str(channel.pk),
            ping_mentions=[role_choice(role)],
            additional_message="x" * 2100,
        )

        response = self.client.post(reverse("fleetops:start_fleet"), data)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Discord accepts at most 2000", " ".join(response.context["form"].non_field_errors()))
        self.assertFalse(FleetOperation.objects.exists())
        self.discord_post.assert_not_called()


class BossHandoverAtStartFlowTests(FlowTestBase):
    def test_new_boss_start_ends_the_old_record_at_once(self):
        old_fc = f.create_user(perms=f.FC_PERMS, main_name="Old Boss")
        old_char = f.main_of(old_fc)
        f.add_token(old_fc, old_char)
        self.esi.put_in_fleet(old_char)
        self.esi.members[FLEET_ID] = [
            f.esi_member(old_char, role="fleet_commander", wing_id=-1, squad_id=-1),
            f.esi_member(self.fc_char),
        ]
        self.client.force_login(old_fc)
        self.client.post(reverse("fleetops:start_fleet"), self.start_data(old_char))
        old = FleetOperation.objects.get()
        self.assertEqual((old.status, old.fc_character_id), (Status.ACTIVE, old_char.character_id))

        # The old boss hands the boss role over in game; the new boss starts a fleet in FleetOps.
        self.esi.put_in_fleet(self.fc_char)
        self.esi.put_in_fleet(old_char, role="squad_member", boss=self.fc_char)
        self.esi.members[FLEET_ID] = [
            f.esi_member(self.fc_char, role="fleet_commander", wing_id=-1, squad_id=-1),
            f.esi_member(old_char),
        ]
        self.client.force_login(self.fc)
        response = self.client.post(reverse("fleetops:start_fleet"), self.start_data(self.fc_char))

        new = FleetOperation.objects.exclude(pk=old.pk).get()
        self.assertRedirects(response, reverse("fleetops:operation_detail", args=[new.uuid]), fetch_redirect_response=False)
        self.assertIn("The fleet boss was handed over to you, so the previous operation (FC Old Boss) was ended.", " ".join(flashed(response)))
        self.assertEqual((new.status, new.fc_user, new.esi_fleet_id), (Status.ACTIVE, self.fc, FLEET_ID))
        old.refresh_from_db()
        self.assertEqual(old.status, Status.CLOSED)
        self.assertEqual(old.end_reason, FleetOperation.EndReason.BOSS_HANDOVER)
        self.assertEqual(old.end_detail, "Plan FC")
        self.assertEqual(old.fleet_boss_character_id, self.fc_char.character_id)
        self.assertEqual(old.ended_at, new.started_at)
        end = AuditLog.objects.get(action="fleet.auto_end", object_id=str(old.pk))
        self.assertEqual(end.new_value["fleet_boss_character_id"], self.fc_char.character_id)
        self.assertEqual(AuditLog.objects.filter(action="fleet.start").count(), 2)
        handover = AuditLog.objects.get(action="fleet.boss_handover")
        self.assertEqual(handover.new_value["new_operation_id"], new.pk)


class CommsJoinFlowTests(FlowTestBase):
    def test_join_link_in_the_ping_opens_for_logged_out_members(self):
        channel = f.ping_channel("STR Pings", allow_everyone=True)
        self.esi.put_in_fleet(self.fc_char)
        self.esi.members[FLEET_ID] = [f.esi_member(self.fc_char, role="fleet_commander", wing_id=-1, squad_id=-1)]
        self.client.force_login(self.fc)
        self.client.post(
            reverse("fleetops:start_fleet"),
            self.start_data(self.fc_char, ping_channel=str(channel.pk), ping_mentions=["everyone"]),
        )
        operation = FleetOperation.objects.get()
        join_url = f"http://localhost:8000{reverse('fleetops:comms_join', args=[self.comms.join_token])}"
        self.assertIn(f"[Main Comms]({join_url})", operation.ping_text)
        self.assertNotIn("mumble://", operation.ping_text)
        self.client.logout()

        with override_settings(APPS_WITH_PUBLIC_VIEWS=["fleetops"]):
            urlconf = build_urlconf()
            with override_settings(ROOT_URLCONF=urlconf):
                response = self.client.get(urlsplit(join_url).path)
                dashboard = self.client.get(reverse("fleetops:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Joining Main Comms…")
        self.assertContains(response, 'href="mumble://voice.example.com/Ops"')
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        # Only the join page is public.
        self.assertEqual(dashboard.status_code, 302)
