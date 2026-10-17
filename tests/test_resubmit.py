# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Sending a rejected fleet back for approval: the service, the button on the fleet page and what the approvers see."""

from datetime import timedelta
from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone

from fleetops.models import AttendanceRecord, AuditLog, FleetOperation, ReviewStatus
from fleetops.services.approvals import approve_operation, reject_operation, resubmit_operation

from . import factories as f

Status = FleetOperation.Status
MANUAL = AttendanceRecord.Source.MANUAL
PENDING = ReviewStatus.PENDING
APPROVED = ReviewStatus.APPROVED
REJECTED = ReviewStatus.REJECTED


def message_texts(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class ResubmitTestCase(TestCase):
    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.other_fc = f.create_user("other_fc", perms=f.FC_PERMS)
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.member = f.create_user("member", perms=f.MEMBER_PERMS)
        self.pilot = f.create_user("pilot", perms=f.MEMBER_PERMS)
        self.other_pilot = f.create_user("other_pilot", perms=f.MEMBER_PERMS)
        self.start = timezone.now() - timedelta(days=2)
        self.operation = f.create_operation(
            self.fc,
            status=Status.CLOSED,
            is_manual=True,
            esi_fleet_id=None,
            started_at=self.start,
            ended_at=self.start + timedelta(hours=1),
            review_status=PENDING,
        )
        # The lead rejects the fleet with its pending row; another row was rejected on its own earlier.
        self.with_fleet = f.add_attendance(
            self.operation, self.pilot, source=MANUAL, review_status=PENDING, created_by=self.fc, granted=False
        )
        self.alone = f.add_attendance(
            self.operation,
            self.other_pilot,
            source=MANUAL,
            review_status=REJECTED,
            reviewed_at=self.start,
            review_note="Not there",
            created_by=self.fc,
            granted=False,
        )
        reject_operation(self.operation, actor=self.lead, note="Wrong date")
        self.operation.refresh_from_db()

    def resubmit_url(self, operation=None):
        return reverse("fleetops:resubmit_operation", args=[(operation or self.operation).uuid])

    def post(self, user, data=None, operation=None):
        self.client.force_login(user)
        return self.client.post(self.resubmit_url(operation), data or {})


class ResubmitServiceTests(ResubmitTestCase):
    def test_a_rejected_fleet_goes_back_to_pending_with_the_rows_rejected_with_it(self):
        count = resubmit_operation(self.operation, actor=self.fc, note="Date corrected")

        self.assertEqual(count, 1)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.review_status, PENDING)
        self.assertEqual((self.operation.reviewed_by, self.operation.reviewed_at), (None, None))
        # The rejection reason stays readable; the new note is kept next to it.
        self.assertEqual((self.operation.review_note, self.operation.review_request_note), ("Wrong date", "Date corrected"))
        self.with_fleet.refresh_from_db()
        self.alone.refresh_from_db()
        self.assertEqual((self.with_fleet.review_status, self.with_fleet.reviewed_by, self.with_fleet.reviewed_at), (PENDING, None, None))
        # The row that was rejected on its own stays rejected.
        self.assertEqual(self.alone.review_status, REJECTED)

    def test_only_the_fleet_goes_back_when_the_rows_are_left_out(self):
        count = resubmit_operation(self.operation, actor=self.fc, include_attendance=False)

        self.assertEqual(count, 0)
        self.with_fleet.refresh_from_db()
        self.assertEqual(self.with_fleet.review_status, REJECTED)

    def test_the_audit_entry_keeps_the_old_status_and_the_note(self):
        resubmit_operation(self.operation, actor=self.fc, note="Date corrected")

        entry = AuditLog.objects.get(action="approval.operation_resubmit")
        self.assertEqual((entry.actor, entry.object_type, entry.object_id), (self.fc, "FleetOperation", str(self.operation.pk)))
        self.assertEqual(entry.old_value, {"review_status": "rejected", "review_note": "Wrong date"})
        self.assertEqual(
            entry.new_value,
            {"review_status": "pending", "note": "Date corrected", "attendance_ids": [self.with_fleet.pk]},
        )

    def test_the_note_is_cut_to_the_length_of_the_field(self):
        resubmit_operation(self.operation, actor=self.fc, note="x" * 400)

        self.operation.refresh_from_db()
        self.assertEqual(len(self.operation.review_request_note), 255)

    def test_only_a_rejected_fleet_can_be_sent_back(self):
        for status in (PENDING, APPROVED):
            with self.subTest(status=status):
                fleet = f.create_operation(self.fc, status=Status.CLOSED, review_status=status)
                with self.assertRaisesMessage(ValueError, "Only rejected fleets can be sent back for approval."):
                    resubmit_operation(fleet, actor=self.fc)
                fleet.refresh_from_db()
                self.assertEqual(fleet.review_status, status)
        self.assertFalse(AuditLog.objects.filter(action="approval.operation_resubmit").exists())

    def test_a_second_resubmission_is_refused(self):
        resubmit_operation(self.operation, actor=self.fc)

        with self.assertRaises(ValueError):
            resubmit_operation(self.operation, actor=self.fc)

    def test_a_decision_clears_the_note(self):
        resubmit_operation(self.operation, actor=self.fc, note="Date corrected")

        approve_operation(self.operation, actor=self.lead)

        self.operation.refresh_from_db()
        self.assertEqual((self.operation.review_status, self.operation.review_request_note), (APPROVED, ""))
        resubmit_operation_again = f.create_operation(
            self.fc, status=Status.CLOSED, review_status=REJECTED, review_note="No", review_request_note=""
        )
        resubmit_operation(resubmit_operation_again, actor=self.fc, note="Again")
        reject_operation(resubmit_operation_again, actor=self.lead, note="Still wrong")
        resubmit_operation_again.refresh_from_db()
        self.assertEqual(resubmit_operation_again.review_request_note, "")

    def test_the_pap_of_a_closed_fleet_is_calculated_again_and_the_month_follows(self):
        with mock.patch("fleetops.services.pap.refresh_after_change") as refresh, mock.patch(
            "fleetops.services.incentives.refresh_months_after_commit"
        ) as months:
            resubmit_operation(self.operation, actor=self.fc)

        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[0].pk, self.operation.pk)
        months.assert_called_once()
        self.assertEqual(sorted(months.call_args.args), [(self.start.year, self.start.month)])


class ResubmitViewTests(ResubmitTestCase):
    def test_a_manager_sends_the_fleet_back_with_a_note(self):
        response = self.post(self.fc, {"note": "Date corrected", "include_attendance": "on"})

        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[self.operation.uuid]), fetch_redirect_response=False
        )
        self.operation.refresh_from_db()
        self.assertEqual((self.operation.review_status, self.operation.review_request_note), (PENDING, "Date corrected"))
        self.assertEqual(message_texts(response), ["Fleet sent back for approval with 1 PAP row."])
        self.with_fleet.refresh_from_db()
        self.assertEqual(self.with_fleet.review_status, PENDING)

    def test_the_note_is_optional_and_the_box_decides_about_the_rows(self):
        response = self.post(self.fc, {})

        self.operation.refresh_from_db()
        self.assertEqual((self.operation.review_status, self.operation.review_request_note), (PENDING, ""))
        self.assertEqual(message_texts(response), ["Fleet sent back for approval."])
        self.with_fleet.refresh_from_db()
        self.assertEqual(self.with_fleet.review_status, REJECTED)

    def test_a_fleet_that_is_not_rejected_gives_an_error_and_changes_nothing(self):
        fleet = f.create_operation(self.fc, status=Status.CLOSED, review_status=PENDING)

        response = self.post(self.fc, {"note": "Please"}, operation=fleet)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(message_texts(response), ["Only rejected fleets can be sent back for approval."])
        fleet.refresh_from_db()
        self.assertEqual((fleet.review_status, fleet.review_request_note), (PENDING, ""))

    def test_who_may_send_a_fleet_back(self):
        # Another FC does not manage this fleet, a member holds no permission to manage fleets at all.
        self.assertEqual(self.post(self.other_fc).status_code, 404)
        self.assertEqual(self.post(self.member).status_code, 403)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.review_status, REJECTED)
        # An FC lead manages every fleet.
        self.assertEqual(self.post(self.lead).status_code, 302)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.review_status, PENDING)

    def test_only_post_is_accepted(self):
        self.client.force_login(self.fc)

        self.assertEqual(self.client.get(self.resubmit_url()).status_code, 405)

    def test_an_edit_never_sends_a_rejected_fleet_back(self):
        self.client.force_login(self.fc)
        start = self.operation.started_at.replace(second=0, microsecond=0)

        for changes in (
            {"formup": "Amarr"},
            {"doctrine_name": "Ferox"},
            {"additional_message": "Notes"},
            {"ended_at": (start + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")},
            {"started_at": (start - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")},
        ):
            with self.subTest(changes=changes):
                data = {
                    "fleet_type": self.operation.fleet_type_id,
                    "doctrine_name": self.operation.doctrine_name,
                    "formup": self.operation.formup,
                    "additional_message": "",
                    "started_at": start.strftime("%Y-%m-%dT%H:%M"),
                    "ended_at": (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
                    **changes,
                }

                response = self.client.post(reverse("fleetops:edit_operation", args=[self.operation.uuid]), data)

                self.assertEqual(response.status_code, 302)
                self.operation.refresh_from_db()
                self.assertEqual(self.operation.review_status, REJECTED)


class ResubmitPageTests(ResubmitTestCase):
    def page(self, user, operation=None):
        self.client.force_login(user)
        return self.client.get(reverse("fleetops:operation_detail", args=[(operation or self.operation).uuid]))

    def test_the_manager_sees_the_reason_and_the_button(self):
        page = self.page(self.fc)

        self.assertContains(page, "Rejected by an FC lead</strong>: Wrong date")
        self.assertContains(page, 'id="fleet-resubmit-form"')
        self.assertContains(page, reverse("fleetops:resubmit_operation", args=[self.operation.uuid]))
        self.assertContains(page, "Edit the fleet first if something was wrong, then resubmit it.")
        self.assertContains(page, "Resubmit for approval")
        self.assertContains(page, "Also send its PAP rows rejected with it back for approval (1)")
        self.assertContains(page, 'maxlength="255"')
        self.assertNotContains(page, "Correct the fleet with Edit Operation")

    def test_nothing_in_the_form_when_no_row_was_rejected_with_the_fleet(self):
        self.with_fleet.delete()

        page = self.page(self.fc)

        self.assertContains(page, "Resubmit for approval")
        self.assertNotContains(page, "Also send its PAP rows")

    def test_members_and_approvers_have_no_resubmit_form(self):
        # A pilot who was in the fleet sees the banner without the reason and without a form.
        f.member_state(self.operation, f.main_of(self.pilot), user=self.pilot, active=False)
        pilot_page = self.page(self.pilot)
        self.assertContains(pilot_page, "Rejected by an FC lead")
        self.assertNotContains(pilot_page, "</strong>: Wrong date")
        self.assertNotContains(pilot_page, "Resubmit for approval")
        self.assertEqual(self.page(self.member).status_code, 404)
        lead_page = self.page(self.lead)
        self.assertNotContains(lead_page, "Resubmit for approval")
        self.assertContains(lead_page, "Approve fleet")

    def test_the_banner_shows_the_note_to_the_approvers_and_the_queue_lists_it(self):
        resubmit_operation(self.operation, actor=self.fc, note="Date corrected")

        lead_page = self.page(self.lead)
        self.assertContains(lead_page, "Pending FC lead approval")
        self.assertContains(lead_page, "Resubmitted: Date corrected")
        self.assertContains(lead_page, "Reject fleet")
        self.assertNotContains(lead_page, "Resubmit for approval")
        queue = self.client.get(reverse("fleetops:approval_queue"))
        self.assertContains(queue, "Resubmitted: Date corrected")
        self.assertContains(queue, self.operation.fleet_type.name)

    def test_a_fleet_that_waits_has_no_resubmit_form_and_an_approved_one_no_note(self):
        resubmit_operation(self.operation, actor=self.fc, note="Date corrected")
        self.assertNotContains(self.page(self.fc), "Resubmit for approval")

        approve_operation(self.operation, actor=self.lead)

        approved = self.page(self.lead)
        self.assertNotContains(approved, "Resubmitted:")
        self.assertNotContains(self.client.get(reverse("fleetops:approval_queue")), "Resubmitted:")


@tag("catalog")
class ChineseBannerTests(ResubmitTestCase):
    def test_the_banner_is_in_chinese(self):
        self.client.force_login(self.fc)
        self.client.cookies["django_language"] = "zh-hans"

        page = self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))

        self.assertContains(page, "重新提交审批")
        self.assertNotContains(page, "Resubmit for approval")
