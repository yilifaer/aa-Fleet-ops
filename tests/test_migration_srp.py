# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Migrations 0016-0017: the SRP tables, the SRP settings row, request_srp for everyone with basic_access and
the SRP line in the untouched default MOTD."""

import importlib
import inspect
from io import StringIO

from django.apps import apps
from django.contrib.auth.models import Group, Permission, User
from django.core.management import call_command
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from fleetops.constants import DEFAULT_MOTD_TEMPLATE
from fleetops.models import AuditLog, FleetOpsSettings, MessageTemplate, SrpCategory, SrpClaim, SrpPayoutRule, SrpSettings

from . import factories as f

schema = importlib.import_module("fleetops.migrations.0016_srp_schema")
upgrade = importlib.import_module("fleetops.migrations.0017_srp_data")
motd_0009 = importlib.import_module("fleetops.migrations.0009_convert_ping_targets_and_presets")
data_0020 = importlib.import_module("fleetops.migrations.0020_b2_data")
categories_schema = importlib.import_module("fleetops.migrations.0024_srp_categories")
categories_data = importlib.import_module("fleetops.migrations.0025_srp_categories_data")

NEW_CODENAMES = ("request_srp", "manage_srp")
SRP_LINE = '{% if srp_url %}<br>SRP: <a href="{{ srp_url }}">Request SRP</a>{% endif %}'


def permission(codename):
    return Permission.objects.get(content_type__app_label="fleetops", codename=codename)


def holders_of(codename):
    """Names of the groups, states and usernames that hold the fleetops permission."""
    from allianceauth.authentication.models import State

    perm = permission(codename)
    return (
        set(Group.objects.filter(permissions=perm).values_list("name", flat=True)),
        set(State.objects.filter(permissions=perm).values_list("name", flat=True)),
        set(User.objects.filter(user_permissions=perm).values_list("username", flat=True)),
    )


class MigrationModuleTests(TestCase):
    def test_migrations_do_not_import_fleetops_code(self):
        # They must keep working when the constants or models they were written against change.
        for module in (schema, upgrade, categories_schema, categories_data):
            source = inspect.getsource(module)
            with self.subTest(module=module.__name__):
                self.assertNotIn("from fleetops", source)
                self.assertNotIn("import fleetops", source)

    def test_copied_motd_templates_match_the_app(self):
        self.assertEqual(upgrade.OLD_MOTD, motd_0009.NEW_MOTD)
        # Migration 0020 made the FC and the form-up place links; its copy of the old default is this one.
        self.assertEqual(upgrade.NEW_MOTD, data_0020.OLD_MOTD)
        self.assertEqual(data_0020.NEW_MOTD, DEFAULT_MOTD_TEMPLATE)
        # The SRP line comes before the additional message and nothing else differs.
        self.assertEqual(upgrade.NEW_MOTD.count(SRP_LINE), 1)
        self.assertEqual(upgrade.NEW_MOTD.replace(SRP_LINE, ""), upgrade.OLD_MOTD)
        self.assertLess(upgrade.NEW_MOTD.index(SRP_LINE), upgrade.NEW_MOTD.index("{% if additional_message %}"))

    def test_new_permissions_match_the_model(self):
        declared = dict(FleetOpsSettings._meta.permissions)
        self.assertEqual(dict(upgrade.NEW_PERMISSIONS), {code: declared[code] for code in NEW_CODENAMES})
        self.assertEqual(upgrade.BASIC_ACCESS, "basic_access")
        self.assertEqual(upgrade.SETTINGS_MODEL, FleetOpsSettings._meta.model_name)

    def test_dependencies(self):
        self.assertIn(("fleetops", "0016_srp_schema"), upgrade.Migration.dependencies)
        self.assertEqual(schema.Migration.dependencies[0], ("fleetops", "0015_statistics_targets"))
        self.assertIn(("fleetops", "0023_group_members_are_leaders_help"), categories_schema.Migration.dependencies)
        self.assertEqual(categories_data.Migration.dependencies, [("fleetops", "0024_srp_categories")])

    def test_the_migrations_start_with_the_plain_header(self):
        for module in (categories_schema, categories_data):
            with self.subTest(module=module.__name__):
                self.assertTrue(inspect.getsource(module).startswith("# AA FleetOps 0.3.0b1:"))

    def test_the_constants_copied_into_the_data_migration_match_the_app(self):
        from fleetops.models import SrpCategory, SrpClaim, SrpSettings
        from fleetops.texts import SRP_CATEGORY_FLEET

        self.assertEqual(categories_data.SYSTEM_KEY, SrpCategory.SYSTEM_KEY)
        self.assertEqual(categories_data.SYSTEM_NAME, SRP_CATEGORY_FLEET.msgid)
        self.assertEqual(categories_data.IN_FLEET, SrpClaim.Scope.IN_FLEET)
        self.assertEqual(categories_data.ZKILL_FITTED, SrpSettings.ValueSource.ZKILL_FITTED)
        self.assertEqual(categories_data.ZKILL_TOTAL, SrpSettings.ValueSource.ZKILL_TOTAL)
        self.assertEqual(SrpSettings._meta.get_field("value_source").default, categories_data.ZKILL_TOTAL)

    def test_models_and_migrations_are_in_sync(self):
        out = StringIO()
        call_command("makemigrations", "fleetops", check=True, dry_run=True, stdout=out, stderr=StringIO())


class FreshInstallTests(TestCase):
    def test_srp_settings_row_exists_and_is_disabled(self):
        row = SrpSettings.objects.get(pk=1)

        self.assertFalse(row.enabled)
        self.assertTrue(row.capsules_need_rule)
        self.assertFalse(row.allow_self_review)
        self.assertEqual(SrpSettings.objects.count(), 1)

    def test_permissions_exist_on_the_settings_model(self):
        for codename, name in upgrade.NEW_PERMISSIONS:
            with self.subTest(codename=codename):
                perm = permission(codename)
                self.assertEqual(perm.name, name)
                self.assertEqual(perm.content_type.model, "fleetopssettings")

    def test_default_motd_has_the_srp_line(self):
        motd = MessageTemplate.objects.get(template_type="motd", name="Default MOTD")

        self.assertEqual(motd.content, DEFAULT_MOTD_TEMPLATE)
        self.assertIn("{{ srp_url }}", motd.content)

    def test_seed_command_keeps_existing_srp_settings(self):
        SrpSettings.objects.filter(pk=1).update(enabled=True, percentage=80)

        call_command("fleetops_seed", stdout=StringIO())

        row = SrpSettings.objects.get(pk=1)
        self.assertTrue(row.enabled)
        self.assertEqual(int(row.percentage), 80)

    def test_seed_command_creates_missing_srp_settings(self):
        SrpSettings.objects.all().delete()

        call_command("fleetops_seed", stdout=StringIO())

        self.assertFalse(SrpSettings.objects.get(pk=1).enabled)


class SettingsRowTests(TestCase):
    def test_row_is_created_once(self):
        SrpSettings.objects.all().delete()

        upgrade.create_srp_settings(apps, None)
        upgrade.create_srp_settings(apps, None)

        self.assertEqual(list(SrpSettings.objects.values_list("pk", "enabled")), [(1, False)])

    def test_existing_row_is_not_changed(self):
        SrpSettings.objects.filter(pk=1).update(enabled=True, in_fleet_loss_cap=5)

        upgrade.create_srp_settings(apps, None)

        row = SrpSettings.objects.get(pk=1)
        self.assertEqual((row.enabled, row.in_fleet_loss_cap), (True, 5))


class GrantTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from allianceauth.authentication.models import State

        cls.basic = permission("basic_access")
        cls.start = permission("start_fleet")
        cls.members = Group.objects.create(name="Members")
        cls.members.permissions.add(cls.basic)
        cls.fcs = Group.objects.create(name="FCs")
        cls.fcs.permissions.add(cls.basic, cls.start)
        cls.others = Group.objects.create(name="Others")
        cls.others.permissions.add(cls.start)
        cls.empty = Group.objects.create(name="Empty")
        cls.state = State.objects.create(name="Alliance Member", priority=900)
        # Alliance Auth gives states a proxy of Permission, so they take primary keys.
        cls.state.permissions.add(cls.basic.pk)
        # Alliance Auth creates the Blue state itself.
        cls.blue, _created = State.objects.get_or_create(name="Blue", defaults={"priority": 800})
        cls.direct = User.objects.create(username="direct")
        cls.direct.user_permissions.add(cls.basic)
        cls.plain = User.objects.create(username="plain")
        cls.plain.user_permissions.add(cls.start)

    def setUp(self):
        # As before the upgrade: the new permissions are not granted to anybody.
        for codename in NEW_CODENAMES:
            permission(codename).delete()

    def test_request_srp_goes_to_every_holder_of_basic_access(self):
        upgrade.grant_request_srp(apps, None)

        groups, states, users = holders_of("request_srp")
        self.assertEqual(groups, {"Members", "FCs"})
        self.assertEqual(states, {"Alliance Member"})
        self.assertEqual(users, {"direct"})

    def test_manage_srp_is_granted_to_nobody(self):
        upgrade.grant_request_srp(apps, None)

        self.assertEqual(holders_of("manage_srp"), (set(), set(), set()))

    def test_existing_permissions_stay(self):
        upgrade.grant_request_srp(apps, None)

        self.assertEqual(set(self.fcs.permissions.values_list("codename", flat=True)), {"basic_access", "start_fleet", "request_srp"})
        self.assertEqual(set(self.others.permissions.values_list("codename", flat=True)), {"start_fleet"})

    def test_permissions_are_created_with_the_model_names(self):
        upgrade.grant_request_srp(apps, None)

        for codename, name in upgrade.NEW_PERMISSIONS:
            self.assertEqual(permission(codename).name, name)

    def test_grant_is_idempotent(self):
        upgrade.grant_request_srp(apps, None)
        upgrade.grant_request_srp(apps, None)

        self.assertEqual(Permission.objects.filter(codename="request_srp").count(), 1)
        self.assertEqual(self.members.permissions.filter(codename="request_srp").count(), 1)
        groups, states, users = holders_of("request_srp")
        self.assertEqual((groups, states, users), ({"Members", "FCs"}, {"Alliance Member"}, {"direct"}))

    def test_a_holder_that_already_has_request_srp_is_not_touched(self):
        upgrade.grant_request_srp(apps, None)
        self.members.permissions.remove(permission("request_srp"))
        self.fcs.permissions.add(permission("manage_srp"))

        upgrade.grant_request_srp(apps, None)

        # Members get it back, nobody else changes.
        self.assertEqual(holders_of("request_srp")[0], {"Members", "FCs"})
        self.assertEqual(holders_of("manage_srp")[0], {"FCs"})

    def test_without_basic_access_permission_the_new_permissions_are_still_created(self):
        # A new installation: the permission rows of the app do not exist yet while it migrates.
        self.basic.delete()

        upgrade.grant_request_srp(apps, None)

        self.assertEqual(Permission.objects.filter(codename__in=NEW_CODENAMES).count(), 2)
        self.assertEqual(holders_of("request_srp"), (set(), set(), set()))


class DefaultMotdTests(TestCase):
    def set_content(self, name, content, template_type="motd"):
        MessageTemplate.objects.update_or_create(
            template_type=template_type, name=name, defaults={"content": content}
        )

    def content(self, name, template_type="motd"):
        return MessageTemplate.objects.get(template_type=template_type, name=name).content

    def test_untouched_default_gets_the_srp_line(self):
        self.set_content("Default MOTD", upgrade.OLD_MOTD)

        upgrade.add_srp_line_to_default_motd(apps, None)

        self.assertEqual(self.content("Default MOTD"), upgrade.NEW_MOTD)

    def test_default_saved_with_a_trailing_line_end_gets_the_srp_line(self):
        # A browser form adds line ends when the template was saved from the admin.
        self.set_content("Default MOTD", upgrade.OLD_MOTD + "\r\n")

        upgrade.add_srp_line_to_default_motd(apps, None)

        self.assertEqual(self.content("Default MOTD"), upgrade.NEW_MOTD)

    def test_customised_default_is_left_alone(self):
        custom = upgrade.OLD_MOTD.replace("Form Up:", "Staging:")
        self.set_content("Default MOTD", custom)

        upgrade.add_srp_line_to_default_motd(apps, None)

        self.assertEqual(self.content("Default MOTD"), custom)

    def test_other_templates_are_left_alone(self):
        self.set_content("Custom MOTD", upgrade.OLD_MOTD)
        self.set_content("Default MOTD", "<b>{{ fleet_type }}</b>")
        self.set_content("Default Ping", upgrade.OLD_MOTD, template_type="ping")

        upgrade.add_srp_line_to_default_motd(apps, None)

        self.assertEqual(self.content("Custom MOTD"), upgrade.OLD_MOTD)
        self.assertEqual(self.content("Default MOTD"), "<b>{{ fleet_type }}</b>")
        self.assertEqual(self.content("Default Ping", "ping"), upgrade.OLD_MOTD)

    def test_update_is_idempotent(self):
        self.set_content("Default MOTD", upgrade.OLD_MOTD)

        upgrade.add_srp_line_to_default_motd(apps, None)
        upgrade.add_srp_line_to_default_motd(apps, None)

        self.assertEqual(self.content("Default MOTD"), upgrade.NEW_MOTD)

    def test_other_fields_of_the_template_stay(self):
        self.set_content("Default MOTD", upgrade.OLD_MOTD)
        MessageTemplate.objects.filter(name="Default MOTD", template_type="motd").update(is_default=True, is_active=True)

        upgrade.add_srp_line_to_default_motd(apps, None)

        motd = MessageTemplate.objects.get(name="Default MOTD", template_type="motd")
        self.assertTrue(motd.is_default and motd.is_active)


class UpgradeFrom0015Tests(TransactionTestCase):
    """Migrates a database holding groups, states, users and message templates from 0015 to 0017."""

    serialized_rollback = True
    # The states of Alliance Auth are part of the starting point: 0015 does not depend on that app.
    start = [("fleetops", "0015_statistics_targets"), ("authentication", "0026_alter_characterownership_user_and_more")]
    target = [("fleetops", "0017_srp_data")]

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
        self.insert_0015_rows(old_apps)
        self.new_apps = self.migrate(self.target)

    def insert_0015_rows(self, old_apps):
        Permission = old_apps.get_model("auth", "Permission")
        GroupModel = old_apps.get_model("auth", "Group")
        UserModel = old_apps.get_model("auth", "User")
        State = old_apps.get_model("authentication", "State")
        Template = old_apps.get_model("fleetops", "MessageTemplate")
        # An upgrade starts without the new permissions.
        Permission.objects.filter(content_type__app_label="fleetops", codename__in=NEW_CODENAMES).delete()
        basic = Permission.objects.get(content_type__app_label="fleetops", codename="basic_access")
        other = Permission.objects.get(content_type__app_label="fleetops", codename="start_fleet")

        members = GroupModel.objects.create(name="Alliance Members")
        members.permissions.add(basic, other)
        GroupModel.objects.create(name="Without FleetOps").permissions.add(other)
        GroupModel.objects.create(name="Empty Group")
        # Alliance Auth creates the Guest and Member states itself.
        state, _created = State.objects.get_or_create(name="Member", defaults={"priority": 950})
        # Passed as a primary key: the state model of the historical apps has its own Permission class.
        state.permissions.add(basic.pk)
        State.objects.get_or_create(name="Guest", defaults={"priority": 1})
        UserModel.objects.create(username="direct-holder").user_permissions.add(basic)
        UserModel.objects.create(username="no-permissions")

        Template.objects.update_or_create(
            template_type="motd",
            name="Default MOTD",
            defaults={"content": upgrade.OLD_MOTD.replace("\n", "\r\n"), "is_default": True, "is_active": True},
        )
        Template.objects.update_or_create(
            template_type="motd",
            name="Custom MOTD",
            defaults={"content": upgrade.OLD_MOTD.replace("Form Up:", "Staging:")},
        )

    def holders(self, codename):
        Permission = self.new_apps.get_model("auth", "Permission")
        perm = Permission.objects.get(content_type__app_label="fleetops", codename=codename)
        return (
            set(self.new_apps.get_model("auth", "Group").objects.filter(permissions=perm).values_list("name", flat=True)),
            set(self.new_apps.get_model("authentication", "State").objects.filter(permissions=perm).values_list("name", flat=True)),
            set(self.new_apps.get_model("auth", "User").objects.filter(user_permissions=perm).values_list("username", flat=True)),
        )

    def test_request_srp_is_granted_to_holders_of_basic_access(self):
        self.assertEqual(self.holders("request_srp"), ({"Alliance Members"}, {"Member"}, {"direct-holder"}))

    def test_manage_srp_is_granted_to_nobody(self):
        self.assertEqual(self.holders("manage_srp"), (set(), set(), set()))

    def test_permissions_have_the_model_names(self):
        Permission = self.new_apps.get_model("auth", "Permission")
        names = dict(
            Permission.objects.filter(content_type__app_label="fleetops", codename__in=NEW_CODENAMES).values_list(
                "codename", "name"
            )
        )
        self.assertEqual(names, dict(upgrade.NEW_PERMISSIONS))

    def test_srp_settings_row_is_created_disabled(self):
        Settings = self.new_apps.get_model("fleetops", "SrpSettings")

        self.assertEqual(list(Settings.objects.values_list("pk", "enabled", "capsules_need_rule", "allow_self_review")), [(1, False, True, False)])

    def test_untouched_default_motd_is_updated_and_customised_one_is_not(self):
        Template = self.new_apps.get_model("fleetops", "MessageTemplate")

        self.assertEqual(Template.objects.get(template_type="motd", name="Default MOTD").content, upgrade.NEW_MOTD)
        self.assertEqual(
            Template.objects.get(template_type="motd", name="Custom MOTD").content,
            upgrade.OLD_MOTD.replace("Form Up:", "Staging:"),
        )

    def test_srp_tables_are_usable_after_the_upgrade(self):
        Killmail = self.new_apps.get_model("fleetops", "SrpKillmail")
        PayoutRule = self.new_apps.get_model("fleetops", "SrpPayoutRule")
        from django.utils import timezone

        Killmail.objects.create(
            killmail_id=1, killmail_hash="a" * 40, killmail_time=timezone.now(), solar_system_id=2, ship_type_id=3
        )
        PayoutRule.objects.create(ship_type_id=587, doctrine_key="", scope="any", amount=1_000_000)

        self.assertEqual(Killmail.objects.count(), 1)
        self.assertEqual(PayoutRule.objects.count(), 1)


class CategoryDataFunctionTests(TestCase):
    """The functions of migration 0025, run on the current models."""

    def setUp(self):
        self.user = f.create_user("pilot", perms=f.SRP_MEMBER_PERMS)

    def switch(self):
        categories_data.switch_untouched_value_source(apps, None)
        return SrpSettings.get_solo().value_source

    def test_a_new_database_uses_the_total_value(self):
        self.assertEqual(SrpSettings.get_solo().value_source, "zkill_total")
        self.assertEqual(SrpSettings._meta.get_field("value_source").default, "zkill_total")

    def test_untouched_settings_switch_to_the_total_value(self):
        SrpSettings.objects.filter(pk=1).update(value_source="zkill_fitted")

        self.assertEqual(self.switch(), "zkill_total")

    def test_settings_that_were_saved_once_keep_their_value(self):
        SrpSettings.objects.filter(pk=1).update(value_source="zkill_fitted")
        AuditLog.objects.create(action="configuration.update", object_type="SrpSettings", object_id="1")

        self.assertEqual(self.switch(), "zkill_fitted")

    def test_settings_with_claims_keep_their_value(self):
        SrpSettings.objects.filter(pk=1).update(value_source="zkill_fitted")
        f.srp_claim(self.user)

        self.assertEqual(self.switch(), "zkill_fitted")

    def test_an_audit_entry_of_another_object_does_not_count(self):
        SrpSettings.objects.filter(pk=1).update(value_source="zkill_fitted")
        AuditLog.objects.create(action="configuration.update", object_type="SrpPayoutRule", object_id="1")

        self.assertEqual(self.switch(), "zkill_total")

    def test_another_value_source_is_left_alone(self):
        SrpSettings.objects.filter(pk=1).update(value_source="esi_estimate")

        self.assertEqual(self.switch(), "esi_estimate")

    def test_the_fleet_category_is_created_once(self):
        categories_data.create_fleet_category(apps, None)
        categories_data.create_fleet_category(apps, None)

        self.assertEqual(list(SrpCategory.objects.values_list("key", flat=True)), ["fleet"])

    def test_the_backfill_puts_fleet_claims_into_the_fleet_category(self):
        operation = f.create_operation(f.create_user("fc", perms=f.FC_PERMS))
        link = f.fleet_link(operation)
        inside = f.srp_claim(self.user, link=link, category=None)
        outside = f.srp_claim(f.create_user("other", perms=f.SRP_MEMBER_PERMS))
        SrpClaim.objects.update(category=None)

        categories_data.backfill_claims(apps, None)

        self.assertEqual(SrpClaim.objects.get(pk=inside.pk).category.key, "fleet")
        self.assertIsNone(SrpClaim.objects.get(pk=outside.pk).category)

    def test_the_guard_lists_only_rule_ids(self):
        solo = f.srp_category("Solo PVP")
        first = f.payout_rule(587, category=solo, label="Secret label")
        second = f.payout_rule(588, category=solo)
        f.payout_rule(589)

        with self.assertRaises(IrreversibleError) as caught:
            categories_data.refuse_rollback(apps, None)

        message = str(caught.exception)
        self.assertIn(f"{first.pk}, {second.pk}", message)
        self.assertNotIn("Secret label", message)
        self.assertNotIn("Solo PVP", message)

    def test_the_guard_says_when_the_list_is_cut(self):
        solo = f.srp_category("Solo PVP")
        for index in range(categories_data.MAX_LISTED + 7):
            f.payout_rule(1000 + index, category=solo)

        with self.assertRaises(IrreversibleError) as caught:
            categories_data.refuse_rollback(apps, None)

        message = str(caught.exception)
        self.assertIn("7 more", message)
        self.assertIn(f"{categories_data.MAX_LISTED + 7} rules in total", message)

    def test_the_guard_does_not_mention_more_when_the_list_is_complete(self):
        solo = f.srp_category("Solo PVP")
        for index in range(categories_data.MAX_LISTED):
            f.payout_rule(1000 + index, category=solo)

        with self.assertRaises(IrreversibleError) as caught:
            categories_data.refuse_rollback(apps, None)

        self.assertNotIn("more", str(caught.exception))

    def test_the_guard_and_the_cleanup_see_a_stale_category_key(self):
        solo = f.srp_category("Solo PVP")
        rule = f.payout_rule(587, category=solo)
        SrpPayoutRule.objects.filter(pk=rule.pk).update(category=None)

        with self.assertRaises(IrreversibleError) as caught:
            categories_data.refuse_rollback(apps, None)
        self.assertIn(str(rule.pk), str(caught.exception))

        categories_data.clear_categories(apps, None)
        self.assertEqual(SrpPayoutRule.objects.get(pk=rule.pk).category_key, 0)

    def test_the_guard_passes_without_category_rules(self):
        f.payout_rule(587)

        categories_data.refuse_rollback(apps, None)


class CategoryRoundTripTests(TransactionTestCase):
    """0.2.0b3 data goes through 0024 and 0025 and back to 0023."""

    serialized_rollback = True
    before = [("fleetops", "0023_group_members_are_leaders_help")]
    after = [("fleetops", "0025_srp_categories_data")]

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
        old = self.migrate(self.before)
        self.insert_rows(old)

    def insert_rows(self, old):
        from django.utils import timezone

        Killmail = old.get_model("fleetops", "SrpKillmail")
        Claim = old.get_model("fleetops", "SrpClaim")
        Rule = old.get_model("fleetops", "SrpPayoutRule")
        Settings = old.get_model("fleetops", "SrpSettings")
        Settings.objects.update_or_create(pk=1, defaults={"value_source": "zkill_fitted", "percentage": "80"})
        for index, scope in enumerate(("in_fleet", "out_of_fleet", "in_fleet"), start=1):
            km = Killmail.objects.create(
                killmail_id=index, killmail_hash=f"{index:040d}", killmail_time=timezone.now(), solar_system_id=2, ship_type_id=3
            )
            Claim.objects.create(
                killmail=km,
                scope=scope,
                character_id=index,
                character_name=f"Pilot {index}",
                killmail_time=km.killmail_time,
                ship_type_id=3,
                suggested_amount=100 * index,
                status="approved" if index == 3 else "pending",
            )
        Rule.objects.create(ship_type_id=587, doctrine_key="", scope="any", amount=1_000_000)

    def test_the_upgrade_keeps_the_amounts_and_sorts_the_claims(self):
        new = self.migrate(self.after)

        Claim = new.get_model("fleetops", "SrpClaim")
        Category = new.get_model("fleetops", "SrpCategory")
        Rule = new.get_model("fleetops", "SrpPayoutRule")
        Settings = new.get_model("fleetops", "SrpSettings")
        self.assertEqual(list(Category.objects.values_list("key", "is_system", "member_selectable")), [("fleet", True, False)])
        self.assertEqual(
            list(Claim.objects.order_by("character_id").values_list("scope", "category__key", "suggested_amount", "status")),
            [("in_fleet", "fleet", 100, "pending"), ("out_of_fleet", None, 200, "pending"), ("in_fleet", "fleet", 300, "approved")],
        )
        self.assertEqual(list(Rule.objects.values_list("amount", "category_id", "category_key")), [(1_000_000, None, 0)])
        # The settings were never saved and there were no audit entries: the new default applies. There were claims,
        # so this site keeps what it had.
        self.assertEqual(Settings.objects.get().value_source, "zkill_fitted")
        self.assertEqual(Settings.objects.get().percentage, 80)

    def test_the_way_back_restores_0023(self):
        self.migrate(self.after)

        old = self.migrate(self.before)

        Claim = old.get_model("fleetops", "SrpClaim")
        self.assertEqual(
            list(Claim.objects.order_by("character_id").values_list("scope", "suggested_amount", "status")),
            [("in_fleet", 100, "pending"), ("out_of_fleet", 200, "pending"), ("in_fleet", 300, "approved")],
        )
        self.assertFalse(hasattr(Claim, "category"))
        self.assertEqual(old.get_model("fleetops", "SrpPayoutRule").objects.count(), 1)
        self.assertNotIn("srpcategory", {model._meta.model_name for model in old.get_models() if model._meta.app_label == "fleetops"})
        # And forward again.
        again = self.migrate(self.after)
        self.assertEqual(again.get_model("fleetops", "SrpCategory").objects.count(), 1)
        self.assertEqual(again.get_model("fleetops", "SrpClaim").objects.filter(category__isnull=False).count(), 2)

    def test_a_rule_of_a_category_stops_the_way_back_and_changes_nothing(self):
        new = self.migrate(self.after)
        Category = new.get_model("fleetops", "SrpCategory")
        Rule = new.get_model("fleetops", "SrpPayoutRule")
        solo = Category.objects.create(name="Solo PVP", key="solo")
        rule = Rule.objects.create(ship_type_id=589, doctrine_key="", scope="any", amount=5, category=solo, category_key=solo.pk)

        with self.assertRaises(IrreversibleError) as caught:
            self.migrate(self.before)

        self.assertIn(str(rule.pk), str(caught.exception))
        self.assertNotIn("Solo PVP", str(caught.exception))
        # Nothing was undone: the category data is still there.
        current = MigrationExecutor(connection).loader.project_state(self.after).apps
        self.assertEqual(current.get_model("fleetops", "SrpCategory").objects.count(), 2)
        self.assertEqual(current.get_model("fleetops", "SrpClaim").objects.filter(category__key="fleet").count(), 2)

        # A general rule instead: the way back works.
        current.get_model("fleetops", "SrpPayoutRule").objects.filter(pk=rule.pk).update(category=None, category_key=0)
        old = self.migrate(self.before)
        self.assertEqual(old.get_model("fleetops", "SrpPayoutRule").objects.count(), 2)

    def test_a_rule_with_a_stale_category_key_stops_the_way_back_before_anything_is_undone(self):
        new = self.migrate(self.after)
        Category = new.get_model("fleetops", "SrpCategory")
        Rule = new.get_model("fleetops", "SrpPayoutRule")
        solo = Category.objects.create(name="Solo PVP", key="solo")
        Rule.objects.create(ship_type_id=587, doctrine_key="", scope="any", amount=5, category=solo, category_key=solo.pk)
        # A write that skips save() clears the category and leaves the key.
        Rule.objects.filter(category=solo).update(category=None)

        with self.assertRaises(IrreversibleError):
            self.migrate(self.before)

        current = MigrationExecutor(connection).loader.project_state(self.after).apps
        self.assertEqual(current.get_model("fleetops", "SrpCategory").objects.count(), 2)

    def test_a_rule_pointing_to_the_fleet_category_is_caught_by_the_guard_too(self):
        new = self.migrate(self.after)
        fleet = new.get_model("fleetops", "SrpCategory").objects.get(key="fleet")
        new.get_model("fleetops", "SrpPayoutRule").objects.create(
            ship_type_id=588, doctrine_key="", scope="any", amount=5, category=fleet, category_key=fleet.pk
        )

        with self.assertRaises(IrreversibleError):
            self.migrate(self.before)

    def test_the_site_without_claims_and_audit_entries_switches_to_the_total_value(self):
        old = MigrationExecutor(connection).loader.project_state(self.before).apps
        old.get_model("fleetops", "SrpClaim").objects.all().delete()
        old.get_model("fleetops", "SrpKillmail").objects.all().delete()

        new = self.migrate(self.after)

        self.assertEqual(new.get_model("fleetops", "SrpSettings").objects.get().value_source, "zkill_total")
