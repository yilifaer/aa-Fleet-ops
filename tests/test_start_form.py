# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Start form: ping channel, ping targets, fleet time, preview, the Discord length limit and the edit form."""

import json
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from fleetops.forms import OperationEditForm, StartFleetForm
from fleetops.models import AuditLog, FleetOperation, MessageTemplate, OperationAction
from fleetops.providers.esi import FleetDetectionResult
from fleetops.providers.srp import SRPLinkResult
from fleetops.services import operations as operations_module
from fleetops.services.mentions import EVERYONE, EXCLUSIVE_CHOICE_ERROR, HERE, NONE, ROLES, role_choice
from fleetops.services.messages import RenderedMessages, render_messages
from fleetops.services.operations import retry_ping, start_fleet

from . import factories as f
from .static_files import static_source

UTC = dt_timezone.utc
Status = FleetOperation.Status
ActionStatus = OperationAction.Status
FLEET_ID = 1_047_000_000_001
TOO_LONG = "The ping is 2105 characters; Discord accepts at most 2000. Shorten the additional information."


def flashed(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def rendered(ping="Ping text", motd="MOTD text", **extra):
    return RenderedMessages(ping, motd, [], **{"ping_display": ping, "ping_length": len(ping), **extra})


class StartFormTestBase(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Form FC")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.fleet_type = f.fleet_type("StratOp", "1.00")
        self.sig_a = f.ping_role("Sig A")
        self.sig_b = f.ping_role("Sig B")
        self.str_channel = f.ping_channel("STR", allow_here=True, roles=[self.sig_a])
        self.ops_channel = f.ping_channel("Ops", allow_everyone=True, allow_here=True)

    def data(self, **extra):
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fc_character_id": str(self.fc_char.character_id),
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Jita",
        }
        data.update(extra)
        return data

    def form(self, **extra):
        return StartFleetForm(self.data(**extra), user=self.fc)


class PingTargetFormTests(StartFormTestBase):
    def test_no_ping_is_the_default(self):
        form = self.form()

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["mention_selection"].mode, NONE)
        self.assertEqual(StartFleetForm(user=self.fc)["ping_mentions"].value(), [NONE])

    def test_choices_list_the_single_targets_then_active_roles(self):
        f.ping_role("Retired", is_active=False)

        choices = list(StartFleetForm(user=self.fc).fields["ping_mentions"].choices)

        self.assertEqual(
            choices,
            [
                (NONE, "No ping"),
                (EVERYONE, "@everyone"),
                (HERE, "@here"),
                (role_choice(self.sig_a), "@Sig A"),
                (role_choice(self.sig_b), "@Sig B"),
            ],
        )

    def test_no_ping_everyone_and_here_stand_alone(self):
        for values in ([NONE, EVERYONE], [EVERYONE, HERE], [HERE, role_choice(self.sig_a)]):
            with self.subTest(values=values):
                form = self.form(ping_mentions=values, ping_channel=str(self.ops_channel.pk))

                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors["ping_mentions"], [EXCLUSIVE_CHOICE_ERROR])

    def test_roles_can_be_combined(self):
        form = self.form(ping_mentions=[role_choice(self.sig_b), role_choice(self.sig_a)])

        self.assertTrue(form.is_valid(), form.errors)
        selection = form.cleaned_data["mention_selection"]
        self.assertEqual(selection.mode, ROLES)
        self.assertEqual([role.name for role in selection.roles], ["Sig A", "Sig B"])

    def test_channel_rules_are_enforced_on_the_server(self):
        for values, expected in (
            ([EVERYONE], "@everyone is not allowed in STR."),
            ([role_choice(self.sig_b)], "Role Sig B is not allowed in STR."),
        ):
            with self.subTest(values=values):
                form = self.form(ping_channel=str(self.str_channel.pk), ping_mentions=values)

                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors["ping_mentions"], [expected])
        self.assertTrue(self.form(ping_channel=str(self.str_channel.pk), ping_mentions=[HERE]).is_valid())
        self.assertTrue(
            self.form(ping_channel=str(self.str_channel.pk), ping_mentions=[role_choice(self.sig_a)]).is_valid()
        )

    def test_channel_without_mass_mentions(self):
        quiet = f.ping_channel("Quiet")

        form = self.form(ping_channel=str(quiet.pk), ping_mentions=[HERE])

        self.assertEqual(form.errors["ping_mentions"], ["@here is not allowed in Quiet."])

    def test_a_ping_that_is_only_copied_may_name_anybody(self):
        form = self.form(ping_mentions=[EVERYONE])

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["mention_selection"].mode, EVERYONE)

    def test_attendance_only_never_posts_so_channel_rules_do_not_apply(self):
        form = self.form(
            operation_mode="attendance_only", ping_channel=str(self.str_channel.pk), ping_mentions=[EVERYONE]
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_inactive_channels_and_roles_are_not_offered(self):
        self.ops_channel.is_active = False
        self.ops_channel.save()
        self.sig_b.is_active = False
        self.sig_b.save()

        form = self.form(ping_channel=str(self.ops_channel.pk), ping_mentions=[role_choice(self.sig_b)])

        self.assertFalse(form.is_valid())
        self.assertIn("ping_channel", form.errors)
        self.assertIn("ping_mentions", form.errors)

    def test_channel_rules_for_the_page_script(self):
        rules = StartFleetForm(user=self.fc).channel_rules()

        self.assertEqual(
            rules,
            {
                str(self.str_channel.pk): {"everyone": False, "here": True, "roles": [self.sig_a.pk]},
                str(self.ops_channel.pk): {"everyone": True, "here": True, "roles": None},
            },
        )


class FleetTimeFormTests(StartFormTestBase):
    def test_ping_now_has_no_fleet_time(self):
        form = self.form(fleet_date="2099-01-01", fleet_clock="19:00")

        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(form.cleaned_data["is_pre_ping"])
        self.assertIsNone(form.cleaned_data["scheduled_at"])

    @override_settings(TIME_ZONE="Australia/Adelaide")
    def test_pre_ping_time_is_eve_time_whatever_the_time_zone(self):
        with timezone.override("America/Los_Angeles"):
            form = self.form(fleet_timing="pre_ping", fleet_date="2099-10-12", fleet_clock="19:00")
            self.assertTrue(form.is_valid(), form.errors)

        self.assertTrue(form.cleaned_data["is_pre_ping"])
        self.assertEqual(form.cleaned_data["scheduled_at"], datetime(2099, 10, 12, 19, 0, tzinfo=UTC))

    def test_pre_ping_needs_a_future_time(self):
        past = timezone.now() - timedelta(minutes=5)

        form = self.form(fleet_timing="pre_ping", fleet_date=past.strftime("%Y-%m-%d"), fleet_clock=past.strftime("%H:%M"))

        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["fleet_clock"], ["The fleet time must be in the future (EVE time)."])

    def test_pre_ping_needs_date_and_time(self):
        form = self.form(fleet_timing="pre_ping", fleet_date="2099-10-12")

        self.assertEqual(form.errors["fleet_clock"], ["Enter the fleet date and time in EVE time (UTC)."])

    def test_clock_is_24_hour_hh_mm(self):
        for value in ("7pm", "24:00", "19:60", "1900"):
            with self.subTest(value=value):
                form = self.form(fleet_timing="pre_ping", fleet_date="2099-10-12", fleet_clock=value)

                self.assertEqual(form.errors["fleet_clock"], ["Enter the time as HH:MM (24-hour EVE time)."])

    def test_unfinished_hidden_time_does_not_block_a_start_now(self):
        for timing in ("now", ""):
            with self.subTest(timing=timing):
                form = self.form(fleet_timing=timing, fleet_date="2099-10-12", fleet_clock="9:5")

                self.assertTrue(form.is_valid(), form.errors)
                self.assertIsNone(form.cleaned_data["scheduled_at"])

        attendance = self.form(operation_mode="attendance_only", fleet_clock="9:5")
        self.assertTrue(attendance.is_valid(), attendance.errors)

    def test_hidden_time_inputs_are_disabled_on_the_page(self):
        self.client.force_login(self.fc)

        page = self.client.get(reverse("fleetops:start_fleet"))

        self.assertContains(page, "fleetops/start_fleet.js")
        self.assertIn("dateInput.disabled = clockInput.disabled = !pre;", static_source("start_fleet.js"))

    def test_attendance_only_cannot_pre_ping(self):
        form = self.form(
            operation_mode="attendance_only", fleet_timing="pre_ping", fleet_date="2099-10-12", fleet_clock="19:00"
        )

        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["fleet_timing"],
            ["A pre ping is a Discord message, so it cannot be sent in PAP tracking only mode."],
        )


class StartPageTests(StartFormTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.fc)

    def test_page_carries_the_channel_rules_and_the_timing_controls(self):
        page = self.client.get(reverse("fleetops:start_fleet"))

        self.assertEqual(page.status_code, 200)
        content = page.content.decode()
        rules = json.loads(content.split('<script id="ping-channel-rules" type="application/json">', 1)[1].split("</script>", 1)[0])
        self.assertEqual(rules, page.context["form"].channel_rules())
        self.assertIn(str(self.str_channel.pk), rules)
        self.assertContains(page, '<option value="" selected>No Discord message (copy only)</option>', html=True)
        self.assertContains(page, f'value="{role_choice(self.sig_a)}"')
        self.assertContains(page, 'name="fleet_timing" value="pre_ping"')
        self.assertContains(page, 'pattern="([01]\\d|2[0-3]):[0-5]\\d"')
        self.assertContains(page, 'id="eve-clock"')
        for offset in (30, 60, 120):
            self.assertContains(page, f'data-offset="{offset}"')
        self.assertContains(page, "SEND PRE PING & SCHEDULE FLEET")
        self.assertContains(page, 'data-ping-limit="2000"')
        self.assertContains(page, "fleetops/fleetops.js")
        self.assertNotContains(page, "ping_target")

    def test_page_shows_the_current_eve_time(self):
        moment = datetime(2026, 10, 12, 8, 41, tzinfo=UTC)

        with mock.patch("django.utils.timezone.now", return_value=moment), override_settings(TIME_ZONE="Asia/Shanghai"):
            page = self.client.get(reverse("fleetops:start_fleet"))

        self.assertContains(page, '<strong id="eve-clock">08:41</strong>', html=True)


class PreviewTests(StartFormTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.fc)

    def preview(self, **extra):
        return self.client.post(reverse("fleetops:preview_fleet"), self.data(**extra))

    def test_preview_returns_display_text_lengths_and_warnings(self):
        result = rendered(
            "<@&1> Ping",
            "MOTD",
            ping_display="@Sig A Ping",
            warnings=["The ping template does not contain {{ ping_mention }}; nobody will be notified."],
            motd_length=4,
            motd_visible_length=4,
            motd_limit=2000,
        )
        with mock.patch("fleetops.views.render_messages", return_value=result) as render:
            payload = self.preview(ping_channel=str(self.ops_channel.pk), ping_mentions=[EVERYONE]).json()

        self.assertEqual(
            payload,
            {
                "ok": True,
                "ping": "<@&1> Ping",
                "ping_display": "@Sig A Ping",
                "motd": "MOTD",
                "warnings": ["The ping template does not contain {{ ping_mention }}; nobody will be notified."],
                "notes": [],
                "blocking": [],
                "ping_length": 10,
                "motd_length": 4,
                "motd_visible_length": 4,
                "motd_limit": 2000,
            },
        )
        self.assertEqual(render.call_args.kwargs, {"actor": self.fc, "pre_ping": False})
        operation = render.call_args.args[0]
        self.assertIsNone(operation.pk)
        self.assertEqual((operation.ping_channel, operation.ping_mention_mode), (self.ops_channel, EVERYONE))

    def test_blocking_problem_of_a_posted_ping_is_returned(self):
        result = rendered("x" * 2105, blocking=[TOO_LONG])
        with mock.patch("fleetops.views.render_messages", return_value=result):
            payload = self.preview(ping_channel=str(self.ops_channel.pk)).json()

        self.assertEqual(payload["blocking"], [TOO_LONG])
        self.assertEqual(payload["ping_length"], 2105)

    def test_ping_that_is_only_copied_gets_a_length_warning(self):
        payload = self.preview(additional_message="x" * 2105).json()

        self.assertEqual(payload["blocking"], [])
        self.assertGreater(payload["ping_length"], 2000)
        self.assertEqual(
            payload["warnings"][0],
            f"The ping is {payload['ping_length']} characters; Discord accepts at most 2000. Shorten the additional information.",
        )
        # The length is only reported once.
        self.assertEqual(sum("Discord accepts" in warning for warning in payload["warnings"]), 1)

    def test_preview_of_a_pre_ping_renders_it_as_a_pre_ping_with_its_time(self):
        template = MessageTemplate.objects.create(
            name="Time", template_type=MessageTemplate.TemplateType.PING, content="Fleet at {{ fleet_time }}"
        )
        data = {"fleet_timing": "pre_ping", "fleet_date": "2099-10-12", "fleet_clock": "19:30"}

        with mock.patch("fleetops.views.render_messages", wraps=render_messages) as render:
            payload = self.preview(ping_template=str(template.pk), **data).json()

        self.assertTrue(render.call_args.kwargs["pre_ping"])
        self.assertIn("2099-10-12", payload["ping"])
        self.assertIn("19:30", payload["ping"])

    def test_preview_never_saves_a_fleet(self):
        self.preview(ping_channel=str(self.ops_channel.pk), ping_mentions=[EVERYONE])

        self.assertFalse(FleetOperation.objects.exists())


class StartWithChannelTests(StartFormTestBase):
    """Starting a fleet posts to the selected channel with exactly the selected mention."""

    def setUp(self):
        super().setUp()
        patches = {
            "detect": mock.patch.object(
                operations_module,
                "detect_character_fleet",
                side_effect=lambda user, character_id, fresh=False: FleetDetectionResult(
                    fleet_id=FLEET_ID, role="fleet_commander", fleet_boss_id=character_id
                ),
            ),
            "motd": mock.patch.object(operations_module, "set_fleet_motd"),
            "sync": mock.patch.object(operations_module, "sync_operation", return_value=0),
            "srp": mock.patch.object(
                operations_module, "create_srp_link", return_value=SRPLinkResult("test", message="No SRP in tests.")
            ),
            "post": mock.patch("fleetops.providers.pings.requests.post", return_value=mock.Mock(status_code=204, text="")),
        }
        self.mocks = {}
        for name, patcher in patches.items():
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.client.force_login(self.fc)

    def post_start(self, **extra):
        return self.client.post(reverse("fleetops:start_fleet"), self.data(**extra))

    def test_roles_are_posted_with_exactly_their_ids(self):
        response = self.post_start(
            ping_channel=str(self.ops_channel.pk), ping_mentions=[role_choice(self.sig_a), role_choice(self.sig_b)]
        )

        operation = FleetOperation.objects.get()
        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False
        )
        self.assertEqual(operation.ping_channel, self.ops_channel)
        self.assertEqual(operation.ping_channel_name, "Ops")
        self.assertEqual(operation.ping_mention_mode, ROLES)
        self.assertEqual(
            operation.ping_roles,
            [{"id": str(self.sig_a.role_id), "name": "Sig A"}, {"id": str(self.sig_b.role_id), "name": "Sig B"}],
        )
        self.assertIsNone(operation.ping_target)
        self.assertEqual(self.mocks["post"].call_args.args[0], self.ops_channel.webhook_url)
        self.assertEqual(
            self.mocks["post"].call_args.kwargs["json"]["allowed_mentions"],
            {"parse": [], "roles": [str(self.sig_a.role_id), str(self.sig_b.role_id)]},
        )

    def test_copy_only_start_posts_nothing(self):
        self.post_start(ping_mentions=[EVERYONE])

        operation = FleetOperation.objects.get()
        self.mocks["post"].assert_not_called()
        step = operation.actions.get(action="discord_ping")
        self.assertEqual(step.status, ActionStatus.SKIPPED)
        self.assertEqual(step.error_message, "No Discord channel selected; the ping is available to copy.")

    def test_attendance_only_stores_no_channel(self):
        self.post_start(operation_mode="attendance_only", ping_channel=str(self.ops_channel.pk))

        operation = FleetOperation.objects.get()
        self.assertIsNone(operation.ping_channel)
        self.assertEqual(operation.ping_channel_name, "")
        self.mocks["post"].assert_not_called()

    def test_disallowed_mention_is_a_form_error_and_nothing_starts(self):
        response = self.post_start(ping_channel=str(self.str_channel.pk), ping_mentions=[EVERYONE])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors["ping_mentions"], ["@everyone is not allowed in STR."])
        self.assertFalse(FleetOperation.objects.exists())
        self.mocks["detect"].assert_not_called()

    def test_ping_over_the_discord_limit_with_a_channel_stops_the_start(self):
        with mock.patch("fleetops.views.render_messages", return_value=rendered("x" * 2105, blocking=[TOO_LONG])):
            response = self.post_start(ping_channel=str(self.ops_channel.pk))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].non_field_errors(), [TOO_LONG])
        self.assertFalse(FleetOperation.objects.exists())
        self.mocks["detect"].assert_not_called()
        self.mocks["post"].assert_not_called()

    def test_ping_over_the_discord_limit_that_is_only_copied_still_starts(self):
        with mock.patch("fleetops.views.render_messages", return_value=rendered("x" * 2105, blocking=[TOO_LONG])):
            response = self.post_start()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(FleetOperation.objects.get().status, Status.ACTIVE)

    def test_start_renders_as_the_starting_user_and_snapshots_the_doctrine(self):
        fits = [{"fit_id": 3, "fit": "DPS Moa", "ship_type_id": 623, "ship": "Moa"}]
        with mock.patch.object(operations_module, "doctrine_fits_snapshot", return_value=fits) as snapshot, mock.patch.object(
            operations_module, "render_messages", wraps=operations_module.render_messages
        ) as render:
            operation = start_fleet(
                user=self.fc,
                cleaned_data={
                    "operation_mode": "full",
                    "fc_character_id": str(self.fc_char.character_id),
                    "fleet_type": self.fleet_type,
                    "formup": "Jita",
                    "doctrine_source": "fittings:fittings.doctrine",
                    "doctrine_external_id": "12",
                    "doctrine_name": "Moa Fleet",
                },
            )

        snapshot.assert_called_once_with("fittings:fittings.doctrine", "12")
        self.assertEqual(operation.doctrine_fits, fits)
        self.assertEqual(render.call_args.kwargs, {"actor": self.fc})

    def test_one_start_audit_entry(self):
        self.post_start(ping_channel=str(self.ops_channel.pk), ping_mentions=[HERE])

        operation = FleetOperation.objects.get()
        entry = AuditLog.objects.get(action="fleet.start")
        self.assertEqual((entry.actor, entry.object_id), (self.fc, str(operation.pk)))
        self.assertIsNone(entry.old_value)
        self.assertEqual(
            entry.new_value,
            {
                "status": Status.STARTING,
                "esi_fleet_id": FLEET_ID,
                "fc_character_id": self.fc_char.character_id,
                "fleet_type": "StratOp",
                "group_id": None,
                "group": "",
                "operation_mode": "full",
                "ping_channel": "Ops",
                "ping_mention_mode": HERE,
                "from_schedule": False,
                "handed_over_from": [],
                # Added once the start steps ran.
                "steps": {
                    "fleet_detection": ActionStatus.SUCCESS,
                    "discord_ping": ActionStatus.SUCCESS,
                    "motd_update": ActionStatus.SUCCESS,
                    "tracking_start": ActionStatus.SUCCESS,
                    "srp_link": ActionStatus.SKIPPED,
                },
            },
        )


class SendPingTests(TestCase):
    """The ping is checked against its channel again when it is sent (start, retry)."""

    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.sig_a = f.ping_role("Sig A")
        self.channel = f.ping_channel("STR", allow_everyone=True, roles=[self.sig_a])
        post = mock.patch("fleetops.providers.pings.requests.post", return_value=mock.Mock(status_code=204, text=""))
        self.post = post.start()
        self.addCleanup(post.stop)

    def operation(self, mode=EVERYONE, roles=None):
        return f.create_operation(
            self.fc, ping_channel=self.channel, ping_mention_mode=mode, ping_roles=roles or [], ping_text="Ping"
        )

    def test_retry_posts_with_the_stored_mention(self):
        result = retry_ping(self.operation(), actor=self.fc)

        self.assertEqual(result.status, ActionStatus.SUCCESS)
        self.assertEqual(
            self.post.call_args.kwargs["json"],
            {"content": "Ping", "allowed_mentions": {"parse": ["everyone"]}, "flags": 4},
        )

    def test_retry_of_an_here_ping_never_notifies_everyone(self):
        operation = self.operation(HERE)
        self.channel.allow_here = True
        self.channel.save()
        FleetOperation.objects.filter(pk=operation.pk).update(ping_text="@here\nStratOp\n\nBring alts, @everyone welcome")
        operation.refresh_from_db()

        retry_ping(operation, actor=operation.fc_user)

        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(payload["content"], "@here\nStratOp\n\nBring alts, @\u200beveryone welcome")
        self.assertEqual(payload["allowed_mentions"], {"parse": ["everyone"]})

    def test_everyone_and_role_pings_are_sent_as_stored(self):
        text = "@everyone\nStratOp @here"
        for mode, roles in ((EVERYONE, None), (ROLES, [{"id": str(self.sig_a.role_id), "name": "Sig A"}])):
            with self.subTest(mode=mode):
                operation = self.operation(mode, roles)
                FleetOperation.objects.filter(pk=operation.pk).update(ping_text=text)
                operation.refresh_from_db()

                retry_ping(operation, actor=operation.fc_user)

                self.assertEqual(self.post.call_args.kwargs["json"]["content"], text)

    def test_channel_disabled_since_the_start(self):
        operation = self.operation()
        self.channel.is_active = False
        self.channel.save()

        result = retry_ping(operation, actor=operation.fc_user)

        self.assertEqual((result.status, result.error_message), (ActionStatus.FAILED, "Ping channel STR is disabled."))
        self.post.assert_not_called()

    def test_mention_no_longer_allowed_in_the_channel(self):
        operation = self.operation()
        self.channel.allow_everyone = False
        self.channel.save()

        result = retry_ping(operation, actor=operation.fc_user)

        self.assertEqual((result.status, result.error_message), (ActionStatus.FAILED, "@everyone is not allowed in STR."))
        self.post.assert_not_called()

    def test_role_deactivated_since_the_start(self):
        operation = self.operation(ROLES, [{"id": str(self.sig_a.role_id), "name": "Sig A"}])
        self.sig_a.is_active = False
        self.sig_a.save()

        result = retry_ping(operation, actor=operation.fc_user)

        self.assertEqual(result.error_message, "Role Sig A is no longer available.")
        self.post.assert_not_called()

    def test_channel_deleted_after_the_start(self):
        operation = self.operation()
        self.channel.delete()
        operation.refresh_from_db()

        result = retry_ping(operation, actor=operation.fc_user)

        self.assertEqual(result.status, ActionStatus.SKIPPED)
        self.post.assert_not_called()


class OperationEditFormTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.operation = f.create_operation(
            self.fc,
            status=Status.CLOSED,
            started_at=datetime(2026, 10, 11, 19, 0, tzinfo=UTC),
            ended_at=datetime(2026, 10, 11, 21, 0, tzinfo=UTC),
        )

    def form(self, started="2026-10-11T19:00", ended="2026-10-11T21:00", **extra):
        data = {"fleet_type": str(self.operation.fleet_type_id), "formup": "Jita", "started_at": started, "ended_at": ended}
        data.update(extra)
        return OperationEditForm(data, instance=self.operation)

    def test_labels_name_eve_time(self):
        form = OperationEditForm(instance=self.operation)

        self.assertEqual(form.fields["started_at"].label, "Start Time — EVE time (UTC)")
        self.assertEqual(form.fields["ended_at"].label, "End Time — EVE time (UTC)")

    @override_settings(TIME_ZONE="Australia/Adelaide")
    def test_times_are_read_and_shown_as_utc_whatever_the_time_zone(self):
        with timezone.override("America/Los_Angeles"):
            form = self.form(started="2026-10-11T18:30", ended="2026-10-11T20:15")
            self.assertTrue(form.is_valid(), form.errors)
            shown = str(OperationEditForm(instance=FleetOperation.objects.get(pk=self.operation.pk))["started_at"])

        self.assertEqual(form.cleaned_data["started_at"], datetime(2026, 10, 11, 18, 30, tzinfo=UTC))
        self.assertEqual(form.cleaned_data["ended_at"], datetime(2026, 10, 11, 20, 15, tzinfo=UTC))
        self.assertIn('value="2026-10-11T19:00"', shown)

    def test_end_must_be_after_the_start(self):
        for ended in ("2026-10-11T18:59", "2026-10-11T19:00"):
            with self.subTest(ended=ended):
                form = self.form(ended=ended)

                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors["ended_at"], ["End Time must be after Start Time."])

    def test_new_times_over_six_hours_need_the_confirmation(self):
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

    def test_a_stored_long_fleet_does_not_ask_again(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            ended_at=datetime(2026, 10, 12, 8, 51, 30, tzinfo=UTC)
        )
        self.operation.refresh_from_db()

        form = self.form(ended="2026-10-12T08:51", formup="Amarr")

        self.assertTrue(form.is_valid(), form.errors)


class EditOperationViewTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.operation = f.create_operation(
            self.fc,
            status=Status.CLOSED,
            doctrine_name="Moa Fleet",
            doctrine_source="fittings:fittings.doctrine",
            doctrine_external_id="12",
            doctrine_fits=[{"fit_id": 3, "fit": "DPS Moa", "ship_type_id": 623, "ship": "Moa"}],
        )
        self.ping = MessageTemplate.objects.create(name="Short", template_type=MessageTemplate.TemplateType.PING, content="P")
        self.motd = MessageTemplate.objects.create(name="Short", template_type=MessageTemplate.TemplateType.MOTD, content="M")
        self.client.force_login(self.fc)

    def post(self, **extra):
        data = {
            "fleet_type": str(self.operation.fleet_type_id),
            "doctrine_name": "Moa Fleet",
            "formup": "Jita",
            "started_at": self.operation.started_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M"),
            "ended_at": self.operation.ended_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M"),
        }
        data.update(extra)
        return self.client.post(reverse("fleetops:edit_operation", args=[self.operation.uuid]), data)

    def test_templates_are_saved_and_audited(self):
        response = self.post(ping_template=str(self.ping.pk), motd_template=str(self.motd.pk))

        self.assertEqual(response.status_code, 302)
        self.operation.refresh_from_db()
        self.assertEqual((self.operation.ping_template, self.operation.motd_template), (self.ping, self.motd))
        entry = AuditLog.objects.get(action="operation.edit")
        self.assertEqual(entry.new_value["ping_template"], str(self.ping.pk))
        self.assertEqual(entry.new_value["motd_template"], str(self.motd.pk))

    def test_same_doctrine_keeps_its_fittings_link(self):
        self.post(formup="Amarr")

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.doctrine_source, "fittings:fittings.doctrine")
        self.assertEqual(self.operation.doctrine_external_id, "12")
        self.assertEqual(len(self.operation.doctrine_fits), 1)

    def test_changed_doctrine_becomes_a_custom_doctrine(self):
        self.post(doctrine_name="Ferox Fleet")

        self.operation.refresh_from_db()
        self.assertEqual(
            (self.operation.doctrine_name, self.operation.doctrine_source, self.operation.doctrine_external_id),
            ("Ferox Fleet", "custom", ""),
        )
        self.assertEqual(self.operation.doctrine_fits, [])
        entry = AuditLog.objects.get(action="operation.edit")
        self.assertEqual(entry.old_value["doctrine_source"], "fittings:fittings.doctrine")
        self.assertEqual(entry.new_value["doctrine_source"], "custom")

    def test_page_offers_eve_time_and_the_long_fleet_confirmation(self):
        page = self.client.get(reverse("fleetops:edit_operation", args=[self.operation.uuid]))

        self.assertContains(page, "Start Time — EVE time (UTC)")
        self.assertContains(page, 'id="eve-time-now"')
        self.assertContains(page, 'id="confirm-long-fleet-wrap"')
        started = self.operation.started_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M")
        self.assertContains(page, f'data-stored-start="{started}"')
        self.assertContains(page, f'name="started_at" value="{started}"')

    def test_fleet_type_change_updates_the_weight_snapshot(self):
        other = f.fleet_type("Roam", "3.00")

        self.post(fleet_type=str(other.pk))

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.fleet_point_weight_snapshot, Decimal("3.00"))

    def test_page_says_retries_resend_the_stored_text(self):
        page = self.client.get(reverse("fleetops:edit_operation", args=[self.operation.uuid]))

        self.assertContains(
            page,
            "Retry Ping and Retry MOTD resend the stored text; use Regenerate &amp; Push MOTD to apply a changed "
            "MOTD template. A changed ping template is only recorded on the fleet.",
        )
        self.assertNotContains(page, "Changed templates are used by the next ping or MOTD retry.")

    def test_retries_after_a_template_change_send_the_stored_text(self):
        channel = f.ping_channel("STR", allow_here=True)
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            esi_fleet_id=FLEET_ID,
            ping_channel=channel,
            ping_mention_mode=HERE,
            ping_text="@here OLD PING",
            motd_text="OLD MOTD",
        )
        self.operation.refresh_from_db()
        self.ping.content = "NEW PING {{ fc }}"
        self.ping.save()
        self.motd.content = "NEW MOTD {{ fc }}"
        self.motd.save()

        self.post(ping_template=str(self.ping.pk), motd_template=str(self.motd.pk))
        self.operation.refresh_from_db()
        with mock.patch(
            "fleetops.providers.pings.requests.post", return_value=mock.Mock(status_code=204, text="")
        ) as post, mock.patch("fleetops.services.operations.set_fleet_motd") as set_motd:
            retry_ping(self.operation, actor=self.operation.fc_user)
            operations_module.retry_motd(self.operation)

        self.assertEqual(self.operation.ping_template, self.ping)
        self.assertEqual(post.call_args.kwargs["json"]["content"], "@here OLD PING")
        self.assertEqual(set_motd.call_args.args[3], "OLD MOTD")


class ManualFleetPageTests(TestCase):
    def test_long_fleet_confirmation_hides_again_below_six_hours(self):
        self.client.force_login(f.create_user(perms=f.FC_PERMS))

        page = self.client.get(reverse("fleetops:manual_fleet"))

        self.assertContains(page, "if(!(minutes > LONG_FLEET_MINUTES)){ confirmWrap.classList.add('d-none'); confirmBox.checked = false; }")
