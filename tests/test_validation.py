"""Regression tests for configuration validation, seeding, pruning and admin auditing."""

import importlib
import re
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.apps import apps
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.forms import (
    DiscordWebhookForm,
    FleetOpsSettingsForm,
    FleetTypeForm,
    HistoricalManualAttendanceForm,
    IncentivePeriodForm,
    ManualAttendanceForm,
    MessageTemplateForm,
    OperationEditForm,
)
from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    CommsPreset,
    DiscordWebhook,
    FleetMemberEvent,
    FleetOperation,
    FleetOpsSettings,
    FleetType,
    IncentivePeriod,
    MessageTemplate,
)
from fleetops.services.history import prune_history, retention_cutoff
from fleetops.services.messages import render_operation_messages

from . import factories as f

WEBHOOK_TOKEN = "Qp4-validation-webhook-token"
WEBHOOK_URL = f"https://discord.com/api/webhooks/445566778899/{WEBHOOK_TOKEN}"
CONFIG_PERMS = f.MEMBER_PERMS + ["fleetops.manage_configuration"]


def run_command(name, *args):
    out = StringIO()
    call_command(name, *args, stdout=out, stderr=StringIO())
    return out.getvalue()


def settings_payload(**overrides):
    data = {
        "tracking_interval": "60",
        "stale_threshold": "180",
        "auto_end_enabled": "on",
        "auto_end_missing_count": "3",
        "incentive_minimum_fleets": "3",
        "data_retention_days": "365",
        "history_alliance_ids": "",
        "srp_auto_create": "on",
        "srp_provider": "auto",
    }
    data.update(overrides)
    return data


class MigrationTests(TestCase):
    def test_duplicate_defaults_are_reduced_to_the_one_in_use(self):
        migration = importlib.import_module("fleetops.migrations.0005_settings_validation_and_ordering")
        MessageTemplate.objects.create(name="Alpha Ping", template_type="ping", content="a")
        MessageTemplate.objects.filter(name="Alpha Ping").update(is_default=True)
        MessageTemplate.objects.create(name="Zulu Ping", template_type="ping", content="z")
        MessageTemplate.objects.filter(name="Zulu Ping").update(is_default=True)
        MessageTemplate.objects.create(name="Aardvark Ping", template_type="ping", content="x", is_active=False)
        MessageTemplate.objects.filter(name="Aardvark Ping").update(is_default=True)

        migration.keep_one_default_template(apps, None)

        self.assertEqual(
            list(MessageTemplate.objects.filter(is_default=True).values_list("template_type", "name")),
            [("motd", "Default MOTD"), ("ping", "Alpha Ping")],
        )


class RetentionTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.member = f.create_user()

    def _record(self, age_days):
        operation = f.create_operation(
            self.fc, status=FleetOperation.Status.CLOSED, started_at=timezone.now() - timedelta(days=age_days)
        )
        return f.add_attendance(operation, self.member)

    def test_cutoff_applies_the_one_year_floor(self):
        now = timezone.now()
        for days in (None, 0, 30, 365):
            with self.subTest(days=days):
                self.assertEqual(retention_cutoff(days, now=now), now - timedelta(days=365))
        self.assertEqual(retention_cutoff(36500, now=now), now - timedelta(days=36500))

    def test_cutoff_beyond_the_supported_range_keeps_everything(self):
        for days in (36501, 999_999, 10**12):
            with self.subTest(days=days):
                self.assertIsNone(retention_cutoff(days))

    def test_prune_with_legacy_huge_retention_keeps_history(self):
        f.settings(data_retention_days=999_999)
        old = self._record(5000)
        event = FleetMemberEvent.objects.create(
            operation=old.operation, character_id=1, character_name="Old", event_type="join"
        )
        FleetMemberEvent.objects.filter(pk=event.pk).update(created_at=timezone.now() - timedelta(days=5000))

        result = prune_history()

        self.assertEqual(result, {"old_attendance": 0, "left_alliance": 0, "old_events": 0})
        self.assertTrue(AttendanceRecord.objects.filter(pk=old.pk).exists())
        self.assertTrue(FleetMemberEvent.objects.filter(pk=event.pk).exists())

    def test_prune_with_legacy_huge_retention_still_removes_departed_members(self):
        f.settings(data_retention_days=999_999, history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        departed = f.create_user(alliance=f.FOREIGN_ALLIANCE)
        record = f.add_attendance(self._record(10).operation, departed)

        result = prune_history()

        self.assertEqual(result["left_alliance"], 1)
        self.assertFalse(AttendanceRecord.objects.filter(pk=record.pk).exists())

    def test_model_bounds(self):
        obj = FleetOpsSettings.get_solo()
        obj.data_retention_days = 36500
        obj.full_clean()
        for value in (364, 36501, 999_999):
            with self.subTest(value=value):
                obj.data_retention_days = value
                with self.assertRaises(ValidationError) as ctx:
                    obj.full_clean()
                self.assertIn("data_retention_days", ctx.exception.message_dict)

    def test_settings_page_rejects_huge_retention(self):
        self.client.force_login(f.create_user(perms=CONFIG_PERMS))

        response = self.client.post(
            reverse("fleetops:configuration_settings"), settings_payload(data_retention_days="1000000")
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("data_retention_days", response.context["form"].errors)
        self.assertEqual(FleetOpsSettings.get_solo().data_retention_days, 365)
        self.assertFalse(AuditLog.objects.exists())


class PruneTotalsTests(TestCase):
    def setUp(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        fc = f.create_user(perms=f.FC_PERMS)
        member = f.create_user()
        departed = f.create_user(alliance=f.FOREIGN_ALLIANCE)
        old_op = f.create_operation(fc, status=FleetOperation.Status.CLOSED, started_at=timezone.now() - timedelta(days=400))
        recent_op = f.create_operation(fc, status=FleetOperation.Status.CLOSED, started_at=timezone.now() - timedelta(days=10))
        f.add_attendance(old_op, member)
        f.add_attendance(old_op, departed)
        f.add_attendance(recent_op, departed)
        self.kept = f.add_attendance(recent_op, member)

    def test_dry_run_and_real_run_report_disjoint_totals(self):
        dry = prune_history(dry_run=True)
        self.assertEqual(AttendanceRecord.objects.count(), 4)

        real = prune_history()

        self.assertEqual(dry, real)
        self.assertEqual(real["old_attendance"], 2)
        self.assertEqual(real["left_alliance"], 1)
        self.assertEqual(list(AttendanceRecord.objects.all()), [self.kept])

    def test_command_output_matches_rows_removed(self):
        output = run_command("fleetops_prune_history")

        old_count, departed_count = map(int, re.search(r"(\d+) old attendance, (\d+) departed-member", output).groups())
        self.assertEqual(old_count + departed_count, 3)
        self.assertEqual(AttendanceRecord.objects.count(), 1)


class AllianceIdsValidationTests(TestCase):
    def _clean(self, value):
        obj = FleetOpsSettings.get_solo()
        obj.history_alliance_ids = value
        obj.full_clean()

    def test_numeric_lists_are_accepted(self):
        for value in ("", "  ", "3000001", "3000001, 3000002", "3000001;3000002", " 3000001 ; 3000002 , ", "99000001,"):
            with self.subTest(value=value):
                self._clean(value)

    def test_anything_else_is_rejected(self):
        for value in ("Foreign Alliance", "3000001, Foreign Alliance", "0", "-5", "1.5", "3000001 3000002", "²", "99999999999"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError) as ctx:
                    self._clean(value)
                self.assertIn("history_alliance_ids", ctx.exception.message_dict)

    def test_settings_form_shows_a_clear_error(self):
        form = FleetOpsSettingsForm(
            settings_payload(history_alliance_ids="3000001, Test Alliance"), instance=FleetOpsSettings.get_solo()
        )

        self.assertFalse(form.is_valid())
        self.assertIn("“Test Alliance” is not a valid alliance ID", form.errors["history_alliance_ids"][0])


class FleetTypeWeightTests(TestCase):
    def _form(self, weight):
        return FleetTypeForm({"name": "Roam", "short_name": "", "point_weight": weight, "is_active": "on", "sort_order": "0"})

    def test_negative_weight_is_rejected(self):
        form = self._form("-5")
        self.assertFalse(form.is_valid())
        self.assertIn("point_weight", form.errors)

    def test_zero_and_positive_weights_are_accepted(self):
        for weight in ("0", "0.00", "2.50"):
            with self.subTest(weight=weight):
                self.assertTrue(self._form(weight).is_valid())


class MessageTemplateRulesTests(TestCase):
    def _form(self, content, **extra):
        data = {"name": "Custom", "template_type": "ping", "content": content, "is_active": "on", **extra}
        return MessageTemplateForm(data)

    def test_syntax_errors_are_reported_on_the_content_field(self):
        for content in ("{% if %}x", "{% if fc %}x", "{% endif %}", "{% unknown_tag %}", "{{ fc|no_such_filter }}", "{% load nope %}"):
            with self.subTest(content=content):
                form = self._form(content)
                self.assertFalse(form.is_valid())
                self.assertTrue(form.errors["content"][0].startswith("Invalid template syntax:"))

    def test_valid_template_is_accepted(self):
        form = self._form("{% if fc %}FC {{ fc|upper }}{% endif %} at {{ formup }}")
        self.assertTrue(form.is_valid(), form.errors)

    def test_model_validation_rejects_broken_syntax(self):
        template = MessageTemplate(name="Broken", template_type="motd", content="{% for x in %}{% endfor %}")
        with self.assertRaises(ValidationError) as ctx:
            template.full_clean()
        self.assertIn("content", ctx.exception.message_dict)

    def test_marking_a_default_clears_other_defaults_of_the_same_type(self):
        form = self._form("NEW {{ formup }}", is_default="on")
        self.assertTrue(form.is_valid(), form.errors)
        new_default = form.save()

        self.assertEqual(
            list(MessageTemplate.objects.filter(template_type="ping", is_default=True)), [new_default]
        )
        self.assertTrue(MessageTemplate.objects.get(name="Default MOTD").is_default)
        operation = f.create_operation(f.create_user(perms=f.FC_PERMS), formup="Amarr")
        ping, _ = render_operation_messages(operation)
        self.assertEqual(ping, "NEW Amarr")

    def test_saving_a_non_default_keeps_the_existing_default(self):
        MessageTemplate.objects.create(name="Other", template_type="ping", content="x")
        self.assertTrue(MessageTemplate.objects.get(name="Default Ping").is_default)


class WebhookUrlValidationTests(TestCase):
    def _form(self, url):
        return DiscordWebhookForm(data={"name": "Ops", "webhook_url": url, "is_active": "on"})

    def test_discord_webhook_urls_are_accepted(self):
        for url in (
            WEBHOOK_URL,
            "https://discordapp.com/api/webhooks/1/abc_DEF-123",
            "https://ptb.discord.com/api/webhooks/1/token",
            "https://canary.discord.com/api/webhooks/1/token",
            f"{WEBHOOK_URL}?wait=true&thread_id=123",
            f"  {WEBHOOK_URL}  ",
        ):
            with self.subTest(url=url):
                self.assertTrue(self._form(url).is_valid())

    def test_other_targets_are_rejected_without_echoing_the_url(self):
        for url in (
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:6379/",
            f"http://discord.com/api/webhooks/1/{WEBHOOK_TOKEN}",
            f"https://discord.com.evil.example/api/webhooks/1/{WEBHOOK_TOKEN}",
            f"https://evil.example/?u=https://discord.com/api/webhooks/1/{WEBHOOK_TOKEN}",
            f"https://user@discord.com/api/webhooks/1/{WEBHOOK_TOKEN}",
            f"https://evil.example\\@discord.com/api/webhooks/1/{WEBHOOK_TOKEN}",
            f"https://discord.com:8443/api/webhooks/1/{WEBHOOK_TOKEN}",
            f"https://discord.com/api/v10/users/1/{WEBHOOK_TOKEN}",
            f"https://discord.com/api/webhooks/../../oauth2/{WEBHOOK_TOKEN}",
            f"discord.com/api/webhooks/1/{WEBHOOK_TOKEN}",
        ):
            with self.subTest(url=url):
                form = self._form(url)
                self.assertFalse(form.is_valid())
                self.assertNotIn(WEBHOOK_TOKEN, str(form.errors))

    def test_configuration_page_rejects_internal_address(self):
        self.client.force_login(f.create_user(perms=CONFIG_PERMS))

        response = self.client.post(
            reverse("fleetops:configuration_add", args=["webhooks"]),
            {"name": "Internal", "webhook_url": "http://localhost:8000/admin/", "is_active": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("webhook_url", response.context["form"].errors)
        self.assertFalse(DiscordWebhook.objects.exists())


class ConfigurationListRenderingTests(TestCase):
    def test_is_bool_filter(self):
        template = Template("{% load fleetops_config %}{{ a|is_bool }} {{ b|is_bool }} {{ c|is_bool }} {{ d|is_bool }}")
        rendered = template.render(Context({"a": True, "b": False, "c": 1, "d": Decimal("0.00")}))
        self.assertEqual(rendered, "True True False False")

    def test_numbers_render_as_values_and_booleans_as_badges(self):
        FleetType.objects.create(name="Standard", short_name="", point_weight=Decimal("1.00"), sort_order=1)
        FleetType.objects.create(name="Training", short_name="TR", point_weight=Decimal("0.00"), sort_order=0, is_active=False)
        self.client.force_login(f.create_user(perms=CONFIG_PERMS))

        response = self.client.get(reverse("fleetops:configuration_list", args=["fleet-types"]))

        content = response.content.decode()
        for cell in ("<td>1.00</td>", "<td>0.00</td>", "<td>1</td>", "<td>0</td>"):
            self.assertIn(cell, content)
        self.assertEqual(content.count('<span class="badge bg-success">Yes</span>'), 1)
        self.assertEqual(content.count('<span class="badge bg-secondary">No</span>'), 1)
        self.assertIn('<td><span class="text-muted">—</span></td>', content)


class OperationEditFormTests(TestCase):
    def setUp(self):
        self.operation = f.create_operation(f.create_user(perms=f.FC_PERMS), status=FleetOperation.Status.CLOSED)

    def _form(self, started, ended):
        return OperationEditForm(
            {
                "fleet_type": str(self.operation.fleet_type_id),
                "formup": "Jita",
                "started_at": started.strftime("%Y-%m-%dT%H:%M"),
                "ended_at": ended.strftime("%Y-%m-%dT%H:%M") if ended else "",
            },
            instance=self.operation,
        )

    def test_end_before_start_is_rejected(self):
        start = timezone.now()
        form = self._form(start, start - timedelta(minutes=1))
        self.assertFalse(form.is_valid())
        self.assertIn("End Time cannot be earlier than Start Time.", form.non_field_errors())

    def test_same_or_later_end_and_open_end_are_accepted(self):
        start = timezone.now()
        for ended in (start, start + timedelta(hours=2), None):
            with self.subTest(ended=ended):
                self.assertTrue(self._form(start, ended).is_valid())


class ManualAttendanceFormTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.pilot = f.create_user()
        self.operation = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED)

    def _data(self, **overrides):
        data = {
            "character_id": str(f.main_of(self.pilot).character_id),
            "attendance_value": "1",
            "duplicate_action": "keep",
        }
        data.update(overrides)
        return data

    def test_owned_character_within_bounds_is_accepted(self):
        for value in ("1", "100"):
            with self.subTest(value=value):
                self.assertTrue(ManualAttendanceForm(self._data(attendance_value=value)).is_valid())

    def test_out_of_range_values_are_rejected(self):
        for field, value in (
            ("attendance_value", "0"),
            ("attendance_value", "101"),
            ("attendance_value", str(10**20)),
            ("character_id", "0"),
            ("character_id", str(2**63)),
        ):
            with self.subTest(field=field, value=value):
                form = ManualAttendanceForm(self._data(**{field: value}))
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)

    def test_unowned_character_is_rejected_with_a_clear_error(self):
        unowned = f.create_character("Nobody's Alt")
        for form_class, extra in (
            (ManualAttendanceForm, {}),
            (HistoricalManualAttendanceForm, {"operation": str(self.operation.pk)}),
        ):
            with self.subTest(form=form_class.__name__):
                kwargs = {"user": self.fc} if form_class is HistoricalManualAttendanceForm else {}
                form = form_class(self._data(character_id=str(unowned.character_id), **extra), **kwargs)
                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.non_field_errors(),
                    [
                        f"Character {unowned.character_id} is not registered to any Alliance Auth user, "
                        "so it cannot receive attendance."
                    ],
                )

    def test_unowned_character_gets_no_attendance_through_the_views(self):
        self.client.force_login(self.fc)
        unknown_id = str(f.next_id())

        self.client.post(
            reverse("fleetops:add_manual_attendance", args=[self.operation.uuid]), self._data(character_id=unknown_id)
        )
        response = self.client.post(
            reverse("fleetops:manual_attendance"), {**self._data(character_id=unknown_id), "operation": str(self.operation.pk)}
        )

        self.assertContains(response, f"Character {unknown_id} is not registered to any Alliance Auth user")
        self.assertFalse(AttendanceRecord.objects.exists())
        self.assertFalse(AuditLog.objects.exists())


class IncentivePeriodFormTests(TestCase):
    def _form(self, year, month="1"):
        return IncentivePeriodForm({"year": year, "month": month, "budget": "0", "minimum_fleets": "1"})

    def test_supported_years_are_accepted(self):
        for year in ("2003", "2026", "2200"):
            with self.subTest(year=year):
                self.assertTrue(self._form(year).is_valid())

    def test_out_of_range_year_or_month_is_rejected(self):
        for year, month, field in (
            ("0", "1", "year"),
            ("2002", "1", "year"),
            ("2201", "1", "year"),
            ("99999", "1", "year"),
            ("2026", "0", "month"),
            ("2026", "13", "month"),
        ):
            with self.subTest(year=year, month=month):
                form = self._form(year, month)
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)
        self.assertFalse(IncentivePeriod.objects.exists())


class SeedCommandTests(TestCase):
    def test_rerun_keeps_customised_templates_and_fleet_types(self):
        run_command("fleetops_seed", "--demo")
        MessageTemplate.objects.filter(name="Default MOTD").update(content="Custom {{ fc }}", is_active=False)
        FleetType.objects.filter(name="Peacetime").update(point_weight=Decimal("0.75"), short_name="PT", sort_order=99)

        run_command("fleetops_seed", "--demo")

        motd = MessageTemplate.objects.get(name="Default MOTD")
        self.assertEqual((motd.content, motd.is_active), ("Custom {{ fc }}", False))
        peacetime = FleetType.objects.get(name="Peacetime")
        self.assertEqual(
            (peacetime.point_weight, peacetime.short_name, peacetime.sort_order), (Decimal("0.75"), "PT", 99)
        )

    def test_recreated_seed_template_does_not_take_over_a_custom_default(self):
        MessageTemplate.objects.filter(name="Default Ping").delete()
        custom = MessageTemplate.objects.create(name="Alliance Ping", template_type="ping", content="x", is_default=True)

        run_command("fleetops_seed")

        recreated = MessageTemplate.objects.get(name="Default Ping")
        self.assertFalse(recreated.is_default)
        custom.refresh_from_db()
        self.assertTrue(custom.is_default)

    def test_missing_seed_template_becomes_default_when_none_exists(self):
        MessageTemplate.objects.filter(template_type="motd").delete()

        run_command("fleetops_seed")

        self.assertTrue(MessageTemplate.objects.get(name="Default MOTD").is_default)


class AdminBulkDeleteAuditTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser("root", "root@example.com", "password")
        self.client.force_login(self.superuser)

    def _bulk_delete(self, model, objects):
        meta = model._meta
        return self.client.post(
            reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist"),
            {"action": "delete_selected", "_selected_action": [str(obj.pk) for obj in objects], "post": "yes"},
        )

    def test_each_deleted_object_is_audited(self):
        comms = [CommsPreset.objects.create(name=f"Comms {i}") for i in range(3)]

        response = self._bulk_delete(CommsPreset, comms)

        self.assertEqual(response.status_code, 302)
        self.assertFalse(CommsPreset.objects.exists())
        entries = AuditLog.objects.filter(action="configuration.delete", object_type="CommsPreset")
        self.assertEqual(set(entries.values_list("object_id", flat=True)), {str(c.pk) for c in comms})
        for entry in entries:
            self.assertEqual(entry.actor, self.superuser)
            self.assertIsNone(entry.new_value)
            self.assertTrue(entry.old_value["name"].startswith("Comms "))

    def test_bulk_deleted_webhook_audit_is_redacted(self):
        hook = DiscordWebhook.objects.create(name="Bulk Hook", webhook_url=WEBHOOK_URL)

        self._bulk_delete(DiscordWebhook, [hook])

        entry = AuditLog.objects.get(action="configuration.delete", object_type="DiscordWebhook")
        self.assertEqual(entry.old_value["webhook_url"], "***redacted***")
        self.assertNotIn(WEBHOOK_TOKEN, str(entry.old_value))
