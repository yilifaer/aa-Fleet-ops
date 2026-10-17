# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Migrations 0019-0021: new fields, the default ping and MOTD, the step texts of old versions and group access.

Every one of them can be reversed with rows present, and the data steps only touch what they are meant to."""

import importlib
import inspect
from io import StringIO

from django.apps import apps
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone, translation

from fleetops import texts
from fleetops.constants import DEFAULT_MOTD_TEMPLATE, DEFAULT_PING_TEMPLATE
from fleetops.models import (
    FleetOperation,
    FleetOpsSettings,
    GroupAccess,
    MessageTemplate,
    OperationAction,
    OperationActionAttempt,
)

from . import factories as f

fields = importlib.import_module("fleetops.migrations.0019_b2_fields")
data = importlib.import_module("fleetops.migrations.0020_b2_data")
groups = importlib.import_module("fleetops.migrations.0021_group_access")
ping_0009 = importlib.import_module("fleetops.migrations.0009_convert_ping_targets_and_presets")
motd_0017 = importlib.import_module("fleetops.migrations.0017_srp_data")

OLD_STEP_TEXTS = (
    ("fleet_end", "Manual", "Ended manually"),
    ("fleet_end", "Automatic", "Ended automatically: the fleet no longer exists or the FC left it"),
    ("discord_ping", "Attendance-only mode: Discord ping intentionally skipped.", None),
    ("motd_update", "Attendance-only mode: MOTD update intentionally skipped.", None),
    ("srp_link", "Attendance-only mode: SRP creation intentionally skipped.", None),
    (
        "discord_ping",
        "No active webhook configured. Ping remains available for manual copy.",
        "No Discord channel selected; the ping is available to copy.",
    ),
    ("fleet_detection", "Fleet no longer exists.", "The fleet no longer exists."),
)
UNTOUCHED_STEP_TEXTS = (
    ("discord_ping", "Manual fleet: Discord ping intentionally skipped."),
    ("discord_ping", "Manual"),
    ("fleet_detection", "Automatic"),
    ("discord_ping", "Attendance-only mode: Something else intentionally skipped."),
    ("discord_ping", "Attendance-only mode: Discord ping intentionally skipped. Try again."),
    ("discord_ping", "PAP-only mode: Discord ping intentionally skipped."),
    ("fleet_detection", "The fleet no longer exists."),
    ("fleet_detection", "Could not read fleet information: Fleet no longer exists."),
)


def expected_text(text, rewritten):
    if rewritten is not None:
        return rewritten
    return text.replace("Attendance-only mode", "PAP-only mode")


class MigrationModuleTests(TestCase):
    def test_migrations_do_not_import_fleetops_code(self):
        # They must keep working when the constants or models they were written against change.
        for module in (fields, data, groups):
            source = inspect.getsource(module)
            with self.subTest(module=module.__name__):
                self.assertNotIn("from fleetops", source)
                self.assertNotIn("import fleetops", source)

    def test_they_follow_each_other(self):
        self.assertEqual(fields.Migration.dependencies, [("fleetops", "0018_translatable_labels")])
        self.assertEqual(data.Migration.dependencies, [("fleetops", "0019_b2_fields")])
        self.assertIn(("fleetops", "0020_b2_data"), groups.Migration.dependencies)
        self.assertIn(("auth", "0012_alter_user_first_name_max_length"), groups.Migration.dependencies)
        # Leaders are read from Alliance Auth when they are needed, so no migration of its group management is needed.
        self.assertFalse([dependency for dependency in groups.Migration.dependencies if "groupmanagement" in dependency[0]])

    def test_the_models_need_no_further_migration(self):
        call_command("makemigrations", "fleetops", check=True, dry_run=True, stdout=StringIO(), stderr=StringIO())

    def test_the_copies_of_the_default_templates_match_the_app(self):
        self.assertEqual(data.OLD_PING, ping_0009.NEW_PING)
        self.assertEqual(data.NEW_PING, DEFAULT_PING_TEMPLATE)
        self.assertEqual(data.OLD_MOTD, motd_0017.NEW_MOTD)
        self.assertEqual(data.NEW_MOTD, DEFAULT_MOTD_TEMPLATE)

    def test_the_new_default_motd_only_swaps_the_two_variables(self):
        swapped = data.NEW_MOTD.replace("{{ fc_link }}", "{{ fc }}").replace("{{ formup_link }}", "{{ formup }}")

        self.assertEqual(swapped, data.OLD_MOTD)
        self.assertNotEqual(data.NEW_MOTD, data.OLD_MOTD)

    def test_the_new_default_ping_keeps_the_english_footer(self):
        footer = "-# This was a FleetOps broadcast from {{ sender }} to {{ ping_target_label }} at {{ sent_at }} EVE"

        self.assertTrue(data.OLD_PING.endswith(footer))
        self.assertTrue(data.NEW_PING.endswith(footer))

    def test_the_copies_of_the_step_texts_are_the_stored_texts(self):
        self.assertEqual(data.END_MANUAL, texts.END_MANUAL())
        self.assertEqual(data.END_FLEET_GONE, texts.END_FLEET_GONE())
        self.assertEqual(data.NO_PING_CHANNEL, texts.NO_PING_CHANNEL())
        self.assertEqual(data.JOB_FLEET_MISSING, texts.JOB_FLEET_MISSING())
        steps = {"Discord ping", "MOTD update", "SRP creation"}
        for step in steps:
            current = texts.STEP_SKIPPED(mode=texts.MODE_PAP_ONLY(), step=texts.STEP_NAMES[step]())
            self.assertEqual(current, f"PAP-only mode: {step} intentionally skipped.")
            self.assertTrue(data.ATTENDANCE_ONLY.fullmatch(f"Attendance-only mode: {step} intentionally skipped."))
            self.assertIsNone(data.ATTENDANCE_ONLY.fullmatch(current))


class DefaultTemplateTests(TestCase):
    def set_content(self, name, content, template_type="ping"):
        return MessageTemplate.objects.update_or_create(
            template_type=template_type, name=name, defaults={"content": content}
        )[0]

    def content(self, name, template_type="ping"):
        return MessageTemplate.objects.get(template_type=template_type, name=name).content

    # --- ping ---

    def test_an_untouched_default_ping_is_updated(self):
        self.set_content("Default Ping", data.OLD_PING)

        data.update_default_ping(apps, None)

        self.assertEqual(self.content("Default Ping"), data.NEW_PING)

    def test_an_untouched_default_ping_saved_with_windows_line_ends_is_updated(self):
        self.set_content("Default Ping", data.OLD_PING.replace("\n", "\r\n") + "\r\n")

        data.update_default_ping(apps, None)

        self.assertEqual(self.content("Default Ping"), data.NEW_PING)

    def test_a_copy_under_another_name_is_updated_too(self):
        self.set_content("Alliance Ping", data.OLD_PING)

        data.update_default_ping(apps, None)

        self.assertEqual(self.content("Alliance Ping"), data.NEW_PING)

    def test_an_edited_ping_is_left_alone(self):
        edited = data.OLD_PING.replace("**FC Name:**", "**Commander:**")
        self.set_content("Default Ping", edited)

        data.update_default_ping(apps, None)

        self.assertEqual(self.content("Default Ping"), edited)

    def test_a_ping_template_holding_another_text_is_left_alone(self):
        self.set_content("Default Ping", data.OLD_MOTD)
        self.set_content("Default MOTD", data.OLD_PING, template_type="motd")

        data.update_default_ping(apps, None)

        self.assertEqual(self.content("Default Ping"), data.OLD_MOTD)
        self.assertEqual(self.content("Default MOTD", "motd"), data.OLD_PING)

    def test_the_ping_update_is_idempotent(self):
        self.set_content("Default Ping", data.OLD_PING)

        data.update_default_ping(apps, None)
        data.update_default_ping(apps, None)

        self.assertEqual(self.content("Default Ping"), data.NEW_PING)

    def test_going_back_restores_the_old_ping(self):
        self.set_content("Default Ping", data.NEW_PING)

        data.restore_default_ping(apps, None)

        self.assertEqual(self.content("Default Ping"), data.OLD_PING)

    def test_going_back_keeps_a_ping_edited_after_the_upgrade(self):
        edited = data.NEW_PING.replace("**FC Name:**", "**Commander:**")
        self.set_content("Default Ping", edited)

        data.restore_default_ping(apps, None)

        self.assertEqual(self.content("Default Ping"), edited)

    def test_the_other_fields_of_the_ping_stay(self):
        MessageTemplate.objects.filter(template_type="ping", name="Default Ping").update(
            content=data.OLD_PING, is_default=True, is_active=True
        )

        data.update_default_ping(apps, None)

        ping = MessageTemplate.objects.get(template_type="ping", name="Default Ping")
        self.assertTrue(ping.is_default and ping.is_active)

    # --- MOTD ---

    def test_an_untouched_default_motd_is_updated(self):
        self.set_content("Default MOTD", data.OLD_MOTD, "motd")

        data.update_default_motd(apps, None)

        self.assertEqual(self.content("Default MOTD", "motd"), data.NEW_MOTD)

    def test_an_untouched_default_motd_saved_with_windows_line_ends_is_updated(self):
        self.set_content("Default MOTD", data.OLD_MOTD + "\r\n", "motd")

        data.update_default_motd(apps, None)

        self.assertEqual(self.content("Default MOTD", "motd"), data.NEW_MOTD)

    def test_a_copy_of_the_old_motd_under_another_name_is_updated_too(self):
        self.set_content("Alliance MOTD", data.OLD_MOTD, "motd")

        data.update_default_motd(apps, None)

        self.assertEqual(self.content("Alliance MOTD", "motd"), data.NEW_MOTD)

    def test_an_edited_motd_is_left_alone(self):
        edited = data.OLD_MOTD.replace("Form Up:", "Staging:")
        self.set_content("Default MOTD", edited, "motd")
        self.set_content("Custom MOTD", "<b>{{ fleet_type }}</b> {{ fc }}", "motd")

        data.update_default_motd(apps, None)

        self.assertEqual(self.content("Default MOTD", "motd"), edited)
        self.assertEqual(self.content("Custom MOTD", "motd"), "<b>{{ fleet_type }}</b> {{ fc }}")

    def test_a_motd_that_already_uses_the_links_is_left_alone(self):
        self.set_content("Default MOTD", data.NEW_MOTD, "motd")

        data.update_default_motd(apps, None)

        self.assertEqual(self.content("Default MOTD", "motd"), data.NEW_MOTD)

    def test_the_motd_update_is_idempotent(self):
        self.set_content("Default MOTD", data.OLD_MOTD, "motd")

        data.update_default_motd(apps, None)
        data.update_default_motd(apps, None)

        self.assertEqual(self.content("Default MOTD", "motd"), data.NEW_MOTD)

    def test_going_back_restores_the_old_motd(self):
        self.set_content("Default MOTD", data.NEW_MOTD, "motd")

        data.restore_default_motd(apps, None)

        self.assertEqual(self.content("Default MOTD", "motd"), data.OLD_MOTD)

    def test_going_back_keeps_a_motd_edited_after_the_upgrade(self):
        edited = data.NEW_MOTD.replace("Form Up:", "Staging:")
        self.set_content("Default MOTD", edited, "motd")

        data.restore_default_motd(apps, None)

        self.assertEqual(self.content("Default MOTD", "motd"), edited)

    def test_a_new_installation_gets_the_current_defaults(self):
        # 0002 creates the first defaults and 0009 and 0017 and 0020 bring them up to date.
        self.assertEqual(self.content("Default Ping"), DEFAULT_PING_TEMPLATE)
        self.assertEqual(self.content("Default MOTD", "motd"), DEFAULT_MOTD_TEMPLATE)


class LegacyStepTextTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc)

    def step(self, action, text, operation=None):
        step = OperationAction.objects.create(
            operation=operation or self.operation,
            action=action,
            status=OperationAction.Status.SUCCESS,
            error_message=text,
        )
        attempt = OperationActionAttempt.objects.create(action=step, status=step.status, message=text)
        return step, attempt

    def read(self, step, attempt):
        step.refresh_from_db()
        attempt.refresh_from_db()
        return step.error_message, attempt.message

    def test_every_legacy_form_is_rewritten_in_every_column(self):
        rows = []
        for action, text, rewritten in OLD_STEP_TEXTS:
            # One fleet per row: a fleet has one step of each kind.
            operation = f.create_operation(self.fc, last_error=text)
            rows.append((operation, *self.step(action, text, operation), text, expected_text(text, rewritten)))

        data.rewrite_legacy_step_texts(apps, None)

        for operation, step, attempt, text, expected in rows:
            with self.subTest(text=text):
                self.assertEqual(self.read(step, attempt), (expected, expected))
                operation.refresh_from_db()
                # "Manual" and "Automatic" are the end of a fleet only; as the last error they say nothing.
                self.assertEqual(operation.last_error, text if text in data.FLEET_END_TEXTS else expected)

    def test_the_last_error_of_a_fleet_is_rewritten(self):
        for old, new in (
            ("Fleet no longer exists.", "The fleet no longer exists."),
            ("No active webhook configured. Ping remains available for manual copy.", texts.NO_PING_CHANNEL()),
            (
                "Attendance-only mode: SRP creation intentionally skipped.",
                "PAP-only mode: SRP creation intentionally skipped.",
            ),
        ):
            operation = f.create_operation(self.fc, last_error=old)
            data.rewrite_legacy_step_texts(apps, None)
            operation.refresh_from_db()
            with self.subTest(old=old):
                self.assertEqual(operation.last_error, new)

    def test_similar_texts_are_left_alone(self):
        rows = []
        for action, text in UNTOUCHED_STEP_TEXTS:
            operation = f.create_operation(self.fc, last_error=text)
            rows.append((operation, *self.step(action, text, operation), text))

        data.rewrite_legacy_step_texts(apps, None)

        for operation, step, attempt, text in rows:
            with self.subTest(text=text):
                self.assertEqual(self.read(step, attempt), (text, text))
                operation.refresh_from_db()
                self.assertEqual(operation.last_error, text)

    def test_manual_and_automatic_are_only_the_end_of_a_fleet(self):
        other = self.step("discord_ping", "Manual")
        end = self.step("fleet_end", "Manual")

        data.rewrite_legacy_step_texts(apps, None)

        self.assertEqual(self.read(*other), ("Manual", "Manual"))
        self.assertEqual(self.read(*end), ("Ended manually", "Ended manually"))

    def test_the_rewrite_is_idempotent(self):
        end = self.step("fleet_end", "Automatic")

        data.rewrite_legacy_step_texts(apps, None)
        data.rewrite_legacy_step_texts(apps, None)

        self.assertEqual(self.read(*end), (texts.END_FLEET_GONE(), texts.END_FLEET_GONE()))

    def test_the_rewritten_texts_are_stored_texts_that_translate(self):
        for action, text, rewritten in OLD_STEP_TEXTS:
            text = expected_text(text, rewritten)
            with self.subTest(text=text), translation.override("zh-hans"):
                self.assertTrue(texts.registered(text), text)
                self.assertNotEqual(texts.translate_stored(text), text)

    def test_the_other_fields_of_a_step_stay(self):
        step, attempt = self.step("fleet_end", "Manual")
        created_at = attempt.created_at

        data.rewrite_legacy_step_texts(apps, None)

        step.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual((step.action, step.status), ("fleet_end", OperationAction.Status.SUCCESS))
        self.assertEqual((attempt.status, attempt.created_at), (OperationAction.Status.SUCCESS, created_at))

    def test_going_back_leaves_the_texts_as_they_are(self):
        end = self.step("fleet_end", "Manual")
        data.rewrite_legacy_step_texts(apps, None)

        reverse = [operation.reverse_code for operation in data.Migration.operations][-1]
        reverse(apps, None)

        self.assertEqual(self.read(*end), ("Ended manually", "Ended manually"))


class UpgradeTests(TransactionTestCase):
    """Migrates a database holding settings, incentive periods, fleets and templates from 0018 to 0020 and back."""

    serialized_rollback = True
    start = [("fleetops", "0018_translatable_labels")]
    middle = [("fleetops", "0019_b2_fields")]
    target = [("fleetops", "0020_b2_data")]

    def migrate(self, nodes):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(nodes)
        return executor.loader.project_state(nodes).apps

    def migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("fleetops"))

    def setUp(self):
        super().setUp()
        self.addCleanup(self.migrate_to_latest)
        self.old_apps = self.migrate(self.start)
        self.insert_0018_rows(self.old_apps)

    def insert_0018_rows(self, old_apps):
        UserModel = old_apps.get_model("auth", "User")
        Settings = old_apps.get_model("fleetops", "FleetOpsSettings")
        FleetType = old_apps.get_model("fleetops", "FleetType")
        Operation = old_apps.get_model("fleetops", "FleetOperation")
        Action = old_apps.get_model("fleetops", "OperationAction")
        Attempt = old_apps.get_model("fleetops", "OperationActionAttempt")
        Period = old_apps.get_model("fleetops", "IncentivePeriod")
        Template = old_apps.get_model("fleetops", "MessageTemplate")

        Settings.objects.update_or_create(pk=1, defaults={"tracking_interval": 90})
        Period.objects.create(year=2026, month=9, budget=5)
        fc = UserModel.objects.create(username="b2-fc")
        fleet_type = FleetType.objects.create(name="Upgrade Type")
        operation = Operation.objects.create(
            created_by=fc,
            fc_user=fc,
            fc_character_id=90_555_001,
            fc_character_name="Upgrade FC",
            fleet_type=fleet_type,
            formup="Jita",
            status="closed",
            last_error="Fleet no longer exists.",
        )
        self.operation_id = operation.pk
        for action, text, _rewritten in (OLD_STEP_TEXTS[1], OLD_STEP_TEXTS[2], OLD_STEP_TEXTS[6]):
            step = Action.objects.create(operation=operation, action=action, status="success", error_message=text)
            Attempt.objects.create(action=step, status="success", message=text)

        Template.objects.update_or_create(
            template_type="ping", name="Default Ping", defaults={"content": data.OLD_PING.replace("\n", "\r\n")}
        )
        Template.objects.update_or_create(template_type="motd", name="Default MOTD", defaults={"content": data.OLD_MOTD})
        Template.objects.update_or_create(
            template_type="motd", name="Custom MOTD", defaults={"content": data.OLD_MOTD.replace("Form Up:", "Staging:")}
        )
        Template.objects.update_or_create(
            template_type="ping", name="Custom Ping", defaults={"content": data.OLD_PING.replace("FC Name", "Commander")}
        )

    def content(self, apps_, name, template_type):
        return apps_.get_model("fleetops", "MessageTemplate").objects.get(template_type=template_type, name=name).content

    def test_new_fields_get_their_defaults(self):
        new_apps = self.migrate(self.middle)

        settings = new_apps.get_model("fleetops", "FleetOpsSettings").objects.get(pk=1)
        period = new_apps.get_model("fleetops", "IncentivePeriod").objects.get()
        operation = new_apps.get_model("fleetops", "FleetOperation").objects.get(pk=self.operation_id)
        self.assertEqual((settings.tracking_interval, settings.motd_warning_length), (90, 2000))
        self.assertEqual((period.budget, period.calculated_at), (5, None))
        self.assertEqual(operation.review_request_note, "")

    def test_the_fields_can_be_removed_again_and_added_again(self):
        self.migrate(self.middle)
        old_apps = self.migrate(self.start)
        Settings = old_apps.get_model("fleetops", "FleetOpsSettings")
        self.assertNotIn("motd_warning_length", {field.name for field in Settings._meta.get_fields()})
        self.assertEqual(Settings.objects.get(pk=1).tracking_interval, 90)

        new_apps = self.migrate(self.middle)

        self.assertEqual(new_apps.get_model("fleetops", "FleetOpsSettings").objects.get(pk=1).motd_warning_length, 2000)

    def test_the_data_step_updates_defaults_and_rewrites_step_texts(self):
        new_apps = self.migrate(self.target)

        self.assertEqual(self.content(new_apps, "Default Ping", "ping"), data.NEW_PING)
        self.assertEqual(self.content(new_apps, "Default MOTD", "motd"), data.NEW_MOTD)
        # Edited and custom templates are not touched.
        self.assertEqual(self.content(new_apps, "Custom MOTD", "motd"), data.OLD_MOTD.replace("Form Up:", "Staging:"))
        self.assertEqual(self.content(new_apps, "Custom Ping", "ping"), data.OLD_PING.replace("FC Name", "Commander"))
        operation = new_apps.get_model("fleetops", "FleetOperation").objects.get(pk=self.operation_id)
        self.assertEqual(operation.last_error, "The fleet no longer exists.")
        steps = dict(
            new_apps.get_model("fleetops", "OperationAction")
            .objects.filter(operation=operation)
            .values_list("action", "error_message")
        )
        self.assertEqual(steps["fleet_end"], "Ended automatically: the fleet no longer exists or the FC left it")
        self.assertEqual(steps["discord_ping"], "PAP-only mode: Discord ping intentionally skipped.")
        self.assertEqual(steps["fleet_detection"], "The fleet no longer exists.")
        attempts = set(new_apps.get_model("fleetops", "OperationActionAttempt").objects.values_list("message", flat=True))
        self.assertIn("PAP-only mode: Discord ping intentionally skipped.", attempts)

    def test_going_back_restores_the_defaults_and_keeps_later_edits(self):
        new_apps = self.migrate(self.target)
        Template = new_apps.get_model("fleetops", "MessageTemplate")
        Template.objects.filter(template_type="motd", name="Default MOTD").update(
            content=data.NEW_MOTD.replace("Form Up:", "Meet at:")
        )

        old_apps = self.migrate(self.middle)

        # The ping is the old default again; the MOTD edited after the upgrade stays as it was edited.
        self.assertEqual(self.content(old_apps, "Default Ping", "ping"), data.OLD_PING)
        self.assertEqual(self.content(old_apps, "Default MOTD", "motd"), data.NEW_MOTD.replace("Form Up:", "Meet at:"))
        self.assertEqual(self.content(old_apps, "Custom Ping", "ping"), data.OLD_PING.replace("FC Name", "Commander"))
        # The step texts keep the wording of today.
        operation = old_apps.get_model("fleetops", "FleetOperation").objects.get(pk=self.operation_id)
        self.assertEqual(operation.last_error, "The fleet no longer exists.")

        # And it can go forward again.
        again = self.migrate(self.target)
        self.assertEqual(self.content(again, "Default Ping", "ping"), data.NEW_PING)

    def test_the_current_models_read_the_upgraded_rows(self):
        self.migrate_to_latest()

        self.assertEqual(FleetOpsSettings.get_solo().motd_warning_length, 2000)
        self.assertEqual(MessageTemplate.objects.get(name="Default Ping").content, DEFAULT_PING_TEMPLATE)
        self.assertEqual(MessageTemplate.objects.get(name="Default MOTD").content, DEFAULT_MOTD_TEMPLATE)
        self.assertEqual(FleetOperation.objects.get(pk=self.operation_id).group_id, None)


class GroupAccessMigrationTests(TransactionTestCase):
    """Group access: nothing changes for existing channels and fleets, and the migration can be reversed with rows."""

    serialized_rollback = True
    start = [("fleetops", "0020_b2_data")]
    target = [("fleetops", "0021_group_access")]

    def migrate(self, nodes):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(nodes)
        return executor.loader.project_state(nodes).apps

    def migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("fleetops"))

    def setUp(self):
        super().setUp()
        self.addCleanup(self.migrate_to_latest)
        old_apps = self.migrate(self.start)
        self.insert_0020_rows(old_apps)

    def insert_0020_rows(self, old_apps):
        UserModel = old_apps.get_model("auth", "User")
        FleetType = old_apps.get_model("fleetops", "FleetType")
        Operation = old_apps.get_model("fleetops", "FleetOperation")
        Webhook = old_apps.get_model("fleetops", "DiscordWebhook")
        Template = old_apps.get_model("fleetops", "MessageTemplate")

        fc = UserModel.objects.create(username="group-fc")
        fleet_type = FleetType.objects.create(name="Group Type")
        self.operation_id = Operation.objects.create(
            created_by=fc,
            fc_user=fc,
            fc_character_id=90_555_001,
            fc_character_name="Group FC",
            fleet_type=fleet_type,
            formup="Jita",
            status="closed",
        ).pk
        self.channel_id = Webhook.objects.create(
            name="Alliance Pings", webhook_url="https://discord.com/api/webhooks/1/token"
        ).pk
        self.template_id = Template.objects.get(template_type="ping", name="Default Ping").pk

    def test_existing_channels_and_fleets_keep_today_s_behaviour(self):
        new_apps = self.migrate(self.target)

        channel = new_apps.get_model("fleetops", "DiscordWebhook").objects.get(pk=self.channel_id)
        operation = new_apps.get_model("fleetops", "FleetOperation").objects.get(pk=self.operation_id)
        self.assertTrue(channel.fcs_allowed)
        self.assertEqual(list(channel.allowed_groups.all()), [])
        self.assertEqual((operation.group_id, operation.group_name), (None, ""))
        self.assertEqual(new_apps.get_model("fleetops", "GroupAccess").objects.count(), 0)
        self.assertEqual(new_apps.get_model("fleetops", "StandalonePing").objects.count(), 0)

    def test_the_templates_keep_their_types_and_the_new_type_is_accepted(self):
        new_apps = self.migrate(self.target)

        Template = new_apps.get_model("fleetops", "MessageTemplate")
        self.assertEqual(Template.objects.get(pk=self.template_id).template_type, "ping")
        Template.objects.create(template_type="standalone", name="Ping without fleet", content="x")
        self.assertEqual(Template.objects.filter(template_type="standalone").count(), 1)

    def test_it_can_be_reversed_with_rows_present_and_repeated(self):
        new_apps = self.migrate(self.target)
        self.add_group_rows(new_apps)

        old_apps = self.migrate(self.start)

        # The tables and columns are gone; everything else is as it was.
        Channel = old_apps.get_model("fleetops", "DiscordWebhook")
        Operation = old_apps.get_model("fleetops", "FleetOperation")
        self.assertNotIn("fcs_allowed", {field.name for field in Channel._meta.get_fields()})
        self.assertNotIn("group_name", {field.name for field in Operation._meta.get_fields()})
        with self.assertRaises(LookupError):
            old_apps.get_model("fleetops", "GroupAccess")
        self.assertEqual(Channel.objects.filter(pk=self.channel_id).count(), 1)
        self.assertEqual(Operation.objects.filter(pk=self.operation_id).count(), 1)

        again = self.migrate(self.target)

        self.assertEqual(again.get_model("fleetops", "DiscordWebhook").objects.get(pk=self.channel_id).fcs_allowed, True)
        self.assertEqual(again.get_model("fleetops", "GroupAccess").objects.count(), 0)
        self.assertEqual(again.get_model("fleetops", "StandalonePing").objects.count(), 0)

    def add_group_rows(self, new_apps):
        AuthGroup = new_apps.get_model("auth", "Group")
        FleetType = new_apps.get_model("fleetops", "FleetType")
        Access = new_apps.get_model("fleetops", "GroupAccess")
        Ping = new_apps.get_model("fleetops", "StandalonePing")
        Channel = new_apps.get_model("fleetops", "DiscordWebhook")
        Operation = new_apps.get_model("fleetops", "FleetOperation")

        group = AuthGroup.objects.create(name="Sig A")
        access = Access.objects.create(group=group, mode="fleets", srp_enabled=True)
        access.fleet_types.add(FleetType.objects.get(name="Group Type"))
        channel = Channel.objects.get(pk=self.channel_id)
        channel.allowed_groups.add(group)
        channel.fcs_allowed = False
        channel.save()
        Ping.objects.create(
            request_id="00000000-0000-4000-8000-000000000001",
            channel=channel,
            channel_name=channel.name,
            group=group,
            group_name=group.name,
            additional_message="Gate camp",
            created_at=timezone.now(),
        )
        Operation.objects.filter(pk=self.operation_id).update(group=group, group_name=group.name)

    def test_a_group_fleet_survives_the_deletion_of_its_group(self):
        self.migrate_to_latest()
        group = Group.objects.create(name="Sig B")
        GroupAccess.objects.create(group=group)
        operation = FleetOperation.objects.get(pk=self.operation_id)
        operation.group, operation.group_name = group, group.name
        operation.save()

        group.delete()

        operation.refresh_from_db()
        self.assertIsNone(operation.group_id)
        self.assertEqual(operation.group_name, "Sig B")
        self.assertFalse(GroupAccess.objects.exists())

    def test_the_current_models_work_after_a_round_trip(self):
        self.migrate(self.target)
        self.migrate(self.start)
        self.migrate_to_latest()

        group = Group.objects.create(name="Sig C")
        access = GroupAccess.objects.create(group=group, mode=GroupAccess.Mode.FLEETS)
        access.fleet_types.add(FleetOperation.objects.get(pk=self.operation_id).fleet_type)

        self.assertEqual(str(access), "Sig C")
        self.assertEqual(access.fleet_types.count(), 1)
        self.assertEqual(User.objects.filter(username="group-fc").count(), 1)
