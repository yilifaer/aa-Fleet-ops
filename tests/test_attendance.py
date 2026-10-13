"""Tests for manual PAP corrections, the PAP multiplier of closed fleets, identity resolution and history retention."""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.messages import get_messages
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.forms import FleetOpsSettingsForm
from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    FleetMemberEvent,
    FleetOperation,
    FleetOpsSettings,
    ReviewStatus,
)
from fleetops.services.attendance import create_manual_attendance, set_operation_attendance_multiplier
from fleetops.services.history import (
    apply_current_membership_filter,
    configured_alliance_ids,
    current_member_user_ids,
    prune_history,
)
from fleetops.services.identity import (
    corporation_main_count,
    get_owned_character,
    identity_for_character_id,
    owned_characters,
    resolve_many,
)
from fleetops.services.operations import end_fleet
from fleetops.services.pap import finalize_pap
from fleetops.services.statistics import attendance_history_queryset
from fleetops.tasks import prune_attendance_history

from . import factories as f

AUTOMATIC = AttendanceRecord.Source.AUTOMATIC
MANUAL = AttendanceRecord.Source.MANUAL
IMPORTED = AttendanceRecord.Source.IMPORTED
CLOSED = FleetOperation.Status.CLOSED


def long_closed_fleet(fc_user, minutes=200, **extra):
    """A closed fleet that ran long enough for every seeded multiplier (90 -> 2x, 180 -> 3x)."""
    started = timezone.now() - timedelta(hours=6)
    return f.create_operation(
        fc_user, status=CLOSED, started_at=started, ended_at=started + timedelta(minutes=minutes), **extra
    )


def tracked_row(operation, character):
    return AttendanceRecord.objects.get(
        operation=operation, character_id=character.character_id, source__in=AttendanceRecord.MANAGED_SOURCES
    )


def audit_actions(obj=None):
    qs = AuditLog.objects.all()
    if obj is not None:
        qs = qs.filter(object_type=obj.__class__.__name__, object_id=str(obj.pk))
    return list(qs.order_by("pk").values_list("action", flat=True))


class ManualAttendanceTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.lead = f.create_user(perms=f.FC_LEAD_PERMS)
        self.closed = f.create_operation(self.fc, status=CLOSED)
        self.active = f.create_operation(self.fc)
        self.pilot = f.create_user()
        self.main = f.main_of(self.pilot)

    def _manual(self, operation, character_id, **kwargs):
        kwargs.setdefault("actor", self.lead)
        return create_manual_attendance(operation, character_id=character_id, **kwargs)

    def _tracked(self, character, operation=None, seconds=1200):
        """Track ``character`` in the closed fleet and grant its PAP."""
        operation = operation or self.closed
        f.member_state(operation, character, accumulated_seconds=seconds, active=False)
        finalize_pap(operation)
        return tracked_row(operation, character)

    def test_fc_lead_entry_is_approved_and_granted(self):
        record = self._manual(self.closed, self.main.character_id, notes="Late login")

        self.assertEqual(record.source, MANUAL)
        self.assertEqual(record.correction, AttendanceRecord.Correction.KEEP)
        self.assertTrue(record.granted)
        self.assertFalse(record.capped)
        self.assertEqual(record.attendance_value, 1)
        self.assertEqual(record.auth_user, self.pilot)
        self.assertEqual(record.created_by, self.lead)
        self.assertEqual(record.character_name, self.main.character_name)
        self.assertEqual(record.notes, "Late login")
        self.assertEqual(record.first_seen, self.closed.started_at)
        self.assertEqual(record.last_seen, self.closed.ended_at)
        self.assertEqual((record.review_status, record.reviewed_by), (ReviewStatus.APPROVED, self.lead))
        self.assertIsNotNone(record.reviewed_at)

    def test_fc_entry_waits_for_fc_lead_approval(self):
        record = self._manual(self.closed, self.main.character_id, actor=self.fc)

        self.assertEqual(record.review_status, ReviewStatus.PENDING)
        self.assertIsNone(record.reviewed_by)
        self.assertFalse(record.granted)

    def test_manual_row_on_active_fleet_uses_start_time_for_last_seen(self):
        record = self._manual(self.active, self.main.character_id)
        self.assertEqual(record.last_seen, self.active.started_at)

    def test_manual_create_is_audited(self):
        record = self._manual(self.closed, self.main.character_id, attendance_value=2, actor=self.fc)

        entry = AuditLog.objects.get(action="attendance.manual_create")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.object_type, "AttendanceRecord")
        self.assertEqual(entry.object_id, str(record.pk))
        self.assertIsNone(entry.old_value)
        self.assertEqual(entry.new_value["attendance_value"], 2)
        self.assertEqual(entry.new_value["operation"], str(self.closed.uuid))
        self.assertEqual(entry.new_value["correction"], "keep")
        self.assertEqual(entry.new_value["review_status"], ReviewStatus.PENDING)

    def test_keep_with_tracked_pap_leaves_both_rows_granted(self):
        tracked = self._tracked(self.main)

        manual = self._manual(self.closed, self.main.character_id, duplicate_action="keep")

        tracked.refresh_from_db()
        self.assertTrue(tracked.granted)
        self.assertEqual(tracked.attendance_value, 1)
        self.assertTrue(manual.granted)
        self.assertEqual(AttendanceRecord.objects.filter(operation=self.closed).count(), 2)

    def test_closed_fleet_accepts_multiple_manual_rows(self):
        for value in (1, 2, 3):
            self._manual(self.closed, self.main.character_id, attendance_value=value)

        rows = AttendanceRecord.objects.filter(operation=self.closed, source=MANUAL, auth_user=self.pilot)
        self.assertEqual(rows.count(), 3)
        self.assertEqual(sum(r.attendance_value for r in rows), 6)
        self.assertEqual(audit_actions().count("attendance.manual_create"), 3)

    def test_merge_adds_value_to_the_tracked_pap(self):
        tracked = self._tracked(self.main)

        manual = self._manual(
            self.closed, self.main.character_id, attendance_value=2, duplicate_action="merge", notes="Logi bonus"
        )

        tracked.refresh_from_db()
        self.assertEqual((tracked.attendance_value, tracked.extra_value, tracked.granted), (3, 2, True))
        # The manual row stays as the record of the correction; its value counts on the tracked row.
        self.assertEqual((manual.source, manual.correction, manual.granted), (MANUAL, "merge", False))
        self.assertEqual(audit_actions(), ["attendance.manual_create"])

    def test_merge_into_capped_row_grants_it(self):
        alt = f.add_alt(self.pilot)
        self.closed.pap_character_limit = 1
        self.closed.save()
        f.member_state(self.closed, self.main, accumulated_seconds=1200, active=False)
        capped = self._tracked(alt)
        self.assertEqual((capped.granted, capped.capped), (False, True))

        self._manual(self.closed, alt.character_id, duplicate_action="merge")

        capped.refresh_from_db()
        self.assertTrue(capped.granted)
        self.assertEqual(capped.attendance_value, 2)
        self.assertTrue(tracked_row(self.closed, self.main).granted)

    def test_merge_without_tracked_pap_is_a_separate_credit(self):
        record = self._manual(self.closed, self.main.character_id, duplicate_action="merge")

        self.assertEqual(record.source, MANUAL)
        self.assertTrue(record.granted)
        self.assertEqual(audit_actions(), ["attendance.manual_create"])

    def test_merge_targets_the_matching_character_only(self):
        alt = f.add_alt(self.pilot)
        main_row = self._tracked(self.main)

        record = self._manual(self.closed, alt.character_id, duplicate_action="merge")

        main_row.refresh_from_db()
        self.assertEqual((main_row.attendance_value, main_row.extra_value), (1, 0))
        self.assertTrue(record.granted)
        self.assertEqual(record.character_id, alt.character_id)

    def test_replace_ungrants_the_tracked_pap_and_grants_the_manual_row(self):
        tracked = self._tracked(self.main)

        manual = self._manual(self.closed, self.main.character_id, attendance_value=2, duplicate_action="replace")

        tracked.refresh_from_db()
        self.assertEqual((tracked.granted, tracked.replaced), (False, True))
        self.assertEqual((manual.source, manual.granted, manual.attendance_value), (MANUAL, True, 2))
        granted = AttendanceRecord.objects.filter(operation=self.closed, auth_user=self.pilot, granted=True)
        self.assertEqual([r.pk for r in granted], [manual.pk])
        self.assertEqual(audit_actions(tracked), [])
        self.assertEqual(audit_actions(manual), ["attendance.manual_create"])

    def test_replace_capped_row_gives_manual_credit(self):
        alt = f.add_alt(self.pilot)
        self.closed.pap_character_limit = 1
        self.closed.save()
        f.member_state(self.closed, self.main, accumulated_seconds=1200, active=False)
        self._tracked(alt)

        manual = self._manual(self.closed, alt.character_id, duplicate_action="replace")

        self.assertTrue(manual.granted)
        granted = AttendanceRecord.objects.filter(operation=self.closed, auth_user=self.pilot, granted=True)
        self.assertEqual(granted.count(), 2)
        # The replaced alt still holds its place under the limit; the main keeps its PAP.
        self.assertTrue(tracked_row(self.closed, self.main).granted)

    def test_replace_without_tracked_pap_creates_manual_row(self):
        record = self._manual(self.closed, self.main.character_id, duplicate_action="replace")

        self.assertEqual(record.source, MANUAL)
        self.assertTrue(record.granted)
        self.assertEqual(audit_actions(), ["attendance.manual_create"])

    def test_manual_alt_in_foreign_alliance_credited_to_main_corp(self):
        alt = f.add_alt(self.pilot, "Far Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)

        record = self._manual(self.closed, alt.character_id)

        self.assertEqual(record.auth_user, self.pilot)
        self.assertEqual(record.character_name, "Far Alt")
        self.assertEqual(record.main_character_id, self.main.character_id)
        self.assertEqual(record.corporation_id, f.DEFAULT_CORP[0])

    def test_manual_for_unknown_character_keeps_given_name(self):
        unknown_id = f.next_id()

        record = self._manual(self.closed, unknown_id, character_name="Mystery Pilot")

        self.assertIsNone(record.auth_user)
        self.assertIsNone(record.main_character_id)
        self.assertEqual(record.character_name, "Mystery Pilot")

    def test_deleting_a_correction_and_recalculating_restores_the_tracked_pap(self):
        tracked = self._tracked(self.main)
        for action in ("merge", "replace"):
            with self.subTest(action=action):
                manual = self._manual(self.closed, self.main.character_id, duplicate_action=action, attendance_value=2)
                manual.delete()

                finalize_pap(self.closed)

                tracked.refresh_from_db()
                self.assertEqual(
                    (tracked.granted, tracked.replaced, tracked.extra_value, tracked.attendance_value),
                    (True, False, 0, 1),
                )

    def test_pending_correction_changes_nothing_until_approved(self):
        tracked = self._tracked(self.main)

        manual = self._manual(self.closed, self.main.character_id, actor=self.fc, duplicate_action="replace")

        tracked.refresh_from_db()
        self.assertEqual((tracked.granted, tracked.replaced), (True, False))
        self.assertFalse(manual.granted)

        AttendanceRecord.objects.filter(pk=manual.pk).update(review_status=ReviewStatus.APPROVED)
        finalize_pap(self.closed)

        tracked.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual((tracked.granted, tracked.replaced, manual.granted), (False, True, True))

    def test_rejected_correction_never_counts(self):
        tracked = self._tracked(self.main)
        manual = self._manual(self.closed, self.main.character_id, actor=self.fc, duplicate_action="merge")
        AttendanceRecord.objects.filter(pk=manual.pk).update(review_status=ReviewStatus.REJECTED)

        finalize_pap(self.closed)

        tracked.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual((tracked.extra_value, tracked.attendance_value, manual.granted), (0, 1, False))

    def test_corrections_on_a_running_fleet_apply_when_it_closes(self):
        f.member_state(self.active, self.main, accumulated_seconds=1200)
        merge = self._manual(self.active, self.main.character_id, duplicate_action="merge", attendance_value=2)
        # Until the close there is no tracked PAP to merge into.
        self.assertTrue(merge.granted)
        self.assertFalse(AttendanceRecord.objects.filter(operation=self.active, source=AUTOMATIC).exists())

        with mock.patch("fleetops.services.operations.sync_operation"):
            end_fleet(self.active, actor=self.fc)

        merge.refresh_from_db()
        tracked = tracked_row(self.active, self.main)
        self.assertFalse(merge.granted)
        self.assertEqual((tracked.granted, tracked.attendance_value), (True, 3))

    def test_imported_rows_are_corrections_too(self):
        record = self._manual(self.closed, self.main.character_id, source=IMPORTED)

        self.assertEqual((record.source, record.granted), (IMPORTED, True))

    def test_only_manual_and_imported_rows_are_entered_by_hand(self):
        for source in (AUTOMATIC, AttendanceRecord.Source.CLICK):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self._manual(self.closed, self.main.character_id, source=source)
        with self.assertRaises(ValueError):
            self._manual(self.closed, self.main.character_id, duplicate_action="add")
        self.assertFalse(AttendanceRecord.objects.exists())


class ManualAttendanceViewTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.pilot = f.create_user()
        self.client.force_login(self.fc)

    def _post(self, operation, **data):
        payload = {
            "character_id": f.main_of(self.pilot).character_id,
            "character_name": "",
            "attendance_value": 1,
            "duplicate_action": "keep",
            "notes": "",
        }
        payload.update(data)
        return self.client.post(reverse("fleetops:add_manual_attendance", args=[operation.uuid]), payload)

    def test_add_to_closed_fleet(self):
        operation = f.create_operation(self.fc, status=CLOSED)

        response = self._post(operation, attendance_value=2)

        self.assertEqual(response.status_code, 302)
        record = AttendanceRecord.objects.get(operation=operation)
        self.assertEqual(record.source, MANUAL)
        self.assertEqual(record.attendance_value, 2)
        # An FC's correction waits for an FC lead.
        self.assertEqual((record.review_status, record.granted), (ReviewStatus.PENDING, False))
        self.assertTrue(AuditLog.objects.filter(action="attendance.manual_create", actor=self.fc).exists())

    def test_add_to_active_fleet(self):
        operation = f.create_operation(self.fc)

        self._post(operation)

        self.assertTrue(AttendanceRecord.objects.filter(operation=operation, source=MANUAL).exists())

    def test_delete_manual_row_is_audited(self):
        operation = f.create_operation(self.fc, status=CLOSED)
        # FCs delete their own rows until an FC lead approved them.
        record = f.add_attendance(operation, self.pilot, source=MANUAL, value=2, created_by=self.fc, review_status="pending")

        response = self.client.post(reverse("fleetops:delete_manual_attendance", args=[record.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(AttendanceRecord.objects.filter(pk=record.pk).exists())
        entry = AuditLog.objects.get(action="attendance.manual_delete")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.object_id, str(record.pk))
        self.assertEqual(entry.old_value, {"attendance_value": 2})

    def test_automatic_rows_cannot_be_deleted_through_manual_delete(self):
        operation = f.create_operation(self.fc, status=CLOSED)
        record = f.add_attendance(operation, self.pilot)

        response = self.client.post(reverse("fleetops:delete_manual_attendance", args=[record.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(AttendanceRecord.objects.filter(pk=record.pk).exists())

    def test_other_fc_cannot_add_attendance_to_foreign_fleet(self):
        operation = f.create_operation(f.create_user(perms=f.FC_PERMS), status=CLOSED)

        response = self._post(operation)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(AttendanceRecord.objects.filter(operation=operation).exists())

    def test_historical_form_does_not_offer_draft_or_cancelled_fleets(self):
        draft = f.create_operation(self.fc, status=FleetOperation.Status.DRAFT)

        response = self.client.post(
            reverse("fleetops:manual_attendance"),
            {
                "operation": draft.pk,
                "character_id": f.main_of(self.pilot).character_id,
                "attendance_value": 1,
                "duplicate_action": "keep",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_invalid_entry_flashes_the_field_errors(self):
        operation = f.create_operation(self.fc, status=CLOSED)

        response = self._post(operation, attendance_value=101)

        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False
        )
        flashed = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertEqual(
            flashed, ["Invalid manual attendance entry. Ensure this value is less than or equal to 100."]
        )
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_historical_form_shows_field_errors(self):
        operation = f.create_operation(self.fc, status=CLOSED)

        response = self.client.post(
            reverse("fleetops:manual_attendance"),
            {
                "operation": operation.pk,
                "character_id": "0",
                "attendance_value": "101",
                "duplicate_action": "keep",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ensure this value is greater than or equal to 1.")
        self.assertContains(response, "Ensure this value is less than or equal to 100.")
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_cancelled_fleet_rejects_manual_attendance(self):
        operation = f.create_operation(self.fc, status=FleetOperation.Status.CANCELLED)

        self._post(operation)

        self.assertFalse(AttendanceRecord.objects.filter(operation=operation).exists())

    def test_draft_fleet_rejects_manual_attendance(self):
        operation = f.create_operation(self.fc, status=FleetOperation.Status.DRAFT)

        self._post(operation)

        self.assertFalse(AttendanceRecord.objects.filter(operation=operation).exists())


class AttendanceMultiplierTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.lead = f.create_user(perms=f.FC_LEAD_PERMS)
        self.operation = long_closed_fleet(self.fc)
        self.pilot = f.create_user()
        self.main = f.main_of(self.pilot)

    def track(self, character, operation=None, seconds=1200):
        operation = operation or self.operation
        f.member_state(operation, character, accumulated_seconds=seconds, active=False)

    def finalize(self, operation=None):
        return finalize_pap(operation or self.operation)

    def correction(self, action, value=1, character=None):
        return create_manual_attendance(
            self.operation,
            actor=self.lead,
            character_id=(character or self.main).character_id,
            attendance_value=value,
            duplicate_action=action,
        )

    def test_only_granted_tracked_rows_carry_the_multiplier_and_extras(self):
        alt = f.add_alt(self.pilot)
        other = f.create_user()
        self.operation.pap_character_limit = 1
        self.operation.save()
        self.track(self.main)
        self.track(alt)
        manual = self.correction("keep", character=f.main_of(other))
        self.finalize()

        summary = set_operation_attendance_multiplier(self.operation, 3, actor=self.fc)

        self.assertEqual(summary["granted"], 1)
        main_row = tracked_row(self.operation, self.main)
        self.assertEqual((main_row.attendance_value, main_row.granted), (3, True))
        self.assertFalse(tracked_row(self.operation, alt).granted)
        manual.refresh_from_db()
        self.assertEqual(manual.attendance_value, 1)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.attendance_multiplier, 3)

    def test_replaced_tracked_row_stays_ungranted_after_a_switch(self):
        self.track(self.main)
        self.finalize()
        self.correction("replace")

        set_operation_attendance_multiplier(self.operation, 2, actor=self.fc)

        tracked = tracked_row(self.operation, self.main)
        self.assertEqual((tracked.granted, tracked.replaced), (False, True))
        manual = AttendanceRecord.objects.get(operation=self.operation, source=MANUAL)
        self.assertEqual((manual.attendance_value, manual.granted), (1, True))

    def test_rows_of_other_operations_are_untouched(self):
        other_operation = long_closed_fleet(self.fc)
        self.track(self.main, other_operation)
        self.finalize(other_operation)
        self.track(self.main)
        self.finalize()

        set_operation_attendance_multiplier(self.operation, 2)

        self.assertEqual(tracked_row(other_operation, self.main).attendance_value, 1)
        self.assertEqual(tracked_row(self.operation, self.main).attendance_value, 2)

    def test_merged_extra_value_survives_multiplier_switches(self):
        self.track(self.main)
        self.finalize()
        self.correction("merge", value=2)
        self.assertEqual(tracked_row(self.operation, self.main).attendance_value, 3)

        expected = {2: 4, 3: 5, 1: 3}
        for multiplier, value in expected.items():
            set_operation_attendance_multiplier(self.operation, multiplier)
            self.assertEqual(tracked_row(self.operation, self.main).attendance_value, value, f"after {multiplier}x")

    def test_merge_into_replaced_row_keeps_extra_after_switch(self):
        self.track(self.main)
        self.finalize()
        self.correction("replace")
        set_operation_attendance_multiplier(self.operation, 2)
        self.correction("merge", value=1)

        set_operation_attendance_multiplier(self.operation, 3)

        tracked = tracked_row(self.operation, self.main)
        # 3x base plus the 1 merged by hand; a merge grants the tracked PAP again, as merges always did.
        self.assertEqual(tracked.attendance_value, 4)
        self.assertTrue(tracked.granted)

    def test_switching_back_and_forth_is_reversible(self):
        users = [f.create_user() for _ in range(3)]
        for user in users:
            self.track(f.main_of(user))
        self.finalize()

        set_operation_attendance_multiplier(self.operation, 3)
        set_operation_attendance_multiplier(self.operation, 1)

        for user in users:
            self.assertEqual(tracked_row(self.operation, f.main_of(user)).attendance_value, 1)

    def test_running_fleet_only_stores_the_multiplier(self):
        running = f.create_operation(self.fc)
        self.track(self.main, running)

        self.assertIsNone(set_operation_attendance_multiplier(running, 2))

        running.refresh_from_db()
        self.assertEqual(running.attendance_multiplier, 2)
        self.assertFalse(AttendanceRecord.objects.filter(operation=running).exists())

    def test_string_value_is_accepted(self):
        set_operation_attendance_multiplier(self.operation, "2")
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.attendance_multiplier, 2)

    def test_invalid_values_raise_and_change_nothing(self):
        self.track(self.main)
        self.finalize()
        for bad in (0, 11, -1, "x", "", None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    set_operation_attendance_multiplier(self.operation, bad, actor=self.fc)
        with self.assertRaises(ValueError):
            set_operation_attendance_multiplier(self.operation, 2, source="guess")
        self.operation.refresh_from_db()
        self.assertEqual(tracked_row(self.operation, self.main).attendance_value, 1)
        self.assertEqual(self.operation.attendance_multiplier, 1)
        self.assertFalse(AuditLog.objects.filter(action="attendance.multiplier").exists())

    def test_audit_written_when_the_value_changes(self):
        self.track(self.main)
        self.track(f.main_of(f.create_user()))
        self.finalize()

        set_operation_attendance_multiplier(self.operation, 1, actor=self.fc)
        self.assertFalse(AuditLog.objects.filter(action="attendance.multiplier").exists())

        set_operation_attendance_multiplier(self.operation, 2, actor=self.fc, reason="Structure bash")
        entry = AuditLog.objects.get(action="attendance.multiplier")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.object_type, "FleetOperation")
        self.assertEqual(entry.object_id, str(self.operation.pk))
        self.assertEqual(entry.old_value["attendance_multiplier"], 1)
        self.assertEqual(entry.new_value["attendance_multiplier"], 2)
        self.assertEqual(entry.new_value["multiplier_source"], "fc")
        self.assertEqual(entry.new_value["pap"]["granted"], 2)
        self.assertEqual(entry.reason, "Structure bash")

        set_operation_attendance_multiplier(self.operation, 2, actor=self.fc)
        self.assertEqual(AuditLog.objects.filter(action="attendance.multiplier").count(), 1)

    def test_keeping_1x_clears_a_pending_choice_and_is_audited(self):
        self.operation.multiplier_pending = True
        self.operation.multiplier_source = FleetOperation.MultiplierSource.DEFAULT
        self.operation.save()

        set_operation_attendance_multiplier(self.operation, 1, actor=self.fc)

        self.operation.refresh_from_db()
        self.assertFalse(self.operation.multiplier_pending)
        self.assertEqual(self.operation.multiplier_source, "fc")
        entry = AuditLog.objects.get(action="attendance.multiplier")
        self.assertTrue(entry.new_value["pending_cleared"])

    def test_view_applies_an_allowed_multiplier(self):
        self.track(self.main)
        self.finalize()
        self.client.force_login(self.fc)

        response = self.client.post(
            reverse("fleetops:set_attendance_multiplier", args=[self.operation.uuid]),
            {"attendance_multiplier": "3"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(tracked_row(self.operation, self.main).attendance_value, 3)
        self.assertTrue(AuditLog.objects.filter(action="attendance.multiplier", actor=self.fc).exists())
        self.assertIn(
            "PAP multiplier set to 3x; PAP recalculated (1 granted).",
            [str(m) for m in get_messages(response.wsgi_request)],
        )

    def test_view_rejects_an_unknown_multiplier(self):
        self.track(self.main)
        self.finalize()
        self.client.force_login(self.fc)

        response = self.client.post(
            reverse("fleetops:set_attendance_multiplier", args=[self.operation.uuid]),
            {"attendance_multiplier": "5"},
        )

        self.assertEqual(response.status_code, 302)
        self.operation.refresh_from_db()
        self.assertEqual(tracked_row(self.operation, self.main).attendance_value, 1)
        self.assertEqual(self.operation.attendance_multiplier, 1)


class IdentityTests(TestCase):
    def setUp(self):
        self.user = f.create_user(main_name="Home Main")
        self.main = f.main_of(self.user)

    def test_main_character(self):
        identity = identity_for_character_id(self.main.character_id)

        self.assertEqual(identity.user, self.user)
        self.assertEqual(identity.character_name, "Home Main")
        self.assertEqual(identity.main_character_id, self.main.character_id)
        self.assertEqual(identity.main_character_name, "Home Main")
        self.assertEqual(identity.corporation_id, f.DEFAULT_CORP[0])
        self.assertEqual(identity.alliance_id, f.DEFAULT_ALLIANCE[0])

    def test_alt_in_foreign_alliance_resolves_to_main_affiliation(self):
        alt = f.add_alt(self.user, "Away Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)

        identity = identity_for_character_id(alt.character_id)

        self.assertEqual(identity.character_id, alt.character_id)
        self.assertEqual(identity.character_name, "Away Alt")
        self.assertEqual(identity.user, self.user)
        self.assertEqual(identity.main_character_id, self.main.character_id)
        self.assertEqual(identity.corporation_id, f.DEFAULT_CORP[0])
        self.assertEqual(identity.corporation_name, f.DEFAULT_CORP[1])
        self.assertEqual(identity.alliance_id, f.DEFAULT_ALLIANCE[0])
        self.assertEqual(identity.alliance_name, f.DEFAULT_ALLIANCE[1])
        # The character's own corporation is kept apart for the member list.
        self.assertEqual(
            (identity.character_corporation_id, identity.character_corporation_name),
            (f.OTHER_CORP[0], f.OTHER_CORP[1]),
        )
        self.assertEqual(
            (identity.character_alliance_id, identity.character_alliance_name),
            (f.FOREIGN_ALLIANCE[0], f.FOREIGN_ALLIANCE[1]),
        )

    def test_character_without_alliance_has_no_own_alliance(self):
        alt = f.add_alt(self.user, corporation=f.OTHER_CORP, alliance=None)

        identity = identity_for_character_id(alt.character_id)

        self.assertEqual(identity.character_corporation_id, f.OTHER_CORP[0])
        self.assertIsNone(identity.character_alliance_id)
        self.assertEqual(identity.character_alliance_name, "")
        self.assertEqual(identity.alliance_id, f.DEFAULT_ALLIANCE[0])

    def test_unowned_character_uses_its_own_affiliation(self):
        stranger = f.create_character("Stranger", corporation=f.OTHER_CORP, alliance=None)

        identity = identity_for_character_id(stranger.character_id)

        self.assertIsNone(identity.user)
        self.assertIsNone(identity.main_character_id)
        self.assertEqual(identity.main_character_name, "")
        self.assertEqual(identity.character_name, "Stranger")
        self.assertEqual(identity.corporation_id, f.OTHER_CORP[0])
        self.assertIsNone(identity.alliance_id)

    def test_unknown_character_id(self):
        unknown = f.next_id()

        identity = identity_for_character_id(unknown)

        self.assertIsNone(identity.user)
        self.assertEqual(identity.character_name, f"Character {unknown}")
        self.assertIsNone(identity.corporation_id)
        self.assertEqual(identity.corporation_name, "")

    def test_owned_character_of_user_without_main(self):
        user = f.create_user(with_main=False)
        character = f.add_alt(user, corporation=f.OTHER_CORP)

        identity = identity_for_character_id(character.character_id)

        self.assertEqual(identity.user, user)
        self.assertIsNone(identity.main_character_id)
        self.assertEqual(identity.corporation_id, f.OTHER_CORP[0])

    def test_resolve_many_matches_single_lookup(self):
        alt = f.add_alt(self.user, corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        stranger = f.create_character()
        unknown = f.next_id()
        ids = [self.main.character_id, alt.character_id, stranger.character_id, unknown]

        result = resolve_many(ids + [str(alt.character_id)])

        self.assertEqual(set(result), set(ids))
        for cid in ids:
            self.assertEqual(result[cid], identity_for_character_id(cid))

    def test_resolve_many_empty(self):
        self.assertEqual(resolve_many([]), {})

    def test_owned_character_lookup_is_scoped_to_user(self):
        alt = f.add_alt(self.user)
        other = f.create_user()

        self.assertEqual(get_owned_character(self.user, alt.character_id).character, alt)
        self.assertIsNone(get_owned_character(other, alt.character_id))
        self.assertIsNone(get_owned_character(self.user, f.main_of(other).character_id))

    def test_owned_characters_sorted_by_name(self):
        f.add_alt(self.user, "Zulu Alt")
        f.add_alt(self.user, "Alpha Alt")

        names = [o.character.character_name for o in owned_characters(self.user)]

        self.assertEqual(names, ["Alpha Alt", "Home Main", "Zulu Alt"])

    def test_corporation_main_count_ignores_alts(self):
        f.add_alt(self.user)
        f.add_alt(self.user)
        f.create_user()
        other_corp_user = f.create_user(corporation=f.OTHER_CORP)
        f.add_alt(other_corp_user, corporation=f.DEFAULT_CORP)

        self.assertEqual(corporation_main_count(f.DEFAULT_CORP[0]), 2)
        self.assertEqual(corporation_main_count(f.OTHER_CORP[0]), 1)
        self.assertEqual(corporation_main_count(9_999_999), 0)


class ConfiguredAllianceIdsTests(TestCase):
    def _parse(self, raw):
        return configured_alliance_ids(FleetOpsSettings(history_alliance_ids=raw))

    def test_empty(self):
        self.assertEqual(self._parse(""), set())
        self.assertEqual(self._parse("   "), set())
        self.assertEqual(self._parse(None), set())

    def test_commas_and_semicolons(self):
        self.assertEqual(self._parse("3000001,3000002"), {3000001, 3000002})
        self.assertEqual(self._parse("3000001;3000002"), {3000001, 3000002})
        self.assertEqual(self._parse(" 3000001 ; 3000002 , 3000003 "), {3000001, 3000002, 3000003})

    def test_junk_and_duplicates_are_ignored(self):
        self.assertEqual(self._parse("abc, 3000001,,;3000001; 12x ;"), {3000001})
        self.assertEqual(self._parse("not an id"), set())

    def test_reads_saved_settings_by_default(self):
        f.settings(history_alliance_ids="3000001;3000005")
        self.assertEqual(configured_alliance_ids(), {3000001, 3000005})

    def test_current_member_user_ids(self):
        member = f.create_user()
        f.create_user(alliance=f.FOREIGN_ALLIANCE)
        f.create_user(with_main=False)

        self.assertIsNone(current_member_user_ids(FleetOpsSettings(history_alliance_ids="")))
        self.assertEqual(
            current_member_user_ids(FleetOpsSettings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))),
            {member.pk},
        )


class MembershipFilterTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED)

        # Main in the configured alliance, attending on an alt elsewhere.
        self.member = f.create_user()
        member_alt = f.add_alt(self.member, corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        self.member_row = f.add_attendance(self.operation, self.member, member_alt)

        # Main in a foreign alliance, attending on an alt inside the configured alliance.
        self.outsider = f.create_user(corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        outsider_alt = f.add_alt(self.outsider)
        self.outsider_row = f.add_attendance(self.operation, self.outsider, outsider_alt)

        stranger = f.create_character()
        self.unmapped_row = AttendanceRecord.objects.create(
            operation=self.operation,
            character_id=stranger.character_id,
            character_name=stranger.character_name,
            source=MANUAL,
            first_seen=self.operation.started_at,
            last_seen=self.operation.started_at,
        )

    def _visible(self):
        return set(apply_current_membership_filter(AttendanceRecord.objects.all()).values_list("pk", flat=True))

    def test_no_configuration_shows_everything(self):
        f.settings(history_alliance_ids="")
        self.assertEqual(self._visible(), {self.member_row.pk, self.outsider_row.pk, self.unmapped_row.pk})

    def test_filter_uses_main_character_alliance(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        self.assertEqual(self._visible(), {self.member_row.pk, self.unmapped_row.pk})

    def test_user_without_main_is_hidden(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        no_main = f.create_user(with_main=False)
        row = AttendanceRecord.objects.create(
            operation=self.operation,
            character_id=f.next_id(),
            character_name="Orphan",
            auth_user=no_main,
            source=AUTOMATIC,
        )
        self.assertNotIn(row.pk, self._visible())

    def test_history_queryset_applies_filter(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))

        rows = set(attendance_history_queryset().values_list("pk", flat=True))

        self.assertEqual(rows, {self.member_row.pk, self.unmapped_row.pk})


class PruneHistoryTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.pilot = f.create_user(perms=f.MEMBER_PERMS)

    def _attendance_at(self, days_ago, user=None, character=None):
        operation = f.create_operation(
            self.fc, status=FleetOperation.Status.CLOSED, started_at=self.now - timedelta(days=days_ago)
        )
        return f.add_attendance(operation, user or self.pilot, character)

    def _event_at(self, days_ago):
        operation = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED)
        event = FleetMemberEvent.objects.create(
            operation=operation,
            character_id=f.main_of(self.pilot).character_id,
            character_name="x",
            event_type=FleetMemberEvent.EventType.JOIN,
        )
        FleetMemberEvent.objects.filter(pk=event.pk).update(created_at=self.now - timedelta(days=days_ago))
        return event

    def _exists(self, obj):
        return obj.__class__.objects.filter(pk=obj.pk).exists()

    def test_default_retention_is_365_days(self):
        recent = self._attendance_at(364)
        old = self._attendance_at(366)
        recent_event = self._event_at(364)
        old_event = self._event_at(366)

        result = prune_history()

        self.assertEqual(result, {"old_attendance": 1, "left_alliance": 0, "old_events": 1})
        self.assertTrue(self._exists(recent))
        self.assertFalse(self._exists(old))
        self.assertTrue(self._exists(recent_event))
        self.assertFalse(self._exists(old_event))

    def test_retention_below_365_days_is_treated_as_365(self):
        f.settings(data_retention_days=30)
        kept = self._attendance_at(100)
        kept_event = self._event_at(100)
        removed = self._attendance_at(400)

        prune_history()

        self.assertTrue(self._exists(kept))
        self.assertTrue(self._exists(kept_event))
        self.assertFalse(self._exists(removed))

    def test_zero_retention_does_not_wipe_history(self):
        f.settings(data_retention_days=0)
        kept = self._attendance_at(1)

        prune_history()

        self.assertTrue(self._exists(kept))

    def test_longer_retention_is_honoured(self):
        f.settings(data_retention_days=730)
        kept = self._attendance_at(400)
        removed = self._attendance_at(800)

        prune_history()

        self.assertTrue(self._exists(kept))
        self.assertFalse(self._exists(removed))

    def _settings_form(self, retention_days):
        data = {
            "multiplier_mode": "fc",
            "pap_minimum_minutes": 0,
            "minimum_fleet_minutes": 0,
            "pap_link_minutes": 30,
            "tracking_interval": 60,
            "stale_threshold": 180,
            "auto_end_missing_count": 3,
            "incentive_minimum_fleets": 3,
            "data_retention_days": retention_days,
            "history_alliance_ids": "",
            "srp_provider": "auto",
        }
        return FleetOpsSettingsForm(data, instance=FleetOpsSettings.get_solo())

    def test_settings_form_accepts_long_retention_up_to_the_maximum(self):
        form = self._settings_form(36_500)
        self.assertTrue(form.is_valid(), form.errors)

    def test_settings_form_rejects_retention_beyond_the_maximum(self):
        for days in (36_501, 999_999):
            with self.subTest(days=days):
                form = self._settings_form(days)
                self.assertFalse(form.is_valid())
                self.assertIn("data_retention_days", form.errors)

    def test_very_long_retention_keeps_everything(self):
        f.settings(data_retention_days=999_999)
        kept = self._attendance_at(5000)

        result = prune_history()

        self.assertEqual(result["old_attendance"], 0)
        self.assertTrue(self._exists(kept))

    def test_history_page_with_very_long_retention(self):
        f.settings(data_retention_days=999_999)
        self._attendance_at(5)
        self.client.force_login(self.pilot)
        self.client.raise_request_exception = False

        response = self.client.get(reverse("fleetops:attendance_history_me"))

        self.assertEqual(response.status_code, 200)

    def test_history_page_with_short_retention_shows_full_year(self):
        f.settings(data_retention_days=30)
        row = self._attendance_at(200)
        self.client.force_login(self.pilot)

        response = self.client.get(reverse("fleetops:attendance_history_me"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([r.pk for r in response.context["rows"]], [row.pk])
        self.assertEqual(response.context["retention_days"], 365)

    def test_departed_members_are_pruned_by_main_alliance(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        foreign_alt = f.add_alt(self.pilot, alliance=f.FOREIGN_ALLIANCE, corporation=f.OTHER_CORP)
        member_row = self._attendance_at(10, character=foreign_alt)
        departed = f.create_user(alliance=f.FOREIGN_ALLIANCE, corporation=f.OTHER_CORP)
        departed_alt = f.add_alt(departed)
        departed_row = self._attendance_at(10, user=departed, character=departed_alt)
        unmapped = AttendanceRecord.objects.create(
            operation=member_row.operation,
            character_id=f.next_id(),
            character_name="Unmapped",
            source=MANUAL,
        )

        result = prune_history()

        self.assertEqual(result["left_alliance"], 1)
        self.assertTrue(self._exists(member_row))
        self.assertFalse(self._exists(departed_row))
        self.assertTrue(self._exists(unmapped))

    def test_unmapped_rows_still_expire_with_retention(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        operation = f.create_operation(
            self.fc, status=FleetOperation.Status.CLOSED, started_at=self.now - timedelta(days=400)
        )
        unmapped = AttendanceRecord.objects.create(
            operation=operation, character_id=f.next_id(), character_name="Old Unmapped", source=MANUAL
        )

        prune_history()

        self.assertFalse(self._exists(unmapped))

    def test_no_membership_pruning_without_configuration(self):
        departed = f.create_user(alliance=f.FOREIGN_ALLIANCE, corporation=f.OTHER_CORP)
        row = self._attendance_at(10, user=departed)

        result = prune_history()

        self.assertEqual(result["left_alliance"], 0)
        self.assertTrue(self._exists(row))

    def test_dry_run_reports_without_deleting(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        old = self._attendance_at(500)
        old_event = self._event_at(500)
        departed = f.create_user(alliance=f.FOREIGN_ALLIANCE)
        departed_row = self._attendance_at(5, user=departed)

        result = prune_history(dry_run=True)

        self.assertEqual(result, {"old_attendance": 1, "left_alliance": 1, "old_events": 1})
        for obj in (old, old_event, departed_row):
            self.assertTrue(self._exists(obj))

    def test_celery_task_prunes(self):
        old = self._attendance_at(400)
        recent = self._attendance_at(5)

        result = prune_attendance_history()

        self.assertEqual(result["old_attendance"], 1)
        self.assertFalse(self._exists(old))
        self.assertTrue(self._exists(recent))

    def test_management_command_dry_run(self):
        old = self._attendance_at(400)
        out = StringIO()

        call_command("fleetops_prune_history", "--dry-run", stdout=out)

        self.assertIn("Would remove: 1 old attendance", out.getvalue())
        self.assertTrue(self._exists(old))
