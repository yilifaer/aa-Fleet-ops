# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""A PAP row's ``updated_at`` says when the row last really changed: a recalculation writes only rows whose values
changed, and writes nothing at all when nothing changed."""

from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from fleetops.models import AttendanceRecord, FleetMemberState, FleetOperation, ReviewStatus
from fleetops.services import approvals
from fleetops.services.attendance import create_manual_attendance, set_operation_attendance_multiplier
from fleetops.services.pap import finalize_pap

from . import factories as f
from .test_pap_engine import EngineTestBase, row

Status = FleetOperation.Status
LONG_AGO = timezone.now() - timedelta(days=3)


def stamps(operation):
    return {record.pk: record.updated_at for record in AttendanceRecord.objects.filter(operation=operation)}


def age_all(operation):
    """Give every row an old ``updated_at`` (``auto_now`` only applies to ``save()``)."""
    AttendanceRecord.objects.filter(operation=operation).update(updated_at=LONG_AGO)


def attendance_updates(captured):
    return [
        query["sql"]
        for query in captured.captured_queries
        if query["sql"].lstrip().upper().startswith("UPDATE") and "fleetops_attendancerecord" in query["sql"]
    ]


class RecalculationTests(EngineTestBase):
    def setUp(self):
        super().setUp()
        f.multiplier_tiers((10, 2))
        # One granted row, one below the minimum and one over the character limit.
        self.operation = self.closed(minimum=20, limit=1)
        self.below = f.main_of(f.create_user(main_name="Pilot Below"))
        self.alt = f.add_alt(self.pilot, "Pilot A Alt")
        self.track(self.main, 60)
        self.track(self.below, 5)
        self.track(self.alt, 30)
        finalize_pap(self.operation)
        age_all(self.operation)

    def test_a_multiplier_change_moves_only_the_rows_that_credit_someone(self):
        set_operation_attendance_multiplier(self.operation, 2, actor=self.fc)

        granted_row = row(self.operation, self.main)
        capped_row = row(self.operation, self.alt)
        below_row = row(self.operation, self.below)
        self.assertEqual((granted_row.granted, granted_row.attendance_value), (True, 2))
        self.assertGreater(granted_row.updated_at, LONG_AGO)
        # The rows that are not granted follow the multiplier too, but nobody is credited by them: they keep their time.
        self.assertEqual((capped_row.granted, capped_row.attendance_value), (False, 2))
        self.assertEqual((below_row.granted, below_row.attendance_value), (False, 2))
        self.assertEqual(capped_row.updated_at, LONG_AGO)
        self.assertEqual(below_row.updated_at, LONG_AGO)

    def test_a_recalculation_without_a_change_keeps_every_row_and_writes_nothing(self):
        before = stamps(self.operation)
        self.client.force_login(f.create_user(perms=f.FC_LEAD_PERMS))

        with CaptureQueriesContext(connection) as captured:
            response = self.client.post(reverse("fleetops:recalculate_pap", args=[self.operation.uuid]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(stamps(self.operation), before)
        self.assertEqual(attendance_updates(captured), [])

    def test_finalize_pap_twice_writes_nothing_the_second_time(self):
        with CaptureQueriesContext(connection) as captured:
            finalize_pap(self.operation)

        self.assertEqual(attendance_updates(captured), [])
        self.assertTrue(all(value == LONG_AGO for value in stamps(self.operation).values()))

    def test_a_row_whose_values_change_gets_a_new_time(self):
        # More time in fleet is recorded for the alt that is over the limit; the other rows stay as they are.
        FleetMemberState.objects.filter(operation=self.operation, character_id=self.alt.character_id).update(
            accumulated_seconds=3 * 3600
        )

        finalize_pap(self.operation)

        self.assertGreater(row(self.operation, self.alt).updated_at, LONG_AGO)
        self.assertEqual(row(self.operation, self.main).updated_at, LONG_AGO)
        self.assertEqual(row(self.operation, self.below).updated_at, LONG_AGO)


class ManualRowTests(EngineTestBase):
    def test_the_grant_of_a_manual_row_moves_its_time_only_when_it_changes(self):
        lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.track(self.main, 30)
        finalize_pap(self.operation)
        manual = create_manual_attendance(self.operation, actor=self.fc, character_id=self.main.character_id)
        self.assertEqual((manual.review_status, manual.granted), (ReviewStatus.PENDING, False))
        age_all(self.operation)

        # Another recalculation changes nothing about a pending row.
        finalize_pap(self.operation)
        manual.refresh_from_db()
        self.assertEqual(manual.updated_at, LONG_AGO)

        approvals.approve_attendance([manual], actor=lead)

        manual.refresh_from_db()
        self.assertEqual((manual.review_status, manual.granted), (ReviewStatus.APPROVED, True))
        self.assertGreater(manual.updated_at, LONG_AGO)

    def test_approving_and_rejecting_rows_sets_their_time(self):
        lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        first = create_manual_attendance(self.operation, actor=self.fc, character_id=self.main.character_id)
        second = create_manual_attendance(
            self.operation, actor=self.fc, character_id=f.main_of(f.create_user()).character_id
        )
        age_all(self.operation)

        approvals.approve_attendance([first], actor=lead)
        approvals.reject_attendance([second], actor=lead, note="Not there")

        for record in (first, second):
            record.refresh_from_db()
            self.assertGreater(record.updated_at, LONG_AGO)
