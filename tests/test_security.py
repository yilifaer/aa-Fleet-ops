"""Security regression tests for FleetOps.

Covers output escaping, message template rendering, CSRF / HTTP method
enforcement, object-level isolation, mass assignment, ESI token ownership,
secret handling and redirects.
"""

import json
import re
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qs, urlparse

import requests
from allianceauth.authentication.models import CharacterOwnership
from django.conf import settings as django_settings
from django.template import TemplateDoesNotExist, TemplateSyntaxError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from esi import app_settings as esi_app_settings
from esi.models import CallbackRedirect, Scope

from fleetops.constants import FLEET_READ_SCOPE
from fleetops.forms import DiscordWebhookForm
from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    CommsPreset,
    DiscordWebhook,
    FleetMemberEvent,
    FleetMemberState,
    FleetOperation,
    FleetType,
    IncentivePeriod,
    MessageTemplate,
    MonthlyFCStatistic,
    OperationAction,
    OperationRoleAssignment,
    PingTarget,
)
from fleetops.providers.esi import FleetDetectionResult, FleetESIError, detect_character_fleet
from fleetops.providers.srp import SRPLinkResult
from fleetops.services.messages import render_operation_messages
from fleetops.services.operations import start_fleet

from . import factories as f

ACTIVE = FleetOperation.Status.ACTIVE
CLOSED = FleetOperation.Status.CLOSED
MANUAL = AttendanceRecord.Source.MANUAL

FLEET_ID = 1_234_567_890_123
CAPSULE_TYPE_ID = 670

WEBHOOK_TOKEN = "Vx9kL2q-fleetops-webhook-secret"
WEBHOOK_URL = f"https://discord.com/api/webhooks/987654321/{WEBHOOK_TOKEN}"

XSS_SCRIPT = '<script>alert("fleetops-xss")</script>'
XSS_ATTR = 'Hull" onmouseover="alert(3)'
RAW_SCRIPT = '<script>alert("fleetops-xss")'
RAW_ATTR = '" onmouseover="alert(3)'
ESCAPED_SCRIPT = escape(XSS_SCRIPT)

CSRF_FIELD = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')


def add_member_state(operation, character, user=None, **overrides):
    main = f.main_of(user) if user else None
    now = timezone.now()
    values = dict(
        operation=operation,
        character_id=character.character_id,
        character_name=character.character_name,
        main_character_id=main.character_id if main else None,
        main_character_name=main.character_name if main else "",
        auth_user=user,
        corporation_id=character.corporation_id,
        corporation_name=character.corporation_name,
        alliance_id=character.alliance_id,
        alliance_name=character.alliance_name or "",
        ship_type_id=11987,
        ship_type_name="Guardian",
        solar_system_id=30000142,
        solar_system_name="Jita",
        fleet_role="squad_member",
        wing_id=1,
        squad_id=1,
        first_seen=operation.started_at,
        last_seen=now,
        is_active=True,
    )
    values.update(overrides)
    return FleetMemberState.objects.create(**values)


def add_role(operation, character, user=None, *, role=OperationRoleAssignment.Role.LOGI_ANCHOR, credit=False, notes=""):
    main = f.main_of(user) if user else None
    return OperationRoleAssignment.objects.create(
        operation=operation,
        role=role,
        character_id=character.character_id,
        character_name=character.character_name,
        main_character_id=main.character_id if main else None,
        main_character_name=main.character_name if main else "",
        auth_user=user,
        corporation_id=character.corporation_id,
        corporation_name=character.corporation_name,
        grants_fc_credit=credit,
        notes=notes,
    )


def csrf_token_from(response):
    match = CSRF_FIELD.search(response.content.decode())
    return match.group(1) if match else None


class StartFleetPatchesMixin:
    """Replaces every external call made while starting or managing a fleet."""

    def patch_externals(self, *, role="fleet_commander", fleet_id=FLEET_ID):
        patchers = {
            "detect": mock.patch(
                "fleetops.services.operations.detect_character_fleet",
                return_value=FleetDetectionResult(fleet_id=fleet_id, role=role),
            ),
            "motd": mock.patch("fleetops.services.operations.set_fleet_motd"),
            "sync": mock.patch("fleetops.services.operations.sync_operation", return_value=0),
            "srp": mock.patch(
                "fleetops.services.operations.create_srp_link",
                return_value=SRPLinkResult("test_srp", message="No SRP provider in tests."),
            ),
            "post": mock.patch("fleetops.providers.pings.requests.post"),
            "kick": mock.patch("fleetops.services.fleet_controls.kick_fleet_member"),
        }
        mocks = {}
        for name, patcher in patchers.items():
            mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        mocks["post"].return_value = mock.Mock(status_code=204, text="")
        return mocks


# ---------------------------------------------------------------------------
# Output escaping
# ---------------------------------------------------------------------------


class OutputEscapingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lead = f.create_user(perms=f.FC_LEAD_PERMS, main_name="Lead Main")
        cls.member = f.create_user(perms=f.MEMBER_PERMS, main_name="Member Main")
        cls.member_alt = f.add_alt(cls.member, "Member Alt")
        cls.fleet_type = FleetType.objects.create(name=XSS_SCRIPT, point_weight=Decimal("1.00"))
        cls.comms = CommsPreset.objects.create(name=XSS_SCRIPT, channel_name=XSS_SCRIPT, voice_url=XSS_ATTR)
        now = timezone.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        cls.operation = f.create_operation(
            cls.lead,
            type_obj=cls.fleet_type,
            # Keep the fleet inside the current month so the monthly statistics pages include it.
            started_at=max(month_start, now - timedelta(minutes=30)),
            formup=XSS_SCRIPT,
            doctrine_name=XSS_SCRIPT,
            additional_message=XSS_SCRIPT,
            fc_character_name=XSS_SCRIPT,
            ping_text=XSS_SCRIPT,
            motd_text=XSS_SCRIPT,
            last_error=XSS_SCRIPT,
            srp_error=XSS_SCRIPT,
            comms=cls.comms,
        )
        OperationAction.objects.create(
            operation=cls.operation, action="discord_ping", status=OperationAction.Status.FAILED, error_message=XSS_SCRIPT
        )
        f.create_operation(
            cls.lead,
            status=CLOSED,
            type_obj=cls.fleet_type,
            started_at=max(month_start, now - timedelta(minutes=30)),
            formup=XSS_SCRIPT,
            doctrine_name=XSS_SCRIPT,
        )
        add_member_state(
            cls.operation,
            cls.member_alt,
            cls.member,
            character_name=XSS_SCRIPT,
            ship_type_name=XSS_ATTR,
            solar_system_name=XSS_SCRIPT,
            corporation_name=XSS_SCRIPT,
            fleet_role=XSS_SCRIPT,
        )
        FleetMemberEvent.objects.create(
            operation=cls.operation,
            character_id=cls.member_alt.character_id,
            character_name=XSS_SCRIPT,
            event_type=FleetMemberEvent.EventType.SHIP_CHANGE,
            old_value=XSS_SCRIPT,
            new_value=XSS_ATTR,
        )
        record = f.add_attendance(cls.operation, cls.member, cls.member_alt)
        AttendanceRecord.objects.filter(pk=record.pk).update(character_name=XSS_SCRIPT, corporation_name=XSS_SCRIPT)
        manual = f.add_attendance(cls.operation, cls.member, source=MANUAL)
        AttendanceRecord.objects.filter(pk=manual.pk).update(character_name=XSS_SCRIPT, created_by=cls.lead)
        add_role(cls.operation, cls.member_alt, cls.member, notes=XSS_SCRIPT)
        AuditLog.objects.create(actor=cls.lead, action=XSS_SCRIPT, object_type="Test", object_id="1", reason=XSS_SCRIPT)

    def assertEscaped(self, response, *, expect_payload=True):
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn(RAW_SCRIPT, content)
        self.assertNotIn(RAW_ATTR, content)
        if expect_payload:
            self.assertIn(ESCAPED_SCRIPT, content)

    def get_as(self, user, name, *args, query=""):
        self.client.force_login(user)
        return self.client.get(reverse(f"fleetops:{name}", args=args) + query)

    def test_operation_detail_escapes_stored_fleet_data(self):
        response = self.get_as(self.lead, "operation_detail", self.operation.uuid)
        self.assertEscaped(response)
        content = response.content.decode()
        # Ping and MOTD previews are shown as text, never as markup.
        self.assertIn(f'<pre id="ping-text" class="preview-box">{ESCAPED_SCRIPT}</pre>', content)
        self.assertIn(f'<pre id="motd-text" class="preview-box">{ESCAPED_SCRIPT}</pre>', content)

    def test_member_view_of_operation_is_escaped(self):
        self.assertEscaped(self.get_as(self.member, "operation_detail", self.operation.uuid))

    def test_edit_form_escapes_stored_values(self):
        self.assertEscaped(self.get_as(self.lead, "edit_operation", self.operation.uuid))

    def test_archive_escapes_fleet_data(self):
        self.assertEscaped(self.get_as(self.lead, "fleet_operations"))

    def test_archive_escapes_reflected_filter_values(self):
        query = "?" + "&".join(
            f"{key}={requests.utils.quote(XSS_SCRIPT)}" for key in ("q", "fc", "doctrine", "status", "fleet_type")
        )
        response = self.get_as(self.lead, "fleet_operations", query=query)
        self.assertEscaped(response)

    def test_dashboard_escapes_participations_and_ships(self):
        self.assertEscaped(self.get_as(self.member, "dashboard"))

    def test_history_and_statistics_pages_escape_names(self):
        for user, name in (
            (self.member, "attendance_history_me"),
            (self.member, "my_statistics"),
            (self.lead, "attendance_history_alliance"),
            (self.lead, "all_fc_statistics"),
            (self.lead, "all_corporation_statistics"),
        ):
            with self.subTest(page=name):
                self.assertEscaped(self.get_as(user, name))
        with self.subTest(page="fc_statistics_detail"):
            self.assertEscaped(self.get_as(self.lead, "fc_statistics_detail", self.lead.pk))

    def test_manual_attendance_page_escapes_entries(self):
        self.assertEscaped(self.get_as(self.lead, "manual_attendance"))

    def test_configuration_and_start_pages_escape_preset_names(self):
        self.assertEscaped(self.get_as(self.lead, "configuration_list", "fleet-types"))
        self.assertEscaped(self.get_as(self.lead, "configuration_list", "comms"))
        self.assertEscaped(self.get_as(self.lead, "configuration_edit", "comms", self.comms.pk))
        self.assertEscaped(self.get_as(self.lead, "start_fleet"))

    def test_audit_log_escapes_action_and_reason(self):
        self.assertEscaped(self.get_as(self.lead, "audit_log"))

    def test_role_assignment_message_is_escaped(self):
        # Character names come from ESI/Auth data and are echoed back in flash messages.
        evil = f.add_alt(self.member, XSS_SCRIPT)
        add_member_state(self.operation, evil, self.member)
        self.client.force_login(self.lead)
        response = self.client.post(
            reverse("fleetops:add_operation_role", args=[self.operation.uuid]),
            {"role": OperationRoleAssignment.Role.SNOWFLAKE, "character_id": evil.character_id, "notes": ""},
            follow=True,
        )
        self.assertEscaped(response)

    def test_preview_endpoint_returns_json_not_html(self):
        f.add_token(self.lead, f.main_of(self.lead))
        self.client.force_login(self.lead)
        response = self.client.post(
            reverse("fleetops:preview_fleet"),
            {
                "request_id": str(uuid.uuid4()),
                "operation_mode": "full",
                "fc_character_id": str(f.main_of(self.lead).character_id),
                "fleet_type": self.fleet_type.pk,
                "formup": XSS_SCRIPT,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.get("X-Content-Type-Options"), "nosniff")
        self.assertIn(XSS_SCRIPT, response.json()["ping"])


# ---------------------------------------------------------------------------
# Admin-editable ping / MOTD templates
# ---------------------------------------------------------------------------


class MessageTemplateRenderingTests(StartFleetPatchesMixin, TestCase):
    PROBE = (
        "[{{ operation.fc_user.password }}]"
        "[{{ operation.created_by.email }}]"
        "[{{ fc_user.password }}][{{ user.password }}][{{ user.email }}]"
        "[{{ request.user.password }}][{{ request.session }}]"
        "[{{ settings.SECRET_KEY }}][{{ settings.ESI_SSO_CLIENT_SECRET }}]"
        "[{{ csrf_token }}][{{ perms }}][{{ view }}]"
        "[{% debug %}]"
        "FC={{ fc }}"
    )

    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Template FC")
        self.fc.email = "fc-private@example.com"
        self.fc.set_password("correct horse battery staple")
        self.fc.save()
        self.fc_char = f.main_of(self.fc)
        self.token = f.add_token(self.fc, self.fc_char)
        self.fleet_type = f.fleet_type("StratOp", "1.00")

    def secrets(self):
        self.fc.refresh_from_db()
        return [
            self.fc.email,
            self.fc.password,
            django_settings.SECRET_KEY,
            django_settings.ESI_SSO_CLIENT_SECRET,
            self.token.access_token,
            self.token.refresh_token,
        ]

    def make_template(self, content, template_type=MessageTemplate.TemplateType.PING, **extra):
        return MessageTemplate.objects.create(
            name=f"Template {uuid.uuid4().hex[:8]}", template_type=template_type, content=content, **extra
        )

    def test_template_context_exposes_no_user_or_settings_data(self):
        ping_template = self.make_template(self.PROBE)
        motd_template = self.make_template(self.PROBE, MessageTemplate.TemplateType.MOTD)
        operation = f.create_operation(self.fc, ping_template=ping_template, motd_template=motd_template)

        for debug in (False, True):
            with self.subTest(debug=debug), override_settings(DEBUG=debug):
                ping, motd = render_operation_messages(operation)
                self.assertIn("FC=Template FC", ping)
                for secret in self.secrets():
                    self.assertNotIn(secret, ping)
                    self.assertNotIn(secret, motd)

    def test_preview_endpoint_does_not_leak_secrets_through_templates(self):
        ping_template = self.make_template(self.PROBE)
        self.client.force_login(self.fc)
        response = self.client.post(
            reverse("fleetops:preview_fleet"),
            {
                "request_id": str(uuid.uuid4()),
                "operation_mode": "full",
                "fc_character_id": str(self.fc_char.character_id),
                "fleet_type": self.fleet_type.pk,
                "formup": "Jita",
                "ping_template": ping_template.pk,
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("FC=Template FC", response.json()["ping"])
        for secret in self.secrets():
            self.assertNotIn(secret, body)

    def test_fc_entered_text_is_not_evaluated_as_template_code(self):
        operation = f.create_operation(
            self.fc,
            formup="{{ 6|add:1 }}",
            doctrine_name="{{ settings.SECRET_KEY }}",
            additional_message="{% now 'Y' %}{% debug %}",
        )
        ping, motd = render_operation_messages(operation)
        for text in (ping, motd):
            self.assertIn("{{ 6|add:1 }}", text)
            self.assertIn("{{ settings.SECRET_KEY }}", text)
            self.assertIn("{% now 'Y' %}{% debug %}", text)
            self.assertNotIn(django_settings.SECRET_KEY, text)

    def test_include_cannot_read_files_outside_template_directories(self):
        for path in ("/etc/passwd", "../../../../../../../../etc/passwd"):
            with self.subTest(path=path):
                template = self.make_template(f'{{% include "{path}" %}}')
                operation = f.create_operation(self.fc, ping_template=template)
                try:
                    ping, _motd = render_operation_messages(operation)
                except (TemplateDoesNotExist, TemplateSyntaxError, AttributeError):
                    # Refusing to render is fine; leaking the file is not.
                    continue
                self.assertNotIn("root:", ping)

    def test_motd_script_filter_is_case_insensitive(self):
        template = self.make_template("<SCRIPT>alert(1)</SCRIPT><Script>alert(2)</Script>", MessageTemplate.TemplateType.MOTD)
        operation = f.create_operation(self.fc, motd_template=template)
        _ping, motd = render_operation_messages(operation)
        self.assertNotIn("<script", motd.lower())

    def test_preview_contains_template_render_errors(self):
        template = self.make_template('{% include "fleetops/no-such-ping.txt" %}')
        self.client.force_login(self.fc)
        self.client.raise_request_exception = False
        response = self.client.post(
            reverse("fleetops:preview_fleet"),
            {
                "request_id": str(uuid.uuid4()),
                "operation_mode": "full",
                "fc_character_id": str(self.fc_char.character_id),
                "fleet_type": self.fleet_type.pk,
                "formup": "Jita",
                "ping_template": template.pk,
            },
        )
        self.assertLess(response.status_code, 500)

    def test_broken_template_does_not_block_attendance_only_start(self):
        self.patch_externals()
        # An administrator saves a typo into the default ping template.
        defaults = MessageTemplate.objects.filter(template_type=MessageTemplate.TemplateType.PING, is_active=True)
        if defaults.exists():
            defaults.update(content="{% if %}broken")
        else:
            self.make_template("{% if %}broken", is_default=True)
        self.client.force_login(self.fc)
        self.client.post(
            reverse("fleetops:start_fleet"),
            {
                "request_id": str(uuid.uuid4()),
                "operation_mode": "attendance_only",
                "fc_character_id": str(self.fc_char.character_id),
                "fleet_type": self.fleet_type.pk,
                "formup": "Jita",
            },
        )
        operation = FleetOperation.objects.filter(fc_user=self.fc).first()
        self.assertIsNotNone(operation)
        self.assertEqual(operation.status, ACTIVE)


# ---------------------------------------------------------------------------
# CSRF and HTTP methods
# ---------------------------------------------------------------------------


class CsrfAndMethodTests(StartFleetPatchesMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        f.settings(incentive_enabled=True)
        cls.lead = f.create_user(perms=f.FC_LEAD_PERMS, main_name="Lead Main")
        cls.member = f.create_user(perms=f.MEMBER_PERMS, main_name="Member Main")
        cls.fleet_type = f.fleet_type("StratOp", "1.00")
        cls.operation = f.create_operation(cls.lead, type_obj=cls.fleet_type)
        add_member_state(cls.operation, f.main_of(cls.member), cls.member, ship_type_id=CAPSULE_TYPE_ID, ship_type_name="Capsule")
        cls.manual = f.add_attendance(cls.operation, cls.member, source=MANUAL)
        cls.role = add_role(cls.operation, f.main_of(cls.member), cls.member)
        cls.comms = CommsPreset.objects.create(name="Main Comms")
        cls.period = IncentivePeriod.objects.create(year=2026, month=9, status=IncentivePeriod.Status.REVIEW, budget=1000)
        MonthlyFCStatistic.objects.create(period=cls.period, fc_user=cls.lead, fleet_count=1, total_points=1)

    def setUp(self):
        self.mocks = self.patch_externals()

    def post_only_urls(self):
        op = self.operation.uuid
        return [
            reverse("fleetops:preview_fleet"),
            reverse("fleetops:end_fleet", args=[op]),
            reverse("fleetops:set_attendance_multiplier", args=[op]),
            reverse("fleetops:retry_ping", args=[op]),
            reverse("fleetops:retry_motd", args=[op]),
            reverse("fleetops:retry_srp", args=[op]),
            reverse("fleetops:add_operation_role", args=[op]),
            reverse("fleetops:delete_operation_role", args=[self.role.pk]),
            reverse("fleetops:kick_capsules", args=[op]),
            reverse("fleetops:add_manual_attendance", args=[op]),
            reverse("fleetops:delete_manual_attendance", args=[self.manual.pk]),
            reverse("fleetops:incentive_recalculate", args=[self.period.pk]),
            reverse("fleetops:incentive_finalize", args=[self.period.pk]),
            reverse("fleetops:incentive_unlock", args=[self.period.pk]),
            reverse("fleetops:incentive_waiver", args=[self.period.pk, self.lead.pk]),
            reverse("fleetops:configuration_delete", args=["comms", self.comms.pk]),
        ]

    def assertNothingChanged(self):
        self.operation.refresh_from_db()
        self.period.refresh_from_db()
        self.assertEqual(self.operation.status, ACTIVE)
        self.assertEqual(self.operation.attendance_multiplier, 1)
        self.assertTrue(AttendanceRecord.objects.filter(pk=self.manual.pk).exists())
        self.assertTrue(OperationRoleAssignment.objects.filter(pk=self.role.pk).exists())
        self.assertTrue(CommsPreset.objects.filter(pk=self.comms.pk).exists())
        self.assertEqual(self.period.status, IncentivePeriod.Status.REVIEW)
        self.assertFalse(AuditLog.objects.exists())
        self.assertFalse(OperationAction.objects.filter(operation=self.operation).exists())
        self.mocks["post"].assert_not_called()
        self.mocks["motd"].assert_not_called()
        self.mocks["srp"].assert_not_called()
        self.mocks["kick"].assert_not_called()

    def test_state_changing_endpoints_reject_get(self):
        self.client.force_login(self.lead)
        for url in self.post_only_urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url, {"attendance_multiplier": 3, "waived": 1}).status_code, 405)
        self.assertNothingChanged()

    def test_get_on_form_views_never_persists_query_data(self):
        self.client.force_login(self.lead)
        urls = [
            reverse("fleetops:edit_operation", args=[self.operation.uuid]) + "?formup=Hacked&fleet_type=999",
            reverse("fleetops:configuration_settings") + "?attendance_limit=1&tracking_interval=999",
            reverse("fleetops:configuration_add", args=["comms"]) + "?name=Injected",
            reverse("fleetops:manual_fleet") + f"?fleet_type={self.fleet_type.pk}&formup=Injected&started_at=2026-01-01+10:00",
            reverse("fleetops:incentive_review") + "?create_period=1&year=2026&month=1&budget=5&minimum_fleets=1",
            reverse("fleetops:start_fleet") + f"?fleet_type={self.fleet_type.pk}&formup=Injected",
            reverse("fleetops:manual_attendance") + f"?operation={self.operation.pk}&character_id=5&attendance_value=9",
        ]
        before = FleetOperation.objects.count(), AttendanceRecord.objects.count(), IncentivePeriod.objects.count()
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.formup, "Jita")
        self.assertIsNone(f.settings().attendance_limit)
        self.assertFalse(CommsPreset.objects.filter(name="Injected").exists())
        self.assertEqual(
            (FleetOperation.objects.count(), AttendanceRecord.objects.count(), IncentivePeriod.objects.count()), before
        )
        self.assertFalse(AuditLog.objects.exists())

    def test_post_without_csrf_token_is_rejected(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.lead)
        requests_to_make = [
            (reverse("fleetops:end_fleet", args=[self.operation.uuid]), {"attendance_multiplier": 2}),
            (reverse("fleetops:set_attendance_multiplier", args=[self.operation.uuid]), {"attendance_multiplier": 3}),
            (reverse("fleetops:delete_manual_attendance", args=[self.manual.pk]), {}),
            (reverse("fleetops:delete_operation_role", args=[self.role.pk]), {}),
            (reverse("fleetops:incentive_finalize", args=[self.period.pk]), {}),
            (reverse("fleetops:configuration_delete", args=["comms", self.comms.pk]), {}),
            (reverse("fleetops:configuration_settings"), {"tracking_interval": 999}),
            (
                reverse("fleetops:start_fleet"),
                {
                    "request_id": str(uuid.uuid4()),
                    "operation_mode": "full",
                    "fc_character_id": str(f.main_of(self.lead).character_id),
                    "fleet_type": self.fleet_type.pk,
                    "formup": "Jita",
                },
            ),
        ]
        for url, data in requests_to_make:
            with self.subTest(url=url):
                self.assertEqual(client.post(url, data).status_code, 403)
        self.assertNothingChanged()
        self.mocks["detect"].assert_not_called()
        self.assertEqual(FleetOperation.objects.count(), 1)

    def test_post_with_csrf_token_from_page_is_accepted(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.lead)
        page = client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))
        token = csrf_token_from(page)
        self.assertTrue(token)
        response = client.post(
            reverse("fleetops:end_fleet", args=[self.operation.uuid]),
            {"attendance_multiplier": 1, "csrfmiddlewaretoken": token},
        )
        self.assertEqual(response.status_code, 302)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.status, CLOSED)

    def test_anonymous_post_redirects_to_login_without_changes(self):
        response = self.client.post(reverse("fleetops:end_fleet", args=[self.operation.uuid]), {"attendance_multiplier": 3})
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])
        self.assertNothingChanged()


# ---------------------------------------------------------------------------
# Object-level isolation
# ---------------------------------------------------------------------------


class ObjectIsolationTests(StartFleetPatchesMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fc_a = f.create_user(perms=f.FC_PERMS, main_name="FC Alpha")
        cls.fc_b = f.create_user(perms=f.FC_PERMS, main_name="FC Bravo")
        cls.member = f.create_user(perms=f.MEMBER_PERMS, main_name="Line Member")
        cls.pilot = f.create_user(perms=f.MEMBER_PERMS, main_name="Pod Pilot")
        cls.fleet_type = f.fleet_type("StratOp", "1.00")
        cls.op_b = f.create_operation(cls.fc_b, type_obj=cls.fleet_type, formup="Bravo staging")
        cls.op_b_closed = f.create_operation(cls.fc_b, type_obj=cls.fleet_type, status=CLOSED)
        add_member_state(cls.op_b, f.main_of(cls.pilot), cls.pilot, ship_type_id=CAPSULE_TYPE_ID, ship_type_name="Capsule")
        cls.manual_b = f.add_attendance(cls.op_b, cls.pilot, source=MANUAL)
        cls.role_b = add_role(cls.op_b, f.main_of(cls.pilot), cls.pilot)

    def setUp(self):
        self.mocks = self.patch_externals()

    def assertHidden(self, response):
        self.assertIn(response.status_code, (403, 404))

    def assertOperationUntouched(self):
        self.op_b.refresh_from_db()
        self.assertEqual(self.op_b.status, ACTIVE)
        self.assertEqual(self.op_b.formup, "Bravo staging")
        self.assertEqual(self.op_b.attendance_multiplier, 1)
        self.assertEqual(AttendanceRecord.objects.filter(operation=self.op_b).count(), 1)
        self.assertEqual(OperationRoleAssignment.objects.filter(operation=self.op_b).count(), 1)
        self.assertFalse(OperationAction.objects.filter(operation=self.op_b).exists())
        self.assertFalse(AuditLog.objects.exists())
        for name in ("post", "motd", "srp", "kick", "sync"):
            self.mocks[name].assert_not_called()

    def test_fc_cannot_manage_another_fcs_fleet(self):
        self.client.force_login(self.fc_a)
        op = self.op_b.uuid
        pilot_id = f.main_of(self.pilot).character_id
        cases = [
            ("get", reverse("fleetops:edit_operation", args=[op]), {}),
            ("post", reverse("fleetops:edit_operation", args=[op]), {"fleet_type": self.fleet_type.pk, "formup": "Hijacked"}),
            ("post", reverse("fleetops:end_fleet", args=[op]), {"attendance_multiplier": 3}),
            ("post", reverse("fleetops:retry_ping", args=[op]), {}),
            ("post", reverse("fleetops:retry_motd", args=[op]), {}),
            ("post", reverse("fleetops:retry_srp", args=[op]), {}),
            ("post", reverse("fleetops:set_attendance_multiplier", args=[op]), {"attendance_multiplier": 3}),
            (
                "post",
                reverse("fleetops:add_manual_attendance", args=[op]),
                {"character_id": pilot_id, "attendance_value": 5, "duplicate_action": "keep"},
            ),
            ("post", reverse("fleetops:add_operation_role", args=[op]), {"role": "backseat_fc", "character_id": pilot_id}),
            ("post", reverse("fleetops:kick_capsules", args=[op]), {}),
        ]
        for method, url, data in cases:
            with self.subTest(method=method, url=url):
                self.assertHidden(getattr(self.client, method)(url, data))
        self.assertOperationUntouched()

    def test_fc_cannot_delete_records_on_another_fcs_fleet(self):
        self.client.force_login(self.fc_a)
        self.assertHidden(self.client.post(reverse("fleetops:delete_manual_attendance", args=[self.manual_b.pk])))
        self.assertHidden(self.client.post(reverse("fleetops:delete_operation_role", args=[self.role_b.pk])))
        self.assertOperationUntouched()

    def test_historical_manual_attendance_rejects_foreign_fleet(self):
        self.client.force_login(self.fc_a)
        response = self.client.post(
            reverse("fleetops:manual_attendance"),
            {
                "operation": self.op_b_closed.pk,
                "character_id": f.main_of(self.member).character_id,
                "attendance_value": 3,
                "duplicate_action": "keep",
            },
        )
        self.assertIn(response.status_code, (200, 403, 404))
        self.assertFalse(AttendanceRecord.objects.filter(operation=self.op_b_closed).exists())
        self.assertFalse(AuditLog.objects.exists())

    def test_non_credit_role_does_not_grant_management(self):
        # A Logi Anchor without FC credit is a participant, not a co-FC.
        add_role(self.op_b, f.main_of(self.fc_a), self.fc_a, credit=False)
        self.client.force_login(self.fc_a)
        self.assertHidden(self.client.post(reverse("fleetops:end_fleet", args=[self.op_b.uuid]), {"attendance_multiplier": 1}))
        self.assertHidden(self.client.post(reverse("fleetops:delete_operation_role", args=[self.role_b.pk])))
        self.op_b.refresh_from_db()
        self.assertEqual(self.op_b.status, ACTIVE)
        self.assertTrue(OperationRoleAssignment.objects.filter(pk=self.role_b.pk).exists())

    def test_manage_attendance_alone_does_not_open_foreign_fleets(self):
        clerk = f.create_user(perms=f.MEMBER_PERMS + ["fleetops.manage_attendance"])
        self.client.force_login(clerk)
        response = self.client.post(
            reverse("fleetops:add_manual_attendance", args=[self.op_b.uuid]),
            {"character_id": f.main_of(clerk).character_id, "attendance_value": 9, "duplicate_action": "keep"},
        )
        self.assertHidden(response)
        self.assertHidden(self.client.post(reverse("fleetops:delete_manual_attendance", args=[self.manual_b.pk])))
        self.assertOperationUntouched()

    def test_member_cannot_open_unrelated_fleet(self):
        self.client.force_login(self.member)
        self.assertHidden(self.client.get(reverse("fleetops:operation_detail", args=[self.op_b.uuid])))
        archive = self.client.get(reverse("fleetops:fleet_operations"))
        self.assertNotContains(archive, str(self.op_b.uuid))
        dashboard = self.client.get(reverse("fleetops:dashboard"))
        self.assertNotContains(dashboard, str(self.op_b.uuid))

    def test_member_cannot_view_other_users_fc_statistics(self):
        self.client.force_login(self.member)
        self.assertHidden(self.client.get(reverse("fleetops:fc_statistics_detail", args=[self.fc_b.pk])))
        self.assertEqual(self.client.get(reverse("fleetops:fc_statistics_detail", args=[self.member.pk])).status_code, 200)

    def test_corp_manager_is_limited_to_own_corporation(self):
        manager = f.create_user(perms=f.CORP_MANAGEMENT_PERMS, main_name="Corp Manager")
        outsider = f.create_user(main_name="Outsider Main", corporation=f.OTHER_CORP)
        f.add_attendance(self.op_b_closed, outsider)
        self.client.force_login(manager)
        self.assertEqual(
            self.client.get(reverse("fleetops:corporation_statistics_detail", args=[f.OTHER_CORP[0]])).status_code, 403
        )
        self.assertEqual(self.client.get(reverse("fleetops:all_corporation_statistics")).status_code, 403)
        self.assertEqual(self.client.get(reverse("fleetops:attendance_history_alliance")).status_code, 403)
        history = self.client.get(reverse("fleetops:attendance_history_corporation"))
        self.assertEqual(history.status_code, 200)
        self.assertNotContains(history, "Outsider Main")
        self.assertNotContains(history, f.OTHER_CORP[1])

    def test_incentive_waiver_requires_row_in_that_period(self):
        manager = f.create_user(perms=f.FC_LEAD_PERMS)
        period = IncentivePeriod.objects.create(year=2026, month=8, status=IncentivePeriod.Status.REVIEW)
        other_period = IncentivePeriod.objects.create(year=2026, month=7, status=IncentivePeriod.Status.REVIEW)
        MonthlyFCStatistic.objects.create(period=period, fc_user=self.fc_a)
        foreign_row = MonthlyFCStatistic.objects.create(period=other_period, fc_user=self.fc_b)
        self.client.force_login(manager)
        response = self.client.post(reverse("fleetops:incentive_waiver", args=[period.pk, self.fc_b.pk]), {"waived": "1"})
        self.assertEqual(response.status_code, 404)
        foreign_row.refresh_from_db()
        self.assertFalse(foreign_row.waived)
        self.assertFalse(AuditLog.objects.exists())

    def test_configuration_pk_is_scoped_to_its_section(self):
        lead = f.create_user(perms=f.FC_LEAD_PERMS)
        webhook = DiscordWebhook.objects.create(name="Ops", webhook_url=WEBHOOK_URL)
        self.assertFalse(CommsPreset.objects.filter(pk=webhook.pk).exists())
        self.client.force_login(lead)
        edit = self.client.get(reverse("fleetops:configuration_edit", args=["comms", webhook.pk]))
        self.assertEqual(edit.status_code, 404)
        self.assertNotContains(edit, WEBHOOK_TOKEN, status_code=404)
        delete = self.client.post(reverse("fleetops:configuration_delete", args=["comms", webhook.pk]))
        self.assertEqual(delete.status_code, 404)
        self.assertTrue(DiscordWebhook.objects.filter(pk=webhook.pk).exists())
        self.assertEqual(self.client.get(reverse("fleetops:configuration_list", args=["users"])).status_code, 404)

    def test_view_all_stats_does_not_grant_fleet_records(self):
        analyst = f.create_user(perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])
        self.client.force_login(analyst)
        self.assertHidden(self.client.get(reverse("fleetops:operation_detail", args=[self.op_b.uuid])))
        self.assertNotContains(self.client.get(reverse("fleetops:fleet_operations")), str(self.op_b.uuid))


# ---------------------------------------------------------------------------
# Mass assignment
# ---------------------------------------------------------------------------


class MassAssignmentTests(StartFleetPatchesMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        f.settings(incentive_enabled=True)
        cls.fc = f.create_user(perms=f.FC_LEAD_PERMS, main_name="Owner FC")
        cls.other = f.create_user(perms=f.FC_PERMS, main_name="Other FC")
        cls.member = f.create_user(perms=f.MEMBER_PERMS, main_name="Member Main")
        cls.fleet_type = f.fleet_type("StratOp", "1.00")
        cls.operation = f.create_operation(cls.fc, type_obj=cls.fleet_type, srp_url="/srp/1/", srp_reference="1")
        cls.other_operation = f.create_operation(cls.other, type_obj=cls.fleet_type)

    def setUp(self):
        self.mocks = self.patch_externals()
        self.client.force_login(self.fc)

    def test_edit_operation_ignores_protected_fields(self):
        original = FleetOperation.objects.get(pk=self.operation.pk)
        response = self.client.post(
            reverse("fleetops:edit_operation", args=[self.operation.uuid]),
            {
                "fleet_type": self.fleet_type.pk,
                "doctrine_name": "Ferox",
                "formup": "New staging",
                "additional_message": "",
                "started_at": original.started_at.strftime("%Y-%m-%dT%H:%M"),
                "ended_at": "",
                # Fields that are not part of the edit form.
                "status": CLOSED,
                "fc_user": self.other.pk,
                "created_by": self.other.pk,
                "fc_character_id": 1,
                "fc_character_name": "Spoofed",
                "attendance_multiplier": 3,
                "is_manual": "on",
                "send_ping": "",
                "srp_url": "javascript:alert(1)",
                "srp_reference": "spoofed",
                "srp_provider": "spoofed",
                "esi_fleet_id": 1,
                "tracking_enabled": "",
                "fleet_point_weight_snapshot": "99.00",
                "ping_text": "spoofed",
                "motd_text": "spoofed",
                "last_error": "spoofed",
                "uuid": str(uuid.uuid4()),
            },
        )
        self.assertEqual(response.status_code, 302)
        updated = FleetOperation.objects.get(pk=self.operation.pk)
        self.assertEqual(updated.formup, "New staging")
        for field in (
            "uuid", "status", "fc_user_id", "created_by_id", "fc_character_id", "fc_character_name",
            "attendance_multiplier", "is_manual", "send_ping", "srp_url", "srp_reference", "srp_provider",
            "esi_fleet_id", "tracking_enabled", "fleet_point_weight_snapshot", "ping_text", "motd_text", "last_error",
        ):
            with self.subTest(field=field):
                self.assertEqual(getattr(updated, field), getattr(original, field))

    def test_incentive_period_creation_ignores_status_fields(self):
        response = self.client.post(
            reverse("fleetops:incentive_review"),
            {
                "create_period": "1",
                "year": 2026,
                "month": 5,
                "budget": 1000,
                "minimum_fleets": 1,
                "status": IncentivePeriod.Status.FINALIZED,
                "finalized_by": self.fc.pk,
                "finalized_at": "2026-05-31 12:00:00",
                "policy_snapshot": '{"forged": true}',
            },
        )
        self.assertEqual(response.status_code, 302)
        period = IncentivePeriod.objects.get(year=2026, month=5)
        self.assertEqual(period.status, IncentivePeriod.Status.OPEN)
        self.assertIsNone(period.finalized_by)
        self.assertIsNone(period.finalized_at)
        self.assertEqual(period.policy_snapshot, {})

    def test_manual_fleet_ignores_owner_and_tracking_fields(self):
        response = self.client.post(
            reverse("fleetops:manual_fleet"),
            {
                "fleet_type": self.fleet_type.pk,
                "doctrine_name": "Ferox",
                "formup": "Manual staging",
                "started_at": "2026-09-01 18:00:00",
                "ended_at": "2026-09-01 20:00:00",
                "attendance_multiplier": 1,
                "status": ACTIVE,
                "fc_user": self.other.pk,
                "created_by": self.other.pk,
                "is_manual": "",
                "esi_fleet_id": 555,
                "tracking_enabled": "on",
                "send_ping": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        operation = FleetOperation.objects.get(formup="Manual staging")
        self.assertEqual(operation.fc_user, self.fc)
        self.assertEqual(operation.created_by, self.fc)
        self.assertEqual(operation.status, CLOSED)
        self.assertTrue(operation.is_manual)
        self.assertIsNone(operation.esi_fleet_id)
        self.assertFalse(operation.tracking_enabled)
        self.assertFalse(operation.send_ping)

    def test_manual_attendance_ignores_record_flags(self):
        member_main = f.main_of(self.member)
        response = self.client.post(
            reverse("fleetops:add_manual_attendance", args=[self.operation.uuid]),
            {
                "character_id": member_main.character_id,
                "attendance_value": 1,
                "duplicate_action": "keep",
                "notes": "",
                "source": AttendanceRecord.Source.AUTOMATIC,
                "granted": "",
                "capped": "on",
                "created_by": self.other.pk,
                "auth_user": self.other.pk,
                "operation": self.other_operation.pk,
                "main_character_id": 1,
                "corporation_id": f.OTHER_CORP[0],
            },
        )
        self.assertEqual(response.status_code, 302)
        record = AttendanceRecord.objects.get(character_id=member_main.character_id)
        self.assertEqual(record.operation, self.operation)
        self.assertEqual(record.source, MANUAL)
        self.assertTrue(record.granted)
        self.assertFalse(record.capped)
        self.assertEqual(record.created_by, self.fc)
        self.assertEqual(record.auth_user, self.member)
        self.assertEqual(record.main_character_id, member_main.character_id)
        self.assertEqual(record.corporation_id, member_main.corporation_id)
        self.assertFalse(AttendanceRecord.objects.filter(operation=self.other_operation).exists())

    def test_role_assignment_cannot_force_fc_credit_or_untracked_characters(self):
        member_main = f.main_of(self.member)
        add_member_state(self.operation, member_main, self.member)
        url = reverse("fleetops:add_operation_role", args=[self.operation.uuid])
        self.client.post(
            url,
            {
                "role": OperationRoleAssignment.Role.BACKSEAT_FC,
                "character_id": member_main.character_id,
                "grants_fc_credit": "on",
                "auth_user": self.fc.pk,
                "assigned_by": self.other.pk,
            },
        )
        assignment = OperationRoleAssignment.objects.get(operation=self.operation)
        self.assertFalse(assignment.grants_fc_credit)
        self.assertEqual(assignment.auth_user, self.member)
        self.assertEqual(assignment.assigned_by, self.fc)

        # A character that was never tracked in this fleet cannot be given a role.
        self.client.post(url, {"role": OperationRoleAssignment.Role.BACKSEAT_FC, "character_id": f.main_of(self.other).character_id})
        self.assertFalse(OperationRoleAssignment.objects.filter(auth_user=self.other).exists())

    def test_start_fleet_always_records_requesting_user(self):
        fc_char = f.main_of(self.fc)
        f.add_token(self.fc, fc_char)
        response = self.client.post(
            reverse("fleetops:start_fleet"),
            {
                "request_id": str(uuid.uuid4()),
                "operation_mode": "full",
                "fc_character_id": str(fc_char.character_id),
                "fleet_type": self.fleet_type.pk,
                "formup": "Fresh start",
                "fc_user": self.other.pk,
                "created_by": self.other.pk,
                "status": CLOSED,
                "esi_fleet_id": 1,
                "attendance_multiplier": 3,
                "is_manual": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        operation = FleetOperation.objects.get(formup="Fresh start")
        self.assertEqual(operation.fc_user, self.fc)
        self.assertEqual(operation.created_by, self.fc)
        self.assertEqual(operation.status, ACTIVE)
        self.assertEqual(operation.esi_fleet_id, FLEET_ID)
        self.assertEqual(operation.attendance_multiplier, 1)
        self.assertFalse(operation.is_manual)


# ---------------------------------------------------------------------------
# ESI token ownership
# ---------------------------------------------------------------------------


class EsiTokenOwnershipTests(StartFleetPatchesMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fc = f.create_user(perms=f.FC_PERMS, main_name="Own FC")
        cls.fc_char = f.main_of(cls.fc)
        cls.fc_token = f.add_token(cls.fc, cls.fc_char)
        cls.other = f.create_user(perms=f.FC_PERMS, main_name="Neighbour FC")
        cls.other_char = f.main_of(cls.other)
        cls.other_token = f.add_token(cls.other, cls.other_char)
        cls.fleet_type = f.fleet_type("StratOp", "1.00")

    def setUp(self):
        esi_patcher = mock.patch("fleetops.providers.esi.esi")
        self.esi = esi_patcher.start()
        self.addCleanup(esi_patcher.stop)
        self.client.force_login(self.fc)

    def start_data(self, character, **extra):
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fc_character_id": str(character.character_id),
            "fleet_type": self.fleet_type.pk,
            "formup": "Jita",
        }
        data.update(extra)
        return data

    def test_detect_fleet_refuses_foreign_character(self):
        response = self.client.get(reverse("fleetops:detect_fleet", args=[self.other_char.character_id]))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "NOT_OWNED")
        self.esi.client.Fleets.GetCharactersCharacterIdFleet.assert_not_called()

    def test_start_form_rejects_foreign_character(self):
        with mock.patch("fleetops.services.operations.detect_character_fleet") as detect:
            response = self.client.post(reverse("fleetops:start_fleet"), self.start_data(self.other_char))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors.get("fc_character_id"))
        detect.assert_not_called()
        self.assertFalse(FleetOperation.objects.exists())

    def test_start_service_rejects_foreign_character(self):
        with mock.patch("fleetops.services.operations.detect_character_fleet") as detect:
            with self.assertRaises(PermissionError):
                start_fleet(
                    user=self.fc,
                    cleaned_data={
                        "operation_mode": "full",
                        "fc_character_id": str(self.other_char.character_id),
                        "fleet_type": self.fleet_type,
                        "formup": "Jita",
                    },
                )
        detect.assert_not_called()
        self.assertFalse(FleetOperation.objects.exists())

    def test_preview_rejects_foreign_character(self):
        response = self.client.post(reverse("fleetops:preview_fleet"), self.start_data(self.other_char))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])

    def test_start_form_lists_only_own_characters(self):
        response = self.client.get(reverse("fleetops:start_fleet"))
        self.assertContains(response, "Own FC")
        self.assertNotContains(response, "Neighbour FC")
        self.assertNotContains(response, str(self.other_char.character_id))

    def test_token_belonging_to_another_account_is_never_used(self):
        # The alt was transferred to the FC's account; the previous owner still holds an old fleet token.
        alt = f.add_alt(self.other, "Transferred Alt")
        f.add_token(self.other, alt)
        CharacterOwnership.objects.filter(character=alt).delete()
        CharacterOwnership.objects.create(user=self.fc, character=alt, owner_hash="hash-after-transfer")
        with self.assertRaises(FleetESIError) as caught:
            detect_character_fleet(self.fc, alt.character_id)
        self.assertEqual(caught.exception.code, "MISSING_READ_SCOPE")
        response = self.client.get(reverse("fleetops:detect_fleet", args=[alt.character_id]))
        self.assertEqual(response.json()["code"], "MISSING_READ_SCOPE")
        self.esi.client.Fleets.GetCharactersCharacterIdFleet.assert_not_called()

    def test_tokens_are_never_echoed_to_the_browser(self):
        self.esi.client.Fleets.GetCharactersCharacterIdFleet.return_value.result.return_value = {
            "fleet_id": FLEET_ID,
            "role": "fleet_commander",
        }
        pages = [
            self.client.get(reverse("fleetops:start_fleet")),
            self.client.get(reverse("fleetops:detect_fleet", args=[self.fc_char.character_id])),
        ]
        self.assertTrue(pages[1].json()["is_fleet_boss"])
        for page in pages:
            for value in (self.fc_token.access_token, self.fc_token.refresh_token, self.other_token.refresh_token):
                self.assertNotIn(value, page.content.decode())

    def test_kick_capsules_never_targets_fc_and_needs_write_scope(self):
        pilot = f.create_user(main_name="Pod Pilot")
        operation = f.create_operation(self.fc, type_obj=self.fleet_type, esi_fleet_id=FLEET_ID)
        add_member_state(operation, self.fc_char, self.fc, ship_type_id=CAPSULE_TYPE_ID, ship_type_name="Capsule", fleet_role="fleet_commander")
        add_member_state(operation, f.main_of(pilot), pilot, ship_type_id=CAPSULE_TYPE_ID, ship_type_name="Capsule")
        kick = self.esi.client.Fleets.DeleteFleetsFleetIdMembersMemberId
        url = reverse("fleetops:kick_capsules", args=[operation.uuid])

        self.client.post(url)
        kick.assert_called_once()
        self.assertEqual(kick.call_args.kwargs["member_id"], f.main_of(pilot).character_id)
        self.assertEqual(kick.call_args.kwargs["token"], self.fc_token)

        # Without the write scope no ESI call is made and the failure is reported.
        kick.reset_mock()
        self.fc_token.scopes.set(Scope.objects.filter(name=FLEET_READ_SCOPE))
        self.client.post(url)
        kick.assert_not_called()
        action = OperationAction.objects.get(operation=operation, action="kick_capsules")
        self.assertEqual(action.status, OperationAction.Status.FAILED)
        audit = AuditLog.objects.filter(action="fleet.kick_capsules").order_by("-pk").first()
        self.assertEqual(len(audit.new_value["failures"]), 1)

        # Closed fleets cannot be used to kick anyone.
        FleetOperation.objects.filter(pk=operation.pk).update(status=CLOSED)
        self.fc_token.scopes.set(Scope.objects.filter(name__startswith="esi-fleets."))
        self.assertEqual(self.client.post(url).status_code, 404)
        kick.assert_not_called()


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


class SecretHandlingTests(StartFleetPatchesMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lead = f.create_user(perms=f.FC_LEAD_PERMS, main_name="Lead Main")
        cls.member = f.create_user(perms=f.MEMBER_PERMS, main_name="Member Main")
        cls.webhook = DiscordWebhook.objects.create(name="Ops Pings", webhook_url=WEBHOOK_URL)
        cls.target = PingTarget.objects.create(name="Everyone", target_value="@everyone", webhook=cls.webhook)
        cls.fleet_type = f.fleet_type("StratOp", "1.00")
        cls.operation = f.create_operation(cls.lead, type_obj=cls.fleet_type, ping_target=cls.target, ping_text="Ping body")
        f.add_attendance(cls.operation, cls.member)

    def setUp(self):
        self.mocks = self.patch_externals()

    def assertNoSecret(self, response):
        self.assertNotIn(WEBHOOK_TOKEN, response.content.decode())

    def test_pages_never_render_webhook_url(self):
        self.client.force_login(self.lead)
        f.add_token(self.lead, f.main_of(self.lead))
        urls = [
            reverse("fleetops:configuration_index"),
            reverse("fleetops:configuration_list", args=["webhooks"]),
            reverse("fleetops:configuration_list", args=["ping-targets"]),
            reverse("fleetops:configuration_add", args=["ping-targets"]),
            reverse("fleetops:configuration_edit", args=["ping-targets", self.target.pk]),
            reverse("fleetops:start_fleet"),
            reverse("fleetops:operation_detail", args=[self.operation.uuid]),
            reverse("fleetops:audit_log"),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNoSecret(response)

    def test_webhook_edit_form_is_where_the_url_is_shown(self):
        self.client.force_login(self.lead)
        response = self.client.get(reverse("fleetops:configuration_edit", args=["webhooks", self.webhook.pk]))
        self.assertContains(response, WEBHOOK_TOKEN)

    def test_webhook_url_is_redacted_from_configuration_audit(self):
        self.client.force_login(self.lead)
        second_secret = "Second-secret-token-77"
        self.client.post(
            reverse("fleetops:configuration_add", args=["webhooks"]),
            {"name": "New Hook", "webhook_url": f"https://discord.com/api/webhooks/1/{second_secret}", "is_active": "on"},
        )
        hook = DiscordWebhook.objects.get(name="New Hook")
        self.client.post(
            reverse("fleetops:configuration_edit", args=["webhooks", hook.pk]),
            {"name": "New Hook", "webhook_url": WEBHOOK_URL, "is_active": "on"},
        )
        self.client.post(
            reverse("fleetops:configuration_add", args=["ping-targets"]),
            {"name": "Logi", "target_value": "@logi", "webhook": hook.pk, "is_active": "on"},
        )
        self.client.post(reverse("fleetops:configuration_delete", args=["webhooks", hook.pk]))

        entries = AuditLog.objects.filter(action__startswith="configuration.")
        self.assertEqual(entries.count(), 4)
        for entry in entries:
            payload = json.dumps([entry.old_value, entry.new_value, entry.reason])
            self.assertNotIn(WEBHOOK_TOKEN, payload)
            self.assertNotIn(second_secret, payload)
        audit_page = self.client.get(reverse("fleetops:audit_log"))
        self.assertNoSecret(audit_page)

    def test_successful_ping_records_no_webhook_url(self):
        self.client.force_login(self.lead)
        self.client.post(reverse("fleetops:retry_ping", args=[self.operation.uuid]), follow=True)
        self.mocks["post"].assert_called_once()
        self.assertEqual(self.mocks["post"].call_args.args[0], WEBHOOK_URL)
        self.assertEqual(self.mocks["post"].call_args.kwargs["json"], {"content": "Ping body"})
        action = OperationAction.objects.get(operation=self.operation, action="discord_ping")
        self.assertEqual(action.status, OperationAction.Status.SUCCESS)
        self.assertNotIn(WEBHOOK_TOKEN, action.error_message)

    def test_failed_ping_does_not_store_webhook_url(self):
        self.mocks["post"].side_effect = requests.ConnectionError(
            "HTTPSConnectionPool(host='discord.com', port=443): Max retries exceeded with url: "
            f"/api/webhooks/987654321/{WEBHOOK_TOKEN} (Caused by NameResolutionError('Failed to resolve discord.com'))"
        )
        self.client.force_login(self.lead)
        self.client.post(reverse("fleetops:retry_ping", args=[self.operation.uuid]))
        action = OperationAction.objects.get(operation=self.operation, action="discord_ping")
        self.assertEqual(action.status, OperationAction.Status.FAILED)
        self.assertNotIn(WEBHOOK_TOKEN, action.error_message)

    def test_members_never_see_webhook_url_after_failed_ping(self):
        broken_url = f"discord.com/api/webhooks/987654321/{WEBHOOK_TOKEN}"
        DiscordWebhook.objects.filter(pk=self.webhook.pk).update(webhook_url=broken_url)
        # Build the exact error requests raises for a URL without a scheme, without sending anything.
        with self.assertRaises(requests.exceptions.MissingSchema) as caught:
            requests.Request("POST", broken_url).prepare()
        self.mocks["post"].side_effect = caught.exception
        self.client.force_login(self.lead)
        self.client.post(reverse("fleetops:retry_ping", args=[self.operation.uuid]))

        self.client.force_login(self.member)
        response = self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))
        self.assertEqual(response.status_code, 200)
        self.assertNoSecret(response)

    def test_webhook_form_rejects_non_discord_targets(self):
        for url in (
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
            "http://127.0.0.1:6379/",
            "http://localhost:8000/admin/",
        ):
            with self.subTest(url=url):
                form = DiscordWebhookForm(data={"name": "Internal", "webhook_url": url, "is_active": "on"})
                self.assertFalse(form.is_valid())


class PingDeliveryTests(StartFleetPatchesMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fc = f.create_user(perms=f.FC_PERMS, main_name="Ping FC")
        cls.fc_char = f.main_of(cls.fc)
        cls.webhook = DiscordWebhook.objects.create(name="Ops Pings", webhook_url=WEBHOOK_URL)
        cls.quiet_target = PingTarget.objects.create(name="Fleet channel only", target_value="", webhook=cls.webhook)
        cls.fleet_type = f.fleet_type("StratOp", "1.00")

    def setUp(self):
        self.mocks = self.patch_externals()
        f.add_token(self.fc, self.fc_char)
        self.client.force_login(self.fc)

    def test_retry_does_not_ping_or_write_motd_for_attendance_only_fleet(self):
        operation = f.create_operation(
            self.fc, type_obj=self.fleet_type, send_ping=False, ping_target=self.quiet_target, ping_text="Quiet fleet"
        )
        for name in ("discord_ping", "motd_update"):
            OperationAction.objects.create(operation=operation, action=name, status=OperationAction.Status.SKIPPED)

        self.client.post(reverse("fleetops:retry_ping", args=[operation.uuid]))
        self.client.post(reverse("fleetops:retry_motd", args=[operation.uuid]))

        self.mocks["post"].assert_not_called()
        self.mocks["motd"].assert_not_called()
        statuses = dict(OperationAction.objects.filter(operation=operation).values_list("action", "status"))
        self.assertEqual(statuses["discord_ping"], OperationAction.Status.SKIPPED)
        self.assertEqual(statuses["motd_update"], OperationAction.Status.SKIPPED)


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------


class RedirectTests(StartFleetPatchesMixin, TestCase):
    EVIL = "https://evil.example/phish"

    @classmethod
    def setUpTestData(cls):
        f.settings(incentive_enabled=True)
        cls.lead = f.create_user(perms=f.FC_LEAD_PERMS, main_name="Lead Main")
        cls.other = f.create_user(perms=f.FC_PERMS, main_name="Other FC")
        cls.fleet_type = f.fleet_type("StratOp", "1.00")

    def setUp(self):
        self.mocks = self.patch_externals()

    def assertLocalRedirect(self, response, expected=None):
        self.assertEqual(response.status_code, 302)
        location = response["Location"]
        self.assertNotIn("evil.example", location)
        parsed = urlparse(location)
        self.assertEqual(parsed.netloc, "")
        if expected is not None:
            self.assertEqual(parsed.path, expected)

    def test_actions_ignore_next_parameters(self):
        self.client.force_login(self.lead)
        operation = f.create_operation(self.lead, type_obj=self.fleet_type)
        comms = CommsPreset.objects.create(name="Comms")
        period = IncentivePeriod.objects.create(year=2026, month=4)
        detail = reverse("fleetops:operation_detail", args=[operation.uuid])
        query = f"?next={self.EVIL}&redirect={self.EVIL}"
        payload = {"next": self.EVIL, "redirect": self.EVIL, "return_to": self.EVIL}

        self.assertLocalRedirect(
            self.client.post(reverse("fleetops:retry_srp", args=[operation.uuid]) + query, payload), detail
        )
        self.assertLocalRedirect(
            self.client.post(reverse("fleetops:end_fleet", args=[operation.uuid]) + query, dict(payload, attendance_multiplier=1)),
            detail,
        )
        self.assertLocalRedirect(
            self.client.post(reverse("fleetops:configuration_delete", args=["comms", comms.pk]) + query, payload),
            reverse("fleetops:configuration_list", args=["comms"]),
        )
        self.assertLocalRedirect(
            self.client.post(reverse("fleetops:incentive_recalculate", args=[period.pk]) + query, payload),
            reverse("fleetops:incentive_review"),
        )

    def test_start_fleet_redirects_to_its_own_operation(self):
        self.client.force_login(self.lead)
        fc_char = f.main_of(self.lead)
        f.add_token(self.lead, fc_char)
        response = self.client.post(
            reverse("fleetops:start_fleet") + f"?next={self.EVIL}",
            {
                "request_id": str(uuid.uuid4()),
                "operation_mode": "attendance_only",
                "fc_character_id": str(fc_char.character_id),
                "fleet_type": self.fleet_type.pk,
                "formup": "Jita",
                "next": self.EVIL,
            },
        )
        operation = FleetOperation.objects.get()
        self.assertLocalRedirect(response, reverse("fleetops:operation_detail", args=[operation.uuid]))

    def test_anonymous_login_redirect_keeps_next_local(self):
        response = self.client.get(reverse("fleetops:dashboard") + f"?next={self.EVIL}")
        self.assertEqual(response.status_code, 302)
        target = urlparse(response["Location"])
        next_values = parse_qs(target.query).get("next", [])
        self.assertTrue(next_values)
        for value in next_values:
            self.assertTrue(value.startswith("/fleetops/"))

    def test_authorize_esi_only_redirects_to_eve_sso(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse("fleetops:authorize_esi") + f"?next={self.EVIL}")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlparse(response["Location"]).netloc, urlparse(esi_app_settings.ESI_OAUTH_LOGIN_URL).netloc)
        callback = CallbackRedirect.objects.get(session_key=self.client.session.session_key)
        self.assertTrue(callback.url.startswith(reverse("fleetops:authorize_esi")))

    def test_authorize_esi_returns_to_start_page_after_callback(self):
        self.client.force_login(self.other)
        session_key = self.client.session.session_key
        token = f.add_token(self.other, f.main_of(self.other))
        CallbackRedirect.objects.create(
            session_key=session_key, state="state", url=reverse("fleetops:authorize_esi"), token=token
        )
        response = self.client.get(reverse("fleetops:authorize_esi") + f"?next={self.EVIL}")
        self.assertLocalRedirect(response, reverse("fleetops:start_fleet"))

    def test_authorize_esi_rejects_another_users_token(self):
        own = f.add_token(self.other, f.main_of(self.other))
        foreign = f.add_token(self.lead, f.main_of(self.lead))
        self.client.force_login(self.other)
        response = self.client.post(reverse("fleetops:authorize_esi"), {"_token": foreign.pk}, follow=True)
        self.assertNotContains(response, f"Fleet ESI token saved for {foreign.character_name}")
        response = self.client.post(reverse("fleetops:authorize_esi"), {"_token": own.pk})
        self.assertLocalRedirect(response, reverse("fleetops:start_fleet"))


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


class MalformedInputTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lead = f.create_user(perms=f.FC_LEAD_PERMS, main_name="Lead Main")
        cls.operation = f.create_operation(cls.lead, status=CLOSED)

    def setUp(self):
        self.client.force_login(self.lead)
        self.client.raise_request_exception = False

    def test_archive_ignores_garbage_filters(self):
        for query in ("?year=abc&month=xyz", "?year=99999&month=13", "?fleet_type=1e5&page=-1", "?page=abc&status=%00"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(reverse("fleetops:fleet_operations") + query).status_code, 200)

    def test_archive_fleet_type_filter_with_unicode_digit(self):
        response = self.client.get(reverse("fleetops:fleet_operations") + "?fleet_type=%C2%B2")
        self.assertEqual(response.status_code, 200)

    def test_archive_fleet_type_filter_out_of_range(self):
        response = self.client.get(reverse("fleetops:fleet_operations") + "?fleet_type=" + "9" * 30)
        self.assertEqual(response.status_code, 200)

    def test_manual_attendance_rejects_out_of_range_value(self):
        response = self.client.post(
            reverse("fleetops:add_manual_attendance", args=[self.operation.uuid]),
            {"character_id": f.main_of(self.lead).character_id, "attendance_value": 10**20, "duplicate_action": "keep"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(AttendanceRecord.objects.filter(operation=self.operation).exists())
