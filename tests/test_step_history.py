# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Attempt history of operation steps, the 0007 data migration and the extension loggers."""

import logging
from datetime import timedelta
from importlib import import_module

from django.apps import apps
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from esi.exceptions import HTTPClientError, HTTPServerError

from fleetops import tasks, views
from fleetops.models import FleetMemberState, FleetOperation, OperationAction, OperationActionAttempt
from fleetops.providers import esi as esi_provider
from fleetops.services import fleet_controls, operations, tracking
from fleetops.services.operations import retry_motd, retry_ping, set_action

from . import factories as f
from .test_operations_tracking import FLEET_ID, FakeESIMixin, http_error

Status = OperationAction.Status
migration_0007 = import_module("fleetops.migrations.0007_end_reasons_and_step_history")


def history(step):
    return list(step.history.values_list("status", "message"))


class SetActionHistoryTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc)

    def test_every_attempt_is_kept_latest_first(self):
        set_action(self.operation, "motd_update", False, "ESI returned HTTP 503 (Service unavailable).")
        set_action(self.operation, "motd_update", False, "ESI returned HTTP 502 (Bad gateway).")
        step = set_action(self.operation, "motd_update", True)

        self.assertEqual(step.attempts, 3)
        self.assertEqual(step.status, Status.SUCCESS)
        self.assertEqual(step.error_message, "")
        self.assertEqual(
            history(step),
            [
                (Status.SUCCESS, ""),
                (Status.FAILED, "ESI returned HTTP 502 (Bad gateway)."),
                (Status.FAILED, "ESI returned HTTP 503 (Service unavailable)."),
            ],
        )

    def test_skipped_step_is_recorded_once(self):
        operation = f.create_operation(self.fc, send_ping=False)

        retry_ping(operation)
        step = retry_ping(operation)

        self.assertEqual(step.status, Status.SKIPPED)
        self.assertEqual(step.history.count(), 1)

    def test_attempts_go_with_the_operation(self):
        set_action(self.operation, "discord_ping", False, "No webhook.")

        self.operation.delete()

        self.assertFalse(OperationActionAttempt.objects.exists())


class StepHistoryPageTests(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.operation = f.create_operation(self.fc, esi_fleet_id=FLEET_ID, motd_text="Form up in Jita")
        self.esi.fleet_info[FLEET_ID] = {"is_free_move": False, "motd": ""}
        self.client.force_login(self.fc)

    def detail(self):
        return self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))

    def test_failure_reason_stays_visible_after_a_successful_retry(self):
        self.esi.put_outcome = http_error(503)
        retry_motd(self.operation)
        self.esi.put_outcome = None
        retry_motd(self.operation)

        page = self.detail()

        step = next(a for a in page.context["actions"] if a.action == "motd_update")
        self.assertEqual(step.status, Status.SUCCESS)
        self.assertEqual(
            [(a.status, a.message) for a in step.previous_attempts],
            [(Status.FAILED, "Could not update fleet MOTD: ESI returned HTTP 503 (Service unavailable).")],
        )
        self.assertContains(page, "Previous attempts")
        self.assertContains(page, "Could not update fleet MOTD: ESI returned HTTP 503 (Service unavailable).")

    def test_only_recent_attempts_are_listed(self):
        for number in range(8):
            set_action(self.operation, "discord_ping", False, f"failure {number}")

        page = self.detail()

        step = next(a for a in page.context["actions"] if a.action == "discord_ping")
        self.assertEqual([a.message for a in step.previous_attempts], [f"failure {n}" for n in (6, 5, 4, 3, 2)])
        self.assertEqual(step.older_attempt_count, 2)
        self.assertContains(page, "and 2 older attempts")

    def test_single_attempt_has_no_history_block(self):
        set_action(self.operation, "discord_ping", True)

        self.assertNotContains(self.detail(), "Previous attempts")


class MigrationBackfillTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)

    def test_latest_result_of_existing_steps_becomes_the_first_attempt(self):
        operation = f.create_operation(self.fc)
        executed = timezone.now() - timedelta(days=3)
        step = OperationAction.objects.create(
            operation=operation, action="motd_update", status=Status.FAILED, error_message="Old failure", attempts=2
        )
        OperationAction.objects.filter(pk=step.pk).update(executed_at=executed)

        migration_0007.keep_latest_step_results(apps, None)

        attempt = OperationActionAttempt.objects.get(action=step)
        self.assertEqual((attempt.status, attempt.message, attempt.created_at), (Status.FAILED, "Old failure", executed))

    def test_ended_fleets_get_a_reason_and_lose_their_active_members(self):
        manual = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED)
        automatic = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED)
        recorded = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED, is_manual=True)
        active = f.create_operation(self.fc)
        OperationAction.objects.create(operation=manual, action="fleet_end", status=Status.SUCCESS, error_message="Manual")
        OperationAction.objects.create(
            operation=automatic, action="fleet_end", status=Status.SUCCESS, error_message="Automatic"
        )
        now = timezone.now()
        for operation in (automatic, active):
            FleetMemberState.objects.create(
                operation=operation, character_id=1, character_name="Pilot", first_seen=now, last_seen=now
            )

        migration_0007.label_ended_fleets(apps, None)

        reasons = dict(FleetOperation.objects.values_list("pk", "end_reason"))
        self.assertEqual(reasons[manual.pk], "manual")
        self.assertEqual(reasons[automatic.pk], "fleet_gone")
        self.assertEqual(reasons[recorded.pk], "")
        self.assertEqual(reasons[active.pk], "")
        left = FleetMemberState.objects.get(operation=automatic)
        self.assertFalse(left.is_active)
        self.assertEqual(left.left_at, automatic.ended_at)
        self.assertTrue(FleetMemberState.objects.get(operation=active).is_active)


LEGACY_COOKIE = "__cf_bm=legacy-cookie-value"
LEGACY_HEADERS = {"date": "Sun, 11 Oct 2026 19:21:00 GMT", "x-esi-request-id": "legacy-request", "set-cookie": LEGACY_COOKIE}


def legacy_text(prefix, exc):
    """A step message or fleet error as 0.1.0a5 stored it: the django-esi exception text."""
    return f"{prefix}: {exc}"


class LegacyErrorTextTests(FakeESIMixin, TestCase):
    """0.1.0a5 stored django-esi's exception text, with every response header; 0007 shortens it."""

    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)

    def test_legacy_texts_are_shortened(self):
        forbidden = HTTPClientError(status_code=403, headers=LEGACY_HEADERS, data=None)
        server_error = HTTPServerError(status_code=500, headers=LEGACY_HEADERS, data={"error": "Internal error occurred"})
        pydantic_style = (
            "<HTTPServerError 503 details=None error='Service unavailable' status=None "
            f"Headers({LEGACY_HEADERS!r})>"
        )
        cases = [
            (legacy_text("Could not update fleet MOTD", forbidden), "Could not update fleet MOTD: ESI returned HTTP 403 (Forbidden)."),
            (
                legacy_text("Could not read fleet members", server_error),
                "Could not read fleet members: ESI returned HTTP 500 (Internal error occurred).",
            ),
            (
                legacy_text("Could not detect fleet", pydantic_style),
                "Could not detect fleet: ESI returned HTTP 503 (Service unavailable).",
            ),
            (
                # Cut at 4000 characters before the headers ended.
                legacy_text("Could not detect fleet", pydantic_style)[:120],
                "Could not detect fleet: ESI returned HTTP 503 (Service unavailable).",
            ),
            (
                "Kicked 0 capsule member(s). 1 failed. "
                + str([{"character": "Pod", "error": legacy_text("Could not kick fleet member 5", forbidden)}]),
                "Kicked 0 capsule member(s). 1 failed. "
                """[{'character': 'Pod', 'error': "Could not kick fleet member 5: ESI returned HTTP 403 (Forbidden)."}]""",
            ),
            ("Could not read fleet members: odd Headers({'a': 'b'}) text", "Could not read fleet members: ESI returned an error (details not kept)."),
        ]
        for text, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(migration_0007.clean_esi_error(text), expected)

    def test_current_texts_are_kept(self):
        for text in (
            "",
            "Manual",
            "Could not update fleet MOTD: ESI returned HTTP 403 (Forbidden).",
            "No active webhook configured. Ping remains available for manual copy.",
        ):
            with self.subTest(text=text):
                self.assertEqual(migration_0007.clean_esi_error(text), text)

    def test_legacy_text_is_gone_from_the_page_after_a_successful_retry(self):
        old_error = legacy_text("Could not update fleet MOTD", HTTPClientError(status_code=403, headers=LEGACY_HEADERS, data=None))
        operation = f.create_operation(
            self.fc,
            esi_fleet_id=FLEET_ID,
            motd_text="Form up in Jita",
            last_error=legacy_text("Could not read fleet members", HTTPServerError(status_code=503, headers=LEGACY_HEADERS, data=None)),
        )
        step = OperationAction.objects.create(
            operation=operation, action="motd_update", status=Status.FAILED, error_message=old_error
        )
        executed = timezone.now() - timedelta(days=3)
        OperationAction.objects.filter(pk=step.pk).update(executed_at=executed)
        FleetOperation.objects.filter(pk=operation.pk).update(updated_at=executed)

        migration_0007.keep_latest_step_results(apps, None)
        migration_0007.clean_fleet_errors(apps, None)

        step.refresh_from_db()
        operation.refresh_from_db()
        self.assertEqual((step.error_message, step.executed_at), ("Could not update fleet MOTD: ESI returned HTTP 403 (Forbidden).", executed))
        self.assertEqual(history(step), [(Status.FAILED, "Could not update fleet MOTD: ESI returned HTTP 403 (Forbidden).")])
        self.assertEqual(
            (operation.last_error, operation.updated_at),
            ("Could not read fleet members: ESI returned HTTP 503 (Service unavailable).", executed),
        )

        self.esi.fleet_info[FLEET_ID] = {"is_free_move": False, "motd": ""}
        self.assertEqual(retry_motd(operation).status, Status.SUCCESS)
        self.client.force_login(self.fc)
        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

        self.assertContains(page, "Previous attempts")
        self.assertContains(page, "Could not update fleet MOTD: ESI returned HTTP 403 (Forbidden).")
        self.assertNotContains(page, "Headers(")
        self.assertNotContains(page, LEGACY_COOKIE)
        self.assertNotContains(page, "legacy-request")


class ExtensionLoggerTests(TestCase):
    def test_fleetops_modules_log_through_the_alliance_auth_extensions_logger(self):
        for module in (operations, tracking, fleet_controls, tasks, views, esi_provider):
            with self.subTest(module=module.__name__):
                self.assertIs(module.logger, logging.getLogger(f"extensions.{module.__name__}"))

    def test_records_reach_the_extensions_logger(self):
        fc = f.create_user(perms=f.FC_PERMS)
        operation = f.create_operation(fc)

        with self.assertLogs("extensions", "ERROR") as logs:
            operations.report_failure(operations.logger, "MOTD update", RuntimeError("boom"), operation)

        self.assertIn(f"MOTD update failed for operation {operation.pk} (RuntimeError)", logs.output[0])
