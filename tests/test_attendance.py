"""Tests for attendance crediting, manual corrections, identity resolution and history retention."""

import unittest
from datetime import timedelta
from io import StringIO
from unittest import mock

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
)
from fleetops.services.attendance import (
    create_manual_attendance,
    ensure_automatic_attendance,
    set_operation_attendance_multiplier,
)
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
from fleetops.services.statistics import attendance_history_queryset
from fleetops.services.tracking import sync_operation
from fleetops.tasks import prune_attendance_history

from . import factories as f

AUTOMATIC = AttendanceRecord.Source.AUTOMATIC
MANUAL = AttendanceRecord.Source.MANUAL


def credit(operation, character, seen_at=None):
    """Run automatic attendance for a character the way the tracker does."""
    return ensure_automatic_attendance(
        operation,
        identity_for_character_id(character.character_id),
        seen_at or timezone.now(),
    )


def automatic_rows(operation, **filters):
    return AttendanceRecord.objects.filter(operation=operation, source=AUTOMATIC, **filters)


def audit_actions(obj=None):
    qs = AuditLog.objects.all()
    if obj is not None:
        qs = qs.filter(object_type=obj.__class__.__name__, object_id=str(obj.pk))
    return list(qs.order_by("pk").values_list("action", flat=True))


class EnsureAutomaticAttendanceTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc)
        self.pilot = f.create_user()
        self.main = f.main_of(self.pilot)

    def test_unowned_character_gets_no_attendance(self):
        stranger = f.create_character("Unowned Pilot")
        self.assertIsNone(credit(self.operation, stranger))
        self.assertFalse(AttendanceRecord.objects.filter(operation=self.operation).exists())

    def test_unknown_character_gets_no_attendance(self):
        identity = identity_for_character_id(f.next_id())
        self.assertIsNone(ensure_automatic_attendance(self.operation, identity, timezone.now()))
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_creates_single_granted_row_for_owned_character(self):
        seen = timezone.now() - timedelta(minutes=10)
        record = credit(self.operation, self.main, seen)

        self.assertIsNotNone(record)
        self.assertEqual(record.source, AUTOMATIC)
        self.assertEqual(record.auth_user, self.pilot)
        self.assertEqual(record.character_id, self.main.character_id)
        self.assertEqual(record.main_character_id, self.main.character_id)
        self.assertEqual(record.main_character_name, self.main.character_name)
        self.assertEqual(record.corporation_id, f.DEFAULT_CORP[0])
        self.assertEqual(record.attendance_value, 1)
        self.assertTrue(record.granted)
        self.assertFalse(record.capped)
        self.assertEqual(record.first_seen, seen)
        self.assertEqual(record.last_seen, seen)

    def test_repeated_sightings_reuse_the_row_and_move_last_seen_forward(self):
        first = timezone.now() - timedelta(minutes=20)
        later = first + timedelta(minutes=5)
        record = credit(self.operation, self.main, first)

        again = credit(self.operation, self.main, later)

        self.assertEqual(again.pk, record.pk)
        self.assertEqual(automatic_rows(self.operation).count(), 1)
        record.refresh_from_db()
        self.assertEqual(record.first_seen, first)
        self.assertEqual(record.last_seen, later)

    def test_older_sighting_does_not_move_last_seen_backwards(self):
        latest = timezone.now() - timedelta(minutes=1)
        record = credit(self.operation, self.main, latest)

        credit(self.operation, self.main, latest - timedelta(minutes=30))

        record.refresh_from_db()
        self.assertEqual(record.last_seen, latest)

    def test_last_seen_filled_when_previously_empty(self):
        record = f.add_attendance(self.operation, self.pilot)
        AttendanceRecord.objects.filter(pk=record.pk).update(last_seen=None)
        seen = timezone.now()

        credit(self.operation, self.main, seen)

        record.refresh_from_db()
        self.assertEqual(record.last_seen, seen)

    def test_rows_are_per_operation(self):
        other = f.create_operation(self.fc)
        credit(self.operation, self.main)
        credit(other, self.main)

        self.assertEqual(automatic_rows(self.operation).count(), 1)
        self.assertEqual(automatic_rows(other).count(), 1)

    def test_value_follows_fleet_multiplier(self):
        self.operation.attendance_multiplier = 3
        self.operation.save()

        record = credit(self.operation, self.main)

        self.assertEqual(record.attendance_value, 3)

    def test_unlimited_when_attendance_limit_is_null(self):
        f.settings(attendance_limit=None)
        alts = [f.add_alt(self.pilot) for _ in range(3)]

        for character in [self.main, *alts]:
            credit(self.operation, character)

        rows = automatic_rows(self.operation, auth_user=self.pilot)
        self.assertEqual(rows.count(), 4)
        self.assertTrue(all(r.granted and not r.capped for r in rows))

    def test_limit_of_one_caps_extra_alts_but_keeps_them(self):
        f.settings(attendance_limit=1)
        alt_one = f.add_alt(self.pilot)
        alt_two = f.add_alt(self.pilot)

        main_row = credit(self.operation, self.main)
        alt_one_row = credit(self.operation, alt_one)
        alt_two_row = credit(self.operation, alt_two)

        self.assertTrue(main_row.granted)
        self.assertFalse(main_row.capped)
        for row in (alt_one_row, alt_two_row):
            self.assertFalse(row.granted)
            self.assertTrue(row.capped)
            self.assertIn("capped", row.notes.lower())
            self.assertEqual(row.main_character_id, self.main.character_id)
        self.assertEqual(automatic_rows(self.operation).count(), 3)

    def test_limit_of_two_grants_first_two_characters(self):
        f.settings(attendance_limit=2)
        alts = [f.add_alt(self.pilot) for _ in range(3)]

        rows = [credit(self.operation, c) for c in [alts[0], self.main, alts[1], alts[2]]]

        self.assertEqual([r.granted for r in rows], [True, True, False, False])
        self.assertEqual([r.capped for r in rows], [False, False, True, True])

    def test_limit_is_per_user(self):
        f.settings(attendance_limit=1)
        other = f.create_user()

        credit(self.operation, self.main)
        other_row = credit(self.operation, f.main_of(other))

        self.assertTrue(other_row.granted)

    def test_limit_is_per_fleet(self):
        f.settings(attendance_limit=1)
        other_operation = f.create_operation(self.fc)
        alt = f.add_alt(self.pilot)

        credit(self.operation, self.main)
        row = credit(other_operation, alt)

        self.assertTrue(row.granted)

    def test_capped_row_is_not_regranted_on_later_sightings(self):
        f.settings(attendance_limit=1)
        alt = f.add_alt(self.pilot)
        credit(self.operation, self.main)
        capped = credit(self.operation, alt)

        again = credit(self.operation, alt, timezone.now() + timedelta(minutes=1))

        self.assertEqual(again.pk, capped.pk)
        again.refresh_from_db()
        self.assertFalse(again.granted)
        self.assertTrue(again.capped)

    def test_alt_in_foreign_alliance_is_credited_to_main_and_main_corporation(self):
        alt = f.add_alt(self.pilot, "Foreign Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)

        record = credit(self.operation, alt)

        self.assertEqual(record.character_id, alt.character_id)
        self.assertEqual(record.character_name, "Foreign Alt")
        self.assertEqual(record.auth_user, self.pilot)
        self.assertEqual(record.main_character_id, self.main.character_id)
        self.assertEqual(record.main_character_name, self.main.character_name)
        self.assertEqual(record.corporation_id, f.DEFAULT_CORP[0])
        self.assertEqual(record.corporation_name, f.DEFAULT_CORP[1])

    def test_alt_of_main_in_other_corp_uses_main_corp(self):
        pilot = f.create_user(corporation=f.OTHER_CORP)
        alt = f.add_alt(pilot, corporation=f.DEFAULT_CORP)

        record = credit(self.operation, alt)

        self.assertEqual(record.corporation_id, f.OTHER_CORP[0])


class TrackingAttendanceIntegrationTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc)

    def _sync(self, characters):
        rows = [f.esi_member(c) for c in characters]
        with mock.patch("fleetops.services.tracking.get_fleet_members", return_value=rows):
            return sync_operation(self.operation)

    def test_sync_credits_owned_members_once_and_skips_unowned(self):
        pilot = f.create_user()
        alt = f.add_alt(pilot, corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        stranger = f.create_character("Random Stranger", corporation=f.OTHER_CORP)

        self._sync([f.main_of(self.fc), alt, stranger])
        self._sync([f.main_of(self.fc), alt, stranger])

        rows = AttendanceRecord.objects.filter(operation=self.operation)
        self.assertEqual(rows.count(), 2)
        self.assertFalse(rows.filter(character_id=stranger.character_id).exists())
        alt_row = rows.get(character_id=alt.character_id)
        self.assertEqual(alt_row.main_character_id, f.main_of(pilot).character_id)
        self.assertEqual(alt_row.corporation_id, f.DEFAULT_CORP[0])

    def test_sync_respects_attendance_limit(self):
        f.settings(attendance_limit=1)
        pilot = f.create_user()
        alt = f.add_alt(pilot)

        self._sync([f.main_of(pilot), alt])

        rows = AttendanceRecord.objects.filter(operation=self.operation, auth_user=pilot)
        self.assertEqual(rows.filter(granted=True).count(), 1)
        self.assertEqual(rows.filter(capped=True).count(), 1)


class ManualAttendanceTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.closed = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED)
        self.active = f.create_operation(self.fc)
        self.pilot = f.create_user()
        self.main = f.main_of(self.pilot)

    def _manual(self, operation, character_id, **kwargs):
        kwargs.setdefault("actor", self.fc)
        return create_manual_attendance(operation, character_id=character_id, **kwargs)

    def test_keep_without_automatic_creates_granted_manual_row(self):
        record = self._manual(self.closed, self.main.character_id, notes="Late login")

        self.assertEqual(record.source, MANUAL)
        self.assertTrue(record.granted)
        self.assertFalse(record.capped)
        self.assertEqual(record.attendance_value, 1)
        self.assertEqual(record.auth_user, self.pilot)
        self.assertEqual(record.created_by, self.fc)
        self.assertEqual(record.character_name, self.main.character_name)
        self.assertEqual(record.notes, "Late login")
        self.assertEqual(record.first_seen, self.closed.started_at)
        self.assertEqual(record.last_seen, self.closed.ended_at)

    def test_manual_row_on_active_fleet_uses_start_time_for_last_seen(self):
        record = self._manual(self.active, self.main.character_id)
        self.assertEqual(record.last_seen, self.active.started_at)

    def test_manual_create_is_audited(self):
        record = self._manual(self.closed, self.main.character_id, attendance_value=2)

        entry = AuditLog.objects.get(action="attendance.manual_create")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.object_type, "AttendanceRecord")
        self.assertEqual(entry.object_id, str(record.pk))
        self.assertIsNone(entry.old_value)
        self.assertEqual(entry.new_value["attendance_value"], 2)
        self.assertEqual(entry.new_value["operation"], str(self.closed.uuid))

    def test_keep_with_automatic_leaves_both_rows_granted(self):
        automatic = f.add_attendance(self.closed, self.pilot)

        manual = self._manual(self.closed, self.main.character_id, duplicate_action="keep")

        automatic.refresh_from_db()
        self.assertTrue(automatic.granted)
        self.assertEqual(automatic.attendance_value, 1)
        self.assertNotEqual(manual.pk, automatic.pk)
        self.assertEqual(AttendanceRecord.objects.filter(operation=self.closed).count(), 2)

    def test_closed_fleet_accepts_multiple_manual_rows(self):
        for value in (1, 2, 3):
            self._manual(self.closed, self.main.character_id, attendance_value=value)

        rows = AttendanceRecord.objects.filter(operation=self.closed, source=MANUAL, auth_user=self.pilot)
        self.assertEqual(rows.count(), 3)
        self.assertEqual(sum(r.attendance_value for r in rows), 6)
        self.assertEqual(audit_actions().count("attendance.manual_create"), 3)

    def test_merge_adds_value_to_automatic_row(self):
        automatic = f.add_attendance(self.closed, self.pilot)

        result = self._manual(
            self.closed, self.main.character_id, attendance_value=2, duplicate_action="merge", notes="Logi bonus"
        )

        self.assertEqual(result.pk, automatic.pk)
        automatic.refresh_from_db()
        self.assertEqual(automatic.attendance_value, 3)
        self.assertTrue(automatic.granted)
        self.assertIn("Logi bonus", automatic.notes)
        self.assertFalse(AttendanceRecord.objects.filter(operation=self.closed, source=MANUAL).exists())

        entry = AuditLog.objects.get(action="attendance.merge")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.object_id, str(automatic.pk))
        self.assertEqual(entry.old_value, {"attendance_value": 1, "granted": True})
        self.assertEqual(entry.new_value, {"attendance_value": 3, "granted": True})

    def test_merge_into_capped_row_grants_it(self):
        capped = f.add_attendance(self.closed, self.pilot, granted=False, capped=True)

        self._manual(self.closed, self.main.character_id, duplicate_action="merge")

        capped.refresh_from_db()
        self.assertTrue(capped.granted)
        self.assertFalse(capped.capped)
        self.assertEqual(capped.attendance_value, 2)

    def test_merge_without_automatic_row_creates_manual_row(self):
        record = self._manual(self.closed, self.main.character_id, duplicate_action="merge")

        self.assertEqual(record.source, MANUAL)
        self.assertEqual(audit_actions(), ["attendance.manual_create"])

    def test_merge_targets_the_matching_character_only(self):
        alt = f.add_alt(self.pilot)
        main_row = f.add_attendance(self.closed, self.pilot)

        record = self._manual(self.closed, alt.character_id, duplicate_action="merge")

        main_row.refresh_from_db()
        self.assertEqual(main_row.attendance_value, 1)
        self.assertEqual(record.source, MANUAL)
        self.assertEqual(record.character_id, alt.character_id)

    def test_replace_ungrants_automatic_and_creates_manual(self):
        automatic = f.add_attendance(self.closed, self.pilot)

        manual = self._manual(self.closed, self.main.character_id, attendance_value=2, duplicate_action="replace")

        automatic.refresh_from_db()
        self.assertFalse(automatic.granted)
        self.assertIn("Replaced", automatic.notes)
        self.assertEqual(manual.source, MANUAL)
        self.assertTrue(manual.granted)
        self.assertEqual(manual.attendance_value, 2)

        granted = AttendanceRecord.objects.filter(operation=self.closed, auth_user=self.pilot, granted=True)
        self.assertEqual([r.pk for r in granted], [manual.pk])

        self.assertEqual(audit_actions(automatic), ["attendance.replace_auto"])
        self.assertEqual(audit_actions(manual), ["attendance.manual_create"])
        replace_entry = AuditLog.objects.get(action="attendance.replace_auto")
        self.assertEqual(replace_entry.old_value, {"granted": True})
        self.assertEqual(replace_entry.new_value, {"granted": False})

    def test_replace_capped_row_gives_manual_credit(self):
        f.add_attendance(self.closed, self.pilot, granted=False, capped=True)

        manual = self._manual(self.closed, self.main.character_id, duplicate_action="replace")

        self.assertTrue(manual.granted)
        self.assertEqual(
            AttendanceRecord.objects.filter(operation=self.closed, auth_user=self.pilot, granted=True).count(), 1
        )

    def test_replace_without_automatic_creates_manual_row(self):
        record = self._manual(self.closed, self.main.character_id, duplicate_action="replace")

        self.assertEqual(record.source, MANUAL)
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

    def test_manual_rows_are_not_touched_by_later_tracking(self):
        manual = self._manual(self.active, self.main.character_id)

        automatic = credit(self.active, self.main)

        self.assertNotEqual(automatic.pk, manual.pk)
        self.assertEqual(automatic.source, AUTOMATIC)
        manual.refresh_from_db()
        self.assertEqual(manual.attendance_value, 1)


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
        operation = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED)

        response = self._post(operation, attendance_value=2)

        self.assertEqual(response.status_code, 302)
        record = AttendanceRecord.objects.get(operation=operation)
        self.assertEqual(record.source, MANUAL)
        self.assertEqual(record.attendance_value, 2)
        self.assertTrue(AuditLog.objects.filter(action="attendance.manual_create", actor=self.fc).exists())

    def test_add_to_active_fleet(self):
        operation = f.create_operation(self.fc)

        self._post(operation)

        self.assertTrue(AttendanceRecord.objects.filter(operation=operation, source=MANUAL).exists())

    def test_delete_manual_row_is_audited(self):
        operation = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED)
        record = f.add_attendance(operation, self.pilot, source=MANUAL, value=2)

        response = self.client.post(reverse("fleetops:delete_manual_attendance", args=[record.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(AttendanceRecord.objects.filter(pk=record.pk).exists())
        entry = AuditLog.objects.get(action="attendance.manual_delete")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.object_id, str(record.pk))
        self.assertEqual(entry.old_value, {"attendance_value": 2})

    def test_automatic_rows_cannot_be_deleted_through_manual_delete(self):
        operation = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED)
        record = f.add_attendance(operation, self.pilot)

        response = self.client.post(reverse("fleetops:delete_manual_attendance", args=[record.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(AttendanceRecord.objects.filter(pk=record.pk).exists())

    def test_other_fc_cannot_add_attendance_to_foreign_fleet(self):
        operation = f.create_operation(f.create_user(perms=f.FC_PERMS), status=FleetOperation.Status.CLOSED)

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
        self.operation = f.create_operation(self.fc)
        self.pilot = f.create_user()
        self.main = f.main_of(self.pilot)

    def test_only_granted_automatic_rows_are_multiplied(self):
        alt = f.add_alt(self.pilot)
        other = f.create_user()
        granted = f.add_attendance(self.operation, self.pilot)
        capped = f.add_attendance(self.operation, self.pilot, alt, granted=False, capped=True)
        manual = f.add_attendance(self.operation, other, source=MANUAL, value=1)

        updated = set_operation_attendance_multiplier(self.operation, 3, actor=self.fc)

        self.assertEqual(updated, 1)
        granted.refresh_from_db()
        capped.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(granted.attendance_value, 3)
        self.assertEqual(capped.attendance_value, 1)
        self.assertEqual(manual.attendance_value, 1)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.attendance_multiplier, 3)

    def test_replaced_automatic_row_is_not_multiplied(self):
        automatic = f.add_attendance(self.operation, self.pilot)
        create_manual_attendance(
            self.operation, actor=self.fc, character_id=self.main.character_id, duplicate_action="replace"
        )

        set_operation_attendance_multiplier(self.operation, 2, actor=self.fc)

        automatic.refresh_from_db()
        self.assertEqual(automatic.attendance_value, 1)
        self.assertFalse(automatic.granted)
        manual = AttendanceRecord.objects.get(operation=self.operation, source=MANUAL)
        self.assertEqual(manual.attendance_value, 1)

    def test_rows_of_other_operations_are_untouched(self):
        other_operation = f.create_operation(self.fc)
        elsewhere = f.add_attendance(other_operation, self.pilot)
        f.add_attendance(self.operation, self.pilot)

        set_operation_attendance_multiplier(self.operation, 2)

        elsewhere.refresh_from_db()
        self.assertEqual(elsewhere.attendance_value, 1)

    def test_merged_extra_value_survives_multiplier_switches(self):
        automatic = f.add_attendance(self.operation, self.pilot)
        create_manual_attendance(
            self.operation,
            actor=self.fc,
            character_id=self.main.character_id,
            attendance_value=2,
            duplicate_action="merge",
        )
        automatic.refresh_from_db()
        self.assertEqual(automatic.attendance_value, 3)

        expected = {2: 4, 3: 5, 1: 3}
        for multiplier, value in expected.items():
            set_operation_attendance_multiplier(self.operation, multiplier)
            automatic.refresh_from_db()
            self.assertEqual(automatic.attendance_value, value, f"after switching to {multiplier}x")

    def test_switching_back_and_forth_is_reversible(self):
        rows = [f.add_attendance(self.operation, f.create_user()) for _ in range(3)]

        set_operation_attendance_multiplier(self.operation, 3)
        set_operation_attendance_multiplier(self.operation, 1)

        for row in rows:
            row.refresh_from_db()
            self.assertEqual(row.attendance_value, 1)

    def test_new_automatic_rows_use_current_multiplier(self):
        set_operation_attendance_multiplier(self.operation, 2)

        record = credit(self.operation, self.main)

        self.assertEqual(record.attendance_value, 2)

    def test_string_value_is_accepted(self):
        f.add_attendance(self.operation, self.pilot)
        set_operation_attendance_multiplier(self.operation, "2")
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.attendance_multiplier, 2)

    def test_invalid_values_raise_and_change_nothing(self):
        row = f.add_attendance(self.operation, self.pilot)
        for bad in (0, 4, -1, 10, "x", ""):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    set_operation_attendance_multiplier(self.operation, bad, actor=self.fc)
        row.refresh_from_db()
        self.operation.refresh_from_db()
        self.assertEqual(row.attendance_value, 1)
        self.assertEqual(self.operation.attendance_multiplier, 1)
        self.assertFalse(AuditLog.objects.filter(action="attendance.multiplier").exists())

    def test_audit_written_only_when_value_changes(self):
        f.add_attendance(self.operation, self.pilot)
        f.add_attendance(self.operation, f.create_user())

        set_operation_attendance_multiplier(self.operation, 1, actor=self.fc)
        self.assertFalse(AuditLog.objects.filter(action="attendance.multiplier").exists())

        set_operation_attendance_multiplier(self.operation, 2, actor=self.fc)
        entry = AuditLog.objects.get(action="attendance.multiplier")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.object_type, "FleetOperation")
        self.assertEqual(entry.object_id, str(self.operation.pk))
        self.assertEqual(entry.old_value, {"attendance_multiplier": 1})
        self.assertEqual(entry.new_value, {"attendance_multiplier": 2, "automatic_rows_updated": 2})

        set_operation_attendance_multiplier(self.operation, 2, actor=self.fc)
        self.assertEqual(AuditLog.objects.filter(action="attendance.multiplier").count(), 1)

    def test_view_rejects_invalid_multiplier(self):
        row = f.add_attendance(self.operation, self.pilot)
        self.client.force_login(self.fc)

        response = self.client.post(
            reverse("fleetops:set_attendance_multiplier", args=[self.operation.uuid]),
            {"attendance_multiplier": "5"},
        )

        self.assertEqual(response.status_code, 302)
        row.refresh_from_db()
        self.operation.refresh_from_db()
        self.assertEqual(row.attendance_value, 1)
        self.assertEqual(self.operation.attendance_multiplier, 1)

    def test_view_applies_valid_multiplier(self):
        row = f.add_attendance(self.operation, self.pilot)
        self.client.force_login(self.fc)

        self.client.post(
            reverse("fleetops:set_attendance_multiplier", args=[self.operation.uuid]),
            {"attendance_multiplier": "3"},
        )

        row.refresh_from_db()
        self.assertEqual(row.attendance_value, 3)
        self.assertTrue(AuditLog.objects.filter(action="attendance.multiplier", actor=self.fc).exists())

    # Known issue: merged extra is inferred from the current multiplier, so a row merged while ungranted loses its extra on the next switch
    @unittest.expectedFailure
    def test_merge_into_replaced_row_keeps_extra_after_switch(self):
        automatic = f.add_attendance(self.operation, self.pilot)
        create_manual_attendance(
            self.operation, actor=self.fc, character_id=self.main.character_id, duplicate_action="replace"
        )
        set_operation_attendance_multiplier(self.operation, 2)
        create_manual_attendance(
            self.operation,
            actor=self.fc,
            character_id=self.main.character_id,
            attendance_value=1,
            duplicate_action="merge",
        )

        set_operation_attendance_multiplier(self.operation, 3)

        automatic.refresh_from_db()
        # 3x base plus the 1 merged by hand.
        self.assertEqual(automatic.attendance_value, 4)


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
