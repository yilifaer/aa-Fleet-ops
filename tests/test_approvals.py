# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FC lead approval of manual fleets and manual PAP: the services, the approvals page and the fleet page."""

from datetime import timedelta
from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import AttendanceRecord, AuditLog, FleetOperation, FleetOpsSettings, ReviewStatus
from fleetops.services.approvals import (
    approve_attendance,
    approve_operation,
    reject_attendance,
    reject_operation,
)
from fleetops.services.counting import counted_operations, evaluate_stats_inclusion
from fleetops.services.pap import finalize_pap
from fleetops.services.roster_import import import_rows
from fleetops.services.statistics import fc_statistics

from . import factories as f

Status = FleetOperation.Status
MANUAL = AttendanceRecord.Source.MANUAL
IMPORTED = AttendanceRecord.Source.IMPORTED
PENDING = ReviewStatus.PENDING
APPROVED = ReviewStatus.APPROVED
REJECTED = ReviewStatus.REJECTED


def message_texts(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def last_message(response):
    # Messages pile up over several posts without a page in between.
    return message_texts(response)[-1]


class ApprovalTestCase(TestCase):
    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.pilot = f.create_user("pilot", perms=f.MEMBER_PERMS)
        self.other_pilot = f.create_user("other_pilot", perms=f.MEMBER_PERMS)
        patcher = mock.patch("fleetops.services.pap.refresh_after_change")
        self.refresh = patcher.start()
        self.addCleanup(patcher.stop)

    def manual_fleet(self, **extra):
        values = dict(status=Status.CLOSED, is_manual=True, esi_fleet_id=None, review_status=PENDING)
        values.update(extra)
        return f.create_operation(self.fc, **values)

    def manual_row(self, operation, user, *, review_status=PENDING, source=MANUAL, **extra):
        return f.add_attendance(operation, user, source=source, review_status=review_status, created_by=self.fc, **extra)


class OperationReviewServiceTests(ApprovalTestCase):
    def test_approving_a_fleet_approves_its_pending_rows(self):
        operation = self.manual_fleet()
        pending = self.manual_row(operation, self.pilot)
        imported = self.manual_row(operation, self.other_pilot, source=IMPORTED)
        rejected = self.manual_row(operation, self.fc, review_status=REJECTED)
        automatic = f.add_attendance(operation, self.lead)

        count = approve_operation(operation, actor=self.lead, note="Checked the roster")

        self.assertEqual(count, 2)
        operation.refresh_from_db()
        self.assertEqual(
            (operation.review_status, operation.reviewed_by, operation.review_note),
            (APPROVED, self.lead, "Checked the roster"),
        )
        self.assertIsNotNone(operation.reviewed_at)
        for row, status in ((pending, APPROVED), (imported, APPROVED), (rejected, REJECTED), (automatic, APPROVED)):
            row.refresh_from_db()
            self.assertEqual(row.review_status, status)
        pending.refresh_from_db()
        self.assertEqual((pending.reviewed_by, pending.reviewed_at), (self.lead, operation.reviewed_at))
        entry = AuditLog.objects.get(action="approval.operation_approve")
        self.assertEqual((entry.actor, entry.object_type, entry.object_id), (self.lead, "FleetOperation", str(operation.pk)))
        self.assertEqual(entry.old_value, {"review_status": "pending"})
        self.assertEqual(
            entry.new_value,
            {"review_status": "approved", "note": "Checked the roster", "attendance_ids": [pending.pk, imported.pk]},
        )
        self.refresh.assert_called_once()
        self.assertEqual(self.refresh.call_args.args[0].pk, operation.pk)
        self.assertEqual(self.refresh.call_args.kwargs["actor"], self.lead)

    def test_approving_only_the_fleet_keeps_the_rows_waiting(self):
        operation = self.manual_fleet()
        row = self.manual_row(operation, self.pilot)

        count = approve_operation(operation, actor=self.lead, include_attendance=False)

        self.assertEqual(count, 0)
        row.refresh_from_db()
        self.assertEqual(row.review_status, PENDING)
        self.assertEqual(AuditLog.objects.get(action="approval.operation_approve").new_value["attendance_ids"], [])

    def test_rejecting_needs_a_reason(self):
        operation = self.manual_fleet()

        for note in ("", "   "):
            with self.subTest(note=note), self.assertRaisesMessage(ValueError, "Give a reason for the rejection."):
                reject_operation(operation, actor=self.lead, note=note)

        operation.refresh_from_db()
        self.assertEqual(operation.review_status, PENDING)
        self.assertFalse(AuditLog.objects.exists())
        self.refresh.assert_not_called()

    def test_rejecting_a_fleet_rejects_its_pending_rows(self):
        operation = self.manual_fleet()
        row = self.manual_row(operation, self.pilot)
        approved = self.manual_row(operation, self.other_pilot, review_status=APPROVED)

        count = reject_operation(operation, actor=self.lead, note="Wrong date")

        self.assertEqual(count, 1)
        operation.refresh_from_db()
        row.refresh_from_db()
        approved.refresh_from_db()
        self.assertEqual((operation.review_status, operation.review_note), (REJECTED, "Wrong date"))
        self.assertEqual((row.review_status, row.review_note), (REJECTED, "Wrong date"))
        self.assertEqual(approved.review_status, APPROVED)
        entry = AuditLog.objects.get(action="approval.operation_reject")
        self.assertEqual(entry.new_value, {"review_status": "rejected", "note": "Wrong date", "attendance_ids": [row.pk]})
        self.refresh.assert_called_once()

    def test_rejecting_only_the_fleet(self):
        operation = self.manual_fleet()
        row = self.manual_row(operation, self.pilot)

        reject_operation(operation, actor=self.lead, note="Wrong date", include_attendance=False)

        row.refresh_from_db()
        self.assertEqual(row.review_status, PENDING)

    def test_a_rejected_fleet_can_be_approved_later(self):
        operation = self.manual_fleet()
        reject_operation(operation, actor=self.lead, note="Wrong date")

        approve_operation(operation, actor=self.lead, note="Date fixed")

        operation.refresh_from_db()
        self.assertEqual((operation.review_status, operation.review_note), (APPROVED, "Date fixed"))

    def test_decided_fleets_cannot_be_decided_again(self):
        approved = self.manual_fleet(review_status=APPROVED)
        rejected = self.manual_fleet(review_status=REJECTED)

        with self.assertRaisesMessage(ValueError, "This fleet is already approved."):
            approve_operation(approved, actor=self.lead)
        with self.assertRaisesMessage(ValueError, "Only fleets waiting for approval can be rejected."):
            reject_operation(approved, actor=self.lead, note="No")
        with self.assertRaisesMessage(ValueError, "Only fleets waiting for approval can be rejected."):
            reject_operation(rejected, actor=self.lead, note="No")
        self.assertFalse(AuditLog.objects.exists())

    def test_the_passed_operation_shows_the_decision(self):
        operation = self.manual_fleet()

        approve_operation(operation, actor=self.lead)

        self.assertEqual((operation.review_status, operation.reviewed_by), (APPROVED, self.lead))


class AttendanceReviewServiceTests(ApprovalTestCase):
    def test_rows_of_several_fleets_are_approved_with_one_audit_entry_per_fleet(self):
        first = self.manual_fleet(review_status=APPROVED)
        second = f.create_operation(self.fc, status=Status.CLOSED)
        a = self.manual_row(first, self.pilot)
        b = self.manual_row(first, self.other_pilot, source=IMPORTED)
        c = self.manual_row(second, self.pilot)
        automatic = f.add_attendance(second, self.other_pilot, review_status=PENDING)
        done = self.manual_row(second, self.fc, review_status=APPROVED)

        count = approve_attendance([a, b, c, automatic, done], actor=self.lead, note="ok")

        self.assertEqual(count, 3)
        for row, status in ((a, APPROVED), (b, APPROVED), (c, APPROVED), (automatic, PENDING), (done, APPROVED)):
            row.refresh_from_db()
            self.assertEqual(row.review_status, status)
        entries = {entry.object_id: entry for entry in AuditLog.objects.filter(action="approval.attendance_approve")}
        self.assertEqual(set(entries), {str(first.pk), str(second.pk)})
        self.assertEqual(entries[str(first.pk)].new_value, {"review_status": "approved", "note": "ok", "attendance_ids": [a.pk, b.pk]})
        self.assertEqual(entries[str(first.pk)].old_value, {"review_status": {str(a.pk): "pending", str(b.pk): "pending"}})
        self.assertEqual({call.args[0].pk for call in self.refresh.call_args_list}, {first.pk, second.pk})

    def test_ids_are_accepted_too(self):
        operation = self.manual_fleet(review_status=APPROVED)
        row = self.manual_row(operation, self.pilot)

        self.assertEqual(approve_attendance([row.pk], actor=self.lead), 1)

    def test_rejecting_rows_needs_a_reason_and_only_touches_pending_rows(self):
        operation = self.manual_fleet(review_status=APPROVED)
        pending = self.manual_row(operation, self.pilot)
        approved = self.manual_row(operation, self.other_pilot, review_status=APPROVED)

        with self.assertRaisesMessage(ValueError, "Give a reason for the rejection."):
            reject_attendance([pending], actor=self.lead, note="")
        count = reject_attendance([pending, approved], actor=self.lead, note="Was not there")

        self.assertEqual(count, 1)
        pending.refresh_from_db()
        approved.refresh_from_db()
        self.assertEqual((pending.review_status, pending.review_note), (REJECTED, "Was not there"))
        self.assertEqual(approved.review_status, APPROVED)
        self.assertEqual(AuditLog.objects.filter(action="approval.attendance_reject").count(), 1)

    def test_rejected_rows_can_be_approved(self):
        operation = self.manual_fleet(review_status=APPROVED)
        row = self.manual_row(operation, self.pilot, review_status=REJECTED)

        self.assertEqual(approve_attendance([row], actor=self.lead), 1)
        row.refresh_from_db()
        self.assertEqual(row.review_status, APPROVED)

    def test_nothing_to_change(self):
        self.assertEqual(approve_attendance([], actor=self.lead), 0)
        self.assertFalse(AuditLog.objects.exists())
        self.refresh.assert_not_called()


class ApprovalQueueViewTests(ApprovalTestCase):
    def get(self, user, url):
        self.client.force_login(user)
        return self.client.get(url)

    def post(self, user, url, data):
        self.client.force_login(user)
        return self.client.post(url, data)

    def test_only_approvers_open_the_queue(self):
        url = reverse("fleetops:approval_queue")
        for user in (self.fc, self.pilot):
            with self.subTest(user=user.username):
                self.assertEqual(self.get(user, url).status_code, 403)
        self.assertEqual(self.get(self.lead, url).status_code, 200)

    def test_only_approvers_decide(self):
        operation = self.manual_fleet()
        row = self.manual_row(operation, self.pilot)

        responses = [
            self.post(self.fc, reverse("fleetops:review_operation", args=[operation.uuid]), {"action": "approve"}),
            self.post(self.fc, reverse("fleetops:review_attendance"), {"action": "approve", "ids": [row.pk]}),
        ]

        self.assertEqual([response.status_code for response in responses], [403, 403])
        operation.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual((operation.review_status, row.review_status), (PENDING, PENDING))
        missing = "00000000-0000-0000-0000-000000000002"
        self.assertEqual(
            self.post(self.lead, reverse("fleetops:review_operation", args=[missing]), {"action": "approve"}).status_code,
            404,
        )

    def test_queue_lists_waiting_fleets_and_rows(self):
        waiting = self.manual_fleet(started_at=timezone.now() - timedelta(hours=3), ended_at=timezone.now() - timedelta(hours=1))
        self.manual_row(waiting, self.pilot)
        rejected = self.manual_fleet(review_status=REJECTED, formup="Amarr")
        self.manual_row(rejected, self.other_pilot, notes="Late login")
        approved = self.manual_fleet(review_status=APPROVED)
        self.manual_row(approved, self.pilot, review_status=APPROVED)

        response = self.get(self.lead, reverse("fleetops:approval_queue"))

        self.assertEqual([op.pk for op in response.context["pending_fleets"]], [waiting.pk])
        self.assertEqual(response.context["pending_fleets"][0].pending_rows, 1)
        self.assertEqual(response.context["pending_fleets"][0].duration_text, "2 h 00 min")
        groups = {group["operation"].pk: group["rows"] for group in response.context["row_groups"]}
        self.assertEqual(set(groups), {waiting.pk, rejected.pk})
        self.assertContains(response, "Fleet rejected")
        self.assertContains(response, "Late login")
        self.assertContains(response, reverse("fleetops:review_operation", args=[waiting.uuid]))
        self.assertNotContains(response, reverse("fleetops:review_operation", args=[approved.uuid]))
        # The navigation counts both waiting fleets and waiting rows.
        self.assertContains(response, f'href="{reverse("fleetops:approval_queue")}">Approvals (3)</a>')

    def test_empty_queue(self):
        response = self.get(self.lead, reverse("fleetops:approval_queue"))

        self.assertContains(response, "No fleets are waiting for approval.")
        self.assertContains(response, "No manual PAP is waiting for approval.")

    def test_approve_and_reject_a_fleet_from_the_queue(self):
        first = self.manual_fleet()
        self.manual_row(first, self.pilot)
        second = self.manual_fleet()

        approve = self.post(
            self.lead,
            reverse("fleetops:review_operation", args=[first.uuid]),
            {"action": "approve", "note": "", "include_attendance": "on"},
        )
        reject = self.post(
            self.lead, reverse("fleetops:review_operation", args=[second.uuid]), {"action": "reject", "note": "Duplicate"}
        )

        self.assertRedirects(approve, reverse("fleetops:approval_queue"), fetch_redirect_response=False)
        self.assertEqual(message_texts(approve), ["Fleet approved with 1 PAP row."])
        self.assertEqual(last_message(reject), "Fleet rejected.")
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.review_status, second.review_status), (APPROVED, REJECTED))
        self.assertFalse(first.attendance_records.filter(review_status=PENDING).exists())

    def test_unticked_box_approves_only_the_fleet(self):
        operation = self.manual_fleet()
        row = self.manual_row(operation, self.pilot)

        self.post(self.lead, reverse("fleetops:review_operation", args=[operation.uuid]), {"action": "approve"})

        row.refresh_from_db()
        self.assertEqual(row.review_status, PENDING)

    def test_reject_without_reason_is_refused(self):
        operation = self.manual_fleet()

        response = self.post(
            self.lead, reverse("fleetops:review_operation", args=[operation.uuid]), {"action": "reject", "note": " "}
        )

        self.assertEqual(message_texts(response), ["The fleet was not reviewed. Give a reason for the rejection."])
        operation.refresh_from_db()
        self.assertEqual(operation.review_status, PENDING)

    def test_decision_returns_to_the_fleet_page(self):
        operation = self.manual_fleet()
        fleet_page = reverse("fleetops:operation_detail", args=[operation.uuid])

        response = self.post(
            self.lead, reverse("fleetops:review_operation", args=[operation.uuid]), {"action": "approve", "next": fleet_page}
        )
        foreign = self.post(
            self.lead,
            reverse("fleetops:review_attendance"),
            {"action": "approve", "ids": ["1"], "next": "https://evil.example/"},
        )

        self.assertRedirects(response, fleet_page, fetch_redirect_response=False)
        self.assertRedirects(foreign, reverse("fleetops:approval_queue"), fetch_redirect_response=False)

    def test_bulk_review_of_rows(self):
        operation = self.manual_fleet(review_status=APPROVED)
        first = self.manual_row(operation, self.pilot)
        second = self.manual_row(operation, self.other_pilot)
        url = reverse("fleetops:review_attendance")

        nothing = self.post(self.lead, url, {"action": "approve"})
        no_reason = self.post(self.lead, url, {"action": "reject", "ids": [first.pk]})
        approve = self.post(self.lead, url, {"action": "approve", "ids": [first.pk, second.pk, "x"]})
        again = self.post(self.lead, url, {"action": "approve", "ids": [first.pk]})

        self.assertEqual(message_texts(nothing), ["Select at least one PAP row."])
        self.assertEqual(last_message(no_reason), "The PAP rows were not reviewed. Give a reason for the rejection.")
        self.assertEqual(last_message(approve), "2 PAP rows approved.")
        self.assertEqual(last_message(again), "No PAP rows were changed; they were already reviewed.")
        self.assertEqual(operation.attendance_records.filter(review_status=APPROVED).count(), 2)


class FleetPageReviewTests(ApprovalTestCase):
    def page(self, user, operation):
        self.client.force_login(user)
        response = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        self.assertEqual(response.status_code, 200)
        return response

    def test_pending_fleet_banner(self):
        operation = self.manual_fleet()
        self.manual_row(operation, self.pilot)
        review_url = reverse("fleetops:review_operation", args=[operation.uuid])

        fc_page = self.page(self.fc, operation)
        lead_page = self.page(self.lead, operation)

        for response in (fc_page, lead_page):
            self.assertContains(response, "Pending FC lead approval")
        self.assertNotContains(fc_page, review_url)
        self.assertContains(fc_page, "1 manual PAP row of this fleet waits for FC lead approval.")
        self.assertContains(lead_page, review_url)
        self.assertContains(lead_page, "Reject fleet")
        self.assertContains(lead_page, "With its PAP rows waiting for approval (1)")
        self.assertContains(lead_page, "Manual PAP waiting for review")

    def test_rejected_fleet_banner(self):
        operation = self.manual_fleet(review_status=REJECTED, review_note="Wrong date", reviewed_by=self.lead,
                                      reviewed_at=timezone.now())
        f.add_attendance(operation, self.pilot)

        fc_page = self.page(self.fc, operation)
        lead_page = self.page(self.lead, operation)
        member_page = self.page(self.pilot, operation)

        self.assertContains(fc_page, "Rejected by an FC lead</strong>: Wrong date")
        self.assertContains(fc_page, "Correct the fleet with Edit Operation to send it back for approval.")
        self.assertContains(lead_page, "Approve fleet")
        self.assertNotContains(lead_page, "Reject fleet")
        self.assertContains(member_page, "Rejected by an FC lead")
        self.assertNotContains(member_page, "Wrong date")

    def test_approved_fleets_have_no_banner(self):
        operation = f.create_operation(self.fc, status=Status.CLOSED)

        self.assertNotContains(self.page(self.lead, operation), 'id="fleet-review"')

    def test_approvers_review_rows_on_the_fleet_page(self):
        operation = f.create_operation(self.fc, status=Status.CLOSED)
        pending = self.manual_row(operation, self.pilot)
        rejected = self.manual_row(operation, self.other_pilot, review_status=REJECTED, review_note="Not seen")
        self.manual_row(operation, self.fc, review_status=APPROVED)

        response = self.page(self.lead, operation)

        self.assertEqual([row.pk for row in response.context["review_rows"]], [rejected.pk, pending.pk])
        self.assertContains(response, f'name="ids" value="{pending.pk}" checked')
        self.assertContains(response, f'name="ids" value="{rejected.pk}" aria-label')
        self.assertContains(response, "Not seen")


class EditReviewTests(ApprovalTestCase):
    def setUp(self):
        super().setUp()
        start = (timezone.now() - timedelta(days=2)).replace(second=0, microsecond=0)
        self.start = start
        self.operation = self.manual_fleet(review_status=APPROVED, started_at=start, ended_at=start + timedelta(hours=2))

    def edit(self, user, operation=None, **changes):
        operation = operation or self.operation
        data = {
            "fleet_type": operation.fleet_type_id,
            "doctrine_name": operation.doctrine_name,
            "formup": operation.formup,
            "additional_message": "",
            "started_at": operation.started_at.strftime("%Y-%m-%dT%H:%M"),
            "ended_at": operation.ended_at.strftime("%Y-%m-%dT%H:%M"),
        }
        data.update(changes)
        self.client.force_login(user)
        return self.client.post(reverse("fleetops:edit_operation", args=[operation.uuid]), data)

    def test_fc_changing_the_times_sends_the_fleet_back_to_review(self):
        later = (self.start + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")

        response = self.edit(self.fc, ended_at=later)

        self.assertEqual(
            message_texts(response),
            ["Fleet operation updated. Changes need FC lead approval; the fleet does not count until then."],
        )
        self.operation.refresh_from_db()
        self.assertEqual((self.operation.review_status, self.operation.reviewed_by), (PENDING, None))
        entry = AuditLog.objects.get(action="operation.edit")
        self.assertEqual((entry.old_value["review_status"], entry.new_value["review_status"]), ("approved", "pending"))

    def test_fc_changing_the_fleet_type_sends_the_fleet_back_to_review(self):
        self.edit(self.fc, fleet_type=f.fleet_type("Roam").pk)

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.review_status, PENDING)

    def test_other_changes_keep_the_approval(self):
        response = self.edit(self.fc, formup="Amarr", doctrine_name="Ferox")

        self.assertEqual(message_texts(response), ["Fleet operation updated."])
        self.operation.refresh_from_db()
        self.assertEqual((self.operation.review_status, self.operation.formup), (APPROVED, "Amarr"))

    def test_approvers_keep_the_approval(self):
        self.edit(self.lead, ended_at=(self.start + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M"))

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.review_status, APPROVED)

    def test_new_times_of_tracked_fleets_go_back_to_review_too(self):
        # The multiplier tiers and the minimum fleet duration are measured from the times.
        tracked = f.create_operation(self.fc, status=Status.CLOSED, started_at=self.start)

        self.edit(self.fc, operation=tracked, ended_at=(self.start + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M"))

        tracked.refresh_from_db()
        self.assertEqual(tracked.review_status, PENDING)

    def test_tracked_fleets_keep_the_approval_for_a_new_fleet_type(self):
        tracked = f.create_operation(self.fc, status=Status.CLOSED, started_at=self.start)

        self.edit(self.fc, operation=tracked, fleet_type=f.fleet_type("Roam").pk)

        tracked.refresh_from_db()
        self.assertEqual(tracked.review_status, APPROVED)

    def test_link_only_and_rejected_fleets_go_back_to_review(self):
        link_only = f.create_operation(
            self.fc, status=Status.CLOSED, link_only=True, esi_fleet_id=None, started_at=self.start
        )
        rejected = self.manual_fleet(review_status=REJECTED, started_at=self.start, ended_at=self.start + timedelta(hours=1))
        later = (self.start + timedelta(hours=2, minutes=30)).strftime("%Y-%m-%dT%H:%M")

        self.edit(self.fc, operation=link_only, ended_at=later)
        self.edit(self.fc, operation=rejected, ended_at=later)

        link_only.refresh_from_db()
        rejected.refresh_from_db()
        self.assertEqual((link_only.review_status, rejected.review_status), (PENDING, PENDING))

    def test_time_changes_evaluate_the_statistics_rule(self):
        with mock.patch("fleetops.views.evaluate_stats_inclusion") as evaluate:
            self.edit(self.fc, formup="Amarr")
            evaluate.assert_not_called()
            self.edit(self.lead, ended_at=(self.start + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M"))

        evaluate.assert_called_once()
        self.assertEqual(evaluate.call_args.args[0].pk, self.operation.pk)


class TrackedTimeEditTests(TestCase):
    """An FC cannot raise the multiplier allowance or make a short fleet count by editing its times."""

    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.pilot = f.create_user("pilot", perms=f.MEMBER_PERMS)
        self.start = (timezone.now() - timedelta(days=2)).replace(second=0, microsecond=0)

    def tracked(self, minutes, **extra):
        ended_at = self.start + timedelta(minutes=minutes)
        operation = f.create_operation(self.fc, status=Status.CLOSED, started_at=self.start, ended_at=ended_at, **extra)
        f.member_state(operation, f.main_of(self.pilot), accumulated_seconds=minutes * 60, active=False)
        finalize_pap(operation)
        return operation

    def post(self, user, name, operation, data):
        self.client.force_login(user)
        return self.client.post(reverse(f"fleetops:{name}", args=[operation.uuid]), data)

    def edit(self, user, operation, *, started_at=None, ended_at=None):
        operation.refresh_from_db()
        started_at = started_at or operation.started_at
        ended_at = ended_at or operation.ended_at
        return self.post(
            user,
            "edit_operation",
            operation,
            {
                "fleet_type": operation.fleet_type_id,
                "doctrine_name": operation.doctrine_name,
                "formup": operation.formup,
                "additional_message": "",
                "started_at": started_at.strftime("%Y-%m-%dT%H:%M"),
                "ended_at": ended_at.strftime("%Y-%m-%dT%H:%M") if ended_at else "",
            },
        )

    def set_multiplier(self, user, operation, value):
        return self.post(user, "set_attendance_multiplier", operation, {"attendance_multiplier": value})

    def counts(self, operation):
        return counted_operations().filter(pk=operation.pk).exists()

    def test_stretched_times_need_an_fc_lead_before_a_higher_multiplier_counts(self):
        operation = self.tracked(60)
        self.set_multiplier(self.fc, operation, 3)
        operation.refresh_from_db()
        self.assertEqual(operation.attendance_multiplier, 1)

        self.edit(self.fc, operation, started_at=self.start - timedelta(hours=3))
        self.set_multiplier(self.fc, operation, 3)

        operation.refresh_from_db()
        self.assertEqual((operation.attendance_multiplier, operation.review_status), (3, PENDING))
        self.assertFalse(self.counts(operation))
        self.assertEqual(fc_statistics(self.fc, self.start.year, self.start.month)["fleet_count"], 0)
        self.client.force_login(self.lead)
        queue = self.client.get(reverse("fleetops:approval_queue"))
        self.assertContains(queue, "Times edited")
        self.assertContains(queue, "PAP 3x")

    def test_times_put_back_after_choosing_lower_the_multiplier_again(self):
        operation = self.tracked(30)
        self.edit(self.fc, operation, started_at=self.start - timedelta(hours=4))
        self.set_multiplier(self.fc, operation, 3)

        response = self.edit(self.fc, operation, started_at=self.start)

        operation.refresh_from_db()
        self.assertEqual((operation.attendance_multiplier, operation.multiplier_source), (1, "fc"))
        self.assertEqual(AttendanceRecord.objects.get(operation=operation).attendance_value, 1)
        self.assertIn("PAP multiplier lowered to 1x: the new times do not allow more.", message_texts(response))
        entry = AuditLog.objects.filter(action="attendance.multiplier").latest("pk")
        self.assertEqual((entry.old_value["attendance_multiplier"], entry.new_value["attendance_multiplier"]), (3, 1))
        self.assertIn("Fleet times edited", entry.reason)

    def test_shorter_times_lower_the_multiplier_to_the_highest_allowed(self):
        operation = self.tracked(200, attendance_multiplier=3, multiplier_source="fc")

        self.edit(self.fc, operation, ended_at=self.start + timedelta(minutes=100))

        operation.refresh_from_db()
        self.assertEqual(operation.attendance_multiplier, 2)

    def test_an_fc_lead_override_is_kept(self):
        operation = self.tracked(200, attendance_multiplier=3, multiplier_source="override")

        self.edit(self.fc, operation, ended_at=self.start + timedelta(minutes=60))

        operation.refresh_from_db()
        self.assertEqual((operation.attendance_multiplier, operation.review_status), (3, PENDING))

    def test_fc_leads_edit_times_without_review_or_lowering(self):
        operation = self.tracked(200, attendance_multiplier=3, multiplier_source="fc")

        self.edit(self.lead, operation, ended_at=self.start + timedelta(minutes=60))

        operation.refresh_from_db()
        self.assertEqual((operation.attendance_multiplier, operation.review_status), (3, APPROVED))

    def test_a_start_moved_back_on_a_running_fleet_needs_an_fc_lead(self):
        operation = f.create_operation(self.fc, started_at=timezone.now() - timedelta(minutes=40))
        self.post(self.fc, "end_fleet", operation, {"attendance_multiplier": "3"})
        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.ACTIVE)

        self.edit(self.fc, operation, started_at=operation.started_at - timedelta(hours=3))
        with mock.patch("fleetops.services.operations.sync_operation"):
            self.post(self.fc, "end_fleet", operation, {"attendance_multiplier": "3"})

        operation.refresh_from_db()
        self.assertEqual((operation.status, operation.attendance_multiplier), (Status.CLOSED, 3))
        self.assertEqual(operation.review_status, PENDING)
        self.assertFalse(self.counts(operation))

    def test_a_short_fleet_does_not_start_counting_without_an_fc_lead(self):
        settings = FleetOpsSettings.get_solo()
        settings.minimum_fleet_minutes = 60
        settings.save()
        operation = self.tracked(10)
        evaluate_stats_inclusion(operation)
        operation.save()
        self.assertFalse(operation.counts_for_stats)

        self.edit(self.fc, operation, ended_at=self.start + timedelta(minutes=70))

        operation.refresh_from_db()
        self.assertEqual((operation.counts_for_stats, operation.review_status), (True, PENDING))
        self.assertFalse(self.counts(operation))


class ManualRowDeleteTests(ApprovalTestCase):
    def test_imported_rows_are_deleted_like_manual_rows_and_pap_is_recalculated(self):
        operation = f.create_operation(self.fc, status=Status.CLOSED)
        imported = self.manual_row(operation, self.pilot, source=IMPORTED, review_status=REJECTED)
        self.client.force_login(self.fc)

        response = self.client.post(reverse("fleetops:delete_manual_attendance", args=[imported.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(AttendanceRecord.objects.filter(pk=imported.pk).exists())
        self.assertTrue(AuditLog.objects.filter(action="attendance.manual_delete", actor=self.fc).exists())
        self.refresh.assert_called_once()
        self.assertEqual(self.refresh.call_args.args[0].pk, operation.pk)

    def delete(self, user, row):
        self.client.force_login(user)
        return self.client.post(reverse("fleetops:delete_manual_attendance", args=[row.pk]))

    def test_fcs_cannot_delete_approved_corrections(self):
        # Deleting a lead's "replace" row would give the tracked PAP back without an FC lead.
        operation = f.create_operation(self.fc, status=Status.CLOSED)
        leads_row = f.add_attendance(operation, self.pilot, source=MANUAL, correction="replace", created_by=self.lead)
        own_approved = self.manual_row(operation, self.other_pilot, review_status=APPROVED)

        for row in (leads_row, own_approved):
            with self.subTest(row=row.pk):
                self.assertEqual(self.delete(self.fc, row).status_code, 404)
                self.assertTrue(AttendanceRecord.objects.filter(pk=row.pk).exists())
        self.refresh.assert_not_called()
        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        self.assertNotContains(page, "Delete</button>")
        listing = self.client.get(reverse("fleetops:manual_attendance"))
        self.assertNotContains(listing, "Delete</button>")

    def test_fcs_delete_their_own_rows_until_they_are_approved(self):
        operation = f.create_operation(self.fc, status=Status.CLOSED)
        pending = self.manual_row(operation, self.pilot)
        self.client.force_login(self.fc)
        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        self.assertContains(page, reverse("fleetops:delete_manual_attendance", args=[pending.pk]))

        self.assertEqual(self.delete(self.fc, pending).status_code, 302)

        self.assertFalse(AttendanceRecord.objects.filter(pk=pending.pk).exists())

    def test_fc_leads_delete_any_row(self):
        operation = f.create_operation(self.fc, status=Status.CLOSED)
        approved = self.manual_row(operation, self.pilot, review_status=APPROVED)

        self.assertEqual(self.delete(self.lead, approved).status_code, 302)

        self.assertFalse(AttendanceRecord.objects.filter(pk=approved.pk).exists())

    def test_manual_attendance_page_lists_the_review_state(self):
        operation = f.create_operation(self.fc, status=Status.CLOSED)
        self.manual_row(operation, self.pilot, review_status=REJECTED, review_note="Never in fleet")
        self.manual_row(operation, self.other_pilot, source=IMPORTED)
        self.client.force_login(self.fc)

        response = self.client.get(reverse("fleetops:manual_attendance"))

        self.assertEqual(len(response.context["recent_manual"]), 2)
        self.assertContains(response, "Never in fleet")
        self.assertContains(response, '<span class="badge bg-warning text-dark">Pending</span>')
        self.assertContains(response, "Manual PAP you add here counts once an FC lead approves it.")


class ReviewOnCreateTests(TestCase):
    """Entries of FCs wait for an FC lead; an FC lead's own entries are approved at once."""

    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.pilot = f.create_user("pilot", perms=f.MEMBER_PERMS)

    def create_manual_fleet(self, user):
        start = timezone.now() - timedelta(days=1)
        self.client.force_login(user)
        response = self.client.post(
            reverse("fleetops:manual_fleet"),
            {
                "fleet_type": f.fleet_type().pk,
                "formup": "Jita",
                "started_at": start.strftime("%Y-%m-%dT%H:%M"),
                "ended_at": (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
                "attendance_multiplier": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        return FleetOperation.objects.filter(created_by=user).latest("pk"), response

    def test_manual_fleets(self):
        by_fc, response = self.create_manual_fleet(self.fc)
        by_lead, _response = self.create_manual_fleet(self.lead)

        self.assertEqual((by_fc.review_status, by_fc.reviewed_by), (PENDING, None))
        self.assertEqual((by_lead.review_status, by_lead.reviewed_by), (APPROVED, self.lead))
        self.assertIn("It counts once an FC lead approves it.", message_texts(response)[0])

    def test_manual_rows(self):
        operation = f.create_operation(self.fc, status=Status.CLOSED)
        payload = {
            "character_id": f.main_of(self.pilot).character_id,
            "attendance_value": 1,
            "duplicate_action": "keep",
        }
        url = reverse("fleetops:add_manual_attendance", args=[operation.uuid])

        self.client.force_login(self.fc)
        response = self.client.post(url, payload)
        self.client.force_login(self.lead)
        self.client.post(url, payload)

        rows = list(operation.attendance_records.filter(source=MANUAL).order_by("pk"))
        self.assertEqual([(row.created_by, row.review_status) for row in rows], [(self.fc, PENDING), (self.lead, APPROVED)])
        self.assertEqual(message_texts(response), ["Manual PAP saved. It counts once an FC lead approves it."])

    def test_roster_imports(self):
        operation = f.create_operation(self.fc, status=Status.CLOSED)
        row = {"character_id": f.main_of(self.pilot).character_id, "character_name": "pilot Main", "status": "add"}

        by_fc = import_rows(operation, [row], actor=self.fc, attendance_value=1, duplicate_action="keep", notes="")
        by_lead = import_rows(
            f.create_operation(self.fc, status=Status.CLOSED),
            [row],
            actor=self.lead,
            attendance_value=1,
            duplicate_action="keep",
            notes="",
        )

        self.assertEqual([record.review_status for record in by_fc.created], [PENDING])
        self.assertEqual([record.review_status for record in by_lead.created], [APPROVED])
