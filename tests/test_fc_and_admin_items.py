# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FC and administration items: poll cadence, write-only webhook URLs, start/retry/tracking audits,
the incentive period form and the doctrines Fittings shows each FC."""

import json
import uuid
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

import requests
from django.apps import apps as django_apps
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db.models import Q
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.forms import StartFleetForm
from fleetops.models import (
    AuditLog,
    FleetOperation,
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
from .link_fakes import setUpModule, tearDownModule  # noqa: F401 - unittest runs them around the module

Status = FleetOperation.Status
ActionStatus = OperationAction.Status
FLEET_ID = 1_047_200_000_001
WEBHOOK_TOKEN = "Kq3-webhook-token-never-shown"
WEBHOOK_URL = f"https://discord.com/api/webhooks/445566/{WEBHOOK_TOKEN}"


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
