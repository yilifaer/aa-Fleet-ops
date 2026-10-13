"""PAP changes that run while another request changes the same fleet, serialised by row locks (MariaDB, MySQL)."""

import threading
import time
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from fleetops.models import AttendanceRecord, FleetOperation, ReviewStatus
from fleetops.services import pap as pap_module
from fleetops.services import roster_import
from fleetops.services.approvals import approve_attendance
from fleetops.services.attendance import create_manual_attendance, set_operation_attendance_multiplier
from fleetops.services.operations import end_fleet
from fleetops.services.roster_import import import_rows

from . import factories as f

Status = FleetOperation.Status
MANAGED = AttendanceRecord.MANAGED_SOURCES


def run_together(*targets):
    """Run each target in its own thread with its own database connection; returns the errors."""
    errors = []

    def run(target):
        try:
            target()
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(target,)) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return errors


@skipUnlessDBFeature("has_select_for_update")
class ConcurrencyTestCase(TransactionTestCase):
    # Brings back the migrated rows (permissions, default templates) a flush removes.
    serialized_rollback = True

    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Fleet Boss")
        self.lead = f.create_user(perms=f.FC_LEAD_PERMS, main_name="Fleet Lead")
        self.pilot = f.create_user(main_name="Line Pilot")
        self.main = f.main_of(self.pilot)

    def closing_fleet(self):
        """A running fleet with the pilot tracked, and an end that pauses while it holds the fleet's lock."""
        operation = f.create_operation(self.fc, started_at=timezone.now() - timedelta(minutes=40))
        f.member_state(operation, self.main, accumulated_seconds=1800, active=False)
        holding = self.pause_in_the_recalculation()
        sync = mock.patch("fleetops.services.operations.sync_operation")
        sync.start()
        self.addCleanup(sync.stop)

        def end():
            end_fleet(FleetOperation.objects.get(pk=operation.pk), actor=self.fc)

        return operation, end, holding

    def pause_in_the_recalculation(self):
        """The first PAP calculation pauses while it holds the fleet's lock; returns the event it sets then."""
        holding = threading.Event()
        original = pap_module.compute_pap
        calls = []

        def compute_and_pause(*args, **kwargs):
            plan = original(*args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                holding.set()
                # The other request starts now and waits for the lock.
                time.sleep(1)
            return plan

        patcher = mock.patch.object(pap_module, "compute_pap", side_effect=compute_and_pause)
        patcher.start()
        self.addCleanup(patcher.stop)
        return holding


class ConcurrentImportTests(ConcurrencyTestCase):
    """The same roster preview imported twice at once (a double click on Import)."""

    def import_twice(self, operation):
        pilots = [f.main_of(f.create_user()) for _ in range(5)]
        rows = [{"character_id": c.character_id, "character_name": c.character_name, "status": "add"} for c in pilots]
        together = threading.Barrier(2, timeout=10)
        original = roster_import._import_row
        results = []

        def import_row_together(row):
            if row is rows[0]:
                together.wait()
            return original(row)

        def run():
            results.append(
                import_rows(
                    FleetOperation.objects.get(pk=operation.pk),
                    rows,
                    actor=self.lead,
                    attendance_value=1,
                    duplicate_action="keep",
                    notes="",
                )
            )

        with mock.patch.object(roster_import, "_import_row", side_effect=import_row_together):
            errors = run_together(run, run)
        return pilots, results, errors

    def test_an_active_fleet_gets_each_pilot_once(self):
        operation = f.create_operation(self.fc)

        pilots, results, errors = self.import_twice(operation)

        self.assertEqual(errors, [])
        self.assertEqual(AttendanceRecord.objects.filter(operation=operation).count(), len(pilots))
        self.assertEqual(sorted(len(result.created) for result in results), [0, len(pilots)])

    def test_a_closed_fleet_gets_each_pilot_once(self):
        operation = f.create_operation(self.fc, status=Status.CLOSED)

        pilots, results, errors = self.import_twice(operation)

        self.assertEqual(errors, [])
        self.assertEqual(
            AttendanceRecord.objects.filter(operation=operation, granted=True).count(), len(pilots)
        )


class ChangesDuringTheCloseTests(ConcurrencyTestCase):
    """A manual PAP row or an approval that arrives while the fleet closes."""

    def test_a_replacement_added_while_the_fleet_closes_is_applied(self):
        operation, end, holding = self.closing_fleet()
        # The fleet page was loaded while the fleet was still running.
        stale = FleetOperation.objects.get(pk=operation.pk)

        def add_replacement():
            holding.wait(10)
            create_manual_attendance(
                stale, actor=self.lead, character_id=self.main.character_id, attendance_value=5, duplicate_action="replace"
            )

        errors = run_together(end, add_replacement)

        self.assertEqual(errors, [])
        rows = {row.source: row for row in AttendanceRecord.objects.filter(operation=operation)}
        self.assertEqual((rows["automatic"].granted, rows["automatic"].replaced), (False, True))
        self.assertEqual((rows["manual"].granted, rows["manual"].attendance_value), (True, 5))

    def test_a_roster_import_while_the_fleet_closes_is_applied(self):
        operation, end, holding = self.closing_fleet()
        stale = FleetOperation.objects.get(pk=operation.pk)
        # The FC lead ticked the tracked pilot ("Has PAP") to replace the tracked PAP.
        rows = [{"character_id": self.main.character_id, "character_name": self.main.character_name, "status": "has_pap"}]

        def import_replacement():
            holding.wait(10)
            import_rows(stale, rows, actor=self.lead, attendance_value=5, duplicate_action="replace", notes="")

        errors = run_together(end, import_replacement)

        self.assertEqual(errors, [])
        rows = {row.source: row for row in AttendanceRecord.objects.filter(operation=operation)}
        self.assertEqual((rows["automatic"].granted, rows["automatic"].replaced), (False, True))
        self.assertEqual((rows["imported"].granted, rows["imported"].attendance_value), (True, 5))

    def test_an_approval_while_the_fleet_closes_waits_for_it(self):
        operation, end, holding = self.closing_fleet()
        other = f.create_user(main_name="Late Pilot")
        pending = f.add_attendance(
            operation, other, source="manual", review_status=ReviewStatus.PENDING, granted=False
        )

        def approve():
            holding.wait(10)
            approve_attendance([pending.pk], actor=self.lead)

        errors = run_together(end, approve)

        self.assertEqual(errors, [])
        pending.refresh_from_db()
        self.assertEqual((pending.review_status, pending.granted), (ReviewStatus.APPROVED, True))
        self.assertEqual(FleetOperation.objects.get(pk=operation.pk).status, Status.CLOSED)
        self.assertTrue(AttendanceRecord.objects.get(operation=operation, source__in=MANAGED).granted)

    def test_an_approval_while_the_multiplier_changes_waits_for_it(self):
        # Every recalculation of a closed fleet locks the fleet before its rows.
        operation = f.create_operation(self.fc, status=Status.CLOSED)
        f.member_state(operation, self.main, accumulated_seconds=1800, active=False)
        other = f.create_user(main_name="Late Pilot")
        pending = f.add_attendance(
            operation, other, source="manual", review_status=ReviewStatus.PENDING, granted=False
        )
        holding = self.pause_in_the_recalculation()

        def change():
            set_operation_attendance_multiplier(
                FleetOperation.objects.get(pk=operation.pk), 2, actor=self.lead, source="override", reason="Long fight"
            )

        def approve():
            holding.wait(10)
            approve_attendance([pending.pk], actor=self.lead)

        errors = run_together(change, approve)

        self.assertEqual(errors, [])
        pending.refresh_from_db()
        self.assertEqual((pending.review_status, pending.granted), (ReviewStatus.APPROVED, True))
        tracked = AttendanceRecord.objects.get(operation=operation, source__in=MANAGED)
        self.assertEqual((tracked.granted, tracked.attendance_value), (True, 2))
