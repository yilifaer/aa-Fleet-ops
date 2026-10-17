# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Send Ping: pings without a fleet for FCs and group leaders, their checks, records, audit entries and page."""

import json
import re
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings, tag
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation

from fleetops import texts
from fleetops.forms_groups import SendPingForm
from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    FleetOperation,
    GroupAccess,
    MessageTemplate,
    StandalonePing,
)
from fleetops.services import mentions, standalone_pings
from fleetops.views_errors import PING_SENDER_REASON

from . import factories as f

PING = GroupAccess.Mode.PING
FLEETS = GroupAccess.Mode.FLEETS
POST = "fleetops.providers.pings.requests.post"
SCRIPT = Path(__file__).resolve().parent.parent / "fleetops" / "static" / "fleetops" / "send_ping.js"


def fresh(user):
    """The user as the next request loads it: no cached permissions, no cached leadership."""
    return get_user_model().objects.get(pk=user.pk)


def discord(status=204, text=""):
    return mock.Mock(status_code=status, text=text)


class PingFixture(TestCase):
    """A ping group and a fleet group with their reserved channels, an alliance channel and the people."""

    def setUp(self):
        super().setUp()
        self.member = f.create_user("member", perms=f.MEMBER_PERMS)
        self.leader = f.create_user("leader", perms=f.MEMBER_PERMS, main_name="Pilot A")
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Example FC")
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS, main_name="Example Lead")
        self.fc_leader = f.create_user("fcleader", perms=f.FC_PERMS, main_name="Example FC Leader")
        self.activity = f.fleet_type("Group Activity", "0.00")
        self.ping_group = f.aa_group("Example Ping Group", leaders=[self.leader, self.fc_leader])
        self.fleet_group = f.aa_group("Example Fleet Group")
        self.ping_access = f.group_access(self.ping_group, PING)
        self.fleet_access = f.group_access(self.fleet_group, FLEETS, types=[self.activity])
        self.role = f.ping_role("Sig A")
        self.other_role = f.ping_role("Sig B")
        self.alliance = f.ping_channel("Alliance Pings", allow_here=True)
        self.group_channel = f.group_channel("Group Pings", groups=[self.ping_group], allow_here=True, roles=[self.role])
        self.fleet_channel = f.group_channel("Fleet Group Pings", groups=[self.fleet_group])

    def data(self, channel, *, group=None, mentions_=("none",), message="Form up for mining", **extra):
        values = {
            "request_id": str(uuid.uuid4()),
            "ping_channel": str(channel.pk),
            "ping_mentions": list(mentions_),
            "additional_message": message,
        }
        if group is not None:
            values["group"] = str(group.pk)
        values.update(extra)
        return values

    def send(self, user, data, **extra):
        self.client.force_login(user)
        return self.client.post(reverse("fleetops:send_ping"), data, **extra)

    def preview(self, user, data, **extra):
        self.client.force_login(user)
        return self.client.post(reverse("fleetops:preview_ping"), data, **extra)

    def page(self, user, query=""):
        self.client.force_login(user)
        return self.client.get(reverse("fleetops:send_ping") + query)


class AccessTests(PingFixture):
    """Who may open the page, preview and send."""

    def test_members_and_leaders_of_an_inactive_row_are_refused(self):
        response = self.page(self.member)
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, str(PING_SENDER_REASON), status_code=403)
        self.assertEqual(self.preview(self.member, {}).status_code, 403)
        with mock.patch(POST) as post:
            self.assertEqual(self.send(self.member, self.data(self.alliance)).status_code, 403)
        GroupAccess.objects.update(is_active=False)
        self.assertEqual(self.page(self.leader).status_code, 403)
        with mock.patch(POST) as post:
            self.assertEqual(self.send(self.leader, self.data(self.group_channel, group=self.ping_group)).status_code, 403)
        post.assert_not_called()
        self.assertFalse(StandalonePing.objects.exists())

    def test_fcs_leaders_and_fc_leads_open_the_page(self):
        for user in (self.leader, self.fc, self.lead, self.fc_leader):
            with self.subTest(user=user.username):
                response = self.page(user)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'id="send-ping-form"')


class FormChoiceTests(PingFixture):
    """The form offers only the channels and groups the user may use."""

    def channels(self, user):
        return set(SendPingForm(user=fresh(user)).fields["ping_channel"].queryset)

    def test_a_leader_sees_only_the_channels_of_their_groups(self):
        self.assertEqual(self.channels(self.leader), {self.group_channel})

    def test_an_fc_sees_the_alliance_channels(self):
        open_group_channel = f.group_channel("Open Group Pings", groups=[self.fleet_group], fcs_allowed=True)
        self.assertEqual(self.channels(self.fc), {self.alliance, open_group_channel})

    def test_an_fc_who_leads_a_group_sees_both(self):
        self.assertEqual(self.channels(self.fc_leader), {self.alliance, self.group_channel})

    def test_an_fc_lead_sees_every_active_channel(self):
        f.ping_channel("Switched Off", is_active=False)
        self.assertEqual(self.channels(self.lead), {self.alliance, self.group_channel, self.fleet_channel})

    def test_the_group_choice_per_role(self):
        fc_form = SendPingForm(user=fresh(self.fc))
        self.assertNotIn("group", fc_form.fields)
        leader_form = SendPingForm(user=fresh(self.leader))
        self.assertEqual(leader_form.fixed_group.group_id, self.ping_group.pk)
        self.assertTrue(leader_form.fields["group"].widget.is_hidden)
        both = SendPingForm(user=fresh(self.fc_leader))
        self.assertIsNone(both.fixed_group)
        self.assertEqual(both.fields["group"].widget.input_type, "radio")
        labels = [str(label) for _value, label in both.fields["group"].choices]
        self.assertEqual(labels, ["Alliance ping", "Example Ping Group"])
        self.assertEqual(both.group_rules(), {"": [self.alliance.pk], str(self.ping_group.pk): [self.group_channel.pk]})

    def test_a_leader_without_a_channel_is_told_who_assigns_one(self):
        self.group_channel.allowed_groups.clear()
        response = self.page(self.leader)
        self.assertContains(response, 'id="send-ping-no-channel"')
        self.assertNotContains(self.page(self.fc_leader), 'id="send-ping-no-channel"')

    def test_more_than_four_choices_become_a_list(self):
        for number in range(4):
            group = f.aa_group(f"Example Group {number}", leaders=[self.fc_leader])
            f.group_access(group, PING)
        form = SendPingForm(user=fresh(self.fc_leader))
        self.assertEqual(form.fields["group"].widget.input_type, "select")

    def test_the_page_shows_the_fixed_group_pill_to_a_leader_of_one_group(self):
        html = self.page(self.leader).content.decode()
        self.assertIn('id="send-ping-group-pill"', html)
        self.assertIn("fo-pill-group", html)
        self.assertIn(f'value="{self.ping_group.pk}"', html)
        form = html.split('id="send-ping-form"', 1)[1].split("</form>", 1)[0]
        self.assertEqual(form.count("btn-primary"), 1)


class RefusalTests(PingFixture):
    """Crafted forms and channels that may not be used never reach Discord."""

    def assertRefused(self, response, post):
        self.assertEqual(response.status_code, 200)
        post.assert_not_called()
        self.assertFalse(StandalonePing.objects.exists())

    def test_a_leader_cannot_post_to_an_alliance_channel(self):
        with mock.patch(POST) as post:
            response = self.send(self.leader, self.data(self.alliance, group=self.ping_group))
        self.assertRefused(response, post)
        self.assertIn("ping_channel", response.context["form"].errors)

    def test_a_leader_cannot_post_an_alliance_ping(self):
        with mock.patch(POST) as post:
            data = self.data(self.group_channel)
            response = self.send(self.leader, data)
        self.assertRefused(response, post)

    def test_a_leader_cannot_post_to_the_channel_of_another_group(self):
        with mock.patch(POST) as post:
            response = self.send(self.leader, self.data(self.fleet_channel, group=self.fleet_group))
        self.assertRefused(response, post)

    def test_a_leader_of_two_groups_uses_each_channel_for_its_own_group(self):
        self.fleet_group.authgroup.group_leaders.add(self.leader.pk)
        with mock.patch(POST) as post:
            response = self.send(self.leader, self.data(self.fleet_channel, group=self.ping_group))
        self.assertRefused(response, post)
        with mock.patch(POST, return_value=discord()) as post:
            response = self.send(self.leader, self.data(self.fleet_channel, group=self.fleet_group))
        self.assertEqual(response.status_code, 302)
        post.assert_called_once()
        self.assertEqual(StandalonePing.objects.get().group, self.fleet_group)

    def test_an_fc_cannot_post_to_a_reserved_group_channel(self):
        with mock.patch(POST) as post:
            response = self.send(self.fc, self.data(self.group_channel))
        self.assertRefused(response, post)
        self.assertIn("ping_channel", response.context["form"].errors)
        # An FC who leads the group is offered the channel, but only for the group's pings.
        with mock.patch(POST) as post:
            response = self.send(self.fc_leader, self.data(self.group_channel))
        self.assertRefused(response, post)
        self.assertEqual(
            response.context["form"].errors["ping_channel"],
            [texts.translate_stored(texts.CHANNEL_NOT_ALLOWED(name="Group Pings"))],
        )

    def test_an_fc_lead_posts_to_a_reserved_channel(self):
        with mock.patch(POST, return_value=discord()) as post:
            response = self.send(self.lead, self.data(self.fleet_channel))
        self.assertEqual(response.status_code, 302)
        post.assert_called_once()

    def test_the_channel_mention_rules_apply(self):
        with mock.patch(POST) as post:
            response = self.send(self.leader, self.data(self.group_channel, group=self.ping_group, mentions_=("everyone",)))
        self.assertRefused(response, post)
        self.assertIn("ping_mentions", response.context["form"].errors)
        with mock.patch(POST) as post:
            role = mentions.role_choice(self.other_role)
            response = self.send(self.leader, self.data(self.group_channel, group=self.ping_group, mentions_=(role,)))
        self.assertRefused(response, post)

    def test_a_past_fleet_time_and_an_empty_message_are_refused(self):
        past = timezone.now() - timedelta(hours=1)
        data = self.data(
            self.group_channel, group=self.ping_group, message="  ",
            fleet_date=past.strftime("%Y-%m-%d"), fleet_clock=past.strftime("%H:%M"),
        )
        with mock.patch(POST) as post:
            response = self.send(self.leader, data)
        self.assertRefused(response, post)
        self.assertIn("additional_message", response.context["form"].errors)
        self.assertIn("fleet_clock", response.context["form"].errors)
        self.assertContains(response, 'id="send-ping-more" open')


class ServiceRecheckTests(PingFixture):
    """The service checks the channel again under its lock, after the form was checked."""

    def cleaned(self, user, data):
        form = SendPingForm(data, user=fresh(user))
        self.assertTrue(form.is_valid(), form.errors)
        return form.cleaned_data

    def test_a_channel_unassigned_after_the_form_check_is_refused(self):
        cleaned = self.cleaned(self.leader, self.data(self.group_channel, group=self.ping_group))
        self.group_channel.allowed_groups.clear()
        with mock.patch(POST) as post, self.assertRaises(standalone_pings.PingRefused) as caught:
            standalone_pings.send_standalone_ping(user=fresh(self.leader), cleaned_data=cleaned)
        post.assert_not_called()
        self.assertEqual(
            str(caught.exception), texts.GROUP_CHANNEL_NOT_ASSIGNED(name="Group Pings", group="Example Ping Group")
        )

    def test_a_channel_switched_off_or_deleted_after_the_form_check_is_refused(self):
        cleaned = self.cleaned(self.fc, self.data(self.alliance))
        DiscordWebhookModel = type(self.alliance)
        DiscordWebhookModel.objects.filter(pk=self.alliance.pk).update(is_active=False)
        with mock.patch(POST) as post, self.assertRaises(standalone_pings.PingRefused) as caught:
            standalone_pings.send_standalone_ping(user=fresh(self.fc), cleaned_data=cleaned)
        self.assertEqual(str(caught.exception), texts.PING_CHANNEL_DISABLED(name="Alliance Pings"))
        self.alliance.delete()
        with self.assertRaises(standalone_pings.PingRefused) as caught:
            standalone_pings.send_standalone_ping(user=fresh(self.fc), cleaned_data=cleaned)
        self.assertEqual(str(caught.exception), texts.PING_CHANNEL_GONE(name="Alliance Pings"))
        post.assert_not_called()

    def test_a_leader_who_lost_the_group_is_refused(self):
        cleaned = self.cleaned(self.leader, self.data(self.group_channel, group=self.ping_group))
        self.ping_group.authgroup.group_leaders.remove(self.leader.pk)
        with mock.patch(POST) as post, self.assertRaises(standalone_pings.PingRefused):
            standalone_pings.send_standalone_ping(user=fresh(self.leader), cleaned_data=cleaned)
        post.assert_not_called()

    def test_the_form_id_of_another_sender_is_refused(self):
        data = self.data(self.alliance)
        with mock.patch(POST, return_value=discord()):
            self.send(self.fc, data)
        cleaned = self.cleaned(self.lead, data)
        with mock.patch(POST) as post, self.assertRaises(standalone_pings.PingRefused) as caught:
            standalone_pings.send_standalone_ping(user=fresh(self.lead), cleaned_data=cleaned)
        post.assert_not_called()
        self.assertEqual(str(caught.exception), texts.PING_REQUEST_USED())


class SendTests(PingFixture):
    """What is posted, stored and audited."""

    def test_the_payload_notifies_exactly_the_selection(self):
        role = mentions.role_choice(self.role)
        cases = (("none", ("none",)), ("here", ("here",)), ("roles", (role,)))
        for label, choice in cases:
            with self.subTest(target=label):
                StandalonePing.objects.all().delete()
                with mock.patch(POST, return_value=discord()) as post:
                    self.send(self.leader, self.data(self.group_channel, group=self.ping_group, mentions_=choice))
                payload = post.call_args.kwargs["json"]
                ping = StandalonePing.objects.get()
                self.assertEqual(payload["allowed_mentions"], mentions.allowed_mentions(standalone_pings.selection_of(ping)))
                self.assertEqual(payload["content"], ping.text)

    def test_a_stray_mass_mention_in_the_message_is_neutralised(self):
        with mock.patch(POST, return_value=discord()) as post:
            self.send(self.leader, self.data(self.group_channel, group=self.ping_group, mentions_=("here",),
                                             message="Everyone @everyone and @here"))
        content = post.call_args.kwargs["json"]["content"]
        self.assertTrue(content.startswith("@here\n"))
        self.assertNotIn("@everyone", content)
        self.assertIn("@​everyone", content)
        self.assertEqual(content.count("@here"), 1)

    def test_the_row_the_audit_entry_and_nothing_that_counts(self):
        role = mentions.role_choice(self.role)
        when = timezone.now() + timedelta(hours=2)
        data = self.data(
            self.group_channel, group=self.ping_group, mentions_=(role,), formup="Jita",
            fleet_date=when.strftime("%Y-%m-%d"), fleet_clock=when.strftime("%H:%M"),
        )
        with mock.patch(POST, return_value=discord()):
            response = self.send(self.leader, data)
        self.assertRedirects(response, reverse("fleetops:send_ping"), fetch_redirect_response=False)
        ping = StandalonePing.objects.get()
        self.assertEqual(ping.status, StandalonePing.Status.SENT)
        self.assertEqual((ping.sender, ping.sender_name), (self.leader, "Pilot A"))
        self.assertEqual((ping.group, ping.group_name), (self.ping_group, "Example Ping Group"))
        self.assertEqual((ping.channel, ping.channel_name), (self.group_channel, "Group Pings"))
        self.assertEqual(ping.mention_mode, "roles")
        self.assertEqual(ping.mention_roles, [{"id": str(self.role.role_id), "name": "Sig A"}])
        self.assertEqual(ping.formup, "Jita")
        self.assertEqual(ping.fleet_time, when.replace(second=0, microsecond=0))
        self.assertEqual(ping.http_status, 204)
        self.assertIsNotNone(ping.sent_at)
        self.assertIn(f"<@&{self.role.role_id}>", ping.text)
        self.assertIn("@Sig A", ping.display_text)
        self.assertIn("**Group:** Example Ping Group", ping.text)
        self.assertIn("-# FleetOps ping from Pilot A to Sig A at ", ping.text)
        self.assertEqual(str(ping.request_id), data["request_id"])
        self.assertFalse(FleetOperation.objects.exists())
        self.assertFalse(AttendanceRecord.objects.exists())
        entry = AuditLog.objects.get(action="ping.send")
        self.assertEqual(entry.actor, self.leader)
        self.assertEqual((entry.object_type, entry.object_id), ("StandalonePing", str(ping.pk)))
        self.assertEqual(
            set(entry.new_value),
            {"group_id", "group", "channel_id", "channel", "mention_mode", "roles", "fleet_time", "template_id",
             "status", "http_status"},
        )
        self.assertEqual(entry.new_value["roles"], ["Sig A"])
        self.assertEqual(entry.new_value["status"], "sent")

    def test_the_webhook_address_is_never_stored(self):
        token = "factory-token-secret"
        self.group_channel.webhook_url = f"https://discord.com/api/webhooks/123/{token}"
        self.group_channel.save()
        answer = discord(500, f'{{"message": "boom at {self.group_channel.webhook_url}"}}')
        with mock.patch(POST, return_value=answer):
            self.send(self.leader, self.data(self.group_channel, group=self.ping_group))
        ping = StandalonePing.objects.get()
        self.assertEqual(ping.status, StandalonePing.Status.FAILED)
        self.assertEqual(ping.http_status, 500)
        stored = json.dumps([list(StandalonePing.objects.values().get().values()) for _ in (1,)], default=str)
        audit_entry = AuditLog.objects.get(action="ping.send")
        recorded = json.dumps([audit_entry.old_value, audit_entry.new_value, audit_entry.reason])
        for text in (stored, recorded):
            self.assertNotIn(token, text)
            self.assertNotIn("discord.com/api/webhooks", text)
        self.assertIn("***", ping.result_message)

    def test_a_discord_failure_names_the_cause_and_the_next_step(self):
        maybe_posted = "The ping may not have been posted; check the channel before sending it again."
        refused = "Discord refused the ping. Check the channel's webhook in FleetOps Administration."
        cases = ((500, "Discord returned HTTP 500.", maybe_posted), (400, "Discord returned HTTP 400.", refused))
        for status, cause, hint in cases:
            with self.subTest(status=status):
                StandalonePing.objects.all().delete()
                with mock.patch(POST, return_value=discord(status)):
                    response = self.send(self.leader, self.data(self.group_channel, group=self.ping_group), follow=True)
                ping = StandalonePing.objects.get()
                self.assertEqual(ping.status, StandalonePing.Status.FAILED)
                self.assertEqual(ping.result_message, cause)
                self.assertEqual(response.redirect_chain[-1][0], f"{reverse('fleetops:send_ping')}?again={ping.uuid}")
                shown = [str(message) for message in response.context["messages"]]
                self.assertEqual(shown, [f"{cause} {hint}"])
                # The form opens again with the ping, under a new form id.
                form = response.context["form"]
                self.assertEqual(form.initial["additional_message"], "Form up for mining")
                self.assertNotEqual(str(form.initial["request_id"]), str(ping.request_id))
                self.assertContains(response, "alert-danger")

    def test_a_request_that_never_reached_discord(self):
        import requests

        with mock.patch(POST, side_effect=requests.ConnectionError("https://discord.com/api/webhooks/1/secret")):
            response = self.send(self.leader, self.data(self.group_channel, group=self.ping_group), follow=True)
        ping = StandalonePing.objects.get()
        self.assertEqual(ping.result_message, texts.PING_REQUEST_FAILED(error_type="ConnectionError"))
        self.assertIsNone(ping.http_status)
        shown = [str(message) for message in response.context["messages"]]
        self.assertEqual(
            shown,
            ["Discord webhook request failed (ConnectionError). The ping may not have been posted; check the channel "
             "before sending it again."],
        )

    def test_a_webhook_that_is_not_discords_posts_nothing(self):
        type(self.group_channel).objects.filter(pk=self.group_channel.pk).update(webhook_url="https://example.com/hook")
        with mock.patch(POST) as post:
            response = self.send(self.leader, self.data(self.group_channel, group=self.ping_group), follow=True)
        post.assert_not_called()
        shown = [str(message) for message in response.context["messages"]]
        self.assertEqual(
            shown,
            [f"{texts.PING_NOT_A_WEBHOOK()} Nothing was posted. Check the channel's webhook in FleetOps Administration."],
        )

    def test_the_same_form_sent_twice_posts_once(self):
        data = self.data(self.group_channel, group=self.ping_group)
        with mock.patch(POST, return_value=discord()) as post:
            first = self.send(self.leader, data)
            second = self.send(self.leader, data)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(StandalonePing.objects.count(), 1)
        self.assertEqual(AuditLog.objects.filter(action="ping.send").count(), 1)
        self.assertEqual(first.status_code, second.status_code, 302)

    def test_a_second_ping_within_the_cooldown_is_refused(self):
        with mock.patch(POST, return_value=discord()) as post:
            self.send(self.leader, self.data(self.group_channel, group=self.ping_group), follow=True)
            response = self.send(self.fc_leader, self.data(self.group_channel, group=self.ping_group))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(response.status_code, 200)
        shown = [str(message) for message in response.context["messages"]]
        self.assertEqual(len(shown), 1)
        self.assertRegex(shown[0], r"^Group Pings received a ping \d+ seconds ago\. Wait \d+ seconds\.$")
        # A failed ping does not hold the channel; another channel is not held either.
        StandalonePing.objects.update(status=StandalonePing.Status.FAILED)
        with mock.patch(POST, return_value=discord()) as post:
            self.send(self.fc_leader, self.data(self.group_channel, group=self.ping_group))
            self.send(self.fc_leader, self.data(self.alliance))
        self.assertEqual(post.call_count, 2)

    def test_the_cooldown_ends_after_a_minute(self):
        with mock.patch(POST, return_value=discord()):
            self.send(self.leader, self.data(self.group_channel, group=self.ping_group))
        StandalonePing.objects.update(created_at=timezone.now() - timedelta(seconds=61))
        with mock.patch(POST, return_value=discord()) as post:
            self.send(self.leader, self.data(self.group_channel, group=self.ping_group))
        post.assert_called_once()

    def test_a_ping_template_of_its_own(self):
        template = MessageTemplate.objects.create(
            name="Short", template_type=MessageTemplate.TemplateType.STANDALONE,
            content="{{ ping_mention }} {{ additional_message }} ({{ group }})",
        )
        with mock.patch(POST, return_value=discord()) as post:
            self.send(self.leader, self.data(self.group_channel, group=self.ping_group, ping_template=str(template.pk)))
        self.assertEqual(post.call_args.kwargs["json"]["content"], "Form up for mining (Example Ping Group)")
        self.assertEqual(StandalonePing.objects.get().template, template)


class LanguageTests(PingFixture):
    """Pings are English whatever the site and the sender's language; the warnings follow the sender (5-10)."""

    def run_in(self, site, viewer):
        StandalonePing.objects.all().delete()
        frozen = timezone.now().replace(second=0, microsecond=0) + timedelta(seconds=30)
        data = self.data(self.group_channel, group=self.ping_group, mentions_=(mentions.role_choice(self.role),))
        with override_settings(LANGUAGE_CODE=site), mock.patch("django.utils.timezone.now", return_value=frozen):
            self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = viewer
            preview = self.preview(self.leader, data).json()
            with mock.patch(POST, return_value=discord()) as post:
                self.send(self.leader, data)
        ping = StandalonePing.objects.get()
        return preview["ping"], post.call_args.kwargs["json"]["content"], ping.text

    def test_a_ping_is_the_same_in_every_language(self):
        english = self.run_in("en-us", "en")
        self.assertIn(" to Sig A at ", english[1])
        for site, viewer in (("zh-hans", "zh-hans"), ("en-us", "zh-hans"), ("zh-hans", "en")):
            with self.subTest(site=site, viewer=viewer):
                self.assertEqual(self.run_in(site, viewer), english)

    def test_the_warnings_of_the_preview_follow_the_sender(self):
        template = MessageTemplate.objects.create(
            name="No mention", template_type=MessageTemplate.TemplateType.STANDALONE, content="{{ additional_message }}"
        )
        data = self.data(self.group_channel, group=self.ping_group, mentions_=("here",), ping_template=str(template.pk))
        with override_settings(LANGUAGE_CODE="zh-hans"):
            self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "zh-hans"
            answer = self.preview(self.leader, data).json()
        with translation.override("zh-hans"):
            expected = str(texts_missing_mention())
        self.assertIn(expected, answer["warnings"])
        self.assertNotIn("nobody will be notified", " ".join(answer["warnings"]))


def texts_missing_mention():
    from fleetops.services.messages import MISSING_MENTION_WARNING

    return MISSING_MENTION_WARNING


class PreviewTests(PingFixture):
    def test_the_preview_shows_what_is_posted(self):
        data = self.data(self.group_channel, group=self.ping_group, mentions_=(mentions.role_choice(self.role),))
        answer = self.preview(self.leader, data).json()
        self.assertTrue(answer["ok"])
        self.assertTrue(answer["ping"].startswith(f"<@&{self.role.role_id}>\nForm up for mining"))
        self.assertTrue(answer["ping_display"].startswith("@Sig A\nForm up for mining"))
        self.assertEqual(answer["blocking"], [])
        self.assertEqual(answer["ping_limit"], 2000)
        self.assertEqual(answer["ping_length"], mentions.discord_length(answer["ping"]))
        self.assertFalse(StandalonePing.objects.exists())

    def test_an_incomplete_form_names_what_is_missing(self):
        answer = self.preview(self.leader, {"request_id": str(uuid.uuid4())}).json()
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["missing"], ["Ping channel", "Message"])

    def test_a_leader_cannot_preview_an_alliance_channel(self):
        answer = self.preview(self.leader, self.data(self.alliance, group=self.ping_group)).json()
        self.assertFalse(answer["ok"])
        self.assertIn("ping_channel", answer["errors"])

    def test_a_too_long_ping_blocks_and_the_cooldown_warns(self):
        answer = self.preview(self.leader, self.data(self.group_channel, group=self.ping_group, message="x" * 1990)).json()
        self.assertTrue(answer["ok"])
        self.assertEqual(len(answer["blocking"]), 1)
        self.assertIn("Discord accepts at most 2000", answer["blocking"][0])
        f.standalone_ping(sender=self.fc_leader, channel=self.group_channel, group=self.ping_group)
        answer = self.preview(self.leader, self.data(self.group_channel, group=self.ping_group)).json()
        self.assertTrue(any("Wait" in warning for warning in answer["warnings"]))

    def test_members_cannot_preview(self):
        self.assertEqual(self.preview(self.member, self.data(self.alliance)).status_code, 403)


class RecentPingTests(PingFixture):
    """Who sees which past pings, "Not confirmed" and "Use again"."""

    def setUp(self):
        super().setUp()
        self.own = f.standalone_ping(sender=self.leader, channel=self.group_channel, group=self.ping_group,
                                     additional_message="Own ping")
        self.colleague = f.standalone_ping(sender=self.fc_leader, channel=self.group_channel, group=self.ping_group,
                                           additional_message="Colleague ping")
        self.other = f.standalone_ping(sender=self.lead, channel=self.fleet_channel, group=self.fleet_group,
                                       additional_message="Other group ping")
        self.alliance_ping = f.standalone_ping(sender=self.fc, channel=self.alliance, additional_message="Alliance ping")

    def shown(self, user, query=""):
        response = self.page(user, query)
        return {row["ping"] for row in response.context["recent"]}

    def test_each_role_sees_its_own_pings_and_those_of_its_groups(self):
        self.assertEqual(self.shown(self.leader), {self.own, self.colleague})
        self.assertEqual(self.shown(self.fc), {self.alliance_ping})
        self.assertEqual(self.shown(self.fc_leader), {self.own, self.colleague})
        everything = {self.own, self.colleague, self.other, self.alliance_ping}
        self.assertEqual(self.shown(self.lead), everything)
        self.assertEqual(self.shown(self.lead, "?pings=mine"), {self.other})

    def test_old_pings_leave_the_list(self):
        StandalonePing.objects.filter(pk=self.own.pk).update(created_at=timezone.now() - timedelta(days=31))
        self.assertEqual(self.shown(self.leader), {self.colleague})

    def test_a_pending_ping_is_not_confirmed_after_five_minutes(self):
        StandalonePing.objects.filter(pk=self.own.pk).update(
            status=StandalonePing.Status.PENDING, created_at=timezone.now() - timedelta(minutes=6)
        )
        StandalonePing.objects.filter(pk=self.colleague.pk).update(status=StandalonePing.Status.PENDING)
        rows = {row["ping"].pk: row for row in self.page(self.leader).context["recent"]}
        self.assertEqual((rows[self.own.pk]["tone"], rows[self.own.pk]["label"]), ("warn", "Not confirmed"))
        self.assertEqual(rows[self.colleague.pk]["label"], "Sending")

    def test_use_again_fills_the_form_for_visible_pings_only(self):
        StandalonePing.objects.filter(pk=self.colleague.pk).update(
            mention_mode="roles", mention_roles=[{"id": str(self.role.role_id), "name": "Sig A"}], formup="Jita"
        )
        response = self.page(self.leader, f"?again={self.colleague.uuid}")
        initial = response.context["form"].initial
        self.assertEqual(initial["additional_message"], "Colleague ping")
        self.assertEqual(initial["ping_channel"], self.group_channel.pk)
        self.assertEqual(initial["ping_mentions"], [mentions.role_choice(self.role)])
        self.assertEqual(initial["formup"], "Jita")
        self.assertNotEqual(str(initial["request_id"]), str(self.colleague.request_id))
        self.assertContains(response, 'id="send-ping-more" open')
        for value in (self.other.uuid, uuid.uuid4(), "not-a-uuid"):
            with self.subTest(value=value):
                missing = self.page(self.leader, f"?again={value}")
                self.assertEqual(missing.status_code, 404)
                self.assertContains(missing, 'id="fo-denied"', status_code=404)

    def test_use_again_drops_a_group_the_sender_no_longer_leads(self):
        old_group = f.aa_group("Example Old Group")
        old_ping = f.standalone_ping(sender=self.leader, channel=self.fleet_channel, group=old_group)
        form = self.page(self.leader, f"?again={old_ping.uuid}").context["form"]
        self.assertEqual(form.initial["group"], self.ping_group.pk)
        self.assertNotIn(self.fleet_channel, form.fields["ping_channel"].queryset)

    def test_use_again_keeps_a_doctrine_that_is_no_longer_offered_by_name(self):
        StandalonePing.objects.filter(pk=self.own.pk).update(
            doctrine_source="fittings", doctrine_external_id="7", doctrine_name="Example Doctrine"
        )
        form = self.page(self.leader, f"?again={self.own.uuid}").context["form"]
        self.assertEqual((form.initial["doctrine_choice"], form.initial["custom_doctrine"]), ("", "Example Doctrine"))

    def test_a_single_channel_is_chosen_already(self):
        form = self.page(self.leader).context["form"]
        self.assertEqual(form.initial["ping_channel"], self.group_channel.pk)
        form = self.page(self.fc_leader).context["form"]
        self.assertNotIn("ping_channel", form.initial)

    def test_an_empty_list_says_so(self):
        StandalonePing.objects.all().delete()
        response = self.page(self.leader)
        self.assertContains(response, 'id="recent-pings-empty"')
        self.assertContains(response, "No pings yet.")

    def test_failed_rows_show_why(self):
        StandalonePing.objects.filter(pk=self.own.pk).update(
            status=StandalonePing.Status.FAILED, result_message=texts.PING_HTTP(status=502), http_status=502
        )
        response = self.page(self.leader)
        self.assertContains(response, "Discord returned HTTP 502.")
        self.assertContains(response, "fo-pill-bad")

    def test_the_page_does_not_cost_more_with_more_pings_or_groups(self):
        def count():
            user = fresh(self.fc_leader)
            self.client.force_login(user)
            with CaptureQueriesContext(connection) as captured:
                self.assertEqual(self.client.get(reverse("fleetops:send_ping")).status_code, 200)
            return len(captured)

        count()  # The first page of a test also sets up Alliance Auth's menu.
        small = count()
        for number in range(3):
            group = f.aa_group(f"Extra Group {number}", leaders=[self.fc_leader])
            f.group_access(group, PING)
            f.group_channel(f"Extra Channel {number}", groups=[group])
            for _ in range(5):
                f.standalone_ping(sender=self.fc_leader, channel=self.group_channel, group=group)
        self.assertEqual(count(), small)


class ScriptTests(TestCase):
    """The page script builds the preview from text and DOM nodes only."""

    def test_the_script_writes_no_html(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("insertAdjacentHTML", source)
        self.assertIn("FleetOpsPreview.discord(", source)
        self.assertIn("dataset.foConfirm", source)

    def test_the_page_loads_the_preview_helpers_once(self):
        user = f.create_user("fc", perms=f.FC_PERMS)
        f.ping_channel("Alliance Pings")
        self.client.force_login(user)
        html = self.client.get(reverse("fleetops:send_ping")).content.decode()
        self.assertEqual(html.count("fleetops/preview.js"), 1)
        self.assertEqual(html.count("fleetops/send_ping.js"), 1)
        self.assertEqual(len(re.findall(r'id="ping-(?:look|preview|length)"', html)), 3)


@tag("catalog")
class ChinesePageTests(PingFixture):
    def test_the_page_in_chinese(self):
        f.standalone_ping(sender=self.leader, channel=self.group_channel, group=self.ping_group)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "zh-hans"
        html = self.page(self.leader).content.decode()
        for text in ("发送 Ping", "最近的 Ping", "再用一次", "更多设置", "消息"):
            self.assertIn(text, html)
        self.assertNotIn("Recent pings", html)
        self.assertNotIn("Use again", html)
