# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Paging of the PAP history pages."""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import (
    FleetOperation,
)

from . import factories as f

Status = FleetOperation.Status


def closed_op(fc_user, started_at, **extra):
    return f.create_operation(fc_user, status=Status.CLOSED, started_at=started_at, **extra)


@mock.patch("fleetops.views_history.HISTORY_PAGE_SIZE", 2)
class AttendanceHistoryPagingTests(TestCase):
    BAD_PAGES = ("abc", "0", "-1", "1.5", "999", "")

    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.pilot = f.create_user("pilot", perms=f.CORP_MANAGEMENT_PERMS)
        self.outsider = f.create_user("outsider", perms=f.MEMBER_PERMS, corporation=f.OTHER_CORP)
        now = timezone.now()
        self.operations = [closed_op(self.fc, now - timedelta(days=day + 1)) for day in range(5)]
        for value, operation in enumerate(self.operations, start=1):
            f.add_attendance(operation, self.pilot, value=value)
        f.add_attendance(self.operations[0], self.outsider, value=10)

    def get(self, user, name, page=None):
        self.client.force_login(user)
        response = self.client.get(reverse(f"fleetops:{name}"), {} if page is None else {"page": page})
        self.assertEqual(response.status_code, 200)
        return response

    def assert_paged(self, user, name, *, count, total):
        first = self.get(user, name)
        self.assertEqual(len(first.context["rows"]), 2)
        self.assertEqual(first.context["page"].paginator.count, count)
        self.assertEqual(first.context["total"], total)
        self.assertContains(first, "Page 1 of 3")
        self.assertContains(first, "?page=2")

        last = self.get(user, name, page=3)
        self.assertEqual(len(last.context["rows"]), count - 4)
        self.assertEqual(last.context["total"], total)
        self.assertContains(last, "?page=2")

        seen = {r.pk for page in (1, 2, 3) for r in self.get(user, name, page=page).context["rows"]}
        self.assertEqual(len(seen), count)

        for page in self.BAD_PAGES:
            with self.subTest(page=page):
                self.assertIn(self.get(user, name, page=page).context["page"].number, (1, 3))

    def test_personal_history(self):
        self.assert_paged(self.pilot, "attendance_history_me", count=5, total=15)

    def test_corporation_history(self):
        self.assert_paged(self.pilot, "attendance_history_corporation", count=5, total=15)

    def test_alliance_history(self):
        lead = f.create_user("lead", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])
        self.assert_paged(lead, "attendance_history_alliance", count=6, total=25)

    def test_fleet_links_require_access_to_the_fleet(self):
        lead = f.create_user("lead", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])
        link = reverse("fleetops:operation_detail", args=[self.operations[0].uuid])

        self.assertNotContains(self.get(lead, "attendance_history_alliance"), link)
        self.assertContains(self.get(self.pilot, "attendance_history_me"), link)
        viewer = f.create_user("viewer", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats", "fleetops.view_all_fleets"])
        self.assertContains(self.get(viewer, "attendance_history_alliance"), link)
