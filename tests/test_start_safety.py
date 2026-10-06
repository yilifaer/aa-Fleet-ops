"""Regression tests for fleet start robustness, ping error secrecy, retries and SRP links."""

import uuid
from types import SimpleNamespace
from unittest import mock

import requests
from allianceauth.eveonline.models import EveCharacter
from django.contrib.messages import get_messages
from django.db import models
from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.urls import include, path, reverse

from fleetops.models import (
    AuditLog,
    DiscordWebhook,
    FleetOperation,
    MessageTemplate,
    OperationAction,
    PingTarget,
    validate_template_syntax,
)
from fleetops.providers import srp as srp_module
from fleetops.providers.esi import FleetDetectionResult, FleetESIError
from fleetops.providers.pings import _redact, send_discord_webhook
from fleetops.providers.srp import AllianceAuthBuiltinSRPProvider, SRPLinkResult, register_srp_provider
from fleetops.services.messages import render_messages, render_operation_messages
from fleetops.services.operations import end_fleet, retry_motd, retry_ping, retry_srp, start_fleet

from . import factories as f

Status = FleetOperation.Status
ActionStatus = OperationAction.Status
PING = MessageTemplate.TemplateType.PING
MOTD = MessageTemplate.TemplateType.MOTD

FLEET_ID = 1_046_000_000_001
WEBHOOK_TOKEN = "Zq7-start-safety-webhook-token"
WEBHOOK_URL = f"https://discord.com/api/webhooks/555000111/{WEBHOOK_TOKEN}"


def statuses(operation):
    return dict(OperationAction.objects.filter(operation=operation).values_list("action", "status"))


def action(operation, name):
    return OperationAction.objects.get(operation=operation, action=name)


def make_template(content, template_type=PING, **extra):
    return MessageTemplate.objects.create(
        name=f"Template {uuid.uuid4().hex[:6]}", template_type=template_type, content=content, **extra
    )


def flashed(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


# ---------------------------------------------------------------------------
# Discord webhook errors
# ---------------------------------------------------------------------------


class DiscordWebhookErrorTests(TestCase):
    def setUp(self):
        patcher = mock.patch("fleetops.providers.pings.requests.post")
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def test_connection_error_text_with_webhook_path_is_not_returned(self):
        self.post.side_effect = requests.ConnectionError(
            "HTTPSConnectionPool(host='discord.com', port=443): Max retries exceeded with url: "
            f"/api/webhooks/555000111/{WEBHOOK_TOKEN} (Caused by NewConnectionError('refused'))"
        )

        result = send_discord_webhook(WEBHOOK_URL, "ping")

        self.assertFalse(result.success)
        self.assertNotIn(WEBHOOK_TOKEN, result.message)
        self.assertIn("ConnectionError", result.message)

    def test_url_without_scheme_is_not_sent(self):
        result = send_discord_webhook(f"discord.com/api/webhooks/555000111/{WEBHOOK_TOKEN}", "ping")

        self.assertFalse(result.success)
        self.assertEqual(
            result.message, "The configured webhook URL is not a Discord webhook URL, so nothing was sent."
        )
        self.post.assert_not_called()

    def test_documented_discord_webhook_urls_are_sent_to(self):
        self.post.return_value = mock.Mock(status_code=204, text="")
        for url in (
            WEBHOOK_URL,
            f"{WEBHOOK_URL}/",
            f"https://discord.com/api/v10/webhooks/555000111/{WEBHOOK_TOKEN}",
            f"https://canary.discordapp.com/api/webhooks/555000111/{WEBHOOK_TOKEN}?wait=true&thread_id=1",
        ):
            with self.subTest(url=url):
                self.post.reset_mock()

                self.assertTrue(send_discord_webhook(url, "ping").success)
                self.assertEqual(self.post.call_args.args, (url,))

    def test_anything_else_is_never_sent_to(self):
        for url in (
            "http://127.0.0.1:8080/internal",
            "https://example.com/api/webhooks/555000111/token",
            f"http://discord.com/api/webhooks/555000111/{WEBHOOK_TOKEN}",
            f"{WEBHOOK_URL}/slack",
            f"https://discord.com/api/webhooks/{WEBHOOK_TOKEN}",
        ):
            with self.subTest(url=url):
                result = send_discord_webhook(url, "ping")

                self.assertFalse(result.success)
                self.assertNotIn(WEBHOOK_TOKEN, result.message)
        self.post.assert_not_called()

    def test_error_body_echoing_the_bare_token_is_redacted(self):
        self.post.return_value = mock.Mock(
            status_code=401, text=f'{{"message": "Invalid Webhook Token {WEBHOOK_TOKEN}"}}'
        )
        for url in (
            WEBHOOK_URL,
            f"{WEBHOOK_URL}?wait=true",
            f"https://discord.com/api/v10/webhooks/555000111/{WEBHOOK_TOKEN}/",
        ):
            with self.subTest(url=url):
                result = send_discord_webhook(url, "ping")

                self.assertIn("Invalid Webhook Token ***", result.message)
                self.assertNotIn(WEBHOOK_TOKEN, result.message)

    def test_redaction_takes_the_token_after_the_webhook_id(self):
        text = _redact(f"bad token {WEBHOOK_TOKEN} via slack", f"{WEBHOOK_URL}/slack")

        self.assertEqual(text, "bad token *** via slack")

    def test_timeout_names_the_error_only(self):
        self.post.side_effect = requests.ReadTimeout(f"{WEBHOOK_URL}: read timed out (read timeout=15)")

        result = send_discord_webhook(WEBHOOK_URL, "ping")

        self.assertNotIn(WEBHOOK_TOKEN, result.message)
        self.assertIn("ReadTimeout", result.message)

    def test_error_body_echoing_the_webhook_is_redacted(self):
        self.post.return_value = mock.Mock(status_code=404, text=f'{{"message": "Unknown Webhook {WEBHOOK_URL}"}}')

        result = send_discord_webhook(WEBHOOK_URL, "ping")

        self.assertEqual(result.status_code, 404)
        self.assertIn("Unknown Webhook", result.message)
        self.assertNotIn(WEBHOOK_TOKEN, result.message)

    def test_empty_error_body_reports_the_status(self):
        self.post.return_value = mock.Mock(status_code=401, text="")

        result = send_discord_webhook(WEBHOOK_URL, "ping")

        self.assertEqual(result.message, "Discord returned HTTP 401.")


# ---------------------------------------------------------------------------
# Ping / MOTD rendering
# ---------------------------------------------------------------------------


class SafeMessageRenderingTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Render FC")

    def test_invalid_selected_template_falls_back_to_built_in_default(self):
        broken = make_template("{% if %}broken")
        operation = f.create_operation(self.fc, ping_template=broken, formup="Amarr")

        rendered = render_messages(operation)

        self.assertIn("FC: Render FC", rendered.ping)
        self.assertIn("Form Up: Amarr", rendered.ping)
        self.assertEqual(len(rendered.errors), 1)
        self.assertIn(broken.name, rendered.errors[0])
        self.assertIn("TemplateSyntaxError", rendered.errors[0])

    def test_template_failing_while_rendering_falls_back(self):
        broken = make_template('{% include "fleetops/no-such-template.txt" %}')
        operation = f.create_operation(self.fc, ping_template=broken)

        ping, _motd = render_operation_messages(operation)

        self.assertIn("FC: Render FC", ping)

    def test_broken_default_motd_only_affects_the_motd(self):
        MessageTemplate.objects.filter(template_type=MOTD).update(is_active=False)
        make_template("{{ fc|no_such_filter }}", MOTD, is_default=True)
        operation = f.create_operation(self.fc, ping_template=make_template("PING {{ formup }}"), formup="Dodixie")

        rendered = render_messages(operation)

        self.assertEqual(rendered.ping, "PING Dodixie")
        self.assertTrue(rendered.motd.startswith("<b>"))
        self.assertEqual(len(rendered.errors), 1)
        self.assertTrue(rendered.errors[0].startswith("MOTD template"))

    def test_working_templates_report_no_errors(self):
        operation = f.create_operation(self.fc)

        self.assertEqual(render_messages(operation).errors, [])

    def test_script_tags_are_escaped_in_any_letter_case(self):
        motd = make_template("<ScRiPt>a()</sCrIpT><SCRIPT src=x></SCRIPT>", MOTD)
        operation = f.create_operation(self.fc, motd_template=motd)

        _ping, text = render_operation_messages(operation)

        self.assertNotIn("<script", text.lower())
        self.assertNotIn("</script", text.lower())
        self.assertIn("&lt;ScRiPt>", text)

    def preview(self, **extra):
        self.client.force_login(self.fc)
        self.client.raise_request_exception = False
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fc_character_id": str(f.main_of(self.fc).character_id),
            "fleet_type": str(f.fleet_type().pk),
            "formup": "Jita",
        }
        data.update(extra)
        return self.client.post(reverse("fleetops:preview_fleet"), data)

    def test_preview_warns_about_a_template_that_fails_while_rendering(self):
        content = f'{{% include "{WEBHOOK_URL}.txt" %}}'
        validate_template_syntax(content)  # compiles, so it passes the save-time check
        broken = make_template(content)

        response = self.preview(ping_template=str(broken.pk))

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertIn("FC: Render FC", body["ping"])
        self.assertEqual(len(body["warnings"]), 1)
        self.assertIn(broken.name, body["warnings"][0])
        self.assertIn("TemplateDoesNotExist", body["warnings"][0])
        self.assertNotIn(WEBHOOK_TOKEN, response.content.decode())

    def test_preview_without_template_problems_has_no_warnings(self):
        response = self.preview()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["warnings"], [])


# ---------------------------------------------------------------------------
# Fleet start
# ---------------------------------------------------------------------------


class StartFleetSafetyTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Start FC")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.fleet_type = f.fleet_type("StratOp", "1.00")
        webhook = DiscordWebhook.objects.create(name="Pings", webhook_url=WEBHOOK_URL)
        self.target = PingTarget.objects.create(name="Everyone", target_value="@everyone", webhook=webhook)
        patches = {
            "detect": ("detect_character_fleet", FleetDetectionResult(fleet_id=FLEET_ID, role="fleet_commander")),
            "motd": ("set_fleet_motd", None),
            "sync": ("sync_operation", 0),
            "srp": (
                "create_srp_link",
                SRPLinkResult("test_srp", reference="9", url="/srp/9/", created=True, message="SRP fleet created."),
            ),
        }
        self.mocks = {}
        for key, (name, value) in patches.items():
            patcher = mock.patch(f"fleetops.services.operations.{name}", return_value=value)
            self.mocks[key] = patcher.start()
            self.addCleanup(patcher.stop)
        post = mock.patch("fleetops.providers.pings.requests.post", return_value=mock.Mock(status_code=204, text=""))
        self.mocks["post"] = post.start()
        self.addCleanup(post.stop)

    def start(self, mode="full", request_id=None, **extra):
        data = {
            "operation_mode": mode,
            "fc_character_id": str(self.fc_char.character_id),
            "fleet_type": self.fleet_type,
            "formup": "Jita",
            "ping_target": self.target,
        }
        data.update(extra)
        return start_fleet(user=self.fc, cleaned_data=data, request_id=request_id or uuid.uuid4())

    def view_payload(self):
        return {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fc_character_id": str(self.fc_char.character_id),
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Jita",
            "ping_target": str(self.target.pk),
        }

    def assertActive(self, operation):
        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.ACTIVE)

    def assertLoggedSafely(self, logs, *expected, secrets=(WEBHOOK_TOKEN,)):
        """The log names the failure and has a traceback, but never the exception text."""
        log = "\n".join(logs.output)
        for text in expected:
            self.assertIn(text, log)
        self.assertIn("Traceback", log)
        for secret in secrets:
            self.assertNotIn(secret, log)
        # No exc_info either, or a handler would print the exception text.
        self.assertTrue(all(record.exc_info is None for record in logs.records))

    def test_broken_ping_template_falls_back_and_the_fleet_is_pinged(self):
        broken = MessageTemplate.objects.create(name="Typo Ping", template_type=PING, content="{% if %}x")

        operation = self.start(ping_template=broken)

        self.assertActive(operation)
        self.assertIn("FC: Start FC", operation.ping_text)
        self.mocks["post"].assert_called_once()
        self.assertEqual(self.mocks["post"].call_args.kwargs["json"], {"content": operation.ping_text})
        render = action(operation, "message_render")
        self.assertEqual(render.status, ActionStatus.FAILED)
        self.assertIn("Typo Ping", render.error_message)
        self.assertEqual(action(operation, "discord_ping").status, ActionStatus.SUCCESS)
        self.assertEqual(action(operation, "motd_update").status, ActionStatus.SUCCESS)

    def test_broken_templates_never_block_an_attendance_only_start(self):
        MessageTemplate.objects.update(content="{% if %}broken")
        make_template("{% if %}broken", is_default=True)

        operation = self.start(mode="attendance_only")

        self.assertActive(operation)
        self.assertEqual(
            statuses(operation),
            {
                "fleet_detection": ActionStatus.SUCCESS,
                "message_render": ActionStatus.FAILED,
                "discord_ping": ActionStatus.SKIPPED,
                "motd_update": ActionStatus.SKIPPED,
                "tracking_start": ActionStatus.SUCCESS,
                "srp_link": ActionStatus.SKIPPED,
            },
        )
        self.mocks["post"].assert_not_called()
        self.mocks["motd"].assert_not_called()

    def test_start_view_succeeds_with_a_broken_default_template(self):
        MessageTemplate.objects.update(content="{% if %}broken")
        self.client.force_login(self.fc)

        response = self.client.post(reverse("fleetops:start_fleet"), self.view_payload())

        operation = FleetOperation.objects.get()
        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False
        )
        self.assertEqual(operation.status, Status.ACTIVE)

    def test_start_view_keeps_unexpected_error_text_off_the_page_and_out_of_the_log(self):
        self.client.force_login(self.fc)
        error = RuntimeError(f"Failed posting to {WEBHOOK_URL}")

        with mock.patch("fleetops.views.start_fleet", side_effect=error), self.assertLogs(
            "fleetops.views", "ERROR"
        ) as logs:
            response = self.client.post(reverse("fleetops:start_fleet"), self.view_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"].non_field_errors(),
            ["Fleet start failed because of an unexpected error. Please try again or contact an administrator."],
        )
        self.assertNotContains(response, WEBHOOK_TOKEN)
        self.assertFalse(FleetOperation.objects.exists())
        self.assertLoggedSafely(logs, "Fleet start failed (RuntimeError)")

    def test_working_templates_record_no_render_action(self):
        operation = self.start()

        self.assertNotIn("message_render", statuses(operation))

    def test_unexpected_discord_error_is_recorded_without_its_text(self):
        with mock.patch(
            "fleetops.services.operations.send_discord_webhook", side_effect=ValueError(f"bad url {WEBHOOK_URL}")
        ), self.assertLogs("fleetops.services.operations", "ERROR") as logs:
            operation = self.start()

        self.assertActive(operation)
        ping = action(operation, "discord_ping")
        self.assertEqual(ping.status, ActionStatus.FAILED)
        self.assertNotIn(WEBHOOK_TOKEN, ping.error_message)
        self.assertIn("ValueError", ping.error_message)
        self.assertEqual(action(operation, "motd_update").status, ActionStatus.SUCCESS)
        self.assertEqual(action(operation, "tracking_start").status, ActionStatus.SUCCESS)
        self.assertEqual(action(operation, "srp_link").status, ActionStatus.SUCCESS)
        self.assertLoggedSafely(logs, f"Discord ping failed for operation {operation.pk} (ValueError)")

    def test_unexpected_motd_and_tracking_errors_are_recorded_safely(self):
        self.mocks["motd"].side_effect = KeyError("access-token-123")
        self.mocks["sync"].side_effect = TypeError("refresh-token-456")

        with self.assertLogs("fleetops.services.operations", "ERROR") as logs:
            operation = self.start()

        self.assertActive(operation)
        motd = action(operation, "motd_update")
        self.assertEqual(motd.status, ActionStatus.FAILED)
        self.assertNotIn("access-token-123", motd.error_message)
        self.assertIn("KeyError", motd.error_message)
        tracking = action(operation, "tracking_start")
        self.assertEqual(tracking.status, ActionStatus.FAILED)
        self.assertNotIn("refresh-token-456", operation.last_error)
        self.assertIn("TypeError", operation.last_error)
        self.assertEqual(tracking.error_message, operation.last_error)
        self.assertEqual(action(operation, "srp_link").status, ActionStatus.SUCCESS)
        self.assertLoggedSafely(
            logs,
            "MOTD update failed for operation",
            "Fleet tracking failed for operation",
            secrets=("access-token-123", "refresh-token-456"),
        )

    def test_esi_error_messages_are_kept_for_the_fc(self):
        message = "ESI denied permission to update the fleet MOTD."
        self.mocks["motd"].side_effect = FleetESIError("MOTD_FORBIDDEN", message)

        with self.assertNoLogs("fleetops", "ERROR"):
            operation = self.start()

        self.assertEqual(action(operation, "motd_update").error_message, message)

    def test_render_crash_skips_ping_and_motd_but_the_fleet_starts(self):
        with mock.patch("fleetops.services.operations.render_messages", side_effect=RuntimeError("boom")):
            operation = self.start()

        self.assertActive(operation)
        self.assertEqual(
            statuses(operation),
            {
                "fleet_detection": ActionStatus.SUCCESS,
                "message_render": ActionStatus.FAILED,
                "discord_ping": ActionStatus.FAILED,
                "motd_update": ActionStatus.FAILED,
                "tracking_start": ActionStatus.SUCCESS,
                "srp_link": ActionStatus.SUCCESS,
            },
        )
        self.mocks["post"].assert_not_called()
        self.mocks["motd"].assert_not_called()

    def test_webhook_saved_before_validation_is_never_posted_to(self):
        # objects.create() skips validation, like a row saved by an earlier release.
        legacy = DiscordWebhook.objects.create(name="Legacy", webhook_url="http://127.0.0.1:8080/internal")
        target = PingTarget.objects.create(name="Legacy target", webhook=legacy)

        operation = self.start(ping_target=target)
        retried = retry_ping(operation)

        self.assertActive(operation)
        self.mocks["post"].assert_not_called()
        for result in (action(operation, "discord_ping"), retried):
            self.assertEqual(result.status, ActionStatus.FAILED)
            self.assertEqual(
                result.error_message, "The configured webhook URL is not a Discord webhook URL, so nothing was sent."
            )

    def test_retry_after_a_render_failure_renders_the_messages_and_sends_them(self):
        with mock.patch("fleetops.services.operations.render_messages", side_effect=RuntimeError("boom")):
            operation = self.start()
        self.assertEqual((operation.ping_text, operation.motd_text), ("", ""))

        ping = retry_ping(operation)
        motd = retry_motd(operation)

        operation.refresh_from_db()
        self.assertIn("FC: Start FC", operation.ping_text)
        self.assertIn("FC: Start FC", operation.motd_text)
        self.assertEqual(ping.status, ActionStatus.SUCCESS)
        self.assertEqual(motd.status, ActionStatus.SUCCESS)
        self.mocks["post"].assert_called_once()
        self.assertEqual(self.mocks["post"].call_args.kwargs["json"], {"content": operation.ping_text})
        self.mocks["motd"].assert_called_once_with(self.fc, self.fc_char.character_id, FLEET_ID, operation.motd_text)
        self.assertEqual(action(operation, "message_render").status, ActionStatus.SUCCESS)

    def test_retry_sends_nothing_while_the_messages_still_cannot_be_rendered(self):
        with mock.patch("fleetops.services.operations.render_messages", side_effect=RuntimeError("boom")):
            operation = self.start()
            ping = retry_ping(operation)
            motd = retry_motd(operation)

        for result in (ping, motd):
            self.assertEqual(result.status, ActionStatus.FAILED)
            self.assertEqual(result.error_message, "Not sent because the fleet messages could not be rendered.")
        render = action(operation, "message_render")
        self.assertEqual(render.status, ActionStatus.FAILED)
        self.assertEqual(render.error_message, "Message rendering failed unexpectedly (RuntimeError).")
        self.assertEqual(render.attempts, 3)
        self.mocks["post"].assert_not_called()
        self.mocks["motd"].assert_not_called()
        operation.refresh_from_db()
        self.assertEqual((operation.ping_text, operation.motd_text), ("", ""))

    def test_database_error_inside_the_srp_provider_does_not_break_the_start(self):
        def write_then_fail(operation, provider_key):
            first = AuditLog.objects.create(action="srp.partial", object_type="SRP", object_id="1")
            AuditLog.objects.create(pk=first.pk, action="srp.duplicate", object_type="SRP", object_id="1")

        self.mocks["srp"].side_effect = write_then_fail

        with self.assertLogs("fleetops.services.operations", "ERROR"):
            operation = self.start()

        self.assertActive(operation)
        srp = action(operation, "srp_link")
        self.assertEqual(srp.status, ActionStatus.FAILED)
        self.assertEqual(srp.error_message, "SRP creation failed unexpectedly (IntegrityError).")
        self.assertFalse(AuditLog.objects.filter(action__startswith="srp.").exists())

    def test_settings_lookup_failure_is_recorded_as_srp_failure(self):
        with mock.patch(
            "fleetops.services.operations.FleetOpsSettings.get_solo", side_effect=RuntimeError("settings unavailable")
        ):
            operation = self.start()

        self.assertActive(operation)
        self.assertEqual(action(operation, "srp_link").status, ActionStatus.FAILED)
        self.mocks["srp"].assert_not_called()

    def test_duplicate_submit_racing_the_first_returns_it_without_repeating_steps(self):
        request_id = uuid.uuid4()
        first = {}

        def other_request_finishes_first(user, character_id):
            first["operation"] = f.create_operation(self.fc, start_request_id=request_id, esi_fleet_id=FLEET_ID)
            return FleetDetectionResult(fleet_id=FLEET_ID, role="fleet_commander")

        self.mocks["detect"].side_effect = other_request_finishes_first

        operation = self.start(request_id=request_id)

        self.assertEqual(operation.pk, first["operation"].pk)
        self.assertEqual(FleetOperation.objects.count(), 1)
        self.assertFalse(OperationAction.objects.filter(operation=operation).exists())
        for name in ("post", "motd", "sync", "srp"):
            self.mocks[name].assert_not_called()


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


class FakeSRPProvider:
    def __init__(self, key="safety_srp", *, created=True):
        self.key = key
        self.created = created
        self.calls = []

    def available(self):
        return True

    def create_for_operation(self, operation):
        self.calls.append(operation.pk)
        if not self.created:
            return SRPLinkResult(self.key, message="Provider declined to create an SRP fleet.")
        number = len(self.calls)
        return SRPLinkResult(self.key, reference=f"R{number}", url=f"/srp/{number}/", created=True, message="Created.")


class RetrySafetyTests(TestCase):
    def setUp(self):
        registry = mock.patch.dict(srp_module._PROVIDERS, clear=True)
        registry.start()
        self.addCleanup(registry.stop)
        self.provider = FakeSRPProvider()
        register_srp_provider(self.provider)
        f.settings(srp_provider="safety_srp")
        self.fc = f.create_user(perms=f.FC_PERMS)
        webhook = DiscordWebhook.objects.create(name="Pings", webhook_url=WEBHOOK_URL)
        self.target = PingTarget.objects.create(name="Everyone", target_value="@everyone", webhook=webhook)
        post = mock.patch("fleetops.providers.pings.requests.post", return_value=mock.Mock(status_code=204, text=""))
        self.post = post.start()
        self.addCleanup(post.stop)
        motd = mock.patch("fleetops.services.operations.set_fleet_motd")
        self.motd = motd.start()
        self.addCleanup(motd.stop)

    def test_attendance_only_retries_send_nothing_and_stay_skipped(self):
        operation = f.create_operation(
            self.fc, send_ping=False, ping_target=self.target, ping_text="quiet", motd_text="quiet"
        )
        for name in ("discord_ping", "motd_update", "srp_link"):
            OperationAction.objects.create(
                operation=operation, action=name, status=ActionStatus.SKIPPED, error_message="Skipped at start."
            )

        results = [retry_ping(operation), retry_motd(operation), retry_srp(operation)]

        self.post.assert_not_called()
        self.motd.assert_not_called()
        self.assertEqual(self.provider.calls, [])
        for result in results:
            self.assertEqual(result.status, ActionStatus.SKIPPED)
            self.assertEqual(result.attempts, 1)
            self.assertEqual(result.error_message, "Skipped at start.")
        operation.refresh_from_db()
        self.assertEqual(operation.srp_reference, "")

    def test_attendance_only_retry_without_a_record_explains_the_skip(self):
        operation = f.create_operation(self.fc, send_ping=False, ping_target=self.target, ping_text="quiet")

        result = retry_ping(operation)

        self.assertEqual(result.status, ActionStatus.SKIPPED)
        self.assertIn("Attendance-only", result.error_message)
        self.post.assert_not_called()

    def test_manual_fleet_never_pings_but_can_still_link_srp(self):
        operation = f.create_operation(
            self.fc, status=Status.CLOSED, send_ping=False, is_manual=True, ping_target=self.target, ping_text="late"
        )

        ping = retry_ping(operation)
        srp = retry_srp(operation)

        self.assertEqual(ping.status, ActionStatus.SKIPPED)
        self.assertIn("Manual fleet", ping.error_message)
        self.post.assert_not_called()
        self.assertEqual(srp.status, ActionStatus.SUCCESS)
        self.assertEqual(self.provider.calls, [operation.pk])
        operation.refresh_from_db()
        self.assertEqual(operation.srp_reference, "R1")

    def test_existing_link_is_kept_and_no_second_srp_fleet_is_created(self):
        operation = f.create_operation(self.fc, srp_provider="safety_srp", srp_reference="OLD", srp_url="/srp/old/")

        result = retry_srp(operation)

        self.assertEqual(self.provider.calls, [])
        self.assertEqual(result.status, ActionStatus.SUCCESS)
        self.assertIn("already linked", result.error_message)
        operation.refresh_from_db()
        self.assertEqual((operation.srp_reference, operation.srp_url), ("OLD", "/srp/old/"))

    def test_retry_from_a_stale_instance_does_not_create_a_second_srp_fleet(self):
        operation = f.create_operation(self.fc)
        stale = FleetOperation.objects.get(pk=operation.pk)

        retry_srp(operation)
        retry_srp(stale)

        self.assertEqual(self.provider.calls, [operation.pk])
        operation.refresh_from_db()
        self.assertEqual(operation.srp_reference, "R1")

    def test_declined_retry_records_the_reason_only(self):
        self.provider.created = False
        operation = f.create_operation(self.fc, srp_error="Provider was offline.")

        result = retry_srp(operation)

        self.assertEqual(result.status, ActionStatus.SKIPPED)
        operation.refresh_from_db()
        self.assertEqual(operation.srp_reference, "")
        self.assertEqual(operation.srp_url, "")
        self.assertEqual(operation.srp_error, "Provider declined to create an SRP fleet.")

    def test_failed_ping_retry_never_shows_the_webhook_to_members(self):
        member = f.create_user(perms=f.MEMBER_PERMS)
        operation = f.create_operation(self.fc, ping_target=self.target, ping_text="ping")
        f.add_attendance(operation, member)
        self.post.side_effect = requests.ConnectionError(
            f"Max retries exceeded with url: /api/webhooks/555000111/{WEBHOOK_TOKEN}"
        )

        result = retry_ping(operation)

        self.assertEqual(result.status, ActionStatus.FAILED)
        self.assertNotIn(WEBHOOK_TOKEN, result.error_message)
        self.client.force_login(member)
        response = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, WEBHOOK_TOKEN)

    def test_unexpected_motd_retry_error_is_generic(self):
        self.motd.side_effect = RuntimeError("secret-access-token")
        operation = f.create_operation(self.fc, motd_text="motd")

        with self.assertLogs("fleetops.services.operations", "ERROR") as logs:
            result = retry_motd(operation)

        self.assertEqual(result.status, ActionStatus.FAILED)
        self.assertNotIn("secret-access-token", result.error_message)
        self.assertIn("RuntimeError", result.error_message)
        self.assertNotIn("secret-access-token", "\n".join(logs.output))

    def test_unexpected_ping_retry_error_is_generic(self):
        operation = f.create_operation(self.fc, ping_target=self.target, ping_text="ping")

        with mock.patch(
            "fleetops.services.operations.send_discord_webhook", side_effect=ValueError(f"bad url {WEBHOOK_URL}")
        ), self.assertLogs("fleetops.services.operations", "ERROR") as logs:
            result = retry_ping(operation)

        self.assertEqual(result.status, ActionStatus.FAILED)
        self.assertEqual(result.error_message, "Discord ping failed unexpectedly (ValueError).")
        self.assertNotIn(WEBHOOK_TOKEN, "\n".join(logs.output))

    def test_unexpected_srp_retry_error_is_generic_and_logged_safely(self):
        self.provider.create_for_operation = mock.Mock(side_effect=RuntimeError(f"posted to {WEBHOOK_URL}"))
        operation = f.create_operation(self.fc)

        with self.assertLogs("fleetops.services.operations", "ERROR") as logs:
            result = retry_srp(operation)

        operation.refresh_from_db()
        self.assertEqual(result.status, ActionStatus.FAILED)
        self.assertEqual(operation.srp_error, "SRP creation failed unexpectedly (RuntimeError).")
        log = "\n".join(logs.output)
        self.assertIn(f"SRP creation failed for operation {operation.pk} (RuntimeError)", log)
        self.assertNotIn(WEBHOOK_TOKEN, log)

    def test_retry_never_sends_a_ping_or_motd_that_renders_empty(self):
        operation = f.create_operation(
            self.fc,
            ping_target=self.target,
            ping_template=make_template("{% if additional_message %}x{% endif %}"),
            motd_template=make_template("{# nothing #}", MOTD),
        )

        ping = retry_ping(operation)
        motd = retry_motd(operation)

        self.assertEqual(ping.status, ActionStatus.FAILED)
        self.assertEqual(ping.error_message, "Not sent because the ping text is empty.")
        self.assertEqual(motd.status, ActionStatus.FAILED)
        self.assertEqual(motd.error_message, "Not sent because the MOTD text is empty.")
        self.post.assert_not_called()
        self.motd.assert_not_called()

    def test_retry_views_flash_the_step_detail(self):
        self.provider.created = False
        operation = f.create_operation(self.fc, ping_text="ping", motd_text="motd")
        self.motd.side_effect = FleetESIError("MOTD_FORBIDDEN", "ESI denied permission to update the fleet MOTD.")
        self.client.force_login(self.fc)

        for name, expected in (
            ("retry_ping", "Ping retry: Failed. No active webhook configured. Ping remains available for manual copy."),
            ("retry_motd", "MOTD retry: Failed. ESI denied permission to update the fleet MOTD."),
            ("retry_srp", "SRP retry: Skipped. Provider declined to create an SRP fleet."),
        ):
            with self.subTest(view=name):
                response = self.client.post(reverse(f"fleetops:{name}", args=[operation.uuid]))

                self.assertRedirects(
                    response, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False
                )
                self.assertIn(expected, flashed(response))


# ---------------------------------------------------------------------------
# Alliance Auth built-in SRP provider
# ---------------------------------------------------------------------------


def _blank_view(request, **kwargs):
    return HttpResponse("")


def _srp_urls(prefix, *patterns):
    return [path(prefix, include((list(patterns), "srp")))]


class AllianceAuthSrpUrls:
    urlpatterns = _srp_urls(
        "srp/",
        path("", _blank_view, name="management"),
        path("<int:fleet_id>/view/", _blank_view, name="fleet"),
        path("<str:fleet_srp>/request/", _blank_view, name="request"),
    )


class RequestOnlySrpUrls:
    urlpatterns = _srp_urls(
        "srp/",
        path("", _blank_view, name="management"),
        path("<str:fleet_srp>/request/", _blank_view, name="request"),
    )


class ManagementOnlySrpUrls:
    urlpatterns = _srp_urls("aa/srp/", path("", _blank_view, name="management"))


class FakeSrpFleets:
    def __init__(self):
        self.created = []

    def create(self, **values):
        row = SimpleNamespace(pk=len(self.created) + 1, **values)
        self.created.append(row)
        return row


def fake_srp_fleet_model(with_code=True):
    """Stand-in for allianceauth.srp.models.SrpFleetMain with the same field definitions."""
    definitions = [
        ("id", models.AutoField(primary_key=True)),
        ("fleet_name", models.CharField(max_length=254, default="")),
        ("fleet_doctrine", models.CharField(max_length=254, default="")),
        ("fleet_time", models.DateTimeField()),
        ("fleet_srp_status", models.CharField(max_length=254, default="")),
        ("fleet_commander", models.ForeignKey(EveCharacter, null=True, on_delete=models.SET_NULL)),
    ]
    if with_code:
        definitions.append(("fleet_srp_code", models.CharField(max_length=254, default="")))
    for name, field in definitions:
        field.set_attributes_from_name(name)
    fields = [field for _name, field in definitions]
    return SimpleNamespace(_meta=SimpleNamespace(concrete_fields=fields), objects=FakeSrpFleets())


class BuiltinSRPLinkTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc, doctrine_name="Shield Ferox")
        self.provider = AllianceAuthBuiltinSRPProvider()
        self.install(fake_srp_fleet_model())

    def install(self, model):
        self.model = model
        for name, value in (("available", True), ("_model", model)):
            patcher = mock.patch.object(AllianceAuthBuiltinSRPProvider, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_srp_code_uses_alliance_auth_format(self):
        self.provider.create_for_operation(self.operation)
        self.provider.create_for_operation(self.operation)

        codes = [row.fleet_srp_code for row in self.model.objects.created]
        for code in codes:
            self.assertRegex(code, r"^[0-9A-F]{8}$")
        self.assertNotEqual(codes[0], codes[1])

    def test_schema_without_srp_code_is_still_supported(self):
        self.install(fake_srp_fleet_model(with_code=False))

        result = self.provider.create_for_operation(self.operation)

        self.assertTrue(result.created)
        self.assertFalse(hasattr(self.model.objects.created[0], "fleet_srp_code"))

    @override_settings(ROOT_URLCONF=AllianceAuthSrpUrls)
    def test_link_points_to_the_alliance_auth_fleet_page(self):
        result = self.provider.create_for_operation(self.operation)

        self.assertEqual(result.reference, "1")
        self.assertEqual(result.url, "/srp/1/view/")

    @override_settings(ROOT_URLCONF=RequestOnlySrpUrls)
    def test_request_page_is_used_without_a_fleet_page(self):
        result = self.provider.create_for_operation(self.operation)

        code = self.model.objects.created[0].fleet_srp_code
        self.assertEqual(result.url, f"/srp/{code}/request/")

    @override_settings(ROOT_URLCONF=ManagementOnlySrpUrls)
    def test_management_page_is_preferred_over_the_hard_coded_fallback(self):
        result = self.provider.create_for_operation(self.operation)

        self.assertEqual(result.url, "/aa/srp/")


# ---------------------------------------------------------------------------
# Ending fleets
# ---------------------------------------------------------------------------


class EndFleetAuditTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        patcher = mock.patch("fleetops.services.operations.sync_operation")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_audit_records_the_previous_status_and_tracking_state(self):
        operation = f.create_operation(self.fc, status=Status.STARTING)

        end_fleet(operation, actor=self.fc)

        entry = AuditLog.objects.get(action="fleet.end")
        self.assertEqual(entry.old_value, {"status": Status.STARTING, "tracking_enabled": False})
        self.assertEqual(entry.new_value, {"status": Status.CLOSED, "tracking_enabled": False})

    def test_automatic_end_of_an_active_fleet(self):
        operation = f.create_operation(self.fc)

        end_fleet(operation, automatic=True)

        entry = AuditLog.objects.get(action="fleet.auto_end")
        self.assertEqual(entry.old_value, {"status": Status.ACTIVE, "tracking_enabled": True})


class EndAndManualFleetErrorTests(TestCase):
    def test_end_fleet_logs_unexpected_final_sync_errors_without_their_text(self):
        fc = f.create_user(perms=f.FC_PERMS)
        operation = f.create_operation(fc)
        with mock.patch("fleetops.services.operations.sync_operation", side_effect=RuntimeError(f"boom {WEBHOOK_URL}")):
            with self.assertLogs("fleetops.services.operations", "ERROR") as logs:
                end_fleet(operation, actor=fc)

        operation.refresh_from_db()
        self.assertEqual(operation.status, FleetOperation.Status.CLOSED)
        self.assertIn("Final fleet sync failed", logs.output[0])
        self.assertNotIn(WEBHOOK_TOKEN, "\n".join(logs.output))

    def test_end_fleet_does_not_log_expected_esi_errors(self):
        fc = f.create_user(perms=f.FC_PERMS)
        operation = f.create_operation(fc)
        with mock.patch(
            "fleetops.services.operations.sync_operation",
            side_effect=FleetESIError("FLEET_NOT_FOUND", "Fleet no longer exists.", 404),
        ):
            with self.assertNoLogs("fleetops.services.operations", "ERROR"):
                end_fleet(operation, actor=fc)

        operation.refresh_from_db()
        self.assertEqual(operation.status, FleetOperation.Status.CLOSED)

    def test_manual_fleet_page_hides_unexpected_error_text(self):
        fc = f.create_user(perms=f.FC_PERMS)
        self.client.force_login(fc)
        data = {
            "fleet_type": f.fleet_type().pk,
            "doctrine_name": "",
            "formup": "Jita",
            "started_at": "2026-03-01T18:00",
            "ended_at": "2026-03-01T20:00",
            "attendance_multiplier": 1,
            "notes": "",
        }
        with mock.patch("fleetops.views.create_manual_fleet", side_effect=RuntimeError(f"db error {WEBHOOK_URL}")):
            with self.assertLogs("fleetops.views", "ERROR") as logs:
                response = self.client.post(reverse("fleetops:manual_fleet"), data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "unexpected error")
        self.assertNotContains(response, WEBHOOK_TOKEN)
        self.assertNotIn(WEBHOOK_TOKEN, "\n".join(logs.output))
