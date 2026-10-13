"""Upgrade steps of migrations 0011-0012: multiplier tiers, special roles, PAP rule snapshots, time in fleet
and the merged and replaced attendance of earlier versions."""

import importlib
import inspect
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.apps import apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Sum
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from fleetops.management.commands.fleetops_seed import DEFAULT_MULTIPLIER_TIERS, DEFAULT_SPECIAL_ROLES
from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    FleetMemberState,
    FleetOperation,
    FleetStartPreset,
    MultiplierTier,
    OperationRoleAssignment,
    PapLink,
    PapRegistration,
    ReviewStatus,
    SpecialRole,
)
from fleetops.services.operations import end_fleet
from fleetops.services.pap import refresh_after_change

from . import factories as f

upgrade = importlib.import_module("fleetops.migrations.0012_pap_rules_data")
schema = importlib.import_module("fleetops.migrations.0011_pap_rules_schema")

Status = FleetOperation.Status
Source = AttendanceRecord.Source
REPLACED_NOTE = "Replaced by manual attendance."
CAPPED_NOTE = "Attendance capped by per-user fleet limit."


def tiers():
    return list(MultiplierTier.objects.values_list("threshold_minutes", "multiplier"))


def roles():
    return {role.key: (role.name, role.grants_fc_credit, role.is_active) for role in SpecialRole.objects.all()}


class MigrationModuleTests(TestCase):
    def test_migrations_do_not_import_fleetops_code(self):
        # Migrations must keep working when the constants or models they were written against change.
        for module in (schema, upgrade):
            source = inspect.getsource(module)
            with self.subTest(module=module.__name__):
                self.assertNotIn("from fleetops", source)
                self.assertNotIn("import fleetops", source)

    def test_seed_command_creates_the_same_defaults_as_the_upgrade(self):
        self.assertEqual(upgrade.DEFAULT_TIERS, DEFAULT_MULTIPLIER_TIERS)
        self.assertEqual(upgrade.DEFAULT_ROLES, DEFAULT_SPECIAL_ROLES)

    def test_fixed_roles_match_the_role_choices_of_earlier_versions(self):
        self.assertEqual(
            {key: name for key, name, _credit, _order in upgrade.DEFAULT_ROLES},
            dict(OperationRoleAssignment.Role.choices),
        )

    def test_schema_multiplier_bound_matches_the_models(self):
        from fleetops.models import MAX_MULTIPLIER

        self.assertEqual(schema.MAX_MULTIPLIER, MAX_MULTIPLIER)


class TierSeedTests(TestCase):
    def test_fresh_install_has_the_default_tiers(self):
        self.assertEqual(tiers(), [(90, 2), (180, 3)])

    def test_seed_is_idempotent(self):
        upgrade.seed_multiplier_tiers(apps, None)
        upgrade.seed_multiplier_tiers(apps, None)

        self.assertEqual(tiers(), [(90, 2), (180, 3)])

    def test_configured_tiers_are_kept(self):
        f.multiplier_tiers((60, 2))

        upgrade.seed_multiplier_tiers(apps, None)

        self.assertEqual(tiers(), [(60, 2)])

    def test_empty_table_is_seeded(self):
        MultiplierTier.objects.all().delete()

        upgrade.seed_multiplier_tiers(apps, None)

        self.assertEqual(tiers(), [(90, 2), (180, 3)])


class SpecialRoleUpgradeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fc = f.create_user(perms=f.FC_PERMS)
        cls.operation = f.create_operation(cls.fc, status=Status.CLOSED)

    def assignment(self, role, *, grants_fc_credit=False, operation=None):
        pilot = f.create_user()
        main = f.main_of(pilot)
        assignment = OperationRoleAssignment.objects.create(
            operation=operation or self.operation,
            role=role,
            character_id=main.character_id,
            character_name=main.character_name,
            auth_user=pilot,
            grants_fc_credit=grants_fc_credit,
        )
        # As stored before the upgrade: no role row yet.
        OperationRoleAssignment.objects.filter(pk=assignment.pk).update(special_role=None)
        assignment.special_role = None
        return assignment

    def test_fresh_install_has_the_three_roles(self):
        self.assertEqual(
            roles(),
            {
                "backseat_fc": ("Back Seat FC", True, True),
                "logi_anchor": ("Logi Anchor", False, True),
                "snowflake": ("Snowflake Member", False, True),
            },
        )

    def test_seed_is_idempotent_and_keeps_configured_roles(self):
        SpecialRole.objects.filter(key="backseat_fc").update(name="Backseat", grants_fc_credit=False, sort_order=5)
        SpecialRole.objects.filter(key="snowflake").update(is_active=False)

        upgrade.link_special_roles(apps, None)
        upgrade.link_special_roles(apps, None)

        self.assertEqual(SpecialRole.objects.count(), 3)
        self.assertEqual(
            roles(),
            {
                "backseat_fc": ("Backseat", False, True),
                "logi_anchor": ("Logi Anchor", False, True),
                "snowflake": ("Snowflake Member", False, False),
            },
        )

    def test_deleted_seed_role_is_created_again(self):
        SpecialRole.objects.filter(key="logi_anchor").delete()

        upgrade.link_special_roles(apps, None)

        self.assertEqual(SpecialRole.objects.get(key="logi_anchor").name, "Logi Anchor")

    def test_assignments_are_linked_by_key_and_keep_their_credit_snapshot(self):
        # The snapshot is not recalculated: statistics of earlier fleets stay as they were.
        backseat_without_credit = self.assignment("backseat_fc", grants_fc_credit=False)
        backseat_with_credit = self.assignment("backseat_fc", grants_fc_credit=True)
        logi_with_credit = self.assignment("logi_anchor", grants_fc_credit=True)
        snowflake = self.assignment("snowflake")

        upgrade.link_special_roles(apps, None)

        expected = {
            backseat_without_credit: ("backseat_fc", False),
            backseat_with_credit: ("backseat_fc", True),
            logi_with_credit: ("logi_anchor", True),
            snowflake: ("snowflake", False),
        }
        for assignment, (key, credit) in expected.items():
            assignment.refresh_from_db()
            with self.subTest(role=key, credit=credit):
                self.assertEqual(assignment.special_role, SpecialRole.objects.get(key=key))
                self.assertEqual(assignment.role, key)
                self.assertEqual(assignment.grants_fc_credit, credit)
        self.assertEqual(str(backseat_with_credit), f"Back Seat FC: {backseat_with_credit.character_name}")

    def test_unknown_role_keys_become_inactive_roles(self):
        scout = self.assignment("fleet_scout", grants_fc_credit=True)
        second_scout = self.assignment("fleet_scout")
        blank = self.assignment("")

        upgrade.link_special_roles(apps, None)

        role = SpecialRole.objects.get(key="fleet_scout")
        self.assertEqual((role.name, role.grants_fc_credit, role.is_active), ("Fleet Scout", False, False))
        for assignment in (scout, second_scout):
            assignment.refresh_from_db()
            self.assertEqual(assignment.special_role, role)
        scout.refresh_from_db()
        self.assertTrue(scout.grants_fc_credit)
        blank.refresh_from_db()
        self.assertEqual(blank.special_role.key, "unknown")
        self.assertEqual(blank.role, "")
        self.assertEqual(SpecialRole.objects.count(), 5)

    def test_unknown_key_whose_name_is_taken_gets_the_key_in_its_name(self):
        SpecialRole.objects.create(key="custom", name="Fleet Scout")
        assignment = self.assignment("fleet_scout")

        upgrade.link_special_roles(apps, None)

        assignment.refresh_from_db()
        self.assertEqual(assignment.special_role.name, "Fleet Scout (fleet_scout)")

    def test_upper_case_key_joins_the_role_with_the_same_key(self):
        assignment = self.assignment("BACKSEAT_FC")

        upgrade.link_special_roles(apps, None)

        assignment.refresh_from_db()
        self.assertEqual(assignment.special_role.key, "backseat_fc")
        self.assertEqual(SpecialRole.objects.count(), 3)

    def test_linked_assignments_are_left_alone(self):
        logi = SpecialRole.objects.get(key="logi_anchor")
        assignment = self.assignment("backseat_fc")
        OperationRoleAssignment.objects.filter(pk=assignment.pk).update(special_role=logi)

        upgrade.link_special_roles(apps, None)

        assignment.refresh_from_db()
        self.assertEqual(assignment.special_role, logi)


class OperationSnapshotTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fc = f.create_user(perms=f.FC_PERMS)

    def test_finished_fleets_keep_the_legacy_rules(self):
        operations = {
            status: f.create_operation(self.fc, status=status)
            for status in (Status.CLOSED, Status.CANCELLED, Status.ERROR, Status.ACTIVE, Status.ENDING, Status.STARTING)
        }
        operations[Status.SCHEDULED] = f.create_scheduled_operation(self.fc)
        f.settings(attendance_limit=2)

        upgrade.snapshot_pap_rules(apps, None)

        for status, operation in operations.items():
            operation.refresh_from_db()
            with self.subTest(status=status):
                expected = 1 if status in (Status.CLOSED, Status.CANCELLED, Status.ERROR) else 2
                self.assertEqual(operation.pap_rules, expected)
                self.assertEqual(operation.pap_character_limit, 2)
                self.assertEqual(operation.pap_minimum_minutes, 0)

    def test_unlimited_setting_stays_unlimited(self):
        operation = f.create_operation(self.fc, status=Status.CLOSED, pap_character_limit=4)
        f.settings(attendance_limit=None)

        upgrade.snapshot_pap_rules(apps, None)

        operation.refresh_from_db()
        self.assertIsNone(operation.pap_character_limit)

    def test_upgraded_fleets_stay_approved_and_counted(self):
        operation = f.create_operation(self.fc, status=Status.CLOSED)

        upgrade.snapshot_pap_rules(apps, None)

        operation.refresh_from_db()
        self.assertEqual(operation.review_status, ReviewStatus.APPROVED)
        self.assertEqual(operation.stats_mode, FleetOperation.StatsMode.AUTO)
        self.assertTrue(operation.counts_for_stats)
        self.assertFalse(operation.multiplier_pending)
        self.assertFalse(operation.link_only)


class TimeInFleetTests(TestCase):
    def test_time_between_first_and_last_seen_is_the_approximation(self):
        fc = f.create_user(perms=f.FC_PERMS)
        operation = f.create_operation(fc, status=Status.CLOSED)
        start = operation.started_at
        stayed = f.member_state(operation, f.main_of(fc), first_seen=start, last_seen=start + timedelta(minutes=45))
        clock_skew = f.member_state(operation, 91_000_001, first_seen=start, last_seen=start - timedelta(seconds=5))
        counted = f.member_state(
            operation, 91_000_002, first_seen=start, last_seen=start + timedelta(hours=2), accumulated_seconds=60
        )

        upgrade.approximate_time_in_fleet(apps, None)

        self.assertEqual(
            {state.pk: state.accumulated_seconds for state in FleetMemberState.objects.filter(operation=operation)},
            {stayed.pk: 45 * 60, clock_skew.pk: 0, counted.pk: 2 * 3600},
        )


class AttendanceCorrectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fc = f.create_user(perms=f.FC_PERMS)
        cls.operation = f.create_operation(cls.fc, status=Status.CLOSED, attendance_multiplier=2)

    def pilot(self):
        user = f.create_user()
        return user, f.main_of(user)

    def test_merged_value_above_the_multiplier_becomes_the_legacy_extra(self):
        plain_user, _ = self.pilot()
        merged_user, _ = self.pilot()
        capped_user, _ = self.pilot()
        manual_user, _ = self.pilot()
        plain = f.add_attendance(self.operation, plain_user, value=2)
        merged = f.add_attendance(self.operation, merged_user, value=5)
        capped = f.add_attendance(self.operation, capped_user, value=4, granted=False, capped=True)
        manual = f.add_attendance(self.operation, manual_user, value=3, source=Source.MANUAL)
        updated_at = AttendanceRecord.objects.get(pk=merged.pk).updated_at

        upgrade.keep_merged_attendance(apps, None)

        rows = {
            row.pk: (row.extra_value, row.legacy_extra_value, row.attendance_value)
            for row in AttendanceRecord.objects.filter(operation=self.operation)
        }
        self.assertEqual(
            rows,
            {plain.pk: (0, 0, 2), merged.pk: (3, 3, 5), capped.pk: (0, 0, 4), manual.pk: (0, 0, 3)},
        )
        self.assertEqual(AttendanceRecord.objects.get(pk=merged.pk).updated_at, updated_at)

    def test_merges_on_running_fleets_are_kept_too(self):
        # A merge made on a fleet that was still running at the upgrade must survive its close.
        operation = f.create_operation(self.fc, status=Status.ACTIVE, attendance_multiplier=1)
        user, _ = self.pilot()
        merged = f.add_attendance(operation, user, value=2)

        upgrade.keep_merged_attendance(apps, None)

        merged.refresh_from_db()
        self.assertEqual((merged.extra_value, merged.legacy_extra_value), (1, 1))

    def test_a_merge_on_a_running_fleet_still_forces_the_pap_at_its_close(self):
        # Merges always forced the PAP, also over the character limit.
        user, main = self.pilot()
        alts = [f.add_alt(user) for _ in range(2)]
        operation = f.create_operation(
            self.fc, started_at=timezone.now() - timedelta(hours=2), attendance_multiplier=1, pap_character_limit=2
        )
        for character, seconds in ((main, 7000), (alts[0], 6500), (alts[1], 1800)):
            f.member_state(operation, character, accumulated_seconds=seconds, active=False)
        merged = f.add_attendance(operation, user, alts[1], value=2)
        upgrade.keep_merged_attendance(apps, None)

        with mock.patch("fleetops.services.operations.sync_operation"):
            end_fleet(operation, attendance_multiplier=2)

        merged.refresh_from_db()
        self.assertEqual((merged.granted, merged.capped, merged.attendance_value), (True, True, 3))
        granted = AttendanceRecord.objects.filter(operation=operation, granted=True)
        self.assertEqual(granted.aggregate(total=Sum("attendance_value"))["total"], 7)

    def test_a_later_replacement_still_wins_over_a_merge_made_before_the_upgrade(self):
        user, main = self.pilot()
        operation = f.create_operation(self.fc, status=Status.CLOSED, attendance_multiplier=1)
        f.member_state(operation, main, accumulated_seconds=600, active=False)
        merged = f.add_attendance(operation, user, value=2)
        upgrade.keep_merged_attendance(apps, None)
        f.add_attendance(operation, user, source=Source.MANUAL, correction="replace", value=1)

        refresh_after_change(operation)

        merged.refresh_from_db()
        self.assertEqual((merged.granted, merged.replaced), (False, True))

    def audit(self, action, row):
        AuditLog.objects.create(actor=self.fc, action=action, object_type="AttendanceRecord", object_id=str(row.pk))

    def manual(self, user, operation=None):
        # Earlier versions audited every manual row they created.
        manual = f.add_attendance(operation or self.operation, user, value=1, source=Source.MANUAL)
        self.audit("attendance.manual_create", manual)
        return manual

    def corrections(self):
        return dict(AttendanceRecord.objects.filter(source=Source.MANUAL).values_list("pk", "correction"))

    def test_the_manual_row_a_replace_added_becomes_the_replacement(self):
        user, character = self.pilot()
        automatic = f.add_attendance(self.operation, user, value=2, granted=False, notes=REPLACED_NOTE)
        earlier = self.manual(user)
        self.audit("attendance.replace_auto", automatic)
        replacement = self.manual(user)
        later = self.manual(user)
        other_user, _ = self.pilot()
        other_manual = self.manual(other_user)

        upgrade.keep_replaced_attendance(apps, None)

        automatic.refresh_from_db()
        self.assertTrue(automatic.replaced)
        self.assertFalse(automatic.granted)
        self.assertEqual(
            self.corrections(),
            {earlier.pk: "keep", replacement.pk: "replace", later.pk: "keep", other_manual.pk: "keep"},
        )
        self.assertEqual(automatic.character_id, character.character_id)

    def test_each_replace_marks_the_row_it_added(self):
        user, _ = self.pilot()
        automatic = f.add_attendance(self.operation, user, value=2, granted=False, notes=REPLACED_NOTE)
        self.audit("attendance.replace_auto", automatic)
        first = self.manual(user)
        self.audit("attendance.replace_auto", automatic)
        second = self.manual(user)

        upgrade.keep_replaced_attendance(apps, None)

        self.assertEqual(self.corrections(), {first.pk: "replace", second.pk: "replace"})

    def test_without_audit_entries_every_manual_row_replaces(self):
        user, _ = self.pilot()
        f.add_attendance(self.operation, user, value=2, granted=False, notes=REPLACED_NOTE)
        rows = [f.add_attendance(self.operation, user, value=1, source=Source.MANUAL) for _ in range(2)]

        upgrade.keep_replaced_attendance(apps, None)

        self.assertEqual(self.corrections(), {rows[0].pk: "replace", rows[1].pk: "replace"})

    def test_deleting_an_earlier_extra_credit_keeps_the_tracked_pap_replaced(self):
        operation = f.create_operation(
            self.fc, status=Status.CLOSED, attendance_multiplier=2, pap_rules=FleetOperation.PapRules.LEGACY
        )
        user, _ = self.pilot()
        automatic = f.add_attendance(operation, user, value=2, granted=False, notes=REPLACED_NOTE)
        earlier = self.manual(user, operation)
        self.audit("attendance.replace_auto", automatic)
        self.manual(user, operation)
        upgrade.keep_replaced_attendance(apps, None)

        earlier.delete()
        refresh_after_change(operation)

        automatic.refresh_from_db()
        self.assertEqual((automatic.granted, automatic.replaced), (False, True))
        granted = AttendanceRecord.objects.filter(operation=operation, granted=True)
        self.assertEqual(granted.aggregate(total=Sum("attendance_value"))["total"], 1)

    def test_replaced_capped_row_is_marked_too(self):
        user, _ = self.pilot()
        automatic = f.add_attendance(
            self.operation, user, granted=False, capped=True, notes=f"{CAPPED_NOTE}\n{REPLACED_NOTE}"
        )
        manual = f.add_attendance(self.operation, user, value=1, source=Source.MANUAL)

        upgrade.keep_replaced_attendance(apps, None)

        automatic.refresh_from_db()
        manual.refresh_from_db()
        self.assertTrue(automatic.replaced)
        self.assertTrue(automatic.capped)
        self.assertEqual(manual.correction, "replace")

    def test_replaced_row_without_manual_row_is_only_marked(self):
        user, _ = self.pilot()
        automatic = f.add_attendance(self.operation, user, granted=False, notes=REPLACED_NOTE)

        upgrade.keep_replaced_attendance(apps, None)

        automatic.refresh_from_db()
        self.assertTrue(automatic.replaced)
        self.assertFalse(AttendanceRecord.objects.exclude(correction="keep").exists())

    def test_rows_without_the_note_are_not_replaced(self):
        capped_user, _ = self.pilot()
        capped = f.add_attendance(self.operation, capped_user, granted=False, capped=True, notes=CAPPED_NOTE)
        manual = f.add_attendance(self.operation, capped_user, source=Source.MANUAL)
        granted_user, _ = self.pilot()
        merged_after_replace = f.add_attendance(self.operation, granted_user, value=3, notes=REPLACED_NOTE)

        upgrade.keep_replaced_attendance(apps, None)

        for row in (capped, manual, merged_after_replace):
            row.refresh_from_db()
            self.assertFalse(row.replaced)
            self.assertEqual(row.correction, "keep")

    def test_new_rows_keep_the_defaults(self):
        user, _ = self.pilot()
        row = f.add_attendance(self.operation, user)

        for step in (upgrade.keep_merged_attendance, upgrade.keep_replaced_attendance):
            step(apps, None)

        row.refresh_from_db()
        self.assertEqual(
            (row.review_status, row.correction, row.below_minimum, row.replaced, row.tracked_seconds),
            (ReviewStatus.APPROVED, "keep", False, False, None),
        )


class NewTablesTests(TestCase):
    def test_new_tables_start_empty(self):
        for model in (PapLink, PapRegistration, FleetStartPreset):
            with self.subTest(model=model.__name__):
                self.assertFalse(model.objects.exists())


class UpgradeFrom0010Tests(TransactionTestCase):
    """Migrates a database holding 0.1.0a6 development data from 0010 to 0012."""

    serialized_rollback = True
    start = [("fleetops", "0010_comms_join_token_unique")]
    target = [("fleetops", "0012_pap_rules_data")]

    def migrate(self, nodes):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(nodes)
        return executor.loader.project_state(nodes).apps

    def setUp(self):
        super().setUp()
        self.addCleanup(self.migrate_to_latest)
        old_apps = self.migrate(self.start)
        self.insert_0010_rows(old_apps)
        self.new_apps = self.migrate(self.target)

    def migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("fleetops"))

    def insert_0010_rows(self, old_apps):
        User = old_apps.get_model("auth", "User")
        Settings = old_apps.get_model("fleetops", "FleetOpsSettings")
        FleetType = old_apps.get_model("fleetops", "FleetType")
        Operation = old_apps.get_model("fleetops", "FleetOperation")
        State = old_apps.get_model("fleetops", "FleetMemberState")
        Attendance = old_apps.get_model("fleetops", "AttendanceRecord")
        Assignment = old_apps.get_model("fleetops", "OperationRoleAssignment")
        Audit = old_apps.get_model("fleetops", "AuditLog")

        Settings.objects.update_or_create(pk=1, defaults={"attendance_limit": 2})
        self.fc = User.objects.create(username="legacy-fc")
        pilots = [User.objects.create(username=f"legacy-pilot-{number}") for number in range(5)]
        fleet_type = FleetType.objects.create(name="Legacy Type", point_weight=Decimal("1.50"))
        started = timezone.now() - timedelta(days=3)

        def operation(status, multiplier=1, started_at=started, **extra):
            return Operation.objects.create(
                created_by=self.fc,
                fc_user=self.fc,
                fc_character_id=90_123_456,
                fc_character_name="Legacy FC",
                fleet_type=fleet_type,
                fleet_point_weight_snapshot=Decimal("1.50"),
                formup="Jita",
                status=status,
                attendance_multiplier=multiplier,
                started_at=started_at,
                ended_at=started + timedelta(hours=2) if status == "closed" else None,
                **extra,
            )

        self.closed = operation("closed", multiplier=2)
        self.cancelled = operation("cancelled")
        self.error = operation("error")
        self.active = operation("active", multiplier=3)
        self.scheduled = operation("scheduled", started_at=None, scheduled_at=started + timedelta(days=5))

        def attendance(op, user, character_id, source="automatic", value=1, **extra):
            return Attendance.objects.create(
                operation=op,
                character_id=character_id,
                character_name=f"Pilot {character_id}",
                auth_user=user,
                source=source,
                attendance_value=value,
                **extra,
            )

        self.granted = attendance(self.closed, pilots[0], 91_000_001, value=2)
        self.merged = attendance(self.closed, pilots[1], 91_000_002, value=3, notes="Merged correction")
        self.capped = attendance(
            self.closed, pilots[0], 91_000_003, value=2, granted=False, capped=True, notes=CAPPED_NOTE
        )
        self.replaced = attendance(
            self.closed, pilots[2], 91_000_004, value=2, granted=False, notes=REPLACED_NOTE
        )
        def audit(action, row):
            Audit.objects.create(actor=self.fc, action=action, object_type="AttendanceRecord", object_id=str(row.pk))

        # A "replace" was audited on the automatic row, then created its manual row.
        audit("attendance.replace_auto", self.replaced)
        self.replacing = attendance(self.closed, pilots[2], 91_000_004, source="manual", value=1, created_by=self.fc)
        audit("attendance.manual_create", self.replacing)
        self.second_manual = attendance(self.closed, pilots[2], 91_000_004, source="manual", value=1)
        self.separate = attendance(self.closed, pilots[3], 91_000_005, source="manual", value=2)
        self.active_merged = attendance(self.active, pilots[4], 91_000_006, value=4)

        def state(op, character_id, minutes, active=False):
            return State.objects.create(
                operation=op,
                character_id=character_id,
                character_name=f"Pilot {character_id}",
                first_seen=started,
                last_seen=started + timedelta(minutes=minutes),
                left_at=None if active else started + timedelta(minutes=minutes),
                is_active=active,
            )

        self.long_state = state(self.closed, 91_000_001, 95)
        self.short_state = state(self.closed, 91_000_003, 12)
        self.skewed_state = state(self.closed, 91_000_002, -1)
        self.running_state = state(self.active, 91_000_006, 30, active=True)

        def assignment(role, character_id, grants_fc_credit=False):
            return Assignment.objects.create(
                operation=self.closed,
                role=role,
                character_id=character_id,
                character_name=f"Pilot {character_id}",
                grants_fc_credit=grants_fc_credit,
            )

        self.backseat = assignment("backseat_fc", 91_000_001, grants_fc_credit=True)
        self.backseat_without_credit = assignment("backseat_fc", 91_000_002)
        self.logi = assignment("logi_anchor", 91_000_003)
        self.snowflake = assignment("snowflake", 91_000_004)
        self.scout = assignment("fleet_scout", 91_000_005, grants_fc_credit=True)

    def get(self, model_name, row):
        return self.new_apps.get_model("fleetops", model_name).objects.get(pk=row.pk)

    def test_defaults_are_seeded_and_role_assignments_linked(self):
        self.check_seeds()
        self.check_role_assignments()

    def test_fleets_members_and_attendance_are_upgraded(self):
        self.check_pap_rules()
        self.check_time_in_fleet()
        self.check_attendance()
        operation = FleetOperation.objects.get(pk=self.closed.pk)
        self.assertEqual(operation.fc_points, Decimal("3.00"))

    def check_seeds(self):
        Tier = self.new_apps.get_model("fleetops", "MultiplierTier")
        Role = self.new_apps.get_model("fleetops", "SpecialRole")

        self.assertEqual(
            list(Tier.objects.order_by("threshold_minutes").values_list("threshold_minutes", "multiplier")),
            [(90, 2), (180, 3)],
        )
        self.assertEqual(
            {role.key: (role.name, role.grants_fc_credit, role.is_active) for role in Role.objects.all()},
            {
                "backseat_fc": ("Back Seat FC", True, True),
                "logi_anchor": ("Logi Anchor", False, True),
                "snowflake": ("Snowflake Member", False, True),
                "fleet_scout": ("Fleet Scout", False, False),
            },
        )
        for model_name in ("PapLink", "PapRegistration", "FleetStartPreset"):
            self.assertFalse(self.new_apps.get_model("fleetops", model_name).objects.exists())

    def check_role_assignments(self):
        Role = self.new_apps.get_model("fleetops", "SpecialRole")
        expected = {
            self.backseat: ("backseat_fc", True),
            self.backseat_without_credit: ("backseat_fc", False),
            self.logi: ("logi_anchor", False),
            self.snowflake: ("snowflake", False),
            self.scout: ("fleet_scout", True),
        }
        for old, (key, credit) in expected.items():
            assignment = self.get("OperationRoleAssignment", old)
            with self.subTest(role=key):
                self.assertEqual(assignment.special_role_id, Role.objects.get(key=key).pk)
                self.assertEqual(assignment.role, key)
                self.assertEqual(assignment.grants_fc_credit, credit)

    def check_pap_rules(self):
        for old, rules in (
            (self.closed, 1),
            (self.cancelled, 1),
            (self.error, 1),
            (self.active, 2),
            (self.scheduled, 2),
        ):
            operation = self.get("FleetOperation", old)
            with self.subTest(status=operation.status):
                self.assertEqual(operation.pap_rules, rules)
                self.assertEqual(operation.pap_character_limit, 2)
                self.assertEqual(operation.pap_minimum_minutes, 0)
                self.assertEqual(operation.review_status, "approved")
                self.assertEqual(operation.stats_mode, "auto")
                self.assertTrue(operation.counts_for_stats)
                self.assertFalse(operation.multiplier_pending)
                self.assertEqual(operation.multiplier_source, "")
                self.assertFalse(operation.link_only)
        closed = self.get("FleetOperation", self.closed)
        self.assertEqual(closed.attendance_multiplier, 2)

    def check_time_in_fleet(self):
        self.assertEqual(self.get("FleetMemberState", self.long_state).accumulated_seconds, 95 * 60)
        self.assertEqual(self.get("FleetMemberState", self.short_state).accumulated_seconds, 12 * 60)
        self.assertEqual(self.get("FleetMemberState", self.skewed_state).accumulated_seconds, 0)
        self.assertEqual(self.get("FleetMemberState", self.running_state).accumulated_seconds, 30 * 60)

    def check_attendance(self):
        def summary(old):
            row = self.get("AttendanceRecord", old)
            return (
                row.attendance_value,
                row.granted,
                row.capped,
                row.replaced,
                row.extra_value,
                row.legacy_extra_value,
                row.correction,
                row.review_status,
            )

        self.assertEqual(summary(self.granted), (2, True, False, False, 0, 0, "keep", "approved"))
        self.assertEqual(summary(self.merged), (3, True, False, False, 1, 1, "keep", "approved"))
        self.assertEqual(summary(self.capped), (2, False, True, False, 0, 0, "keep", "approved"))
        self.assertEqual(summary(self.replaced), (2, False, False, True, 0, 0, "keep", "approved"))
        self.assertEqual(summary(self.replacing), (1, True, False, False, 0, 0, "replace", "approved"))
        self.assertEqual(summary(self.second_manual), (1, True, False, False, 0, 0, "keep", "approved"))
        self.assertEqual(summary(self.separate), (2, True, False, False, 0, 0, "keep", "approved"))
        self.assertEqual(summary(self.active_merged), (4, True, False, False, 1, 1, "keep", "approved"))
        for old in (self.granted, self.capped, self.replacing):
            row = self.get("AttendanceRecord", old)
            self.assertFalse(row.below_minimum)
            self.assertIsNone(row.tracked_seconds)
