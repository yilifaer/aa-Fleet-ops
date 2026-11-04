# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The audit log page."""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import (
    AuditLog,
    SrpClaim,
)
from fleetops.services.audit import audit
from fleetops.srp import links, review

from . import factories as f


@mock.patch("fleetops.views.AUDIT_PAGE_SIZE", 2)
class AuditLogPagingTests(TestCase):
    def setUp(self):
        self.auditor = f.create_user("auditor", perms=f.MEMBER_PERMS + ["fleetops.view_audit_log"])
        fleet_type = f.fleet_type()
        self.oldest = audit(self.auditor, "oldest.entry", fleet_type)
        AuditLog.objects.filter(pk=self.oldest.pk).update(created_at=timezone.now() - timedelta(days=30))
        for index in range(4):
            audit(self.auditor, f"entry.{index}", fleet_type)
        self.client.force_login(self.auditor)

    def get(self, **params):
        response = self.client.get(reverse("fleetops:audit_log"), params)
        self.assertEqual(response.status_code, 200)
        return response

    def test_pages_cover_every_entry_newest_first(self):
        first = self.get()
        self.assertEqual([r.action for r in first.context["rows"]], ["entry.3", "entry.2"])
        self.assertContains(first, "Page 1 of 3")
        self.assertContains(first, "?page=2")

        last = self.get(page=3)
        self.assertEqual([r.action for r in last.context["rows"]], ["oldest.entry"])

    def test_bad_page_values_fall_back(self):
        for page in ("abc", "0", "-1", "1.5", "999"):
            with self.subTest(page=page):
                self.assertIn(self.get(page=page).context["page"].number, (1, 3))


class SrpLinkAuditTests(TestCase):
    """The six srp.link_* actions are on the audit page, and their stored values are field values only."""

    ACTIONS = {
        "srp.link_create",
        "srp.link_update",
        "srp.link_open",
        "srp.link_close",
        "srp.link_complete",
        "srp.link_uncomplete",
    }

    def test_the_link_actions_show_on_the_page_with_plain_values(self):
        f.srp_settings()
        auditor = f.create_user("linkauditor", perms=f.SRP_MANAGER_PERMS + ["fleetops.view_audit_log"])
        member = f.create_user("linkpilot", perms=f.SRP_MEMBER_PERMS)
        link = links.create_standing_link(
            actor=auditor, category=f.srp_category("Home defense"), link_date=timezone.now().date()
        )
        links.update_link(link, actor=auditor, doctrine_text="Muninns")
        links.set_link_open(link, actor=auditor, is_open=False)
        links.set_link_open(link, actor=auditor, is_open=True)
        claim = f.srp_claim(member, link=link, status=SrpClaim.Status.PAID, decided_by=auditor)
        links.set_link_open(link, actor=auditor, is_open=False)
        links.complete_link(link, actor=auditor)
        with self.captureOnCommitCallbacks(execute=True):
            review.undo_payment(claim, actor=auditor, reason="Paid by mistake")
        self.client.force_login(auditor)

        response = self.client.get(reverse("fleetops:audit_log"))

        self.assertEqual(response.status_code, 200)
        shown = {row.action for row in response.context["rows"]}
        self.assertTrue(self.ACTIONS <= shown, self.ACTIONS - shown)
        for action in self.ACTIONS:
            self.assertContains(response, action)
        for entry in AuditLog.objects.filter(action__in=self.ACTIONS):
            for values in (entry.old_value, entry.new_value):
                for key, value in (values or {}).items():
                    self.assertIsInstance(value, (str, int, bool, type(None)), (entry.action, key))
