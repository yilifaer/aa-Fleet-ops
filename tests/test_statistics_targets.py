"""Tests for the monthly targets, the leaderboard settings and the Monthly Targets configuration section."""

from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.admin.sites import site as admin_site
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from fleetops.admin import StatisticsTargetAdmin
from fleetops.forms_config import FleetOpsSettingsForm, StatisticsTargetForm
from fleetops.models import AuditLog, FleetOpsSettings, StatisticsTarget
from fleetops.views_config import CONFIG_SECTIONS

from . import factories as f
from .test_configuration_audit import CONFIG_PERMS, NO_CONFIG_PERMS, settings_payload

UTC = dt_timezone.utc
SECTION = "statistics-targets"


def payload(**overrides):
    data = {"year": "2026", "month": "10", "corporation_average_target": "2.50", "member_minimum_pap": "1", "note": ""}
    data.update(overrides)
    return data


class StatisticsTargetModelTests(TestCase):
    def test_defaults(self):
        target = StatisticsTarget.objects.create(year=2026, month=10)
        self.assertIsNone(target.corporation_average_target)
        self.assertEqual(target.member_minimum_pap, 1)
        self.assertEqual(target.note, "")

    def test_effective_from_and_text(self):
        target = f.statistics_target(2026, 3, "2.50")
        self.assertEqual(target.effective_from, "2026-03")
        self.assertEqual(str(target), "From 2026-03")

    def test_newest_first(self):
        f.statistics_target(2025, 12)
        f.statistics_target(2026, 2)
        f.statistics_target(2026, 10)
        self.assertEqual([t.effective_from for t in StatisticsTarget.objects.all()], ["2026-10", "2026-02", "2025-12"])

    def test_month_is_unique(self):
        f.statistics_target(2026, 10)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StatisticsTarget.objects.create(year=2026, month=10)

    def test_validation_bounds(self):
        cases = [
            {"year": 2002, "month": 1},
            {"year": 2201, "month": 1},
            {"year": 2026, "month": 0},
            {"year": 2026, "month": 13},
            {"year": 2026, "month": 1, "corporation_average_target": Decimal("-0.01")},
            {"year": 2026, "month": 1, "member_minimum_pap": 10001},
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                StatisticsTarget(**values).full_clean()

    def test_valid_extremes(self):
        StatisticsTarget(year=2003, month=1, corporation_average_target=Decimal("0"), member_minimum_pap=0).full_clean()
        StatisticsTarget(
            year=2200, month=12, corporation_average_target=Decimal("999999.99"), member_minimum_pap=10000
        ).full_clean()


class LeaderboardSettingsTests(TestCase):
    def test_defaults(self):
        settings_obj = FleetOpsSettings.get_solo()
        self.assertEqual(settings_obj.leaderboard_size, 20)
        self.assertEqual(settings_obj.leaderboard_visibility, "members")
        self.assertEqual(settings_obj.leaderboard_visibility, FleetOpsSettings.LeaderboardVisibility.MEMBERS)

    def test_choices(self):
        choices = dict(FleetOpsSettings._meta.get_field("leaderboard_visibility").choices)
        self.assertEqual(
            choices, {"members": "Everyone with FleetOps access", "managers": "Statistics managers only"}
        )

    def test_size_bounds(self):
        for size, valid in ((2, False), (3, True), (100, True), (101, False)):
            with self.subTest(size=size):
                settings_obj = FleetOpsSettings.get_solo()
                settings_obj.leaderboard_size = size
                if valid:
                    settings_obj.full_clean()
                else:
                    with self.assertRaises(ValidationError) as ctx:
                        settings_obj.full_clean()
                    self.assertIn("leaderboard_size", ctx.exception.message_dict)

    def test_member_alliance_ids_label(self):
        field = FleetOpsSettings._meta.get_field("history_alliance_ids")
        self.assertEqual(field.verbose_name, "Member alliance IDs")
        self.assertIn("Empty = every Alliance Auth user with a main character", field.help_text)
        self.assertEqual(field.column, "history_alliance_ids")

    def test_settings_form_has_the_new_fields_in_the_statistics_group(self):
        form = FleetOpsSettingsForm(instance=FleetOpsSettings.get_solo())
        self.assertIn("leaderboard_size", form.fields)
        self.assertIn("leaderboard_visibility", form.fields)
        groups = {title: [field.name for field in fields] for title, _text, fields in form.field_groups()}
        self.assertEqual(
            groups["Statistics"],
            ["minimum_fleet_minutes", "history_alliance_ids", "leaderboard_size", "leaderboard_visibility"],
        )
        self.assertNotIn("history_alliance_ids", groups["General"])

    def test_settings_form_saves_the_new_fields(self):
        form = FleetOpsSettingsForm(
            settings_payload(leaderboard_size="30", leaderboard_visibility="managers"),
            instance=FleetOpsSettings.get_solo(),
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        settings_obj = FleetOpsSettings.get_solo()
        self.assertEqual(settings_obj.leaderboard_size, 30)
        self.assertEqual(settings_obj.leaderboard_visibility, "managers")

    def test_settings_form_rejects_bad_values(self):
        form = FleetOpsSettingsForm(
            settings_payload(leaderboard_size="2", leaderboard_visibility="everybody"),
            instance=FleetOpsSettings.get_solo(),
        )
        self.assertFalse(form.is_valid())
        self.assertIn("leaderboard_size", form.errors)
        self.assertIn("leaderboard_visibility", form.errors)


class StatisticsTargetFormTests(TestCase):
    def test_valid(self):
        form = StatisticsTargetForm(payload(note="Spring"))
        self.assertTrue(form.is_valid(), form.errors)
        target = form.save()
        self.assertEqual((target.year, target.month), (2026, 10))
        self.assertEqual(target.corporation_average_target, Decimal("2.50"))
        self.assertEqual(target.note, "Spring")

    def test_month_is_a_choice_of_month_names(self):
        form = StatisticsTargetForm()
        choices = list(form.fields["month"].choices)
        self.assertEqual(choices[0], (1, "January"))
        self.assertEqual(choices[11], (12, "December"))
        self.assertEqual(len(choices), 12)

    def test_month_is_an_integer(self):
        form = StatisticsTargetForm(payload(month="3"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["month"], 3)

    def test_invalid_month(self):
        for value in ("0", "13", "march", ""):
            with self.subTest(value=value):
                form = StatisticsTargetForm(payload(month=value))
                self.assertFalse(form.is_valid())
                self.assertIn("month", form.errors)

    def test_no_target_is_allowed(self):
        form = StatisticsTargetForm(payload(corporation_average_target=""))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.save().corporation_average_target)

    def test_a_negative_target_is_rejected(self):
        form = StatisticsTargetForm(payload(corporation_average_target="-1"))
        self.assertFalse(form.is_valid())
        self.assertIn("corporation_average_target", form.errors)

    def test_a_month_cannot_have_two_targets(self):
        f.statistics_target(2026, 10, "1.00")
        form = StatisticsTargetForm(payload())
        self.assertFalse(form.is_valid())
        self.assertEqual(form.non_field_errors(), ["A target for this month already exists."])

    def test_editing_a_target_keeps_its_month(self):
        target = f.statistics_target(2026, 10, "1.00")
        form = StatisticsTargetForm(payload(corporation_average_target="4"), instance=target)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        target.refresh_from_db()
        self.assertEqual(target.corporation_average_target, Decimal("4.00"))

    def test_moving_a_target_onto_another_month_is_rejected(self):
        f.statistics_target(2026, 9)
        target = f.statistics_target(2026, 10)
        form = StatisticsTargetForm(payload(month="9"), instance=target)
        self.assertFalse(form.is_valid())
        self.assertEqual(form.non_field_errors(), ["A target for this month already exists."])

    def test_a_new_entry_starts_with_the_current_eve_month(self):
        with mock.patch("fleetops.services.periods.timezone.now", return_value=datetime(2026, 12, 31, 23, 30, tzinfo=UTC)):
            form = StatisticsTargetForm()
        self.assertEqual((form.initial["year"], form.initial["month"]), (2026, 12))
        self.assertEqual(form["month"].value(), 12)

    def test_an_existing_entry_shows_its_own_month(self):
        target = f.statistics_target(2025, 3)
        form = StatisticsTargetForm(instance=target)
        self.assertEqual((form.initial["year"], form.initial["month"]), (2025, 3))


class StatisticsTargetConfigurationTests(TestCase):
    def setUp(self):
        self.admin = f.create_user(perms=CONFIG_PERMS)
        self.client.force_login(self.admin)

    def entries(self, action):
        return list(AuditLog.objects.filter(action=action, object_type="StatisticsTarget").order_by("pk"))

    def test_section_is_defined(self):
        config = CONFIG_SECTIONS[SECTION]
        self.assertEqual(config["title"], "Monthly Targets")
        self.assertEqual(config["model"], StatisticsTarget)
        self.assertEqual(config["form"], StatisticsTargetForm)
        self.assertEqual(
            [column[1] for column in config["columns"]],
            ["effective_from", "corporation_average_target", "member_minimum_pap", "note"],
        )
        self.assertIn("EVE time", config["description"])

    def test_index_lists_the_section(self):
        f.statistics_target(2026, 1)
        response = self.client.get(reverse("fleetops:configuration_index"))
        self.assertContains(response, "Monthly Targets")
        self.assertContains(response, reverse("fleetops:configuration_list", args=[SECTION]))

    def test_list_shows_the_columns(self):
        f.statistics_target(2026, 3, "2.50", minimum=4, note="Spring")
        f.statistics_target(2026, 9)
        response = self.client.get(reverse("fleetops:configuration_list", args=[SECTION]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Corporation average target")
        self.assertContains(response, "Member minimum PAP")
        self.assertContains(response, "2026-03")
        self.assertContains(response, "2.50")
        self.assertContains(response, "Spring")
        self.assertContains(response, "2026-09")

    def test_add_form_is_prefilled_with_the_current_month(self):
        with mock.patch("fleetops.services.periods.timezone.now", return_value=datetime(2026, 10, 12, tzinfo=UTC)):
            response = self.client.get(reverse("fleetops:configuration_add", args=[SECTION]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="2026"')
        self.assertContains(response, '<option value="10" selected>October</option>', html=True)

    def test_create_audits(self):
        response = self.client.post(reverse("fleetops:configuration_add", args=[SECTION]), payload(note="Autumn"))
        self.assertRedirects(response, reverse("fleetops:configuration_list", args=[SECTION]))
        target = StatisticsTarget.objects.get()
        self.assertEqual(target.corporation_average_target, Decimal("2.50"))
        (entry,) = self.entries("configuration.create")
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.object_id, str(target.pk))
        self.assertIsNone(entry.old_value)
        self.assertEqual(entry.new_value["year"], 2026)
        self.assertEqual(entry.new_value["month"], 10)
        self.assertEqual(entry.new_value["corporation_average_target"], "2.50")
        self.assertEqual(entry.new_value["note"], "Autumn")

    def test_update_audits_old_and_new(self):
        target = f.statistics_target(2026, 10, "2.50", minimum=1)
        response = self.client.post(
            reverse("fleetops:configuration_edit", args=[SECTION, target.pk]),
            payload(corporation_average_target="3.00", member_minimum_pap="2"),
        )
        self.assertRedirects(response, reverse("fleetops:configuration_list", args=[SECTION]))
        (entry,) = self.entries("configuration.update")
        self.assertEqual(entry.old_value["corporation_average_target"], "2.50")
        self.assertEqual(entry.new_value["corporation_average_target"], "3.00")
        self.assertEqual(entry.old_value["member_minimum_pap"], 1)
        self.assertEqual(entry.new_value["member_minimum_pap"], 2)

    def test_duplicate_month_is_refused_with_a_message(self):
        f.statistics_target(2026, 10)
        response = self.client.post(reverse("fleetops:configuration_add", args=[SECTION]), payload())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A target for this month already exists.")
        self.assertEqual(StatisticsTarget.objects.count(), 1)
        self.assertEqual(self.entries("configuration.create"), [])

    def test_delete_audits(self):
        target = f.statistics_target(2026, 10, "2.50")
        response = self.client.post(reverse("fleetops:configuration_delete", args=[SECTION, target.pk]))
        self.assertRedirects(response, reverse("fleetops:configuration_list", args=[SECTION]))
        self.assertFalse(StatisticsTarget.objects.exists())
        (entry,) = self.entries("configuration.delete")
        self.assertEqual(entry.old_value["corporation_average_target"], "2.50")
        self.assertIsNone(entry.new_value)

    def test_needs_the_configuration_permission(self):
        other = f.create_user(perms=NO_CONFIG_PERMS)
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("fleetops:configuration_list", args=[SECTION])).status_code, 403)
        self.assertEqual(self.client.post(reverse("fleetops:configuration_add", args=[SECTION]), payload()).status_code, 403)
        self.assertFalse(StatisticsTarget.objects.exists())

    def test_settings_page_posts_the_new_fields(self):
        response = self.client.post(
            reverse("fleetops:configuration_settings"),
            settings_payload(leaderboard_size="25", leaderboard_visibility="managers"),
        )
        self.assertRedirects(response, reverse("fleetops:configuration_index"))
        settings_obj = FleetOpsSettings.get_solo()
        self.assertEqual((settings_obj.leaderboard_size, settings_obj.leaderboard_visibility), (25, "managers"))
        entry = AuditLog.objects.filter(action="configuration.update", object_type="FleetOpsSettings").latest("pk")
        self.assertEqual(entry.old_value["leaderboard_size"], 20)
        self.assertEqual(entry.new_value["leaderboard_size"], 25)
        self.assertEqual(entry.new_value["leaderboard_visibility"], "managers")

    def test_settings_page_shows_the_statistics_group(self):
        response = self.client.get(reverse("fleetops:configuration_settings"))
        self.assertContains(response, "Leaderboard places")
        self.assertContains(response, "Leaderboard visibility")
        self.assertContains(response, "Member alliance IDs")


class StatisticsTargetAdminTests(TestCase):
    def test_registered_with_the_audit_mixin(self):
        model_admin = admin_site._registry[StatisticsTarget]
        self.assertIsInstance(model_admin, StatisticsTargetAdmin)
        self.assertEqual(
            model_admin.list_display, ("effective_from", "corporation_average_target", "member_minimum_pap", "note")
        )

    def test_admin_changes_are_audited(self):
        admin_user = f.create_user(perms=[])
        admin_user.is_staff = admin_user.is_superuser = True
        admin_user.save()
        self.client.force_login(admin_user)
        response = self.client.post(
            reverse("admin:fleetops_statisticstarget_add"),
            {"year": "2026", "month": "10", "corporation_average_target": "2.00", "member_minimum_pap": "1", "note": ""},
        )
        self.assertEqual(response.status_code, 302)
        target = StatisticsTarget.objects.get()
        entry = AuditLog.objects.get(action="configuration.create", object_type="StatisticsTarget")
        self.assertEqual(entry.actor, admin_user)
        self.assertEqual(entry.object_id, str(target.pk))
        self.assertEqual(entry.new_value["corporation_average_target"], "2.00")

    def test_changelist_renders(self):
        f.statistics_target(2026, 3, "2.50")
        admin_user = f.create_user(perms=[])
        admin_user.is_staff = admin_user.is_superuser = True
        admin_user.save()
        self.client.force_login(admin_user)
        response = self.client.get(reverse("admin:fleetops_statisticstarget_changelist"))
        self.assertContains(response, "2026-03")
