# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Small fixes before 0.3.0b1: fleet list queries, a busy fleet refresh, the end time of a vanished fleet, a
replacement next to a merge, the pending multiplier list, PAP link registrations, multiplier tiers, the
configuration list, the admin, the ship check of payout rules and the name lookup of rosters."""

import time
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.admin.sites import site as admin_site
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from kombu.exceptions import OperationalError as QueueError

from fleetops import forms_config, tasks
from fleetops.constants import CLICK_SCOPES
from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    FleetMemberState,
    FleetOperation,
    IncentivePeriod,
    MonthlyFCStatistic,
    MultiplierTier,
    OperationRoleAssignment,
    PapRegistration,
    ReviewStatus,
    ShipFunctionRule,
)
from fleetops.providers.esi import FleetESIError
from fleetops.services import multipliers, pap_links, roster_import
from fleetops.services.attendance import create_manual_attendance
from fleetops.services.fleet_refresh import refresh_task_lock_key
from fleetops.services.incentives import pending_multiplier_operations
from fleetops.services.operations import end_fleet
from fleetops.services.pap import finalize_pap
from fleetops.services.sde import ShipTypeInfo
from fleetops.views_pap import pap_context

from . import factories as f
from .test_pap_links import PapLinkTestCase

Status = FleetOperation.Status


def queries_on(table, captured):
    return [q["sql"] for q in captured.captured_queries if f'"{table}"' in q["sql"] or f"`{table}`" in q["sql"]]


class FleetListQueryTests(TestCase):
    """L-18: the fleet list asks once which fleets of the page the user holds an FC credit in."""

    def setUp(self):
        self.fc = f.create_user("list_fc", perms=f.FC_PERMS)
        self.other = f.create_user("list_other", perms=f.FC_PERMS)
        self.client.force_login(self.fc)

    def role_queries(self, count):
        for _ in range(count):
            f.closed_operation(self.other)
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("fleetops:fleet_operations"))
        self.assertEqual(response.status_code, 200)
        return len(queries_on("fleetops_operationroleassignment", captured))

    def test_the_role_query_does_not_grow_with_the_page(self):
        few = self.role_queries(2)
        many = self.role_queries(10)

        self.assertEqual(few, many)
        self.assertLessEqual(many, 1)

    def test_the_role_query_has_no_limit_subquery(self):
        # MariaDB refuses LIMIT inside an IN subquery, so the page is turned into a list of ids first.
        f.closed_operation(self.other)
        with CaptureQueriesContext(connection) as captured:
            self.client.get(reverse("fleetops:fleet_operations"))
        for sql in queries_on("fleetops_operationroleassignment", captured):
            self.assertNotIn("LIMIT", sql.upper())

    def test_a_fleet_with_a_credited_role_is_still_editable(self):
        foreign = f.closed_operation(self.other)
        own_role = f.closed_operation(self.other)
        role = f.special_role(key="backseat_fc", grants_fc_credit=True)
        OperationRoleAssignment.objects.create(
            operation=own_role,
            role=role.key,
            special_role=role,
            character_id=f.main_of(self.fc).character_id,
            character_name="x",
            auth_user=self.fc,
            grants_fc_credit=True,
        )

        response = self.client.get(reverse("fleetops:fleet_operations"))

        shown = {operation.pk: operation.can_edit for operation in response.context["page"].object_list}
        self.assertEqual((shown[foreign.pk], shown[own_role.pk]), (False, True))


class BusyRefreshTests(TestCase):
    """L-19: a fleet read that is due finds another read running and tries again instead of being dropped."""

    def setUp(self):
        self.fc = f.create_user("busy_fc", perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc)
        cache.set(refresh_task_lock_key(self.operation.pk), "1", 30)
        self.addCleanup(cache.delete, refresh_task_lock_key(self.operation.pk))

    def test_a_due_read_is_queued_again_shortly(self):
        with mock.patch.object(tasks.refresh_fleet_structure, "apply_async") as queued:
            result = tasks.refresh_fleet_structure(self.operation.pk, not_before=time.time())

        self.assertEqual(result, "requeued")
        (call_args, call_kwargs), options = queued.call_args
        self.assertEqual(call_args, (self.operation.pk,))
        self.assertEqual(options["countdown"], tasks.REFRESH_BUSY_RETRY_SECONDS)
        self.assertEqual(call_kwargs["attempt"], 1)
        self.assertIn("not_before", call_kwargs)

    def test_the_retries_end(self):
        with mock.patch.object(tasks.refresh_fleet_structure, "apply_async") as queued:
            result = tasks.refresh_fleet_structure(
                self.operation.pk, not_before=time.time(), attempt=tasks.REFRESH_BUSY_RETRIES
            )

        self.assertEqual(result, "already-running")
        queued.assert_not_called()

    def test_a_read_without_a_due_time_is_not_queued_again(self):
        with mock.patch.object(tasks.refresh_fleet_structure, "apply_async") as queued:
            self.assertEqual(tasks.refresh_fleet_structure(self.operation.pk), "already-running")
        queued.assert_not_called()

    def test_without_a_queue_the_read_is_dropped_as_before(self):
        with mock.patch.object(tasks.refresh_fleet_structure, "apply_async", side_effect=QueueError("down")):
            with self.assertLogs(tasks.logger, "ERROR"):
                result = tasks.refresh_fleet_structure(self.operation.pk, not_before=time.time())

        self.assertEqual(result, "already-running")


class VanishedFleetEndTests(TestCase):
    """L-20: a fleet that disappeared ended when it was last read, not when the polls gave up."""

    def setUp(self):
        self.fc = f.create_user("gone_fc", perms=f.FC_PERMS)
        # Relative to now: the test does not depend on the time of day.
        self.now = timezone.now()
        self.operation = f.create_operation(
            self.fc,
            started_at=self.now - timedelta(hours=3),
            last_esi_update=self.now - timedelta(minutes=25),
        )

    def test_an_automatic_end_uses_the_last_successful_read(self):
        end_fleet(self.operation, automatic=True)

        self.operation.refresh_from_db()
        last_read = self.now - timedelta(minutes=25)
        self.assertEqual(self.operation.status, Status.CLOSED)
        self.assertEqual(self.operation.end_requested_at, last_read)
        self.assertEqual(self.operation.ended_at, last_read)

    def test_a_manual_end_still_uses_the_time_of_the_request(self):
        before = timezone.now()
        with mock.patch("fleetops.services.operations.sync_operation"):
            end_fleet(self.operation, actor=self.fc)

        self.operation.refresh_from_db()
        self.assertGreaterEqual(self.operation.end_requested_at, before)

    def test_a_fleet_that_was_never_read_ends_now(self):
        operation = f.create_operation(self.fc, started_at=self.now - timedelta(hours=1))
        before = timezone.now()

        end_fleet(operation, automatic=True)

        operation.refresh_from_db()
        self.assertGreaterEqual(operation.ended_at, before)


class ReplaceNextToMergeTests(TestCase):
    """L-21: a replacement does not count once a merge has brought the tracked PAP back."""

    def setUp(self):
        self.lead = f.create_user("rm_lead", perms=f.FC_LEAD_PERMS)
        self.pilot = f.create_user("rm_pilot")
        self.main = f.main_of(self.pilot)
        started = timezone.now() - timedelta(hours=5)
        self.operation = f.create_operation(
            self.lead, status=Status.CLOSED, started_at=started, ended_at=started + timedelta(hours=2),
            attendance_multiplier=2,
        )
        f.member_state(self.operation, self.main, accumulated_seconds=1200, active=False)
        finalize_pap(self.operation)

    def manual(self, action, value=1):
        return create_manual_attendance(
            self.operation, actor=self.lead, character_id=self.main.character_id,
            attendance_value=value, duplicate_action=action,
        )

    def total(self):
        return sum(
            row.attendance_value for row in AttendanceRecord.objects.filter(operation=self.operation, granted=True)
        )

    def test_a_replacement_alone_counts(self):
        replacement = self.manual("replace")

        replacement.refresh_from_db()
        self.assertTrue(replacement.granted)
        self.assertEqual(self.total(), 1)

    def test_a_merge_after_a_replacement_leaves_only_the_merged_pap(self):
        replacement = self.manual("replace")
        self.manual("merge")

        replacement.refresh_from_db()
        self.assertFalse(replacement.granted)
        # The tracked row comes back: 2x plus the merged 1.
        self.assertEqual(self.total(), 3)

    def test_the_order_does_not_matter(self):
        self.manual("merge")
        replacement = self.manual("replace")

        replacement.refresh_from_db()
        self.assertFalse(replacement.granted)
        self.assertEqual(self.total(), 3)

    def test_without_tracked_pap_both_still_count_as_separate_credits(self):
        # Nothing to replace and nothing to merge into: the rows are credits of their own, as before.
        other = f.create_user("rm_other")
        character = f.main_of(other)
        for action in ("replace", "merge"):
            create_manual_attendance(
                self.operation, actor=self.lead, character_id=character.character_id, duplicate_action=action
            )

        rows = AttendanceRecord.objects.filter(operation=self.operation, character_id=character.character_id)
        self.assertEqual([row.granted for row in rows.order_by("pk")], [True, True])


class PendingMultiplierListTests(TestCase):
    """L-22: the incentive page lists only the fleets whose multiplier can still change a result."""

    def setUp(self):
        self.fc = f.create_user("pend_fc", perms=f.FC_PERMS)
        now = timezone.now()
        self.period = IncentivePeriod(year=now.year, month=now.month)
        # Within the month whatever the day: the first of the month, noon.
        self.started = now.replace(day=1, hour=12, minute=0, second=0, microsecond=0)

    def fleet(self, **extra):
        return f.closed_operation(self.fc, started_at=self.started, multiplier_pending=True, **extra)

    def test_a_counted_fleet_is_listed(self):
        counted = self.fleet()
        self.assertEqual(list(pending_multiplier_operations(self.period)), [counted])

    def test_rejected_and_excluded_fleets_are_not(self):
        counted = self.fleet()
        self.fleet(review_status=ReviewStatus.REJECTED)
        self.fleet(counts_for_stats=False)
        self.fleet(stats_mode=FleetOperation.StatsMode.EXCLUDE, counts_for_stats=False)

        self.assertEqual(list(pending_multiplier_operations(self.period)), [counted])


class PapLinkBackgroundDetailsTests(PapLinkTestCase):
    """L-23: the registration is written first; location and ship follow in the background."""

    def test_the_registration_exists_before_ship_and_location_are_read(self):
        seen = []

        def location(token):
            seen.append(PapRegistration.objects.filter(operation=self.operation).count())
            return {"solar_system_id": 30000142}

        with mock.patch.object(pap_links, "character_location", side_effect=location):
            with self.captureOnCommitCallbacks(execute=True):
                pap_links.register_characters(self.link, self.pilot, [self.main.character_id])

        self.assertEqual(seen, [1])

    def test_the_details_are_filled_by_the_task(self):
        with mock.patch.object(tasks.fill_registration_details, "delay") as queued:
            with self.captureOnCommitCallbacks(execute=True):
                pap_links.register_characters(self.link, self.pilot, [self.main.character_id])

        registration = PapRegistration.objects.get(operation=self.operation)
        self.assertIsNone(registration.ship_type_id)
        queued.assert_called_once_with([registration.pk], self.pilot.pk)

        tasks.fill_registration_details(*queued.call_args.args)
        registration.refresh_from_db()
        self.assertEqual(registration.ship_type_name, "Guardian")
        self.assertEqual(registration.solar_system_name, "Jita")

    def test_an_unreachable_queue_does_not_undo_the_registration(self):
        with mock.patch.object(tasks.fill_registration_details, "delay", side_effect=QueueError("down")):
            with self.assertLogs(pap_links.logger, "ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    result = pap_links.register_characters(self.link, self.pilot, [self.main.character_id])

        self.assertEqual(len(result.registered), 1)
        self.assertEqual(PapRegistration.objects.filter(operation=self.operation).count(), 1)

    def test_the_task_is_queued_only_after_the_commit(self):
        with mock.patch.object(tasks.fill_registration_details, "delay") as queued:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                pap_links.register_characters(self.link, self.pilot, [self.main.character_id])
            queued.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
            queued.assert_called_once()

    def test_the_task_does_not_overwrite_details_that_are_there(self):
        registration = f.registration(self.operation, self.main, user=self.pilot, ship_type_id=1, ship_type_name="Kept")

        pap_links.fill_details([registration.pk], self.pilot)

        registration.refresh_from_db()
        self.assertEqual(registration.ship_type_name, "Kept")

    def test_the_task_ignores_registrations_of_other_users(self):
        other = f.create_user("details_other", perms=f.MEMBER_PERMS)
        registration = f.registration(self.operation, self.main, user=self.pilot)

        pap_links.fill_details([registration.pk], other)

        registration.refresh_from_db()
        self.assertIsNone(registration.ship_type_id)

    def test_characters_left_when_the_time_budget_is_used_up_can_be_sent_again(self):
        with mock.patch.object(pap_links, "REGISTRATION_BUDGET_SECONDS", -1):
            result = pap_links.register_characters(
                self.link, self.pilot, [self.main.character_id, self.alt.character_id]
            )

        self.assertEqual(result.registered, [])
        self.assertEqual({reason for _cid, _name, reason in result.rejected}, {pap_links.TOO_SLOW})
        self.assertFalse(PapRegistration.objects.exists())
        # Nothing was asked of ESI.
        self.assertEqual(self.esi.calls, [])


class MultiplierTierReadTests(TestCase):
    """L-24: the fleet page reads the multiplier tiers once."""

    def setUp(self):
        self.lead = f.create_user("tier_lead", perms=f.FC_LEAD_PERMS)
        self.operation = f.create_operation(self.lead)
        self.request = RequestFactory().get("/")
        self.request.user = self.lead

    def test_the_page_context_reads_the_tiers_once(self):
        with CaptureQueriesContext(connection) as captured:
            context = pap_context(self.request, self.operation)

        self.assertIn("end_prompt", context)
        self.assertEqual(len(queries_on("fleetops_multipliertier", captured)), 1)

    def test_the_tiers_are_read_again_on_the_next_call(self):
        pap_context(self.request, self.operation)
        MultiplierTier.objects.all().delete()

        self.assertEqual(multipliers.tiers(), [])
        with multipliers.tiers_read_once():
            self.assertEqual(multipliers.tiers(), [])
        MultiplierTier.objects.create(multiplier=2, threshold_minutes=10)
        self.assertEqual([tier.multiplier for tier in multipliers.tiers()], [2])

    def test_the_snapshot_is_dropped_when_the_block_ends(self):
        with multipliers.tiers_read_once():
            before = multipliers.tiers()
            MultiplierTier.objects.create(multiplier=9, threshold_minutes=1)
            self.assertEqual(multipliers.tiers(), before)
        self.assertIn(9, [tier.multiplier for tier in multipliers.tiers()])

    def test_without_the_block_every_call_reads_the_tiers(self):
        with CaptureQueriesContext(connection) as captured:
            multipliers.allowed_multipliers(timedelta(hours=1))
            multipliers.allowed_multipliers(timedelta(hours=1))

        self.assertEqual(len(queries_on("fleetops_multipliertier", captured)), 2)


class ShipFunctionRuleListTests(TestCase):
    """L-25: the list of ship function rules does not read the function once per rule."""

    def setUp(self):
        self.admin = f.create_user("rules_cfg", perms=f.MEMBER_PERMS + ["fleetops.manage_configuration"])
        self.client.force_login(self.admin)
        f.clear_ship_functions()
        self.function = f.ship_function("Logistics")
        self.next_target = 50_000

    def function_queries(self, count):
        for _ in range(count):
            self.next_target += 1
            ShipFunctionRule.objects.create(
                function=self.function, kind=ShipFunctionRule.Kind.TYPE, target_id=self.next_target
            )
        url = reverse("fleetops:configuration_list", args=["ship-function-rules"])
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url)
        self.assertContains(response, "Logistics")
        return len(queries_on("fleetops_shipfunction", captured))

    def test_the_function_queries_do_not_grow_with_the_rules(self):
        few = self.function_queries(2)
        many = self.function_queries(8)

        self.assertEqual(few, many)


class AdminReadOnlyTests(TestCase):
    """L-26: history and audit records cannot be added, changed or deleted in the Django admin."""

    HISTORY = (
        AuditLog, AttendanceRecord, OperationRoleAssignment, IncentivePeriod, MonthlyFCStatistic, FleetMemberState,
    )

    def setUp(self):
        self.superuser = f.create_user("adm_super")
        self.superuser.is_superuser = self.superuser.is_staff = True
        self.superuser.save()
        self.client.force_login(self.superuser)

    def test_no_add_change_or_delete_on_the_models_themselves(self):
        from fleetops.models import FleetMemberEvent, OperationAction, OperationActionAttempt

        request = RequestFactory().get("/")
        request.user = self.superuser
        for model in (*self.HISTORY, FleetMemberEvent, OperationAction, OperationActionAttempt):
            with self.subTest(model=model.__name__):
                model_admin = admin_site._registry[model]
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request))
                self.assertFalse(model_admin.has_delete_permission(request))

    def test_an_audit_entry_cannot_be_rewritten_or_deleted(self):
        entry = AuditLog.objects.create(actor=self.superuser, action="srp.pay", object_type="SrpClaim", object_id="1", reason="paid")

        change = self.client.post(
            reverse("admin:fleetops_auditlog_change", args=[entry.pk]), {"action": "x", "reason": "changed"}
        )
        delete = self.client.post(reverse("admin:fleetops_auditlog_delete", args=[entry.pk]), {"post": "yes"})
        listing = self.client.get(reverse("admin:fleetops_auditlog_changelist"))

        entry.refresh_from_db()
        self.assertEqual((entry.action, entry.reason), ("srp.pay", "paid"))
        self.assertEqual(change.status_code, 403)
        self.assertEqual(delete.status_code, 403)
        self.assertEqual(listing.status_code, 200)

    def test_a_pap_row_cannot_be_changed_or_deleted_on_its_own(self):
        operation = f.closed_operation(self.superuser)
        row = f.add_attendance(operation, self.superuser, granted=False)

        change = self.client.post(
            reverse("admin:fleetops_attendancerecord_change", args=[row.pk]), {"granted": "on", "attendance_value": 5}
        )
        delete = self.client.post(reverse("admin:fleetops_attendancerecord_delete", args=[row.pk]), {"post": "yes"})

        row.refresh_from_db()
        self.assertEqual((row.granted, row.attendance_value), (False, 1))
        self.assertEqual((change.status_code, delete.status_code), (403, 403))

    def test_deleting_a_fleet_still_removes_its_rows(self):
        operation = f.closed_operation(self.superuser)
        row = f.add_attendance(operation, self.superuser)

        response = self.client.post(reverse("admin:fleetops_fleetoperation_delete", args=[operation.pk]), {"post": "yes"})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(FleetOperation.objects.filter(pk=operation.pk).exists())
        self.assertFalse(AttendanceRecord.objects.filter(pk=row.pk).exists())


class AdminCascadePermissionTests(TestCase):
    """L-26: deleting a fleet in the admin still needs the delete permission of the rows that go with it."""

    def staff(self, *codenames):
        user = f.create_user("adm_staff")
        user.is_staff = True
        user.save()
        from django.contrib.auth.models import Permission

        user.user_permissions.add(*Permission.objects.filter(codename__in=codenames, content_type__app_label="fleetops"))
        return user

    def delete_fleet(self, user):
        operation = f.closed_operation(user)
        row = f.add_attendance(operation, user)
        self.client.force_login(user)
        response = self.client.post(reverse("admin:fleetops_fleetoperation_delete", args=[operation.pk]), {"post": "yes"})
        return response, operation, row

    def test_only_the_fleet_permission_is_not_enough(self):
        response, operation, row = self.delete_fleet(self.staff("view_fleetoperation", "delete_fleetoperation"))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(FleetOperation.objects.filter(pk=operation.pk).exists())
        self.assertTrue(AttendanceRecord.objects.filter(pk=row.pk).exists())

    def test_with_the_permissions_of_the_rows_the_fleet_can_be_deleted(self):
        user = self.staff(
            "view_fleetoperation", "delete_fleetoperation", "delete_attendancerecord", "delete_fleetmemberstate",
            "delete_fleetmemberevent", "delete_operationaction", "delete_operationactionattempt",
            "delete_operationroleassignment", "delete_papregistration",
        )
        response, operation, _row = self.delete_fleet(user)

        self.assertEqual(response.status_code, 302)
        self.assertFalse(FleetOperation.objects.filter(pk=operation.pk).exists())


class ShipCheckTests(TestCase):
    """L-27: a payout rule names a ship, not any item type."""

    def info(self, category_id):
        return {587: ShipTypeInfo(587, "Rifter", 25, "Frigate", category_id, None)}

    def test_the_sde_tells_a_ship_from_another_item(self):
        with mock.patch.object(forms_config.sde_service, "ship_type_details", return_value=self.info(6)):
            self.assertEqual(forms_config.ship_check(587), "")
        with mock.patch.object(forms_config.sde_service, "ship_type_details", return_value=self.info(4)):
            with self.assertRaises(ValidationError) as caught:
                forms_config.ship_check(587)
        self.assertEqual(caught.exception.messages, [forms_config.NOT_A_SHIP])

    def test_esi_is_asked_when_the_sde_does_not_know_the_type(self):
        with mock.patch.object(forms_config.sde_service, "ship_type_details", return_value={}), mock.patch.object(
            forms_config.esi_universe, "get_type", return_value={"group_id": 25}
        ), mock.patch.object(forms_config.esi_universe, "get_group", return_value={"category_id": 6}) as group:
            self.assertEqual(forms_config.ship_check(587), "")
        group.assert_called_once_with(25)

    def test_esi_refuses_an_item_that_is_no_ship(self):
        with mock.patch.object(forms_config.sde_service, "ship_type_details", return_value={}), mock.patch.object(
            forms_config.esi_universe, "get_type", return_value={"group_id": 18}
        ), mock.patch.object(forms_config.esi_universe, "get_group", return_value={"category_id": 4}):
            with self.assertRaises(ValidationError):
                forms_config.ship_check(34)

    def test_when_nobody_can_tell_the_rule_is_kept_with_a_warning(self):
        for failure in (FleetESIError("ESI_ERROR", "down"), None):
            with self.subTest(failure=failure):
                answer = {"group_id": None} if failure is None else {}
                with mock.patch.object(
                    forms_config.sde_service, "ship_type_details", return_value={}
                ), mock.patch.object(
                    forms_config.esi_universe, "get_type", side_effect=failure, return_value=answer
                ):
                    self.assertEqual(forms_config.ship_check(587), forms_config.SHIP_UNCHECKED)

    def test_an_id_of_another_item_is_refused_by_the_lookup(self):
        with mock.patch.object(forms_config.sde_service, "item_type_names", return_value={34: "Tritanium"}), mock.patch.object(
            forms_config.sde_service, "ship_type_details", return_value={34: ShipTypeInfo(34, "Tritanium", 18, "Mineral", 4, None)}
        ):
            with self.assertRaises(ValidationError) as caught:
                forms_config.lookup_ship("34")
        self.assertEqual(caught.exception.messages, [forms_config.NOT_A_SHIP])

    def test_an_id_of_a_ship_passes_the_lookup(self):
        with mock.patch.object(forms_config.sde_service, "item_type_names", return_value={587: "Rifter"}), mock.patch.object(
            forms_config.sde_service, "ship_type_details", return_value=self.info(6)
        ):
            self.assertEqual(forms_config.lookup_ship("587"), forms_config.ShipLookup(587, "Rifter"))

    def test_a_name_found_at_esi_is_checked_too(self):
        with mock.patch.object(forms_config.sde_service, "sde_installed", return_value=False), mock.patch.object(
            forms_config, "_esi_type_by_name", return_value=(34, "Tritanium")
        ), mock.patch.object(forms_config, "ship_check", side_effect=ValidationError(forms_config.NOT_A_SHIP)):
            with self.assertRaises(ValidationError):
                forms_config.lookup_ship("Tritanium")


class RosterNameLookupTests(TestCase):
    """L-28: names are looked up by their exact spelling first, which can use the index of the name column."""

    def setUp(self):
        self.user = f.create_user("roster_pilot", main_name="Exact Pilot")

    def lookup(self, names):
        with CaptureQueriesContext(connection) as captured:
            with mock.patch.object(roster_import.esi_provider, "universe_ids", return_value={}):
                result = roster_import.resolve_names(names)
        return result, [q["sql"] for q in captured.captured_queries if "eveonline_evecharacter" in q["sql"]]

    def test_an_exact_name_needs_no_lower_function_on_the_column(self):
        result, sql = self.lookup(["Exact Pilot"])

        self.assertEqual(result["exact pilot"].status, roster_import.AUTH)
        self.assertFalse(any("LOWER(" in query.upper() for query in sql))

    def test_another_spelling_is_still_found_ignoring_case(self):
        result, _sql = self.lookup(["eXACT pILOT"])

        self.assertEqual(result["exact pilot"].status, roster_import.AUTH)
        self.assertEqual(result["exact pilot"].name, "Exact Pilot")

    def test_only_the_names_not_found_are_looked_up_ignoring_case(self):
        result, sql = self.lookup(["Exact Pilot", "Nobody Here"])

        self.assertEqual(result["exact pilot"].status, roster_import.AUTH)
        self.assertEqual(result["nobody here"].status, roster_import.NOT_FOUND)
        lowered = [query for query in sql if "LOWER(" in query.upper()]
        self.assertEqual(len(lowered), 1)
        self.assertNotIn("exact pilot", lowered[0].lower().split("where")[-1])
