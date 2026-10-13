"""FC lead review: who approves, the status of new entries and the count of waiting entries."""

from django.contrib.auth.models import AnonymousUser
from django.test import TestCase
from django.utils import timezone

from fleetops.models import AttendanceRecord, FleetOperation, ReviewStatus
from fleetops.services.review import (
    initial_review_fields,
    initial_review_status,
    is_approver,
    pending_approval_count,
    pending_attendance,
    pending_operations,
)

from . import factories as f

Status = FleetOperation.Status


class ApproverTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = f.create_user(perms=f.MEMBER_PERMS)
        cls.fc = f.create_user(perms=f.FC_PERMS)
        cls.lead = f.create_user(perms=f.FC_LEAD_PERMS)
        cls.manager = f.create_user(perms=["fleetops.basic_access", "fleetops.manage_fleets"])

    def test_only_manage_fleets_approves(self):
        # FCs hold manage_attendance and create_manual_fleet, but not the FC lead permission.
        self.assertFalse(is_approver(self.member))
        self.assertFalse(is_approver(self.fc))
        self.assertTrue(is_approver(self.lead))
        self.assertTrue(is_approver(self.manager))
        self.assertFalse(is_approver(None))
        self.assertFalse(is_approver(AnonymousUser()))

    def test_superuser_approves(self):
        root = f.create_user()
        root.is_superuser = True
        root.save()

        self.assertTrue(is_approver(root))

    def test_initial_status_follows_the_creator(self):
        self.assertEqual(initial_review_status(self.fc), ReviewStatus.PENDING)
        self.assertEqual(initial_review_status(self.member), ReviewStatus.PENDING)
        self.assertEqual(initial_review_status(None), ReviewStatus.PENDING)
        self.assertEqual(initial_review_status(self.lead), ReviewStatus.APPROVED)

    def test_approvers_entries_are_reviewed_by_themselves(self):
        now = timezone.now()

        self.assertEqual(
            initial_review_fields(self.lead, now),
            {"review_status": ReviewStatus.APPROVED, "reviewed_by": self.lead, "reviewed_at": now},
        )
        self.assertEqual(
            initial_review_fields(self.fc, now),
            {"review_status": ReviewStatus.PENDING, "reviewed_by": None, "reviewed_at": None},
        )
        self.assertIsNotNone(initial_review_fields(self.lead)["reviewed_at"])

    def test_fields_can_be_stored_on_fleets_and_attendance(self):
        operation = f.create_operation(
            self.fc, status=Status.CLOSED, is_manual=True, **initial_review_fields(self.fc)
        )
        row = f.add_attendance(
            operation, self.member, source=AttendanceRecord.Source.MANUAL, **initial_review_fields(self.lead)
        )

        operation.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual((operation.review_status, operation.reviewed_by), (ReviewStatus.PENDING, None))
        self.assertEqual((row.review_status, row.reviewed_by), (ReviewStatus.APPROVED, self.lead))
        self.assertIsNotNone(row.reviewed_at)


class PendingCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fc = f.create_user(perms=f.FC_PERMS)
        cls.member = f.create_user()

    def test_nothing_waits_on_a_fresh_install(self):
        f.create_operation(self.fc, status=Status.CLOSED)

        self.assertEqual(pending_approval_count(), 0)

    def test_pending_fleets_and_rows_are_counted_once_each(self):
        pending_fleet = f.create_operation(
            self.fc, status=Status.CLOSED, is_manual=True, review_status=ReviewStatus.PENDING
        )
        f.create_operation(self.fc, status=Status.CLOSED, is_manual=True, review_status=ReviewStatus.REJECTED)
        link_fleet = f.create_operation(
            self.fc, status=Status.ACTIVE, link_only=True, esi_fleet_id=None, review_status=ReviewStatus.PENDING
        )
        approved_fleet = f.create_operation(self.fc, status=Status.CLOSED)
        pending_row = f.add_attendance(
            approved_fleet, self.member, source=AttendanceRecord.Source.MANUAL, review_status=ReviewStatus.PENDING
        )
        # A pending row on a pending fleet waits as well.
        row_on_pending_fleet = f.add_attendance(
            pending_fleet, self.member, source=AttendanceRecord.Source.IMPORTED, review_status=ReviewStatus.PENDING
        )
        f.add_attendance(
            approved_fleet, self.fc, source=AttendanceRecord.Source.MANUAL, review_status=ReviewStatus.REJECTED
        )
        f.add_attendance(approved_fleet, f.create_user())

        self.assertEqual(pending_approval_count(), 4)
        self.assertEqual(set(pending_operations()), {pending_fleet, link_fleet})
        self.assertEqual(set(pending_attendance()), {pending_row, row_on_pending_fleet})
