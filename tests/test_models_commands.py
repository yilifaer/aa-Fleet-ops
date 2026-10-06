"""Tests for FleetOps models, migrations, admin, hooks and management commands."""

import importlib
import re
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from allianceauth import hooks
from django.apps import apps
from django.contrib import admin
from django.contrib.auth.models import AnonymousUser, Permission, User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import RequestFactory, TestCase
from django.urls import resolve, reverse
from django.utils import timezone

from fleetops.apps import FleetOpsConfig
from fleetops.auth_hooks import FleetOpsMenu
from fleetops.constants import (
    DEFAULT_MOTD_TEMPLATE,
    DEFAULT_PING_TEMPLATE,
    FLEET_READ_SCOPE,
    FLEET_SCOPES,
    FLEET_WRITE_SCOPE,
)
from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    ChannelPreset,
    CommsPreset,
    DiscordWebhook,
    FleetMemberEvent,
    FleetMemberState,
    FleetOperation,
    FleetOpsSettings,
    FleetType,
    IncentivePeriod,
    MessageTemplate,
    MonthlyFCStatistic,
    OperationAction,
    OperationRoleAssignment,
    PingTarget,
)

from . import factories as f

WEBHOOK_SECRET = "https://discord.com/api/webhooks/123456789/sUp3r-s3cret-t0ken"

ALL_PERMISSIONS = [
    "basic_access",
    "start_fleet",
    "manage_own_fleet",
    "view_all_fleets",
    "create_manual_fleet",
    "manage_fleets",
    "view_corp_stats",
    "view_all_stats",
    "manage_attendance",
    "manage_incentives",
    "manage_configuration",
    "view_audit_log",
]


def run_command(name, *args):
    out = StringIO()
    call_command(name, *args, stdout=out, stderr=StringIO())
    return out.getvalue()


def add_event(operation, character, *, age_days=0, event_type=FleetMemberEvent.EventType.JOIN):
    event = FleetMemberEvent.objects.create(
        operation=operation,
        character_id=character.character_id,
        character_name=character.character_name,
        event_type=event_type,
    )
    if age_days:
        FleetMemberEvent.objects.filter(pk=event.pk).update(created_at=timezone.now() - timedelta(days=age_days))
    return event


def add_unmapped_attendance(operation, character_id=None, name="Unknown Pilot"):
    return AttendanceRecord.objects.create(
        operation=operation,
        character_id=character_id or f.next_id(),
        character_name=name,
        source=AttendanceRecord.Source.AUTOMATIC,
        first_seen=operation.started_at,
        last_seen=operation.started_at,
    )


def days_ago(days):
    return timezone.now() - timedelta(days=days)


class FleetOpsSettingsTests(TestCase):
    def test_get_solo_returns_seeded_row_with_defaults(self):
        obj = FleetOpsSettings.get_solo()

        self.assertEqual(obj.pk, 1)
        self.assertIsNone(obj.attendance_limit)
        self.assertEqual(obj.tracking_interval, 60)
        self.assertEqual(obj.stale_threshold, 180)
        self.assertTrue(obj.auto_end_enabled)
        self.assertEqual(obj.auto_end_missing_count, 3)
        self.assertFalse(obj.incentive_enabled)
        self.assertEqual(obj.incentive_minimum_fleets, 3)
        self.assertEqual(obj.data_retention_days, 365)
        self.assertEqual(obj.history_alliance_ids, "")
        self.assertTrue(obj.srp_auto_create)
        self.assertEqual(obj.srp_provider, "auto")
        self.assertEqual(str(obj), "FleetOps Settings")

    def test_get_solo_is_a_singleton(self):
        first = FleetOpsSettings.get_solo()
        second = FleetOpsSettings.get_solo()

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(FleetOpsSettings.objects.count(), 1)

    def test_get_solo_recreates_defaults_when_row_is_missing(self):
        FleetOpsSettings.objects.all().delete()

        obj = FleetOpsSettings.get_solo()

        self.assertEqual(obj.pk, 1)
        self.assertEqual(obj.tracking_interval, 60)
        self.assertEqual(FleetOpsSettings.objects.count(), 1)

    def test_get_solo_returns_saved_values(self):
        f.settings(attendance_limit=2, tracking_interval=90, history_alliance_ids="3000001")

        obj = FleetOpsSettings.get_solo()

        self.assertEqual(obj.attendance_limit, 2)
        self.assertEqual(obj.tracking_interval, 90)
        self.assertEqual(obj.history_alliance_ids, "3000001")

    def test_defaults_pass_validation(self):
        FleetOpsSettings.get_solo().full_clean()

    def test_minimum_values_pass_validation(self):
        obj = FleetOpsSettings.get_solo()
        obj.attendance_limit = 1
        obj.tracking_interval = 30
        obj.stale_threshold = 60
        obj.auto_end_missing_count = 2
        obj.incentive_minimum_fleets = 1
        obj.data_retention_days = 365

        obj.full_clean()

    def test_values_below_minimum_are_rejected(self):
        cases = {
            "attendance_limit": 0,
            "tracking_interval": 29,
            "stale_threshold": 59,
            "auto_end_missing_count": 1,
            "incentive_minimum_fleets": 0,
            "data_retention_days": 364,
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                obj = FleetOpsSettings.get_solo()
                setattr(obj, field, value)
                with self.assertRaises(ValidationError) as ctx:
                    obj.full_clean()
                self.assertIn(field, ctx.exception.message_dict)

    def test_negative_values_are_rejected(self):
        obj = FleetOpsSettings.get_solo()
        obj.tracking_interval = -5
        with self.assertRaises(ValidationError) as ctx:
            obj.full_clean()
        self.assertIn("tracking_interval", ctx.exception.message_dict)

    def test_empty_attendance_limit_means_unlimited_and_is_valid(self):
        obj = FleetOpsSettings.get_solo()
        obj.attendance_limit = None
        obj.full_clean()

    def test_large_retention_is_valid(self):
        obj = FleetOpsSettings.get_solo()
        obj.data_retention_days = 3650
        obj.full_clean()

    def test_history_alliance_ids_accepts_id_list(self):
        obj = FleetOpsSettings.get_solo()
        obj.history_alliance_ids = f"{f.DEFAULT_ALLIANCE[0]}, {f.FOREIGN_ALLIANCE[0]}"
        obj.full_clean()

    def test_history_alliance_ids_rejects_non_numeric_entries(self):
        obj = FleetOpsSettings.get_solo()
        obj.history_alliance_ids = f"{f.DEFAULT_ALLIANCE[0]}, Foreign Alliance"
        with self.assertRaises(ValidationError) as ctx:
            obj.full_clean()
        self.assertIn("history_alliance_ids", ctx.exception.message_dict)


class PermissionTests(TestCase):
    def test_all_twelve_permissions_exist(self):
        codenames = set(
            Permission.objects.filter(
                content_type__app_label="fleetops",
                content_type__model="fleetopssettings",
            ).values_list("codename", flat=True)
        )

        for codename in ALL_PERMISSIONS:
            with self.subTest(codename=codename):
                self.assertIn(codename, codenames)

    def test_permission_bundles_resolve(self):
        lead = f.create_user(perms=f.FC_LEAD_PERMS)

        for codename in ALL_PERMISSIONS:
            with self.subTest(codename=codename):
                self.assertTrue(lead.has_perm(f"fleetops.{codename}"))


class ModelStrTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("strfc", perms=f.FC_PERMS, main_name="Str Commander")
        self.fleet_type = FleetType.objects.create(name="Strategic Operation", short_name="StratOps", point_weight=Decimal("1.00"))
        self.operation = f.create_operation(self.fc, type_obj=self.fleet_type)

    def test_fleet_type_prefers_short_name(self):
        self.assertEqual(str(self.fleet_type), "StratOps")
        self.assertEqual(str(FleetType(name="Call To Arms")), "Call To Arms")

    def test_simple_named_presets(self):
        self.assertEqual(str(CommsPreset(name="Main Comms")), "Main Comms")
        self.assertEqual(str(PingTarget(name="@everyone")), "@everyone")

    def test_discord_webhook_str_never_contains_url(self):
        hook = DiscordWebhook.objects.create(name="Ops Pings", webhook_url=WEBHOOK_SECRET)

        self.assertEqual(str(hook), "Ops Pings")
        self.assertNotIn("discord.com", str(hook))
        self.assertNotIn("discord.com", repr(hook))

    def test_channel_and_template_str_include_type(self):
        logi = ChannelPreset(name="Logi 1", channel_type=ChannelPreset.ChannelType.LOGI, channel_value="logi-1")
        boost = ChannelPreset(name="Boosts", channel_type=ChannelPreset.ChannelType.BOOST, channel_value="boost")
        ping = MessageTemplate(name="Short", template_type=MessageTemplate.TemplateType.PING, content="x")
        motd = MessageTemplate(name="Long", template_type=MessageTemplate.TemplateType.MOTD, content="x")

        self.assertEqual(str(logi), "Logi: Logi 1")
        self.assertEqual(str(boost), "Boost: Boosts")
        self.assertEqual(str(ping), "Ping: Short")
        self.assertEqual(str(motd), "MOTD: Long")

    def test_operation_str(self):
        self.assertEqual(
            str(self.operation),
            f"StratOps — Str Commander ({self.operation.uuid})",
        )

    def test_operation_related_str(self):
        action = OperationAction.objects.create(
            operation=self.operation, action="ping", status=OperationAction.Status.SKIPPED
        )
        state = FleetMemberState.objects.create(
            operation=self.operation,
            character_id=1,
            character_name="Alpha",
            first_seen=timezone.now(),
            last_seen=timezone.now(),
        )
        role = OperationRoleAssignment.objects.create(
            operation=self.operation,
            role=OperationRoleAssignment.Role.BACKSEAT_FC,
            character_id=2,
            character_name="Bravo",
        )

        self.assertEqual(str(action), f"{self.operation.pk}: ping skipped")
        self.assertEqual(str(state), f"Alpha @ {self.operation.pk}")
        self.assertEqual(str(role), "Back Seat FC: Bravo")

    def test_incentive_period_str_is_zero_padded(self):
        self.assertEqual(str(IncentivePeriod(year=2026, month=3)), "2026-03")
        self.assertEqual(str(IncentivePeriod(year=2026, month=11)), "2026-11")

    def test_audit_log_str(self):
        entry = AuditLog.objects.create(action="fleet.end", object_type="FleetOperation", object_id="42")
        self.assertIn("fleet.end FleetOperation:42", str(entry))


class ModelDefaultsAndConstraintsTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)

    def test_operation_defaults(self):
        operation = FleetOperation.objects.create(
            created_by=self.fc,
            fc_user=self.fc,
            fc_character_id=1,
            fc_character_name="FC",
            fleet_type=f.fleet_type(),
            formup="Jita",
        )

        self.assertEqual(operation.status, FleetOperation.Status.DRAFT)
        self.assertIsNotNone(operation.uuid)
        self.assertEqual(operation.attendance_multiplier, 1)
        self.assertTrue(operation.send_ping)
        self.assertFalse(operation.is_manual)
        self.assertFalse(operation.tracking_enabled)
        self.assertEqual(operation.fleet_missing_count, 0)
        self.assertIsNone(operation.start_request_id)

    def test_operation_uuids_are_unique(self):
        first = f.create_operation(self.fc)
        second = f.create_operation(self.fc)
        self.assertNotEqual(first.uuid, second.uuid)

    def test_start_request_id_is_unique(self):
        request_id = f.create_operation(self.fc).uuid
        FleetOperation.objects.filter(pk=f.create_operation(self.fc).pk).update(start_request_id=request_id)
        other = f.create_operation(self.fc)

        with self.assertRaises(IntegrityError), transaction.atomic():
            FleetOperation.objects.filter(pk=other.pk).update(start_request_id=request_id)

    def test_attendance_multiplier_accepts_only_one_to_three(self):
        field = FleetOperation._meta.get_field("attendance_multiplier")
        for value in (1, 2, 3):
            field.run_validators(value)
        for value in (0, 4):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                field.run_validators(value)

    def test_operation_snapshots_weight_independent_of_fleet_type(self):
        type_obj = FleetType.objects.create(name="Snapshot Type", point_weight=Decimal("1.50"))
        operation = f.create_operation(self.fc, type_obj=type_obj)

        type_obj.point_weight = Decimal("3.00")
        type_obj.save()
        operation.refresh_from_db()

        self.assertEqual(operation.fleet_point_weight_snapshot, Decimal("1.50"))

    def test_fleet_type_in_use_cannot_be_deleted(self):
        type_obj = FleetType.objects.create(name="Protected Type", point_weight=Decimal("1.00"))
        f.create_operation(self.fc, type_obj=type_obj)

        with self.assertRaises(ProtectedError):
            type_obj.delete()

    def test_fc_user_cannot_be_deleted_while_operations_exist(self):
        f.create_operation(self.fc)
        with self.assertRaises(ProtectedError):
            self.fc.delete()

    def test_deleting_presets_keeps_operation(self):
        comms = CommsPreset.objects.create(name="Comms A")
        logi = ChannelPreset.objects.create(name="L", channel_type="logi", channel_value="logi")
        hook = DiscordWebhook.objects.create(name="Hook", webhook_url=WEBHOOK_SECRET)
        target = PingTarget.objects.create(name="Target", webhook=hook)
        operation = f.create_operation(self.fc, comms=comms, logi_channel=logi, ping_target=target)

        hook.delete()
        target.refresh_from_db()
        self.assertIsNone(target.webhook)

        comms.delete()
        logi.delete()
        target.delete()
        operation.refresh_from_db()
        self.assertIsNone(operation.comms)
        self.assertIsNone(operation.logi_channel)
        self.assertIsNone(operation.ping_target)

    def test_deleting_operation_cascades_to_children(self):
        operation = f.create_operation(self.fc)
        f.add_attendance(operation, self.fc)
        OperationAction.objects.create(operation=operation, action="ping")
        FleetMemberState.objects.create(
            operation=operation, character_id=1, character_name="A", first_seen=timezone.now(), last_seen=timezone.now()
        )
        add_event(operation, f.main_of(self.fc))
        OperationRoleAssignment.objects.create(
            operation=operation, role=OperationRoleAssignment.Role.SNOWFLAKE, character_id=1, character_name="A"
        )

        operation.delete()

        self.assertFalse(AttendanceRecord.objects.exists())
        self.assertFalse(OperationAction.objects.exists())
        self.assertFalse(FleetMemberState.objects.exists())
        self.assertFalse(FleetMemberEvent.objects.exists())
        self.assertFalse(OperationRoleAssignment.objects.exists())

    def test_deleting_attendee_user_keeps_attendance_unmapped(self):
        member = f.create_user()
        record = f.add_attendance(f.create_operation(self.fc), member)

        member.delete()
        record.refresh_from_db()

        self.assertIsNone(record.auth_user)
        self.assertEqual(record.attendance_value, 1)

    def test_unique_constraints(self):
        operation = f.create_operation(self.fc)
        now = timezone.now()
        period = IncentivePeriod.objects.create(year=2026, month=5)
        cases = [
            (
                lambda: ChannelPreset.objects.create(name="Dup", channel_type="logi", channel_value="a"),
                lambda: ChannelPreset.objects.create(name="Dup", channel_type="logi", channel_value="b"),
            ),
            (
                lambda: MessageTemplate.objects.create(name="Dup", template_type="ping", content="a"),
                lambda: MessageTemplate.objects.create(name="Dup", template_type="ping", content="b"),
            ),
            (
                lambda: OperationAction.objects.create(operation=operation, action="motd"),
                lambda: OperationAction.objects.create(operation=operation, action="motd"),
            ),
            (
                lambda: FleetMemberState.objects.create(
                    operation=operation, character_id=7, character_name="A", first_seen=now, last_seen=now
                ),
                lambda: FleetMemberState.objects.create(
                    operation=operation, character_id=7, character_name="A", first_seen=now, last_seen=now
                ),
            ),
            (
                lambda: OperationRoleAssignment.objects.create(
                    operation=operation, role="logi_anchor", character_id=7, character_name="A"
                ),
                lambda: OperationRoleAssignment.objects.create(
                    operation=operation, role="logi_anchor", character_id=7, character_name="A"
                ),
            ),
            (
                lambda: MonthlyFCStatistic.objects.create(period=period, fc_user=self.fc),
                lambda: MonthlyFCStatistic.objects.create(period=period, fc_user=self.fc),
            ),
            (
                lambda: None,
                lambda: IncentivePeriod.objects.create(year=2026, month=5),
            ),
        ]
        for index, (first, duplicate) in enumerate(cases):
            with self.subTest(case=index):
                first()
                with self.assertRaises(IntegrityError), transaction.atomic():
                    duplicate()

    def test_same_name_allowed_for_different_types(self):
        ChannelPreset.objects.create(name="Shared", channel_type="logi", channel_value="a")
        ChannelPreset.objects.create(name="Shared", channel_type="boost", channel_value="b")
        MessageTemplate.objects.create(name="Shared", template_type="ping", content="a")
        MessageTemplate.objects.create(name="Shared", template_type="motd", content="b")

        self.assertEqual(ChannelPreset.objects.filter(name="Shared").count(), 2)
        self.assertEqual(MessageTemplate.objects.filter(name="Shared").count(), 2)

    def test_incentive_period_month_validators(self):
        IncentivePeriod(year=2026, month=1).full_clean()
        IncentivePeriod(year=2026, month=12).full_clean()
        for month in (0, 13):
            with self.subTest(month=month), self.assertRaises(ValidationError):
                IncentivePeriod(year=2026, month=month).full_clean()

    def test_incentive_period_budget_cannot_be_negative(self):
        with self.assertRaises(ValidationError):
            IncentivePeriod(year=2026, month=1, budget=-1).full_clean()

    def test_operation_action_statuses(self):
        self.assertEqual(
            set(OperationAction.Status.values),
            {"pending", "success", "failed", "skipped"},
        )

    def test_member_event_types(self):
        self.assertEqual(
            set(FleetMemberEvent.EventType.values),
            {
                "join",
                "leave",
                "rejoin",
                "ship_change",
                "system_change",
                "role_change",
                "wing_change",
                "squad_change",
            },
        )

    def test_operation_lifecycle_statuses(self):
        self.assertEqual(
            set(FleetOperation.Status.values),
            {"draft", "starting", "active", "ending", "closed", "cancelled", "error"},
        )


class ModelOrderingTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)

    def test_fleet_types_order_by_sort_order_then_name(self):
        FleetType.objects.all().delete()
        FleetType.objects.create(name="Zulu", sort_order=10)
        FleetType.objects.create(name="Bravo", sort_order=20)
        FleetType.objects.create(name="Alpha", sort_order=20)
        FleetType.objects.create(name="Yankee", sort_order=5)

        self.assertEqual(
            list(FleetType.objects.values_list("name", flat=True)),
            ["Yankee", "Zulu", "Alpha", "Bravo"],
        )

    def test_operations_order_newest_first(self):
        old = f.create_operation(self.fc, started_at=days_ago(3))
        new = f.create_operation(self.fc, started_at=days_ago(1))
        middle = f.create_operation(self.fc, started_at=days_ago(2))

        self.assertEqual(list(FleetOperation.objects.all()), [new, middle, old])

    def test_attendance_orders_by_character_name(self):
        operation = f.create_operation(self.fc)
        for name in ("Charlie", "Alpha", "Bravo"):
            add_unmapped_attendance(operation, name=name)

        self.assertEqual(
            list(AttendanceRecord.objects.values_list("character_name", flat=True)),
            ["Alpha", "Bravo", "Charlie"],
        )

    def test_incentive_periods_order_newest_first(self):
        IncentivePeriod.objects.create(year=2025, month=12)
        IncentivePeriod.objects.create(year=2026, month=2)
        IncentivePeriod.objects.create(year=2026, month=1)

        self.assertEqual(
            [str(p) for p in IncentivePeriod.objects.all()],
            ["2026-02", "2026-01", "2025-12"],
        )

    def test_monthly_statistics_order_by_points_then_username(self):
        period = IncentivePeriod.objects.create(year=2026, month=4)
        zed = f.create_user("zed")
        amy = f.create_user("amy")
        top = f.create_user("top")
        MonthlyFCStatistic.objects.create(period=period, fc_user=zed, total_points=Decimal("5"))
        MonthlyFCStatistic.objects.create(period=period, fc_user=amy, total_points=Decimal("5"))
        MonthlyFCStatistic.objects.create(period=period, fc_user=top, total_points=Decimal("9"))

        self.assertEqual(
            [s.fc_user.username for s in MonthlyFCStatistic.objects.all()],
            ["top", "amy", "zed"],
        )

    def test_role_assignments_order_by_role_then_name(self):
        operation = f.create_operation(self.fc)
        for role, name in (("snowflake", "Alpha"), ("backseat_fc", "Zulu"), ("backseat_fc", "Bravo")):
            OperationRoleAssignment.objects.create(
                operation=operation, role=role, character_id=f.next_id(), character_name=name
            )

        self.assertEqual(
            list(OperationRoleAssignment.objects.values_list("role", "character_name")),
            [("backseat_fc", "Bravo"), ("backseat_fc", "Zulu"), ("snowflake", "Alpha")],
        )

    def test_audit_log_and_events_order_newest_first(self):
        old = AuditLog.objects.create(action="a", object_type="X", object_id="1")
        new = AuditLog.objects.create(action="b", object_type="X", object_id="2")
        AuditLog.objects.filter(pk=old.pk).update(created_at=days_ago(1))

        operation = f.create_operation(self.fc)
        old_event = add_event(operation, f.main_of(self.fc), age_days=2)
        new_event = add_event(operation, f.main_of(self.fc))

        self.assertEqual(list(AuditLog.objects.all()), [new, old])
        self.assertEqual(list(FleetMemberEvent.objects.all()), [new_event, old_event])

    def test_presets_order_by_name(self):
        CommsPreset.objects.create(name="Zeta")
        CommsPreset.objects.create(name="Alpha")
        ChannelPreset.objects.create(name="B", channel_type="logi", channel_value="x")
        ChannelPreset.objects.create(name="A", channel_type="logi", channel_value="x")
        ChannelPreset.objects.create(name="C", channel_type="boost", channel_value="x")

        self.assertEqual(list(CommsPreset.objects.values_list("name", flat=True)), ["Alpha", "Zeta"])
        self.assertEqual(
            list(ChannelPreset.objects.values_list("channel_type", "name")),
            [("boost", "C"), ("logi", "A"), ("logi", "B")],
        )


class MigrationTests(TestCase):
    def test_seed_migration_creates_settings_and_default_templates(self):
        self.assertTrue(FleetOpsSettings.objects.filter(pk=1).exists())
        ping = MessageTemplate.objects.get(template_type="ping", name="Default Ping")
        motd = MessageTemplate.objects.get(template_type="motd", name="Default MOTD")
        self.assertTrue(ping.is_default and ping.is_active)
        self.assertTrue(motd.is_default and motd.is_active)

    def test_seed_migration_templates_match_constants(self):
        migration = importlib.import_module("fleetops.migrations.0002_seed_defaults")
        self.assertEqual(migration.PING, DEFAULT_PING_TEMPLATE)
        self.assertEqual(migration.MOTD, DEFAULT_MOTD_TEMPLATE)

    def test_models_and_migrations_are_in_sync(self):
        out = StringIO()
        try:
            call_command("makemigrations", "fleetops", check=True, dry_run=True, stdout=out, stderr=StringIO())
        except SystemExit:
            self.fail(f"Missing migrations for fleetops:\n{out.getvalue()}")


class AppConfigTests(TestCase):
    def test_app_config(self):
        config = apps.get_app_config("fleetops")
        self.assertIsInstance(config, FleetOpsConfig)
        self.assertEqual(config.verbose_name, "Fleet Operations")
        self.assertEqual(config.default_auto_field, "django.db.models.BigAutoField")

    def test_fleet_scopes(self):
        self.assertEqual(FLEET_READ_SCOPE, "esi-fleets.read_fleet.v1")
        self.assertEqual(FLEET_WRITE_SCOPE, "esi-fleets.write_fleet.v1")
        self.assertEqual(FLEET_SCOPES, [FLEET_READ_SCOPE, FLEET_WRITE_SCOPE])


class AuthHookTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _menu(self):
        items = [fn() for fn in hooks.get_hooks("menu_item_hook")]
        menus = [item for item in items if isinstance(item, FleetOpsMenu)]
        self.assertEqual(len(menus), 1)
        return menus[0]

    def _render(self, user):
        request = self.factory.get("/dashboard/")
        request.user = user
        return self._menu().render(request)

    def test_menu_hook_is_registered(self):
        menu = self._menu()
        self.assertEqual(menu.text, "FleetOps")
        self.assertEqual(menu.url_name, "fleetops:dashboard")

    def test_menu_renders_for_basic_access(self):
        user = f.create_user(perms=f.MEMBER_PERMS)

        html = self._render(user)

        self.assertIn("FleetOps", html)
        self.assertIn(reverse("fleetops:dashboard"), html)

    def test_menu_hidden_without_basic_access(self):
        user = f.create_user()
        self.assertEqual(self._render(user), "")

    def test_menu_hidden_for_other_fleetops_permissions_only(self):
        user = f.create_user(perms=["fleetops.view_all_stats", "fleetops.manage_configuration"])
        self.assertEqual(self._render(user), "")

    def test_menu_hidden_for_anonymous(self):
        self.assertEqual(self._render(AnonymousUser()), "")

    def test_url_hook_registered_under_fleetops(self):
        url_hooks = [fn() for fn in hooks.get_hooks("url_hook")]
        patterns = [str(h.include_pattern.pattern) for h in url_hooks]
        self.assertIn("^fleetops/", patterns)

        self.assertEqual(reverse("fleetops:dashboard"), "/fleetops/")
        match = resolve("/fleetops/")
        self.assertEqual(match.namespace, "fleetops")
        self.assertEqual(match.url_name, "dashboard")
        self.assertEqual(resolve("/fleetops/configuration/settings/").url_name, "configuration_settings")

    def test_dashboard_requires_login(self):
        response = self.client.get("/fleetops/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])


class SeedCommandTests(TestCase):
    def _clear(self):
        MessageTemplate.objects.all().delete()
        FleetOpsSettings.objects.all().delete()
        FleetType.objects.all().delete()
        PingTarget.objects.all().delete()

    def test_seed_without_demo_creates_settings_and_templates_only(self):
        self._clear()

        output = run_command("fleetops_seed")

        self.assertIn("ready", output)
        self.assertTrue(FleetOpsSettings.objects.filter(pk=1).exists())
        ping = MessageTemplate.objects.get(template_type="ping", name="Default Ping")
        motd = MessageTemplate.objects.get(template_type="motd", name="Default MOTD")
        self.assertEqual(ping.content, DEFAULT_PING_TEMPLATE)
        self.assertEqual(motd.content, DEFAULT_MOTD_TEMPLATE)
        self.assertTrue(ping.is_default and ping.is_active)
        self.assertTrue(motd.is_default and motd.is_active)
        self.assertFalse(FleetType.objects.exists())
        self.assertFalse(PingTarget.objects.exists())
        self.assertFalse(DiscordWebhook.objects.exists())

    def test_seed_demo_creates_documented_presets(self):
        self._clear()

        output = run_command("fleetops_seed", "--demo")

        self.assertIn("Demo data created", output)
        weights = {t.short_name: t.point_weight for t in FleetType.objects.all()}
        self.assertEqual(
            weights,
            {"PCT": Decimal("0.50"), "StratOps": Decimal("1.00"), "CTA": Decimal("1.50")},
        )
        self.assertTrue(all(FleetType.objects.values_list("is_active", flat=True)))
        self.assertEqual(
            [str(t) for t in FleetType.objects.all()],
            ["PCT", "StratOps", "CTA"],
        )

        target = PingTarget.objects.get()
        self.assertEqual(target.name, "Manual / Copy Only")
        self.assertEqual(target.target_value, "")
        self.assertIsNone(target.webhook)
        self.assertTrue(target.is_active)

        self.assertEqual(MessageTemplate.objects.filter(is_default=True).count(), 2)
        self.assertEqual(FleetOpsSettings.objects.count(), 1)

    def test_seed_demo_never_creates_webhooks_or_secrets(self):
        self._clear()

        output = run_command("fleetops_seed", "--demo")

        self.assertFalse(DiscordWebhook.objects.exists())
        self.assertFalse(PingTarget.objects.exclude(webhook=None).exists())
        self.assertNotIn("discord.com", output)

    def test_seed_demo_is_idempotent(self):
        run_command("fleetops_seed", "--demo")
        counts = {
            model: model.objects.count()
            for model in (FleetType, PingTarget, MessageTemplate, FleetOpsSettings, DiscordWebhook)
        }

        run_command("fleetops_seed", "--demo")
        run_command("fleetops_seed")

        for model, count in counts.items():
            with self.subTest(model=model.__name__):
                self.assertEqual(model.objects.count(), count)
        self.assertEqual(FleetType.objects.filter(short_name="CTA").count(), 1)
        self.assertEqual(PingTarget.objects.filter(name="Manual / Copy Only").count(), 1)
        self.assertEqual(MessageTemplate.objects.filter(name="Default Ping").count(), 1)
        self.assertEqual(MessageTemplate.objects.filter(name="Default MOTD").count(), 1)

    def test_seed_keeps_existing_settings_and_ping_target(self):
        f.settings(tracking_interval=120, attendance_limit=2)
        hook = DiscordWebhook.objects.create(name="Ops", webhook_url=WEBHOOK_SECRET)
        PingTarget.objects.create(name="Manual / Copy Only", target_value="@here", webhook=hook)

        run_command("fleetops_seed", "--demo")

        settings_obj = FleetOpsSettings.get_solo()
        self.assertEqual(settings_obj.tracking_interval, 120)
        self.assertEqual(settings_obj.attendance_limit, 2)
        target = PingTarget.objects.get(name="Manual / Copy Only")
        self.assertEqual(target.target_value, "@here")
        self.assertEqual(target.webhook, hook)
        self.assertEqual(DiscordWebhook.objects.count(), 1)

    def test_seed_rerun_keeps_customised_default_templates(self):
        template = MessageTemplate.objects.get(template_type="ping", name="Default Ping")
        template.content = "Custom alliance ping {{ fc }}"
        template.is_default = False
        template.save()

        run_command("fleetops_seed")

        template.refresh_from_db()
        self.assertEqual(template.content, "Custom alliance ping {{ fc }}")
        self.assertFalse(template.is_default)

    def test_seed_demo_rerun_keeps_configured_fleet_type_weights(self):
        run_command("fleetops_seed", "--demo")
        cta = FleetType.objects.get(name="Call To Arms")
        cta.point_weight = Decimal("2.25")
        cta.is_active = False
        cta.save()

        run_command("fleetops_seed", "--demo")

        cta.refresh_from_db()
        self.assertEqual(cta.point_weight, Decimal("2.25"))
        self.assertFalse(cta.is_active)


class PruneHistoryCommandTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.member = f.create_user()
        self.old_op = f.create_operation(
            self.fc, status=FleetOperation.Status.CLOSED, started_at=days_ago(400)
        )
        self.recent_op = f.create_operation(
            self.fc, status=FleetOperation.Status.CLOSED, started_at=days_ago(30)
        )
        self.old_record = f.add_attendance(self.old_op, self.member)
        self.recent_record = f.add_attendance(self.recent_op, self.member)
        self.old_event = add_event(self.old_op, f.main_of(self.member), age_days=400)
        self.recent_event = add_event(self.recent_op, f.main_of(self.member), age_days=30)

    def test_dry_run_deletes_nothing(self):
        output = run_command("fleetops_prune_history", "--dry-run")

        self.assertIn("Would remove: 1 old attendance, 0 departed-member attendance, 1 old member events.", output)
        self.assertEqual(AttendanceRecord.objects.count(), 2)
        self.assertEqual(FleetMemberEvent.objects.count(), 2)

    def test_real_run_prunes_records_older_than_retention(self):
        output = run_command("fleetops_prune_history")

        self.assertIn("Removed: 1 old attendance, 0 departed-member attendance, 1 old member events.", output)
        self.assertEqual(list(AttendanceRecord.objects.all()), [self.recent_record])
        self.assertEqual(list(FleetMemberEvent.objects.all()), [self.recent_event])
        self.assertTrue(FleetOperation.objects.filter(pk=self.old_op.pk).exists())

    def test_second_run_has_nothing_left_to_prune(self):
        run_command("fleetops_prune_history")
        output = run_command("fleetops_prune_history")
        self.assertIn("Removed: 0 old attendance, 0 departed-member attendance, 0 old member events.", output)

    def test_records_just_inside_one_year_are_kept(self):
        edge_op = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED, started_at=days_ago(364))
        edge_record = f.add_attendance(edge_op, self.member)

        run_command("fleetops_prune_history")

        self.assertTrue(AttendanceRecord.objects.filter(pk=edge_record.pk).exists())

    def test_retention_below_365_behaves_as_365(self):
        FleetOpsSettings.objects.filter(pk=1).update(data_retention_days=30)
        mid_op = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED, started_at=days_ago(200))
        mid_record = f.add_attendance(mid_op, self.member)
        mid_event = add_event(mid_op, f.main_of(self.member), age_days=200)

        run_command("fleetops_prune_history")

        self.assertTrue(AttendanceRecord.objects.filter(pk=mid_record.pk).exists())
        self.assertTrue(AttendanceRecord.objects.filter(pk=self.recent_record.pk).exists())
        self.assertTrue(FleetMemberEvent.objects.filter(pk=mid_event.pk).exists())
        self.assertFalse(AttendanceRecord.objects.filter(pk=self.old_record.pk).exists())

    def test_longer_retention_is_respected(self):
        f.settings(data_retention_days=500)

        run_command("fleetops_prune_history")

        self.assertEqual(AttendanceRecord.objects.count(), 2)
        self.assertEqual(FleetMemberEvent.objects.count(), 2)

    def test_departed_members_are_pruned_when_alliance_filter_is_configured(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        departed = f.create_user(alliance=f.FOREIGN_ALLIANCE)
        departed_record = f.add_attendance(self.recent_op, departed)
        member_with_foreign_alt = f.create_user()
        alt = f.add_alt(member_with_foreign_alt, corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        alt_record = f.add_attendance(self.recent_op, member_with_foreign_alt, alt)
        unmapped_recent = add_unmapped_attendance(self.recent_op)
        unmapped_old = add_unmapped_attendance(self.old_op)

        dry_output = run_command("fleetops_prune_history", "--dry-run")
        self.assertIn("1 departed-member attendance", dry_output)
        self.assertEqual(AttendanceRecord.objects.count(), 6)

        run_command("fleetops_prune_history")

        remaining = set(AttendanceRecord.objects.values_list("pk", flat=True))
        self.assertEqual(remaining, {self.recent_record.pk, alt_record.pk, unmapped_recent.pk})
        self.assertNotIn(departed_record.pk, remaining)
        self.assertNotIn(unmapped_old.pk, remaining)

    def test_semicolon_separated_alliance_ids_are_accepted(self):
        f.settings(history_alliance_ids=f"{f.FOREIGN_ALLIANCE[0]}; {f.DEFAULT_ALLIANCE[0]}")
        foreign = f.create_user(alliance=f.FOREIGN_ALLIANCE)
        foreign_record = f.add_attendance(self.recent_op, foreign)

        run_command("fleetops_prune_history")

        self.assertTrue(AttendanceRecord.objects.filter(pk=foreign_record.pk).exists())
        self.assertTrue(AttendanceRecord.objects.filter(pk=self.recent_record.pk).exists())

    def test_without_alliance_filter_departed_users_are_kept(self):
        departed = f.create_user(alliance=f.FOREIGN_ALLIANCE)
        record = f.add_attendance(self.recent_op, departed)

        run_command("fleetops_prune_history")

        self.assertTrue(AttendanceRecord.objects.filter(pk=record.pk).exists())

    def test_old_departed_record_is_removed_once(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        departed = f.create_user(alliance=f.FOREIGN_ALLIANCE)
        old_departed = f.add_attendance(self.old_op, departed)

        run_command("fleetops_prune_history")

        self.assertFalse(AttendanceRecord.objects.filter(pk=old_departed.pk).exists())
        self.assertEqual(list(AttendanceRecord.objects.all()), [self.recent_record])

    def test_reported_totals_match_rows_removed(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        departed = f.create_user(alliance=f.FOREIGN_ALLIANCE)
        f.add_attendance(self.old_op, departed)
        before = AttendanceRecord.objects.count()

        output = run_command("fleetops_prune_history")

        removed = before - AttendanceRecord.objects.count()
        old_count, departed_count = map(
            int, re.search(r"(\d+) old attendance, (\d+) departed-member", output).groups()
        )
        # old_op holds the member's and the departed user's rows: two rows removed in total.
        self.assertEqual(removed, 2)
        self.assertEqual(old_count + departed_count, removed)


class AdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser("root", "root@example.com", "password")
        cls.fc = f.create_user(perms=f.FC_PERMS)
        cls.webhook = DiscordWebhook.objects.create(name="Ops Pings", webhook_url=WEBHOOK_SECRET)
        cls.target = PingTarget.objects.create(name="Everyone", target_value="@everyone", webhook=cls.webhook)
        cls.fleet_type = FleetType.objects.create(name="Admin Type", short_name="AT", point_weight=Decimal("1.00"))
        CommsPreset.objects.create(name="Comms", voice_url="mumble://voice.example.com")
        ChannelPreset.objects.create(name="Logi", channel_type="logi", channel_value="logi-ch")
        cls.operation = f.create_operation(cls.fc, type_obj=cls.fleet_type, ping_target=cls.target)
        OperationAction.objects.create(operation=cls.operation, action="ping", status="failed", error_message="boom")
        FleetMemberState.objects.create(
            operation=cls.operation,
            character_id=1,
            character_name="Alpha",
            first_seen=timezone.now(),
            last_seen=timezone.now(),
        )
        add_event(cls.operation, f.main_of(cls.fc))
        f.add_attendance(cls.operation, cls.fc)
        OperationRoleAssignment.objects.create(
            operation=cls.operation, role="backseat_fc", character_id=2, character_name="Bravo"
        )
        period = IncentivePeriod.objects.create(year=2026, month=9)
        MonthlyFCStatistic.objects.create(period=period, fc_user=cls.fc)
        AuditLog.objects.create(
            actor=cls.fc,
            action="configuration.update",
            object_type="DiscordWebhook",
            object_id=str(cls.webhook.pk),
            new_value={"webhook_url": "***redacted***"},
        )

    def setUp(self):
        self.client.force_login(self.superuser)

    @staticmethod
    def _url(model, view, *args):
        meta = model._meta
        return reverse(f"admin:{meta.app_label}_{meta.model_name}_{view}", args=args)

    def _fleetops_models(self):
        return [model for model in admin.site._registry if model._meta.app_label == "fleetops"]

    def test_every_fleetops_model_is_registered(self):
        self.assertEqual(
            set(self._fleetops_models()),
            set(apps.get_app_config("fleetops").get_models()),
        )

    def test_changelists_load(self):
        for model in self._fleetops_models():
            with self.subTest(model=model.__name__):
                response = self.client.get(self._url(model, "changelist"))
                self.assertEqual(response.status_code, 200)

    def test_add_pages_load(self):
        for model in self._fleetops_models():
            if model is FleetOpsSettings:
                continue
            with self.subTest(model=model.__name__):
                response = self.client.get(self._url(model, "add"))
                self.assertEqual(response.status_code, 200)

    def test_change_pages_load(self):
        for model in self._fleetops_models():
            obj = model.objects.first()
            self.assertIsNotNone(obj, model.__name__)
            with self.subTest(model=model.__name__):
                response = self.client.get(self._url(model, "change", obj.pk))
                self.assertEqual(response.status_code, 200)

    def test_settings_admin_is_a_singleton(self):
        self.assertEqual(self.client.get(self._url(FleetOpsSettings, "add")).status_code, 403)
        model_admin = admin.site._registry[FleetOpsSettings]
        request = RequestFactory().get("/")
        request.user = self.superuser
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request, FleetOpsSettings.get_solo()))

    def test_settings_admin_rejects_retention_below_one_year(self):
        data = {
            "attendance_limit": "",
            "tracking_interval": 60,
            "stale_threshold": 180,
            "auto_end_enabled": "on",
            "auto_end_missing_count": 3,
            "incentive_minimum_fleets": 3,
            "data_retention_days": 100,
            "history_alliance_ids": "",
            "srp_auto_create": "on",
            "srp_provider": "auto",
        }
        url = self._url(FleetOpsSettings, "change", 1)

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FleetOpsSettings.get_solo().data_retention_days, 365)

        data["data_retention_days"] = 400
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(FleetOpsSettings.get_solo().data_retention_days, 400)
        entry = AuditLog.objects.get(action="configuration.update", object_type="FleetOpsSettings")
        self.assertEqual(entry.old_value["data_retention_days"], 365)
        self.assertEqual(entry.new_value["data_retention_days"], 400)
        self.assertEqual(entry.actor, self.superuser)

    def test_list_views_never_show_webhook_url(self):
        for model in self._fleetops_models():
            with self.subTest(model=model.__name__):
                response = self.client.get(self._url(model, "changelist"))
                content = response.content.decode()
                self.assertNotIn(WEBHOOK_SECRET, content)
                self.assertNotIn("sUp3r-s3cret", content)

    def test_webhook_url_only_shown_in_its_edit_form(self):
        content = self.client.get(self._url(DiscordWebhook, "change", self.webhook.pk)).content.decode()
        self.assertIn("sUp3r-s3cret", content)

        changelist = self.client.get(self._url(DiscordWebhook, "changelist")).content.decode()
        self.assertIn("Ops Pings", changelist)
        self.assertNotIn("sUp3r-s3cret", changelist)

    def test_ping_target_form_does_not_expose_webhook_url(self):
        for url in (self._url(PingTarget, "add"), self._url(PingTarget, "change", self.target.pk)):
            with self.subTest(url=url):
                content = self.client.get(url).content.decode()
                self.assertIn("Ops Pings", content)
                self.assertNotIn("sUp3r-s3cret", content)

    def test_webhook_delete_confirmation_does_not_expose_url(self):
        content = self.client.get(self._url(DiscordWebhook, "delete", self.webhook.pk)).content.decode()
        self.assertNotIn("sUp3r-s3cret", content)

    def test_webhook_admin_changes_are_audited_without_secret(self):
        new_secret = "https://discord.com/api/webhooks/987/another-hidden-token"
        response = self.client.post(
            self._url(DiscordWebhook, "add"),
            {"name": "New Hook", "webhook_url": new_secret, "is_active": "on"},
        )
        self.assertEqual(response.status_code, 302)
        hook = DiscordWebhook.objects.get(name="New Hook")

        response = self.client.post(
            self._url(DiscordWebhook, "change", hook.pk),
            {"name": "New Hook", "webhook_url": new_secret + "-rotated", "is_active": "on"},
        )
        self.assertEqual(response.status_code, 302)

        response = self.client.post(self._url(DiscordWebhook, "delete", hook.pk), {"post": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(DiscordWebhook.objects.filter(pk=hook.pk).exists())

        entries = AuditLog.objects.filter(object_type="DiscordWebhook", object_id=str(hook.pk))
        self.assertEqual(
            set(entries.values_list("action", flat=True)),
            {"configuration.create", "configuration.update", "configuration.delete"},
        )
        for entry in entries:
            with self.subTest(action=entry.action):
                self.assertEqual(entry.actor, self.superuser)
                self.assertNotIn("another-hidden-token", str(entry.old_value))
                self.assertNotIn("another-hidden-token", str(entry.new_value))
        create = entries.get(action="configuration.create")
        self.assertEqual(create.new_value["webhook_url"], "***redacted***")

        audit_list = self.client.get(self._url(AuditLog, "changelist")).content.decode()
        self.assertNotIn("another-hidden-token", audit_list)

    def test_new_default_template_audits_the_template_it_replaced(self):
        old_default = MessageTemplate.objects.get(name="Default Ping")

        response = self.client.post(
            self._url(MessageTemplate, "add"),
            {"name": "Admin Ping", "template_type": "ping", "content": "{{ fc }}", "is_default": "on", "is_active": "on"},
        )

        self.assertEqual(response.status_code, 302)
        old_default.refresh_from_db()
        self.assertFalse(old_default.is_default)
        entry = AuditLog.objects.get(
            action="configuration.update", object_type="MessageTemplate", object_id=str(old_default.pk)
        )
        self.assertEqual(entry.actor, self.superuser)
        self.assertEqual(entry.old_value, {**entry.new_value, "is_default": True})
        self.assertFalse(entry.new_value["is_default"])

    def test_admin_refuses_an_inactive_default_template(self):
        response = self.client.post(
            self._url(MessageTemplate, "add"),
            {"name": "Draft Ping", "template_type": "ping", "content": "{{ fc }}", "is_default": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A default template must be active.")
        self.assertFalse(MessageTemplate.objects.filter(name="Draft Ping").exists())
        self.assertTrue(MessageTemplate.objects.get(name="Default Ping").is_default)
        self.assertFalse(AuditLog.objects.filter(object_type="MessageTemplate").exists())

    def test_list_editable_change_is_audited(self):
        url = self._url(FleetType, "changelist")
        response = self.client.post(
            url,
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "1",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-id": str(self.fleet_type.pk),
                "form-0-point_weight": "2.50",
                "form-0-is_active": "on",
                "form-0-sort_order": "0",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.fleet_type.refresh_from_db()
        self.assertEqual(self.fleet_type.point_weight, Decimal("2.50"))

        entry = AuditLog.objects.get(action="configuration.update", object_type="FleetType")
        self.assertEqual(entry.old_value["point_weight"], "1.00")
        self.assertEqual(entry.new_value["point_weight"], "2.50")

    def _bulk_delete(self, model, objects):
        return self.client.post(
            self._url(model, "changelist"),
            {
                "action": "delete_selected",
                "_selected_action": [str(obj.pk) for obj in objects],
                "post": "yes",
            },
        )

    def test_bulk_delete_removes_configuration_rows(self):
        comms = CommsPreset.objects.create(name="Bulk Comms")

        response = self._bulk_delete(CommsPreset, [comms])

        self.assertEqual(response.status_code, 302)
        self.assertFalse(CommsPreset.objects.filter(pk=comms.pk).exists())

    def test_bulk_delete_of_configuration_is_audited(self):
        hook = DiscordWebhook.objects.create(name="Bulk Hook", webhook_url=WEBHOOK_SECRET)

        self._bulk_delete(DiscordWebhook, [hook])

        self.assertFalse(DiscordWebhook.objects.filter(pk=hook.pk).exists())
        self.assertTrue(
            AuditLog.objects.filter(
                action="configuration.delete", object_type="DiscordWebhook", object_id=str(hook.pk)
            ).exists()
        )

    def test_operation_search_handles_free_text(self):
        url = self._url(FleetOperation, "changelist")
        for query in ("not-a-number", str(self.operation.esi_fleet_id), str(self.operation.uuid)):
            with self.subTest(query=query):
                response = self.client.get(url, {"q": query})
                self.assertEqual(response.status_code, 200)
        response = self.client.get(url, {"q": str(self.operation.uuid)})
        self.assertContains(response, str(self.operation.uuid))

    def test_admin_requires_staff(self):
        self.client.force_login(f.create_user(perms=f.FC_LEAD_PERMS))
        response = self.client.get(self._url(DiscordWebhook, "changelist"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])
