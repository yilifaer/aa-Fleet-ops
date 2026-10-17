# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The SRP menu of the navigation: "My fleets" follows ``srp_nav["own_fleets"]``."""

from django.test import TestCase
from django.urls import reverse

from fleetops.models import GroupAccess

from . import factories as f


class SrpMenuEntryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        f.srp_settings()
        cls.fc = f.create_user("menu-entry-fc", perms=f.FC_PERMS)
        cls.member = f.create_user("menu-entry-member", perms=f.SRP_MEMBER_PERMS)
        cls.leader = f.create_user("menu-entry-leader", perms=f.MEMBER_PERMS)
        cls.group = f.aa_group("Example Fleet Group", leaders=[cls.leader])
        cls.access = f.group_access(
            cls.group, GroupAccess.Mode.FLEETS, types=[f.fleet_type("Group Activity", "0.00")], srp_enabled=True
        )

    def menu(self, user):
        self.client.force_login(user)
        html = self.client.get(reverse("fleetops:dashboard")).content.decode()
        return html

    def test_an_fc_has_the_entry(self):
        self.assertIn(reverse("fleetops:srp_summary_own_fc"), self.menu(self.fc))

    def test_the_leader_of_a_group_with_srp_has_the_entry_and_a_menu(self):
        html = self.menu(self.leader)

        self.assertIn(reverse("fleetops:srp_summary_own_fc"), html)
        self.assertIn(">My fleets</a>", html)

    def test_the_entry_goes_when_the_group_has_no_srp(self):
        GroupAccess.objects.filter(pk=self.access.pk).update(srp_enabled=False)

        self.assertNotIn(reverse("fleetops:srp_summary_own_fc"), self.menu(self.leader))

    def test_a_member_has_my_srp_but_no_my_fleets(self):
        html = self.menu(self.member)

        self.assertIn(reverse("fleetops:srp_home"), html)
        self.assertNotIn(reverse("fleetops:srp_summary_own_fc"), html)
