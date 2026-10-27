# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Special roles with FC credit wait for the approval of the fleet (owner decision 5-2)."""

from datetime import timedelta

from django.contrib.messages import get_messages
from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone, translation

from fleetops.forms import OperationRoleAssignmentForm
from fleetops.models import AttendanceRecord, AuditLog, FleetOperation, OperationRoleAssignment, ReviewStatus
from fleetops.services.approvals import approve_operation, reject_operation
from fleetops.services.roles import add_role_assignment
from fleetops.texts import translate_stored

from . import factories as f

Status = FleetOperation.Status
PENDING = ReviewStatus.PENDING
APPROVED = ReviewStatus.APPROVED
REJECTED = ReviewStatus.REJECTED
MANUAL = AttendanceRecord.Source.MANUAL
REFUSAL = "Special roles with FC credit can be assigned once an FC lead has approved the fleet."


def message_texts(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class CreditRoleTestCase(TestCase):
    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.pilot = f.create_user("pilot", perms=f.MEMBER_PERMS)
        self.pilot_id = f.main_of(self.pilot).character_id
        self.start = timezone.now() - timedelta(days=1)
        # A manual fleet waiting for approval, with the pilot's manual PAP row (so the pilot is recorded in it).
        self.pending = f.create_operation(
            self.fc,
            status=Status.CLOSED,
            is_manual=True,
            esi_fleet_id=None,
            review_status=PENDING,
            started_at=self.start,
            ended_at=self.start + timedelta(hours=1),
        )
        f.add_attendance(self.pending, self.pilot, source=MANUAL, review_status=PENDING, created_by=self.fc, granted=False)

    def assign(self, user, role_key, operation=None, character_id=None):
        self.client.force_login(user)
        return self.client.post(
            reverse("fleetops:add_operation_role", args=[(operation or self.pending).uuid]),
            {"role": role_key, "character_id": str(character_id or self.pilot_id), "notes": ""},
        )

    def page(self, user, operation=None):
        self.client.force_login(user)
        response = self.client.get(reverse("fleetops:operation_detail", args=[(operation or self.pending).uuid]))
        self.assertEqual(response.status_code, 200)
        return response


class AssignmentRuleTests(CreditRoleTestCase):
    def test_a_credit_role_is_refused_while_the_fleet_waits(self):
        with self.assertRaisesMessage(ValueError, REFUSAL):
            add_role_assignment(self.pending, actor=self.fc, role="backseat_fc", character_id=self.pilot_id)

        response = self.assign(self.fc, "backseat_fc")

        self.assertEqual(message_texts(response), [REFUSAL])
        self.assertFalse(OperationRoleAssignment.objects.exists())
        self.assertFalse(AuditLog.objects.filter(action__startswith="fleet.role").exists())

    def test_a_credit_role_is_refused_on_a_rejected_fleet(self):
        reject_operation(self.pending, actor=self.lead, note="Wrong date")

        with self.assertRaisesMessage(ValueError, REFUSAL):
            add_role_assignment(self.pending, actor=self.fc, role="backseat_fc", character_id=self.pilot_id)

    def test_a_role_without_credit_is_allowed_on_a_pending_fleet(self):
        response = self.assign(self.fc, "logi_anchor")

        assignment = OperationRoleAssignment.objects.get()
        self.assertEqual((assignment.role, assignment.grants_fc_credit), ("logi_anchor", False))
        self.assertEqual(message_texts(response), ["%s assigned as Logi Anchor." % f.main_of(self.pilot).character_name])

    def test_a_credit_role_is_allowed_once_the_fleet_is_approved(self):
        approve_operation(self.pending, actor=self.lead)

        response = self.assign(self.fc, "backseat_fc")

        self.assertEqual(OperationRoleAssignment.objects.get().grants_fc_credit, True)
        self.assertIn("FC credit granted", message_texts(response)[0])

    def test_a_tracked_fleet_is_unaffected(self):
        tracked = f.create_operation(self.fc, status=Status.CLOSED)
        f.member_state(tracked, f.main_of(self.pilot), active=False)

        self.assign(self.fc, "backseat_fc", operation=tracked)

        self.assertTrue(OperationRoleAssignment.objects.get(operation=tracked).grants_fc_credit)

    def test_the_role_a_credit_switch_made_later_does_not_matter_only_the_role_does(self):
        # A role that has no credit today is no credit role: the check follows the role's switch.
        f.special_role(key="logi_anchor", grants_fc_credit=False)

        add_role_assignment(self.pending, actor=self.fc, role="logi_anchor", character_id=self.pilot_id)

        self.assertFalse(OperationRoleAssignment.objects.get().grants_fc_credit)


class FormAndPageTests(CreditRoleTestCase):
    def test_the_form_disables_the_credit_roles_of_a_waiting_fleet(self):
        form = OperationRoleAssignmentForm(operation=self.pending)

        self.assertTrue(form.credit_after_approval)
        html = str(form["role"])
        self.assertRegex(html, r'<option value="backseat_fc"[^>]* disabled>Back Seat FC \(FC credit\)</option>')
        self.assertRegex(html, r'<option value="logi_anchor"(?![^>]*disabled)')

    def test_the_form_offers_every_role_on_an_approved_fleet(self):
        form = OperationRoleAssignmentForm(operation=f.create_operation(self.fc, status=Status.CLOSED))

        self.assertFalse(form.credit_after_approval)
        self.assertNotIn("disabled", str(form["role"]))

    def test_the_page_says_when_credit_roles_can_be_assigned(self):
        page = self.page(self.fc)

        self.assertContains(page, 'id="roles-after-approval"')
        self.assertContains(page, REFUSAL)
        self.assertContains(page, ' disabled>Back Seat FC (FC credit)</option>')

        approve_operation(self.pending, actor=self.lead)
        approved = self.page(self.fc)
        self.assertNotContains(approved, 'id="roles-after-approval"')
        self.assertNotContains(approved, ' disabled>Back Seat FC (FC credit)</option>')

    def test_a_credit_assignment_made_before_the_approval_is_flagged_on_the_fleet_page(self):
        OperationRoleAssignment.objects.create(
            operation=self.pending,
            role="backseat_fc",
            character_id=self.pilot_id,
            character_name="Pilot A",
            auth_user=self.pilot,
            grants_fc_credit=True,
        )

        page = self.page(self.fc)
        self.assertContains(page, "Assigned before approval")
        self.assertContains(page, 'fo-before-approval')

        approve_operation(self.pending, actor=self.lead)
        self.assertNotContains(self.page(self.fc), "Assigned before approval")


class ApprovalQueueTests(CreditRoleTestCase):
    def test_the_queue_lists_the_credit_assignments_given_before_the_approval(self):
        OperationRoleAssignment.objects.create(
            operation=self.pending,
            role="backseat_fc",
            character_id=self.pilot_id,
            character_name="Pilot A",
            auth_user=self.pilot,
            grants_fc_credit=True,
        )
        # A role without credit is not listed.
        OperationRoleAssignment.objects.create(
            operation=self.pending, role="logi_anchor", character_id=self.pilot_id + 1, character_name="Pilot B"
        )
        self.client.force_login(self.lead)

        page = self.client.get(reverse("fleetops:approval_queue"))

        self.assertContains(page, "FC credit: Pilot A (Back Seat FC)")
        self.assertNotContains(page, "Pilot B (Logi Anchor)")
        self.assertContains(page, "Assigned before approval")
        self.assertEqual(page.context["pending_fleets"][0].credit_roles[0].character_name, "Pilot A")

    def test_a_fleet_without_credit_roles_has_no_flag(self):
        self.client.force_login(self.lead)

        page = self.client.get(reverse("fleetops:approval_queue"))

        self.assertContains(page, self.pending.fleet_type.name)
        self.assertNotContains(page, "Assigned before approval")

    def test_the_queue_costs_the_same_queries_for_more_credit_roles(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.client.force_login(self.lead)
        self.client.get(reverse("fleetops:approval_queue"))
        with CaptureQueriesContext(connection) as small:
            self.client.get(reverse("fleetops:approval_queue"))
        for number in range(3):
            fleet = f.create_operation(self.fc, status=Status.CLOSED, is_manual=True, review_status=PENDING)
            OperationRoleAssignment.objects.create(
                operation=fleet, role="backseat_fc", character_id=900_000 + number, character_name=f"Pilot {number}",
                grants_fc_credit=True, auth_user=self.pilot,
            )
        self.client.get(reverse("fleetops:approval_queue"))
        with CaptureQueriesContext(connection) as large:
            self.client.get(reverse("fleetops:approval_queue"))

        self.assertEqual(len(large), len(small))


class DeletedPapTests(CreditRoleTestCase):
    def test_deleting_the_only_trace_of_a_character_with_a_credit_role_warns_the_approver(self):
        approve_operation(self.pending, actor=self.lead)
        manual = AttendanceRecord.objects.get(operation=self.pending)
        add_role_assignment(self.pending, actor=self.fc, role="backseat_fc", character_id=self.pilot_id)
        self.client.force_login(self.lead)

        response = self.client.post(reverse("fleetops:delete_manual_attendance", args=[manual.pk]))

        texts = message_texts(response)
        self.assertEqual(texts[0], "Manual PAP deleted.")
        self.assertIn(
            f"{f.main_of(self.pilot).character_name} is no longer recorded in this fleet but keeps FC credit through Back Seat FC.",
            texts[1],
        )
        # The assignment is not removed by itself.
        self.assertEqual(OperationRoleAssignment.objects.count(), 1)

    def test_no_warning_while_the_character_is_still_recorded_or_has_no_credit(self):
        approve_operation(self.pending, actor=self.lead)
        manual = AttendanceRecord.objects.get(operation=self.pending)
        add_role_assignment(self.pending, actor=self.fc, role="logi_anchor", character_id=self.pilot_id)
        f.member_state(self.pending, f.main_of(self.pilot), active=False)
        self.client.force_login(self.lead)

        response = self.client.post(reverse("fleetops:delete_manual_attendance", args=[manual.pk]))

        self.assertEqual(message_texts(response), ["Manual PAP deleted."])


@tag("catalog")
class ChineseTests(CreditRoleTestCase):
    def test_the_refusal_is_shown_in_chinese(self):
        with translation.override("zh-hans"):
            self.assertNotEqual(translate_stored(REFUSAL), REFUSAL)
        self.client.cookies["django_language"] = "zh-hans"

        response = self.assign(self.fc, "backseat_fc")

        self.assertNotIn(REFUSAL, message_texts(response))
        self.assertTrue(any(ord(char) > 127 for char in message_texts(response)[0]))
