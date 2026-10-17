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
)
from fleetops.services.audit import audit

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
