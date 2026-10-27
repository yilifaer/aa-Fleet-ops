# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Rejecting asks first (the shared confirmation dialog); approving does not. The markup is what the script reads:
``data-fo-confirm`` on the button that was pressed, so Approve and Reject can share one form."""

import re
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import AttendanceRecord, FleetOperation, ReviewStatus

from . import factories as f

Status = FleetOperation.Status
PENDING = ReviewStatus.PENDING
MANUAL = AttendanceRecord.Source.MANUAL
FLEET_QUESTION = "Reject this fleet? The FC sees your note."
ROWS_QUESTION = "Reject the selected PAP rows?"


def buttons(html):
    """``{button text: its attributes}`` for every button with a name="action" in the page."""
    found = {}
    for attributes, text in re.findall(r"<button([^>]*name=\"action\"[^>]*)>(.*?)</button>", html, flags=re.S):
        found.setdefault(text.strip(), []).append(attributes)
    return found


class RejectConfirmTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.pilot = f.create_user("pilot", perms=f.MEMBER_PERMS)
        start = timezone.now() - timedelta(days=1)
        self.operation = f.create_operation(
            self.fc,
            status=Status.CLOSED,
            is_manual=True,
            esi_fleet_id=None,
            review_status=PENDING,
            started_at=start,
            ended_at=start + timedelta(hours=1),
        )
        f.add_attendance(self.operation, self.pilot, source=MANUAL, review_status=PENDING, created_by=self.fc, granted=False)
        self.client.force_login(self.lead)

    def assertAsks(self, attributes, question, label):
        self.assertEqual(len(attributes), 1)
        self.assertIn(f'data-fo-confirm="{question}"', attributes[0])
        self.assertIn(f'data-fo-confirm-label="{label}"', attributes[0])

    def assertDoesNotAsk(self, attributes):
        self.assertTrue(attributes)
        for item in attributes:
            self.assertNotIn("data-fo-confirm", item)

    def test_the_approvals_page_asks_before_rejecting_a_fleet_or_rows(self):
        html = self.client.get(reverse("fleetops:approval_queue")).content.decode()
        found = buttons(html)

        self.assertAsks(found["Reject"], FLEET_QUESTION, "Reject")
        self.assertAsks(found["Reject selected"], ROWS_QUESTION, "Reject")
        self.assertDoesNotAsk(found["Approve"])
        self.assertDoesNotAsk(found["Approve selected"])

    def test_the_fleet_page_asks_before_rejecting_a_fleet_or_rows(self):
        html = self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid])).content.decode()
        found = buttons(html)

        self.assertAsks(found["Reject fleet"], FLEET_QUESTION, "Reject fleet")
        self.assertAsks(found["Reject selected"], ROWS_QUESTION, "Reject")
        self.assertDoesNotAsk(found["Approve fleet"])
        self.assertDoesNotAsk(found["Approve selected"])

    def test_approve_and_reject_share_a_form_that_has_no_question_of_its_own(self):
        # The question of the pressed button wins over the form's; a question on the form would also ask for Approve.
        for url in (
            reverse("fleetops:approval_queue"),
            reverse("fleetops:operation_detail", args=[self.operation.uuid]),
        ):
            html = self.client.get(url).content.decode()
            for form in re.findall(r"<form[^>]*>", html):
                if "review_operation" in form or "review_attendance" in form or "/review" in form:
                    self.assertNotIn("data-fo-confirm", form, url)

    def test_the_server_still_wants_a_reason_whatever_the_dialog_said(self):
        response = self.client.post(
            reverse("fleetops:review_operation", args=[self.operation.uuid]), {"action": "reject", "note": ""}
        )

        self.assertEqual(response.status_code, 302)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.review_status, PENDING)

    def test_the_questions_are_catalog_messages_that_are_in_english_here(self):
        html = self.client.get(reverse("fleetops:approval_queue")).content.decode()

        self.assertIn(FLEET_QUESTION, html)
        self.assertIn(ROWS_QUESTION, html)


class OtherQuestionsTests(TestCase):
    """Deleting a manual PAP row and unlocking a finalised month ask first, too."""

    def setUp(self):
        f.settings(incentive_enabled=True)
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.pilot = f.create_user("pilot", perms=f.MEMBER_PERMS)
        self.operation = f.create_operation(self.lead, status=Status.CLOSED)
        self.row = f.add_attendance(self.operation, self.pilot, source=MANUAL, review_status=ReviewStatus.APPROVED, created_by=self.lead)
        self.client.force_login(self.lead)

    def test_deleting_a_manual_row_asks(self):
        for url in (
            reverse("fleetops:manual_attendance"),
            reverse("fleetops:operation_detail", args=[self.operation.uuid]),
        ):
            html = self.client.get(url).content.decode()
            forms = [form for form in re.findall(r"<form[^>]*>", html) if reverse("fleetops:delete_manual_attendance", args=[self.row.pk]) in form]
            self.assertEqual(len(forms), 1, url)
            self.assertIn("data-fo-confirm=\"Delete this manual PAP row?", forms[0])

    def test_unlocking_a_month_asks_and_finalizing_does_not(self):
        from fleetops.models import IncentivePeriod

        period = IncentivePeriod.objects.create(year=2026, month=3, status=IncentivePeriod.Status.FINALIZED)
        html = self.client.get(reverse("fleetops:incentive_review") + "?year=2026&month=3").content.decode()
        unlock = [form for form in re.findall(r"<form[^>]*>", html) if reverse("fleetops:incentive_unlock", args=[period.pk]) in form]
        self.assertEqual(len(unlock), 1)
        self.assertIn("data-fo-confirm=\"Unlock this month?", unlock[0])

        IncentivePeriod.objects.filter(pk=period.pk).update(status=IncentivePeriod.Status.REVIEW)
        html = self.client.get(reverse("fleetops:incentive_review") + "?year=2026&month=3").content.decode()
        finalize = [form for form in re.findall(r"<form[^>]*>", html) if reverse("fleetops:incentive_finalize", args=[period.pk]) in form]
        self.assertEqual(len(finalize), 1)
        self.assertNotIn("data-fo-confirm", finalize[0])
