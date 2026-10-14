# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Migrations 0013-0014: fleet management fields, ship functions and structure templates."""

import importlib
import inspect
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from fleetops import fleet_defaults
from fleetops.models import (
    MAX_DATABASE_ID,
    MAX_LIVE_REFRESH_SECONDS,
    FleetMemberState,
    FleetOperation,
    FleetOpsSettings,
    FleetStructureTemplate,
    ShipFunction,
    ShipFunctionRule,
)
from fleetops.services.fleet_rules import DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS, parse_structure_layout

from . import factories as f

schema = importlib.import_module("fleetops.migrations.0013_fleet_management")
seed = importlib.import_module("fleetops.migrations.0014_seed_fleet_management")

EXPECTED_FUNCTIONS = [
    # (name, color, fleet positions, default, group rules, type rules)
    ("Fleet Command", "#dc3545", "fleet_commander", False, [1972], []),
    ("DPS", "#0d6efd", "", True, [], []),
    ("Logistics", "#198754", "", False, [832, 1527, 1538], []),
    ("Tackle / EWAR", "#fd7e14", "", False, [831, 833, 893, 906], []),
    ("Interdictors", "#6f42c1", "", False, [541, 894], []),
    ("Boosts", "#20c997", "", False, [540, 941, 4902], []),
    ("Command Destroyers", "#0dcaf0", "", False, [1534], []),
    ("Support", "#adb5bd", "", False, [28, 31, 380, 463, 513, 543, 830, 883, 902, 1022, 1202, 1283], []),
    ("Cyno", "#ffc107", "", False, [], []),
    ("Newbro", "#d63384", "", False, [237], []),
    ("Capsules", "#6c757d", "", False, [29], [670, 33328]),
]


def functions_as_rows(function_model=ShipFunction):
    rows = []
    for function in function_model.objects.order_by("sort_order"):
        rules = function.rules.order_by("target_id")
        rows.append(
            (
                function.name,
                function.color,
                function.fleet_roles,
                function.is_default,
                [rule.target_id for rule in rules if rule.kind == "group"],
                [rule.target_id for rule in rules if rule.kind == "type"],
            )
        )
    return rows


class MigrationModuleTests(TestCase):
    def test_the_data_migration_does_not_import_fleetops_code(self):
        # It must keep working when the constants or models it was written against change.
        source = inspect.getsource(seed)
        self.assertNotIn("from fleetops", source)
        self.assertNotIn("import fleetops", source)

    def test_the_schema_migration_imports_only_field_validators(self):
        source = inspect.getsource(schema)
        self.assertNotIn("from fleetops", source)
        used = {line.strip() for line in source.splitlines() if "fleetops.models." in line}
        self.assertEqual(
            used,
            {
                "validators=[fleetops.models.validate_id_list],",
                "validators=[fleetops.models.validate_fleet_roles],",
                "validators=[fleetops.models.validate_structure_layout],",
            },
        )

    def test_copied_constants_match_the_app(self):
        self.assertEqual(schema.DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS, DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS)
        self.assertEqual(schema.MAX_DATABASE_ID, MAX_DATABASE_ID)
        self.assertEqual(schema.MAX_LIVE_REFRESH_SECONDS, MAX_LIVE_REFRESH_SECONDS)
        self.assertEqual(schema.RULE_KIND_CHOICES, ShipFunctionRule.Kind.choices)

    def test_seed_command_creates_the_same_defaults_as_the_migration(self):
        self.assertEqual(seed.DEFAULT_SHIP_FUNCTIONS, fleet_defaults.DEFAULT_SHIP_FUNCTIONS)
        self.assertEqual(seed.DEFAULT_STRUCTURE_TEMPLATES, fleet_defaults.DEFAULT_STRUCTURE_TEMPLATES)

    def test_default_values_pass_the_model_validation(self):
        for sort_order, name, color, fleet_roles, is_default, groups, types in seed.DEFAULT_SHIP_FUNCTIONS:
            with self.subTest(function=name):
                ShipFunction(name=name, color=color, fleet_roles=fleet_roles, is_default=is_default).clean_fields()
                self.assertTrue(all(label for _id, label in (*groups, *types)))
        for name, layout, description, sort_order in seed.DEFAULT_STRUCTURE_TEMPLATES:
            FleetStructureTemplate(name=name, layout=layout, description=description).full_clean(
                validate_unique=False
            )


class SeedTests(TestCase):
    def test_fresh_install_has_the_default_functions(self):
        self.assertEqual(functions_as_rows(), EXPECTED_FUNCTIONS)
        self.assertEqual(
            list(ShipFunction.objects.values_list("sort_order", flat=True)), list(range(10, 120, 10))
        )
        self.assertTrue(all(ShipFunction.objects.values_list("is_active", flat=True)))

    def test_rule_labels_are_the_english_names(self):
        labels = {(rule.kind, rule.target_id): rule.label for rule in ShipFunctionRule.objects.all()}
        self.assertEqual(labels[("group", 832)], "Logistics")
        self.assertEqual(labels[("group", 1972)], "Flag Cruiser")
        self.assertEqual(labels[("group", 883)], "Capital Industrial Ship")
        self.assertEqual(labels[("type", 670)], "Capsule")
        self.assertEqual(labels[("type", 33328)], "Capsule - Genolution 'Auroral' 197-variant")
        self.assertEqual(len(labels), 30)

    def test_fresh_install_has_the_standard_template(self):
        template = FleetStructureTemplate.objects.get()
        self.assertEqual((template.name, template.is_active, template.sort_order), ("Standard", True, 10))
        self.assertEqual(
            template.wings,
            [
                {"name": "Main", "squads": ["Alpha", "Bravo", "Charlie"]},
                {"name": "Logi", "squads": ["Logi"]},
                {"name": "Boosts", "squads": ["Boosts"]},
                {"name": "Tackle", "squads": ["Tackle"]},
            ],
        )

    def test_seeds_are_not_repeated(self):
        seed.seed_ship_functions(apps, None)
        seed.seed_structure_templates(apps, None)

        self.assertEqual(ShipFunction.objects.count(), 11)
        self.assertEqual(ShipFunctionRule.objects.count(), 30)
        self.assertEqual(FleetStructureTemplate.objects.count(), 1)

    def test_configured_tables_are_kept(self):
        ShipFunction.objects.exclude(name="DPS").delete()
        FleetStructureTemplate.objects.update(name="Ours", layout="Main: A")

        seed.seed_ship_functions(apps, None)
        seed.seed_structure_templates(apps, None)

        self.assertEqual(list(ShipFunction.objects.values_list("name", flat=True)), ["DPS"])
        self.assertFalse(ShipFunctionRule.objects.exists())
        self.assertEqual(list(FleetStructureTemplate.objects.values_list("name", flat=True)), ["Ours"])

    def test_empty_tables_are_seeded(self):
        ShipFunction.objects.all().delete()
        FleetStructureTemplate.objects.all().delete()

        seed.seed_ship_functions(apps, None)
        seed.seed_structure_templates(apps, None)

        self.assertEqual(functions_as_rows(), EXPECTED_FUNCTIONS)
        self.assertEqual(FleetStructureTemplate.objects.get().name, "Standard")

    def test_seed_command_fills_only_empty_tables(self):
        ShipFunction.objects.all().delete()
        FleetStructureTemplate.objects.update(layout="Main: A")

        output = StringIO()
        call_command("fleetops_seed", stdout=output, stderr=StringIO())

        self.assertIn("ship functions and fleet structure templates are ready", output.getvalue())
        self.assertEqual(functions_as_rows(), EXPECTED_FUNCTIONS)
        self.assertEqual(list(FleetStructureTemplate.objects.values_list("layout", flat=True)), ["Main: A"])

        ShipFunction.objects.filter(name="Cyno").delete()
        call_command("fleetops_seed", stdout=StringIO(), stderr=StringIO())
        self.assertFalse(ShipFunction.objects.filter(name="Cyno").exists())

    def test_seed_command_brings_back_the_standard_template(self):
        FleetStructureTemplate.objects.all().delete()

        call_command("fleetops_seed", stdout=StringIO(), stderr=StringIO())

        template = FleetStructureTemplate.objects.get()
        self.assertEqual(
            (template.name, template.layout, template.is_active, template.sort_order),
            ("Standard", "Main: Alpha, Bravo, Charlie\nLogi: Logi\nBoosts: Boosts\nTackle: Tackle", True, 10),
        )
        self.assertEqual([wing["name"] for wing in template.wings], ["Main", "Logi", "Boosts", "Tackle"])

        # A second run with the template in place changes nothing.
        call_command("fleetops_seed", stdout=StringIO(), stderr=StringIO())
        self.assertEqual(
            list(FleetStructureTemplate.objects.values_list("pk", "layout")), [(template.pk, template.layout)]
        )

    def test_default_layout_is_valid(self):
        for _name, layout, _description, _order in fleet_defaults.DEFAULT_STRUCTURE_TEMPLATES:
            self.assertEqual(len(parse_structure_layout(layout)), 4)


class NewFieldTests(TestCase):
    def test_settings_defaults(self):
        settings = FleetOpsSettings.get_solo()
        self.assertEqual(settings.bridge_excluded_group_ids, DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS)
        self.assertEqual(settings.bridge_excluded_groups, {30, 485, 513, 547, 659, 883, 902, 941, 1538, 4594, 5120})
        self.assertEqual(settings.fleet_live_refresh_seconds, 15)

    def test_settings_validation(self):
        settings = FleetOpsSettings.get_solo()
        for seconds, valid in ((0, True), (5, False), (9, False), (10, True), (300, True), (301, False)):
            settings.fleet_live_refresh_seconds = seconds
            with self.subTest(seconds=seconds):
                if valid:
                    settings.full_clean()
                else:
                    with self.assertRaises(ValidationError) as ctx:
                        settings.full_clean()
                    self.assertIn("fleet_live_refresh_seconds", ctx.exception.message_dict)
        settings.fleet_live_refresh_seconds = 15
        for value, valid in (("", True), ("30; 485 ,513", True), ("30,titan", False), ("-1", False)):
            settings.bridge_excluded_group_ids = value
            with self.subTest(groups=value):
                if valid:
                    settings.full_clean()
                else:
                    with self.assertRaises(ValidationError) as ctx:
                        settings.full_clean()
                    self.assertIn("bridge_excluded_group_ids", ctx.exception.message_dict)

    def test_unreadable_stored_exclusions_count_as_none(self):
        FleetOpsSettings.objects.filter(pk=1).update(bridge_excluded_group_ids="30,x")
        self.assertEqual(FleetOpsSettings.get_solo().bridge_excluded_groups, set())

    def test_operation_and_member_defaults(self):
        fc = f.create_user(perms=f.FC_PERMS)
        operation = f.create_operation(fc)
        state = f.member_state(operation, f.main_of(fc), role="fleet_commander", wing_id=-1, squad_id=-1)

        operation.refresh_from_db()
        self.assertEqual((operation.fleet_wings, operation.fleet_wings_read_at), ([], None))
        self.assertEqual((state.station_id, state.takes_fleet_warp, state.fleet_role), (None, None, "fleet_commander"))

    def test_wings_snapshot_round_trip(self):
        fc = f.create_user(perms=f.FC_PERMS)
        wings = f.wings((11, "Main", ((12, "Alpha"),)), ("Logi", ("Logi",)))
        operation = f.create_operation(fc, fleet_wings=wings, fleet_wings_read_at=timezone.now())

        operation.refresh_from_db()
        self.assertEqual(operation.fleet_wings[0], {"id": 11, "name": "Main", "squads": [{"id": 12, "name": "Alpha"}]})
        self.assertEqual(operation.fleet_wings[1]["name"], "Logi")


class ShipFunctionModelTests(TestCase):
    def test_one_active_default(self):
        dps = ShipFunction.objects.get(name="DPS")

        logi = f.ship_function("Logi Wing", is_default=True)

        dps.refresh_from_db()
        self.assertFalse(dps.is_default)
        self.assertEqual(logi.demoted_defaults, [dps])
        self.assertEqual(list(ShipFunction.objects.filter(is_default=True)), [logi])

    def test_an_inactive_default_does_not_take_the_flag(self):
        inactive = ShipFunction.objects.create(name="Old", is_default=True, is_active=False)

        self.assertEqual(inactive.demoted_defaults, [])
        self.assertTrue(ShipFunction.objects.get(name="DPS").is_default)
        with self.assertRaises(ValidationError) as ctx:
            inactive.full_clean()
        self.assertIn("is_default", ctx.exception.message_dict)

    def test_field_validation(self):
        cases = [
            ({"color": "#12345"}, "color"),
            ({"color": "red"}, "color"),
            ({"fleet_roles": "squad_member"}, "fleet_roles"),
            ({"fleet_roles": "boss"}, "fleet_roles"),
        ]
        for values, field in cases:
            with self.subTest(values=values), self.assertRaises(ValidationError) as ctx:
                ShipFunction(name="Test", **values).full_clean()
            self.assertIn(field, ctx.exception.message_dict)
        function = ShipFunction(name="Test", fleet_roles="wing_commander, fleet_commander", color="#ABCdef")
        function.full_clean()
        self.assertEqual(function.roles, ("fleet_commander", "wing_commander"))

    def test_a_ship_type_or_group_has_one_rule(self):
        rule = ShipFunctionRule.objects.get(kind="group", target_id=832)
        duplicate = ShipFunctionRule(function=ShipFunction.objects.get(name="DPS"), kind="group", target_id=832)

        with self.assertRaises(ValidationError):
            duplicate.full_clean()
        ShipFunctionRule(function=rule.function, kind="type", target_id=832).full_clean()

    def test_rules_follow_their_function(self):
        logistics = ShipFunction.objects.get(name="Logistics")
        logistics.delete()
        self.assertFalse(ShipFunctionRule.objects.filter(target_id__in=[832, 1527, 1538]).exists())

    def test_factory_moves_rules(self):
        function = f.ship_function("Logi Alts", rules=(("group", 832, "Logistics"), ("type", 11987)))

        self.assertEqual(ShipFunctionRule.objects.get(kind="group", target_id=832).function, function)
        self.assertEqual(function.rules.count(), 2)

    def test_structure_template_validation(self):
        template = f.structure_template("Roam", "Main: Alpha\n# scouts later\nTackle: Tackle")
        template.full_clean()
        self.assertEqual([wing["name"] for wing in template.wings], ["Main", "Tackle"])

        template.layout = "Main: Logistics 1"
        with self.assertRaises(ValidationError) as ctx:
            template.full_clean()
        self.assertEqual(
            ctx.exception.message_dict["layout"],
            ['Line 1: squad name "Logistics 1" is longer than 10 characters.'],
        )

    def test_str(self):
        rule = ShipFunctionRule.objects.get(kind="group", target_id=832)
        self.assertEqual(str(rule), "Ship group Logistics → Logistics")
        self.assertEqual(str(FleetStructureTemplate.objects.get()), "Standard")


class UpgradeFrom0012Tests(TransactionTestCase):
    """Migrates a database holding fleets, members and settings from 0012 to 0014 and back."""

    serialized_rollback = True
    start = [("fleetops", "0012_pap_rules_data")]
    target = [("fleetops", "0014_seed_fleet_management")]

    def migrate(self, nodes):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(nodes)
        return executor.loader.project_state(nodes).apps

    def setUp(self):
        super().setUp()
        self.addCleanup(self.migrate_to_latest)
        old_apps = self.migrate(self.start)
        self.insert_0012_rows(old_apps)
        self.new_apps = self.migrate(self.target)

    def migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("fleetops"))

    def insert_0012_rows(self, old_apps):
        User = old_apps.get_model("auth", "User")
        Settings = old_apps.get_model("fleetops", "FleetOpsSettings")
        FleetType = old_apps.get_model("fleetops", "FleetType")
        Operation = old_apps.get_model("fleetops", "FleetOperation")
        State = old_apps.get_model("fleetops", "FleetMemberState")

        Settings.objects.update_or_create(pk=1, defaults={"tracking_interval": 90})
        fc = User.objects.create(username="fleet-fc")
        fleet_type = FleetType.objects.create(name="Upgrade Type", point_weight=Decimal("1.00"))
        started = timezone.now() - timedelta(hours=1)
        self.operation_ids = []
        for status in ("active", "closed"):
            operation = Operation.objects.create(
                created_by=fc,
                fc_user=fc,
                fc_character_id=90_555_001,
                fc_character_name="Upgrade FC",
                fleet_type=fleet_type,
                formup="Jita",
                status=status,
                esi_fleet_id=1_045_555_000_001 if status == "active" else 1_045_555_000_002,
                started_at=started,
            )
            self.operation_ids.append(operation.pk)
            State.objects.create(
                operation=operation,
                character_id=90_555_002,
                character_name="Upgrade Pilot",
                fleet_role="squad_member",
                wing_id=2_000_000_001,
                squad_id=3_000_000_001,
                first_seen=started,
                last_seen=started,
            )

    def test_existing_rows_get_the_defaults_and_the_seeds_are_created(self):
        Settings = self.new_apps.get_model("fleetops", "FleetOpsSettings")
        Operation = self.new_apps.get_model("fleetops", "FleetOperation")
        State = self.new_apps.get_model("fleetops", "FleetMemberState")
        Function = self.new_apps.get_model("fleetops", "ShipFunction")
        Rule = self.new_apps.get_model("fleetops", "ShipFunctionRule")
        Template = self.new_apps.get_model("fleetops", "FleetStructureTemplate")

        settings = Settings.objects.get(pk=1)
        self.assertEqual(
            (settings.tracking_interval, settings.bridge_excluded_group_ids, settings.fleet_live_refresh_seconds),
            (90, DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS, 15),
        )
        for operation in Operation.objects.filter(pk__in=self.operation_ids):
            with self.subTest(status=operation.status):
                self.assertEqual((operation.fleet_wings, operation.fleet_wings_read_at), ([], None))
        for state in State.objects.all():
            self.assertEqual((state.station_id, state.takes_fleet_warp), (None, None))
            self.assertEqual((state.wing_id, state.squad_id), (2_000_000_001, 3_000_000_001))
        self.assertEqual(functions_as_rows(Function), EXPECTED_FUNCTIONS)
        self.assertEqual(Rule.objects.count(), 30)
        self.assertEqual(list(Template.objects.values_list("name", flat=True)), ["Standard"])

        # The current models read the upgraded rows.
        self.migrate_to_latest()
        state = FleetMemberState.objects.get(operation_id=self.operation_ids[0])
        self.assertIsNone(state.takes_fleet_warp)
        self.assertEqual(FleetOperation.objects.get(pk=self.operation_ids[1]).fleet_wings, [])

    def test_the_upgrade_can_be_reversed_and_repeated(self):
        old_apps = self.migrate(self.start)
        Operation = old_apps.get_model("fleetops", "FleetOperation")
        self.assertEqual(Operation.objects.filter(pk__in=self.operation_ids).count(), 2)
        self.assertNotIn("fleet_wings", {field.name for field in Operation._meta.get_fields()})

        new_apps = self.migrate(self.target)
        self.assertEqual(new_apps.get_model("fleetops", "ShipFunction").objects.count(), 11)
        self.assertEqual(new_apps.get_model("fleetops", "FleetStructureTemplate").objects.count(), 1)
