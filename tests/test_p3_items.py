"""Smaller FC and admin items: poll cadence, write-only webhook URLs, start/retry/tracking audits,
the incentive period form and the doctrines Fittings shows each FC."""

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest import mock

import requests
from django.apps import apps as django_apps
from django.contrib import admin
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.db.models import Q, QuerySet
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.admin import DiscordWebhookAdmin
from fleetops.forms import StartFleetForm
from fleetops.forms_config import DiscordWebhookForm
from fleetops.models import (
    AuditLog,
    DiscordWebhook,
    FleetOperation,
    IncentivePeriod,
    OperationAction,
)
from fleetops.providers import doctrines as doctrines_module
from fleetops.providers.doctrines import get_doctrines
from fleetops.providers.esi import FleetDetectionResult, FleetESIError
from fleetops.providers.srp import SRPLinkResult
from fleetops.services import operations as operations_module
from fleetops.services.tracking import UnreadableFleet
from fleetops.tasks import POLL_SLACK_SECONDS, schedule_active_fleet_tracking, track_operation

from . import factories as f

Status = FleetOperation.Status
ActionStatus = OperationAction.Status
FLEET_ID = 1_047_200_000_001
WEBHOOK_TOKEN = "Kq3-p3-webhook-token-never-shown"
WEBHOOK_URL = f"https://discord.com/api/webhooks/445566/{WEBHOOK_TOKEN}"


def flashed(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


@contextmanager
def frozen_now(moment):
    with mock.patch("django.utils.timezone.now", return_value=moment):
        yield


# ---------------------------------------------------------------------------
# #27 poll cadence
# ---------------------------------------------------------------------------


class PollCadenceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.start = timezone.now().replace(microsecond=0)
        self.operation = f.create_operation(self.fc, last_esi_update=self.start - timedelta(minutes=10))
        self.polls = []
        sync = mock.patch("fleetops.tasks.sync_operation", side_effect=self.fake_sync)
        sync.start()
        self.addCleanup(sync.stop)

    def fake_sync(self, operation):
        # A successful read, as sync_operation stores it.
        now = timezone.now()
        self.polls.append(now)
        FleetOperation.objects.filter(pk=operation.pk).update(last_esi_update=now, last_error="")
        operation.last_error = ""
        return 1

    def run_beats(self, minutes, latency):
        """One beat per minute, a little after the minute; each queued poll runs ``latency`` later."""

        def run_later(operation_id):
            with frozen_now(timezone.now() + latency):
                track_operation(operation_id)

        with mock.patch.object(track_operation, "delay", side_effect=run_later):
            for minute in range(minutes):
                with frozen_now(self.start + timedelta(minutes=minute, seconds=0.2)):
                    schedule_active_fleet_tracking()
        return [(poll - self.start).total_seconds() for poll in self.polls]

    def queued_at(self, moment):
        with mock.patch.object(track_operation, "delay") as delay, frozen_now(moment):
            schedule_active_fleet_tracking()
        return [call.args[0] for call in delay.call_args_list]

    def test_a_12_second_worker_delay_keeps_the_60_second_interval(self):
        f.settings(tracking_interval=60)

        self.assertEqual(self.run_beats(5, latency=timedelta(seconds=12)), [12.2, 72.2, 132.2, 192.2, 252.2])

    def test_a_slow_worker_with_a_longer_interval(self):
        f.settings(tracking_interval=120)

        self.assertEqual(self.run_beats(6, latency=timedelta(seconds=40)), [40.2, 160.2, 280.2])

    def test_a_waiting_poll_is_not_queued_twice(self):
        f.settings(tracking_interval=60)

        self.assertEqual(self.queued_at(self.start), [self.operation.pk])
        # The worker has not picked the poll up yet: the next beats leave the fleet alone.
        self.assertEqual(self.queued_at(self.start + timedelta(minutes=1)), [])
        self.assertEqual(self.queued_at(self.start + timedelta(minutes=2)), [])
        self.assertLessEqual(cache.ttl(f"fleetops:track:queued:{self.operation.pk}"), 120)

        # The guard ends when the poll starts (or after two intervals).
        with frozen_now(self.start + timedelta(minutes=2, seconds=30)):
            track_operation(self.operation.pk)
        self.assertEqual(self.queued_at(self.start + timedelta(minutes=3)), [self.operation.pk])

    def test_a_poll_the_beat_did_not_queue_does_not_move_the_queue_time(self):
        f.settings(tracking_interval=60)
        self.queued_at(self.start)
        with frozen_now(self.start + timedelta(seconds=5)):
            track_operation(self.operation.pk)
        with frozen_now(self.start + timedelta(seconds=40)):
            track_operation(self.operation.pk)

        self.assertEqual(self.queued_at(self.start + timedelta(seconds=60 - POLL_SLACK_SECONDS)), [self.operation.pk])

    def test_without_a_queue_time_the_last_sync_paces_the_polls(self):
        f.settings(tracking_interval=120)
        FleetOperation.objects.filter(pk=self.operation.pk).update(last_esi_update=self.start)

        self.assertEqual(self.queued_at(self.start + timedelta(seconds=60)), [])
        self.assertEqual(self.queued_at(self.start + timedelta(seconds=120)), [self.operation.pk])


# ---------------------------------------------------------------------------
# #28 write-only webhook URL
# ---------------------------------------------------------------------------


class WebhookUrlTests(TestCase):
    def setUp(self):
        self.lead = f.create_user(perms=f.FC_LEAD_PERMS)
        self.client.force_login(self.lead)
        self.channel = f.ping_channel("Ops Pings", url=WEBHOOK_URL)

    def edit_url(self):
        return reverse("fleetops:configuration_edit", args=["ping-channels", self.channel.pk])

    def payload(self, **extra):
        data = {"name": "Ops Pings", "webhook_url": "", "is_active": "on", "sort_order": "0"}
        data.update(extra)
        return data

    def assertNoSecret(self, response):
        self.assertNotIn(WEBHOOK_TOKEN, response.content.decode())

    def test_edit_page_never_contains_the_token(self):
        response = self.client.get(self.edit_url())

        self.assertEqual(response.status_code, 200)
        self.assertNoSecret(response)
        self.assertContains(response, 'autocomplete="new-password"')
        self.assertContains(response, "Leave empty to keep the stored URL.")

    def test_blank_keeps_the_stored_url(self):
        response = self.client.post(self.edit_url(), self.payload(name="Ops Pings Renamed"))

        self.assertEqual(response.status_code, 302)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.name, "Ops Pings Renamed")
        self.assertEqual(self.channel.webhook_url, WEBHOOK_URL)
        entry = AuditLog.objects.get(action="configuration.update", object_type="DiscordWebhook")
        self.assertNotIn("webhook_url_replaced", entry.new_value)

    def test_a_new_url_replaces_the_stored_one(self):
        rotated = "https://discord.com/api/webhooks/445566/rotated-token"

        self.client.post(self.edit_url(), self.payload(webhook_url=rotated))

        self.channel.refresh_from_db()
        self.assertEqual(self.channel.webhook_url, rotated)
        entry = AuditLog.objects.get(action="configuration.update", object_type="DiscordWebhook")
        self.assertTrue(entry.new_value["webhook_url_replaced"])
        self.assertNotIn("rotated-token", json.dumps([entry.old_value, entry.new_value]))

    def test_invalid_url_is_neither_echoed_nor_rendered_back(self):
        typo = "https://discord.example/api/webhooks/1/typo-secret-token"

        response = self.client.post(self.edit_url(), self.payload(webhook_url=typo))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a Discord webhook URL in the form")
        self.assertNotContains(response, "typo-secret-token")
        self.assertNoSecret(response)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.webhook_url, WEBHOOK_URL)

    def test_a_new_channel_needs_a_url(self):
        response = self.client.post(
            reverse("fleetops:configuration_add", args=["ping-channels"]), self.payload(name="New Channel")
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DiscordWebhook.objects.filter(name="New Channel").exists())
        self.assertFalse(response.context["form"].fields["webhook_url"].help_text)

    def test_admin_uses_the_same_form(self):
        self.assertIs(DiscordWebhookAdmin.form, DiscordWebhookForm)
        self.assertIsInstance(admin.site._registry[DiscordWebhook], DiscordWebhookAdmin)

    def test_admin_change_page_hides_the_url_and_blank_keeps_it(self):
        superuser = f.create_user()
        superuser.is_staff = superuser.is_superuser = True
        superuser.save()
        self.client.force_login(superuser)
        url = reverse("admin:fleetops_discordwebhook_change", args=[self.channel.pk])

        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertNoSecret(page)

        response = self.client.post(url, self.payload(name="Admin Renamed"))
        self.assertEqual(response.status_code, 302)
        self.channel.refresh_from_db()
        self.assertEqual((self.channel.name, self.channel.webhook_url), ("Admin Renamed", WEBHOOK_URL))

    def staff(self, *perms):
        user = f.create_user(perms=list(perms))
        user.is_staff = True
        user.save()
        self.client.force_login(user)
        return user

    def test_view_only_admins_never_see_the_url(self):
        # Without the change permission Django shows every field as text.
        self.staff("fleetops.view_discordwebhook")

        page = self.client.get(reverse("admin:fleetops_discordwebhook_change", args=[self.channel.pk]))

        self.assertEqual(page.status_code, 200)
        self.assertNoSecret(page)
        self.assertNotContains(page, "discord.com/api/webhooks")
        self.assertContains(page, "Stored (hidden)")

    def test_admins_who_may_add_still_enter_the_url(self):
        self.staff("fleetops.add_discordwebhook", "fleetops.view_discordwebhook")

        page = self.client.get(reverse("admin:fleetops_discordwebhook_add"))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'name="webhook_url"')


# ---------------------------------------------------------------------------
# #29 audits: start steps, retries, tracking errors
# ---------------------------------------------------------------------------


class StartAuditTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Audit FC")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.fleet_type = f.fleet_type()
        self.channel = f.ping_channel("Ops", allow_here=True, url=WEBHOOK_URL)
        patches = [
            mock.patch.object(
                operations_module,
                "detect_character_fleet",
                side_effect=lambda user, character_id, fresh=False: FleetDetectionResult(
                    fleet_id=FLEET_ID, role="fleet_commander", fleet_boss_id=character_id
                ),
            ),
            mock.patch.object(operations_module, "set_fleet_motd", side_effect=FleetESIError("ESI_ERROR", "MOTD not set.")),
            mock.patch.object(operations_module, "sync_operation", return_value=0),
            mock.patch("fleetops.providers.pings.requests.post", return_value=mock.Mock(status_code=204, text="")),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        f.settings(srp_auto_create=False)
        self.client.force_login(self.fc)

    def data(self, **extra):
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fc_character_id": str(self.fc_char.character_id),
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Jita",
            "ping_channel": str(self.channel.pk),
            "ping_mentions": ["here"],
        }
        data.update(extra)
        return data

    def test_one_start_entry_with_the_step_results(self):
        self.client.post(reverse("fleetops:start_fleet"), self.data())

        entry = AuditLog.objects.get(action="fleet.start")
        self.assertEqual(entry.new_value["fleet_type"], "StratOp")
        self.assertEqual(entry.new_value["esi_fleet_id"], FLEET_ID)
        self.assertEqual(entry.new_value["fc_character_id"], self.fc_char.character_id)
        self.assertEqual(entry.new_value["operation_mode"], "full")
        self.assertEqual(
            entry.new_value["steps"],
            {
                "fleet_detection": ActionStatus.SUCCESS,
                "discord_ping": ActionStatus.SUCCESS,
                "motd_update": ActionStatus.FAILED,
                "tracking_start": ActionStatus.SUCCESS,
                "srp_link": ActionStatus.SKIPPED,
            },
        )
        self.assertNotIn(WEBHOOK_TOKEN, json.dumps(entry.new_value))

    def test_attendance_only_start_lists_its_skipped_steps(self):
        self.client.post(reverse("fleetops:start_fleet"), self.data(operation_mode="attendance_only"))

        steps = AuditLog.objects.get(action="fleet.start").new_value["steps"]
        self.assertEqual(steps["discord_ping"], ActionStatus.SKIPPED)
        self.assertEqual(steps["motd_update"], ActionStatus.SKIPPED)

    def test_resubmit_and_already_tracked_starts_add_nothing(self):
        data = self.data()
        self.client.post(reverse("fleetops:start_fleet"), data)
        entry = AuditLog.objects.get(action="fleet.start")
        steps = entry.new_value["steps"]
        operation = FleetOperation.objects.get()
        # A retry changes the step; the start entry keeps what the start did.
        with mock.patch.object(operations_module, "set_fleet_motd"):
            self.client.post(reverse("fleetops:retry_motd", args=[operation.uuid]))

        self.client.post(reverse("fleetops:start_fleet"), data)
        self.client.post(reverse("fleetops:start_fleet"), self.data())

        self.assertEqual(AuditLog.objects.filter(action="fleet.start").count(), 1)
        entry.refresh_from_db()
        self.assertEqual(entry.new_value["steps"], steps)


class RetryAuditTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.channel = f.ping_channel("Ops", url=WEBHOOK_URL)
        self.operation = f.create_operation(
            self.fc, ping_channel=self.channel, ping_text="Form up", motd_text="MOTD", send_ping=True
        )
        operations_module.set_action(self.operation, "discord_ping", False, "Discord webhook request failed (ConnectionError).")
        self.client.force_login(self.fc)

    def retry(self, name):
        return self.client.post(reverse(f"fleetops:{name}", args=[self.operation.uuid]))

    def test_ping_retry(self):
        with mock.patch("fleetops.providers.pings.requests.post", return_value=mock.Mock(status_code=204, text="")):
            self.retry("retry_ping")

        entry = AuditLog.objects.get(action="fleet.retry_ping")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.object_id, str(self.operation.pk))
        self.assertEqual(entry.old_value, {"step": "discord_ping", "status": ActionStatus.FAILED})
        self.assertEqual(entry.new_value, {"step": "discord_ping", "status": ActionStatus.SUCCESS, "message": ""})

    def test_failed_ping_retry_keeps_the_short_message_only(self):
        with mock.patch(
            "fleetops.providers.pings.requests.post",
            side_effect=requests.exceptions.ConnectionError(f"Max retries exceeded with url: {WEBHOOK_URL}"),
        ):
            self.retry("retry_ping")

        entry = AuditLog.objects.get(action="fleet.retry_ping")
        self.assertEqual(entry.new_value["status"], ActionStatus.FAILED)
        self.assertEqual(entry.new_value["message"], "Discord webhook request failed (ConnectionError).")
        self.assertNotIn(WEBHOOK_TOKEN, json.dumps([entry.old_value, entry.new_value]))

    def test_motd_retry(self):
        with mock.patch.object(operations_module, "set_fleet_motd", side_effect=FleetESIError("ESI_ERROR", "Fleet MOTD could not be set.")):
            self.retry("retry_motd")

        entry = AuditLog.objects.get(action="fleet.retry_motd")
        self.assertEqual(entry.old_value, {"step": "motd_update", "status": None})
        self.assertEqual(
            entry.new_value,
            {"step": "motd_update", "status": ActionStatus.FAILED, "message": "Fleet MOTD could not be set."},
        )

    def test_srp_retry(self):
        result = SRPLinkResult("aasrp", reference="7", url="https://auth.example/srp/7", created=True, message="Created.")
        with mock.patch.object(operations_module, "create_srp_link", return_value=result):
            self.retry("retry_srp")

        entry = AuditLog.objects.get(action="fleet.retry_srp")
        self.assertEqual(entry.new_value, {"step": "srp_link", "status": ActionStatus.SUCCESS, "message": "Created."})

    def test_refused_retry_is_not_audited(self):
        other = f.create_user(perms=f.FC_PERMS)
        self.client.force_login(other)

        self.assertEqual(self.retry("retry_ping").status_code, 404)
        self.assertFalse(AuditLog.objects.filter(action__startswith="fleet.retry").exists())


class TrackingAuditTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc)
        f.settings(auto_end_enabled=False)

    def poll(self, outcome=None):
        """One poll; ``outcome`` is the exception the fleet read raises, None for a good read."""

        def sync(operation):
            if outcome is not None:
                raise outcome
            FleetOperation.objects.filter(pk=operation.pk).update(last_error="", last_esi_update=timezone.now())
            operation.last_error = ""
            return 3

        with mock.patch("fleetops.tasks.sync_operation", side_effect=sync):
            return track_operation(self.operation.pk)

    def entries(self):
        return list(AuditLog.objects.filter(action__startswith="fleet.tracking").order_by("pk"))

    def test_only_the_transitions_are_audited(self):
        failure = FleetESIError("ESI_ERROR", "Could not read fleet members (HTTP 502).")
        self.assertEqual(self.poll(), 3)
        self.assertEqual(self.poll(failure), "ESI_ERROR")
        self.assertEqual(self.poll(failure), "ESI_ERROR")
        self.assertEqual(self.poll(), 3)
        self.assertEqual(self.poll(), 3)

        error, recovered = self.entries()
        self.assertEqual((error.action, error.actor), ("fleet.tracking_error", None))
        self.assertEqual(error.object_id, str(self.operation.pk))
        self.assertEqual(error.new_value, {"code": "ESI_ERROR", "message": "Could not read fleet members (HTTP 502)."})
        self.assertEqual((recovered.action, recovered.actor), ("fleet.tracking_recovered", None))
        self.assertEqual(recovered.old_value, {"last_error": "Could not read fleet members (HTTP 502)."})
        self.assertEqual(recovered.new_value, {"members": 3})

    def test_unexpected_errors_keep_only_the_error_class(self):
        with self.assertLogs("extensions.fleetops", "ERROR"):
            self.assertEqual(self.poll(RuntimeError(f"boom {WEBHOOK_URL}")), "error")

        (entry,) = self.entries()
        self.assertEqual(entry.new_value, {"code": "ERROR", "message": "Fleet tracking failed unexpectedly (RuntimeError)."})

    def test_unreadable_fleet_polls_count_as_failures(self):
        missing = FleetESIError("FLEET_NOT_FOUND", "Fleet not found.")
        observation = UnreadableFleet(FleetOperation.EndReason.FLEET_GONE)
        with mock.patch("fleetops.tasks.observe_unreadable_fleet", return_value=observation):
            self.assertEqual(self.poll(missing), "FLEET_NOT_FOUND")
            self.assertEqual(self.poll(missing), "FLEET_NOT_FOUND")
        with mock.patch("fleetops.tasks.observe_unreadable_fleet", return_value=None):
            self.assertEqual(self.poll(missing), "FLEET_CHECK_FAILED")

        (entry,) = self.entries()
        self.assertEqual(entry.new_value["code"], "FLEET_NOT_FOUND")
        self.assertEqual(entry.new_value["message"], "The fleet could not be read: it no longer exists or the FC left it.")

    def test_a_failed_tracking_start_recovers_on_the_first_good_poll(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(last_error="Fleet tracking failed unexpectedly (KeyError).")

        self.poll()

        (entry,) = self.entries()
        self.assertEqual(entry.action, "fleet.tracking_recovered")

    def test_inactive_fleets_add_nothing(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.CLOSED)

        self.assertEqual(self.poll(FleetESIError("ESI_ERROR", "x")), "inactive")
        self.assertEqual(self.entries(), [])


# ---------------------------------------------------------------------------
# #30 incentive period form
# ---------------------------------------------------------------------------


class IncentivePeriodPageTests(TestCase):
    def setUp(self):
        f.settings(incentive_enabled=True, incentive_minimum_fleets=4)
        self.manager = f.create_user(perms=f.FC_LEAD_PERMS)
        self.client.force_login(self.manager)

    def page_url(self, year=2026, month=3):
        return reverse("fleetops:incentive_review") + f"?year={year}&month={month}"

    def create(self, **data):
        return self.client.post(self.page_url(), {"create_period": "1", **data})

    def test_create_form_has_only_budget_and_minimum(self):
        response = self.client.get(self.page_url())

        form = response.context["form"]
        self.assertEqual(list(form.fields), ["budget", "minimum_fleets"])
        self.assertEqual(form.initial["minimum_fleets"], 4)
        self.assertContains(response, "Create 2026-03")
        self.assertContains(response, 'action="/fleetops/incentives/?year=2026&amp;month=3"')

    def test_create_takes_the_month_from_the_page(self):
        response = self.create(year=2025, month=7, budget=5_000_000, minimum_fleets=2)

        period = IncentivePeriod.objects.get()
        self.assertEqual((period.year, period.month, period.budget, period.minimum_fleets), (2026, 3, 5_000_000, 2))
        self.assertRedirects(response, self.page_url(), fetch_redirect_response=False)
        entry = AuditLog.objects.get(action="incentive.period_create")
        self.assertEqual(entry.new_value, {"budget": 5_000_000, "minimum_fleets": 2})

    def test_create_needs_the_month_of_the_page(self):
        for query in ("", "?year=2026", "?year=99999&month=1", "?year=2026&month=13"):
            with self.subTest(query=query):
                response = self.client.post(
                    reverse("fleetops:incentive_review") + query,
                    {"create_period": "1", "year": 2026, "month": 3, "budget": 1, "minimum_fleets": 1},
                )

                self.assertRedirects(response, reverse("fleetops:incentive_review"), fetch_redirect_response=False)
                self.assertIn("Choose the month of the new incentive period first.", flashed(response))
        self.assertFalse(IncentivePeriod.objects.exists())

    def test_create_errors_are_shown(self):
        response = self.create(budget=-1, minimum_fleets=0)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ensure this value is greater than or equal to 0.")
        self.assertContains(response, "Ensure this value is greater than or equal to 1.")
        self.assertFalse(IncentivePeriod.objects.exists())

    def test_create_race_returns_the_existing_period(self):
        IncentivePeriod.objects.create(year=2026, month=3, budget=7)
        real_first = QuerySet.first
        looked_up = []

        def not_there_yet(queryset):
            # Another manager creates the month right after this request looked for it.
            if queryset.model is IncentivePeriod and not looked_up:
                looked_up.append(True)
                return None
            return real_first(queryset)

        with mock.patch.object(QuerySet, "first", not_there_yet):
            response = self.create(budget=1, minimum_fleets=1)

        self.assertRedirects(response, self.page_url(), fetch_redirect_response=False)
        self.assertEqual(IncentivePeriod.objects.get().budget, 7)
        self.assertIn("was created meanwhile", flashed(response)[0])
        self.assertFalse(AuditLog.objects.filter(action="incentive.period_create").exists())

    def test_create_for_an_existing_month_changes_nothing(self):
        IncentivePeriod.objects.create(year=2026, month=3, budget=7)

        response = self.create(budget=1, minimum_fleets=1)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(IncentivePeriod.objects.get().budget, 7)
        self.assertIn("already exists", flashed(response)[0])

    def test_edit_budget_and_minimum(self):
        period = IncentivePeriod.objects.create(year=2026, month=3, budget=100, minimum_fleets=3)
        page = self.client.get(self.page_url())
        self.assertContains(page, "Edit budget / minimum fleets")

        response = self.client.post(
            reverse("fleetops:incentive_update", args=[period.pk]), {"budget": 250, "minimum_fleets": 5}
        )

        self.assertRedirects(response, self.page_url(), fetch_redirect_response=False)
        period.refresh_from_db()
        self.assertEqual((period.budget, period.minimum_fleets), (250, 5))
        entry = AuditLog.objects.get(action="incentive.period_update")
        self.assertEqual(entry.old_value, {"budget": 100, "minimum_fleets": 3})
        self.assertEqual(entry.new_value, {"budget": 250, "minimum_fleets": 5})
        self.assertEqual(flashed(response), ["Budget and minimum fleets saved. Recalculate to apply them."])

    def test_edit_errors_are_shown_and_nothing_changes(self):
        period = IncentivePeriod.objects.create(year=2026, month=3, budget=100, minimum_fleets=3)

        response = self.client.post(
            reverse("fleetops:incentive_update", args=[period.pk]), {"budget": 999, "minimum_fleets": 0}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ensure this value is greater than or equal to 1.")
        self.assertContains(response, "<strong>100 ISK</strong>", html=True)
        period.refresh_from_db()
        self.assertEqual((period.budget, period.minimum_fleets), (100, 3))
        self.assertFalse(AuditLog.objects.filter(action="incentive.period_update").exists())

    def test_finalized_periods_are_not_editable(self):
        period = IncentivePeriod.objects.create(
            year=2026, month=3, budget=100, minimum_fleets=3, status=IncentivePeriod.Status.FINALIZED
        )
        page = self.client.get(self.page_url())
        self.assertNotContains(page, "Edit budget / minimum fleets")

        response = self.client.post(
            reverse("fleetops:incentive_update", args=[period.pk]), {"budget": 250, "minimum_fleets": 5}
        )

        self.assertIn("Unlock it before changing", flashed(response)[0])
        period.refresh_from_db()
        self.assertEqual(period.budget, 100)

    def test_update_needs_manage_incentives(self):
        period = IncentivePeriod.objects.create(year=2026, month=3)
        self.client.force_login(f.create_user(perms=f.FC_PERMS))

        response = self.client.post(reverse("fleetops:incentive_update", args=[period.pk]), {"budget": 1})

        self.assertEqual(response.status_code, 403)

    def test_pending_multipliers_and_not_counted_fleets_are_shown(self):
        start = datetime(2026, 3, 10, 18, 0, tzinfo=dt_timezone.utc)
        fc = f.create_user(perms=f.FC_PERMS)
        waiting = f.create_operation(fc, status=Status.CLOSED, started_at=start)
        excluded = f.create_operation(fc, status=Status.CLOSED, started_at=start)
        f.create_operation(fc, status=Status.CLOSED, started_at=start)

        with mock.patch(
            "fleetops.views.pending_multiplier_operations",
            return_value=FleetOperation.objects.filter(pk=waiting.pk),
        ), mock.patch(
            "fleetops.views.counted_operations", side_effect=lambda qs, **kwargs: qs.exclude(pk=excluded.pk)
        ):
            response = self.client.get(self.page_url())

        self.assertContains(response, "1 fleet in this month still waits for the FC")
        self.assertContains(response, reverse("fleetops:operation_detail", args=[waiting.uuid]))
        self.assertContains(response, "1 closed fleet in this month is not counted")


# ---------------------------------------------------------------------------
# #31 doctrines Fittings shows the FC
# ---------------------------------------------------------------------------


def _row_matches(row, condition):
    """Evaluate the doctrine filter on a fake row, like the database would."""
    if isinstance(condition, Q):
        results = [_row_matches(row, child) for child in condition.children]
        matched = any(results) if condition.connector == Q.OR else all(results)
        return not matched if condition.negated else matched
    lookup, value = condition
    if lookup == "category__isnull":
        return (not row.categories) == value
    if lookup == "category__groups__isnull":
        return (not row.categories or any(not groups for groups in row.categories)) == value
    if lookup == "category__groups__in":
        wanted = {group.pk for group in value}
        return any(groups & wanted for groups in row.categories)
    raise AssertionError(f"unexpected lookup {lookup}")


class FakeDoctrines(list):
    def distinct(self):
        return self


def fittings_models(rows, *, filter_error=None, category_layout=True):
    """A Fittings-like Doctrine model whose rows carry their categories as sets of group ids."""
    category = type("Category", (), {"_meta": SimpleNamespace(get_field=lambda name: SimpleNamespace(many_to_many=True, related_model=Group))})
    fields = [SimpleNamespace(name="id"), SimpleNamespace(name="name")]
    if category_layout:
        fields.append(SimpleNamespace(name="category", many_to_many=True, related_model=category))

    def filter_rows(condition):
        if filter_error is not None:
            raise filter_error
        return FakeDoctrines(row for row in rows if _row_matches(row, condition))

    meta = SimpleNamespace(label_lower="fittings.doctrine", get_fields=lambda: fields)
    objects = SimpleNamespace(all=lambda: FakeDoctrines(rows), filter=filter_rows)
    return type("Doctrine", (), {"_meta": meta, "objects": objects})


def doctrine(pk, name, *categories):
    return SimpleNamespace(pk=pk, name=name, categories=[set(groups) for groups in categories])


class DoctrineVisibilityTests(TestCase):
    def setUp(self):
        self.logi = Group.objects.create(name="Logi Pilots")
        self.capitals = Group.objects.create(name="Capital Pilots")
        self.rows = [
            doctrine(1, "Public Ferox"),
            doctrine(2, "Open Category Moa", []),
            doctrine(3, "Logi Guardians", [self.logi.pk]),
            doctrine(4, "Capital Nagas", [self.capitals.pk]),
            doctrine(5, "Mixed Muninns", [self.capitals.pk], [self.logi.pk]),
        ]
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.fc.groups.add(self.logi)

    @contextmanager
    def fittings(self, model):
        config = SimpleNamespace(name="fittings", label="fittings", get_models=lambda: [model])
        real = list(django_apps.get_app_configs())
        with mock.patch.object(doctrines_module.apps, "get_app_configs", return_value=real + [config]):
            yield

    def names(self, user, **options):
        with self.fittings(fittings_models(self.rows, **options)):
            return [choice.name for choice in get_doctrines(user)]

    def test_matrix(self):
        everything = ["Capital Nagas", "Logi Guardians", "Mixed Muninns", "Open Category Moa", "Public Ferox"]
        manager = f.create_user(perms=f.FC_PERMS)
        manager.is_superuser = True
        manager.save()
        outsider = f.create_user(perms=f.FC_PERMS)

        self.assertEqual(self.names(None), everything)
        self.assertEqual(self.names(manager), everything)
        self.assertEqual(self.names(self.fc), ["Logi Guardians", "Mixed Muninns", "Open Category Moa", "Public Ferox"])
        self.assertEqual(self.names(outsider), ["Open Category Moa", "Public Ferox"])

    def test_fittings_manage_sees_everything(self):
        with mock.patch.object(type(self.fc), "has_perm", lambda user, perm, obj=None: perm == "fittings.manage"):
            self.assertEqual(len(self.names(self.fc)), 5)

    def test_without_fittings_there_are_no_doctrines(self):
        self.assertEqual(get_doctrines(self.fc), [])

    def test_unknown_layout_or_failing_query_lists_everything(self):
        self.assertEqual(len(self.names(self.fc, category_layout=False)), 5)
        self.assertEqual(len(self.names(self.fc, filter_error=RuntimeError("no such column"))), 5)

    def test_start_form_offers_and_accepts_only_visible_doctrines(self):
        f.add_token(self.fc, f.main_of(self.fc))
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fc_character_id": str(f.main_of(self.fc).character_id),
            "fleet_type": str(f.fleet_type().pk),
            "formup": "Jita",
        }
        with self.fittings(fittings_models(self.rows)):
            offered = [label for value, label in StartFleetForm(user=self.fc).fields["doctrine_choice"].choices]
            visible = StartFleetForm({**data, "doctrine_choice": "fittings:fittings.doctrine|3|Logi Guardians"}, user=self.fc)
            hidden = StartFleetForm({**data, "doctrine_choice": "fittings:fittings.doctrine|4|Capital Nagas"}, user=self.fc)

            self.assertTrue(visible.is_valid(), visible.errors)
            self.assertFalse(hidden.is_valid())
        self.assertNotIn("Capital Nagas", offered)
        self.assertIn("Logi Guardians", offered)
        self.assertIn("doctrine_choice", hidden.errors)
