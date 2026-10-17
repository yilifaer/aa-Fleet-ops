# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Group fleets in the records: the fleets list (pill, filter, who sees which), the FC incentive minimum, and the
locked fleet type on the edit page."""

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from fleetops.forms import OperationEditForm
from fleetops.models import FleetOperation, GroupAccess, IncentivePeriod, MonthlyFCStatistic, OperationRoleAssignment
from fleetops.services import incentives
from fleetops.services.incentives import fleets_toward_minimum, numbers_differ, rebuild_period
from fleetops.services.operations import end_fleet
from fleetops.services.statistics import fc_statistics

from . import factories as f

UTC = dt_timezone.utc
Status = FleetOperation.Status
FLEETS = GroupAccess.Mode.FLEETS
PING = GroupAccess.Mode.PING


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def fresh(user):
    return get_user_model().objects.get(pk=user.pk)


class GroupRecordsBase(TestCase):
    def setUp(self):
        f.settings(incentive_enabled=True)
        self.activity = f.fleet_type("Group Activity", "0.00")
        self.stratop = f.fleet_type("StratOp", "2.00")
        self.weighted = f.fleet_type("Group Weighted", "1.50")
        self.leader = f.create_user("leader", perms=f.MEMBER_PERMS, main_name="Pilot A")
        self.second = f.create_user("second", perms=f.MEMBER_PERMS, main_name="Pilot B")
        self.other_leader = f.create_user("otherleader", perms=f.MEMBER_PERMS, main_name="Pilot C")
        self.pinger = f.create_user("pinger", perms=f.MEMBER_PERMS, main_name="Pilot D")
        self.member = f.create_user("member", perms=f.MEMBER_PERMS, main_name="Pilot E")
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Example FC")
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS, main_name="Example Lead")
        self.group = f.aa_group("Example Fleet Group", leaders=[self.leader, self.second])
        self.access = f.group_access(self.group, FLEETS, types=[self.activity, self.weighted])
        self.other_group = f.aa_group("Example Other Group", leaders=[self.other_leader])
        self.other_access = f.group_access(self.other_group, FLEETS, types=[self.activity])
        self.ping_group = f.aa_group("Example Ping Group", leaders=[self.pinger])
        f.group_access(self.ping_group, PING)

    def group_fleet(self, user=None, group=None, **extra):
        group = group or self.group
        values = {"group": group, "group_name": group.name, "type_obj": self.activity, "status": Status.CLOSED}
        values.update(extra)
        return f.create_operation(user or self.leader, **values)

    def page(self, user, **query):
        self.client.force_login(user)
        response = self.client.get(reverse("fleetops:fleet_operations"), query)
        self.assertEqual(response.status_code, 200)
        return response


class FleetsListTests(GroupRecordsBase):
    def setUp(self):
        super().setUp()
        self.alliance = f.create_operation(self.fc, status=Status.CLOSED, type_obj=self.stratop)
        self.fleet = self.group_fleet()
        self.second_fleet = self.group_fleet(user=self.second)
        self.other_fleet = self.group_fleet(user=self.other_leader, group=self.other_group)

    def uuids(self, response):
        return {str(operation.uuid) for operation in response.context["page"].object_list}

    def test_the_group_pill_follows_the_fleet_type(self):
        page = self.page(self.lead)

        self.assertContains(
            page,
            '<span class="fo-pill fo-pill-neutral fo-pill-icon fo-pill-group" title="Group fleet">'
            '<i class="fa-solid fa-users" aria-hidden="true"></i>Example Fleet Group</span>',
        )
        # The alliance fleet has none; every group fleet has one.
        self.assertContains(page, "fo-pill-group", count=3)

    def test_a_leader_sees_the_fleets_of_their_groups_only_and_cannot_edit_the_ones_they_did_not_start(self):
        page = self.page(self.leader)

        self.assertEqual(self.uuids(page), {str(self.fleet.uuid), str(self.second_fleet.uuid)})
        by_uuid = {str(operation.uuid): operation for operation in page.context["page"].object_list}
        self.assertTrue(by_uuid[str(self.fleet.uuid)].can_edit)
        self.assertFalse(by_uuid[str(self.second_fleet.uuid)].can_edit)
        self.assertContains(page, reverse("fleetops:edit_operation", args=[self.fleet.uuid]))
        self.assertNotContains(page, reverse("fleetops:edit_operation", args=[self.second_fleet.uuid]))
        self.assertContains(page, reverse("fleetops:operation_detail", args=[self.second_fleet.uuid]))

    def test_a_member_sees_no_group_fleet_they_did_not_fly(self):
        f.member_state(self.fleet, f.main_of(self.member), active=False)

        page = self.page(self.member)

        self.assertEqual(self.uuids(page), {str(self.fleet.uuid)})
        self.assertNotContains(page, reverse("fleetops:operation_detail", args=[self.second_fleet.uuid]))
        self.assertNotContains(page, reverse("fleetops:operation_detail", args=[self.alliance.uuid]))

    def test_a_ping_leader_sees_no_group_fleets(self):
        page = self.page(self.pinger)

        self.assertEqual(self.uuids(page), set())

    def test_who_sees_everything(self):
        page = self.page(self.lead)

        self.assertEqual(
            self.uuids(page),
            {str(operation.uuid) for operation in (self.alliance, self.fleet, self.second_fleet, self.other_fleet)},
        )

    def test_the_filter_values(self):
        everyone = self.uuids(self.page(self.lead))
        alliance = self.uuids(self.page(self.lead, group="alliance"))
        one = self.uuids(self.page(self.lead, group=self.group.pk))
        other = self.uuids(self.page(self.lead, group=self.other_group.pk))
        broken = self.uuids(self.page(self.lead, group="not-a-group"))

        self.assertEqual(alliance, {str(self.alliance.uuid)})
        self.assertEqual(one, {str(self.fleet.uuid), str(self.second_fleet.uuid)})
        self.assertEqual(other, {str(self.other_fleet.uuid)})
        # Something that is no group id is ignored.
        self.assertEqual(broken, everyone)

    def test_the_filter_offers_the_groups_with_an_access_row_or_a_fleet(self):
        quiet = f.aa_group("Example Quiet Group")
        f.group_access(quiet, PING)
        page = self.page(self.lead)

        options = page.context["group_options"]
        self.assertEqual(
            [name for _id, name in options],
            ["Example Fleet Group", "Example Other Group", "Example Ping Group", "Example Quiet Group"],
        )
        self.assertContains(page, 'name="group"')
        self.assertContains(page, "Alliance fleets")

    def test_a_leader_chooses_among_the_groups_they_lead_only(self):
        page = self.page(self.leader)

        self.assertEqual([name for _id, name in page.context["group_options"]], ["Example Fleet Group"])
        # A group of another leader cannot be asked for either.
        asked = self.page(self.leader, group=self.other_group.pk)
        self.assertEqual(self.uuids(asked), set())

    def test_people_without_groups_have_no_filter(self):
        page = self.page(self.member)

        self.assertEqual(page.context["group_options"], [])
        self.assertNotContains(page, 'name="group"')

    def test_the_pages_keep_the_group_in_their_links(self):
        for _ in range(55):
            self.group_fleet()
        page = self.page(self.lead, group=self.group.pk)

        self.assertContains(page, f"&group={self.group.pk}&")
        second = self.page(self.lead, group=self.group.pk, page=2)
        self.assertContains(second, f"&group={self.group.pk}&")

    def test_the_query_count_does_not_grow_with_groups_or_group_fleets(self):
        def count(user):
            self.page(user)
            with CaptureQueriesContext(connection) as captured:
                self.page(user)
            return len(captured)

        small_lead, small_leader = count(self.lead), count(self.leader)
        for number in range(3):
            group = f.aa_group(f"Example Extra Group {number}", leaders=[self.leader])
            f.group_access(group, FLEETS, types=[self.activity])
            for _ in range(3):
                self.group_fleet(group=group)

        self.assertEqual(count(self.lead), small_lead)
        self.assertEqual(count(self.leader), small_leader)


class EmptyStateTests(GroupRecordsBase):
    def test_without_fleets_the_list_offers_a_start_to_those_who_may_start(self):
        for user, offered in ((self.fc, True), (self.leader, True), (self.member, False), (self.pinger, False)):
            with self.subTest(user=user.username):
                page = self.page(user)

                self.assertContains(page, 'id="fleets-empty"')
                self.assertContains(page, "No fleets yet.")
                self.assertEqual(reverse("fleetops:start_fleet") in page.content.decode(), offered)

    def test_with_filters_the_list_offers_to_clear_them(self):
        f.create_operation(self.fc, status=Status.CLOSED)

        page = self.page(self.fc, q="nothing like this")

        self.assertContains(page, "No fleets match these filters.")
        self.assertContains(page, "Clear filters")
        self.assertNotContains(page, "No fleets yet.")


class IncentiveMinimumTests(GroupRecordsBase):
    def setUp(self):
        super().setUp()
        self.period = IncentivePeriod.objects.create(year=2026, month=3, budget=1000, minimum_fleets=1)

    def stat(self, user):
        return MonthlyFCStatistic.objects.get(period=self.period, fc_user=user)

    def test_a_group_fleet_without_points_does_not_count_toward_the_minimum(self):
        self.group_fleet(started_at=utc(2026, 3, 4, 18))

        rebuild_period(self.period)

        row = self.stat(self.leader)
        # The fleet is a fleet of the FC (as on the FC statistics page), but it does not make the FC eligible.
        self.assertEqual((row.fleet_count, row.total_points, row.eligible), (1, Decimal("0.00"), False))
        self.assertEqual(row.fleet_type_breakdown, {"Group Activity": {"count": 1, "points": 0.0, "toward_minimum": 0}})
        self.assertEqual(fc_statistics(self.leader, 2026, 3)["fleet_count"], 1)
        self.assertEqual(row.calculated_payout, 0)

    def test_the_page_reads_one_fleet_and_none_toward_the_minimum(self):
        self.group_fleet(started_at=utc(2026, 3, 4, 18))
        rebuild_period(self.period)
        self.client.force_login(f.create_user("manager", perms=f.FC_LEAD_PERMS))

        page = self.client.get(reverse("fleetops:incentive_review") + "?year=2026&month=3")

        self.assertContains(page, "1 fleet · 0 toward the minimum")
        self.assertNotContains(page, "1 fleets")

    def test_the_page_says_nothing_when_all_fleets_count(self):
        f.closed_operation(self.fc, utc(2026, 3, 4, 18), type_obj=self.stratop)
        rebuild_period(self.period)
        self.client.force_login(f.create_user("manager", perms=f.FC_LEAD_PERMS))

        page = self.client.get(reverse("fleetops:incentive_review") + "?year=2026&month=3")

        self.assertNotContains(page, "toward the minimum")

    def test_a_weighted_group_fleet_counts_like_any_other(self):
        self.group_fleet(started_at=utc(2026, 3, 4, 18), type_obj=self.weighted)

        rebuild_period(self.period)

        row = self.stat(self.leader)
        self.assertEqual((row.fleet_count, row.total_points, row.eligible), (1, Decimal("1.50"), True))
        self.assertEqual(row.fleet_type_breakdown["Group Weighted"]["toward_minimum"], 1)
        self.assertEqual(row.calculated_payout, 1000)

    def test_the_points_are_the_weight_times_the_multiplier(self):
        self.group_fleet(started_at=utc(2026, 3, 4, 18), type_obj=self.weighted, attendance_multiplier=2)

        rebuild_period(self.period)

        self.assertEqual(self.stat(self.leader).total_points, Decimal("3.00"))

    def test_an_alliance_fleet_with_weight_zero_still_counts_toward_the_minimum(self):
        f.closed_operation(self.fc, utc(2026, 3, 4, 18), type_obj=self.activity)

        rebuild_period(self.period)

        row = self.stat(self.fc)
        self.assertEqual((row.fleet_count, row.eligible), (1, True))
        self.assertEqual(row.fleet_type_breakdown["Group Activity"]["toward_minimum"], 1)

    def test_the_helper_counts_the_fleets(self):
        zero = self.group_fleet(started_at=utc(2026, 3, 4, 18))
        weighted = self.group_fleet(started_at=utc(2026, 3, 5, 18), type_obj=self.weighted)
        alliance_zero = f.closed_operation(self.fc, utc(2026, 3, 6, 18), type_obj=self.activity)

        self.assertEqual(fleets_toward_minimum([zero, weighted, alliance_zero]), 2)
        self.assertEqual(fleets_toward_minimum([]), 0)

    def test_a_fleet_of_a_deleted_group_is_still_a_group_fleet(self):
        fleet = self.group_fleet(started_at=utc(2026, 3, 4, 18))
        self.group.delete()
        fleet.refresh_from_db()

        self.assertIsNone(fleet.group_id)
        self.assertEqual(fleets_toward_minimum([fleet]), 0)

    def test_the_policy_snapshot_names_the_rule(self):
        rebuild_period(self.period)
        self.period.refresh_from_db()

        self.assertIn("group_fleets", self.period.policy_snapshot)

    def test_rows_of_older_periods_without_the_figure_count_all_their_fleets(self):
        f.closed_operation(self.fc, utc(2026, 3, 4, 18), type_obj=self.stratop)
        rebuild_period(self.period)
        row = self.stat(self.fc)
        row.fleet_type_breakdown = {"StratOp": {"count": 1, "points": 2.0}}
        row.save()

        self.assertEqual(incentives.toward_minimum_count(row), 1)

    def test_no_warning_after_a_rebuild_with_group_fleets_and_a_warning_after_a_change_behind_its_back(self):
        self.group_fleet(started_at=utc(2026, 3, 4, 18))
        f.closed_operation(self.fc, utc(2026, 3, 5, 18), type_obj=self.stratop)
        rebuild_period(self.period)
        stored = list(self.period.fc_statistics.all())
        self.assertFalse(numbers_differ(self.period, stored))

        self.group_fleet(started_at=utc(2026, 3, 6, 18), type_obj=self.weighted)

        self.assertTrue(numbers_differ(self.period, list(self.period.fc_statistics.all())))

    def test_a_group_fleet_that_ends_recalculates_the_month(self):
        fleet = self.group_fleet(
            started_at=utc(2026, 3, 4, 18), status=Status.ACTIVE, type_obj=self.weighted, ended_at=None
        )
        rebuild_period(self.period)
        self.assertFalse(MonthlyFCStatistic.objects.filter(period=self.period, fc_user=self.leader).exists())

        with self.captureOnCommitCallbacks(execute=True), mock.patch("fleetops.services.operations.sync_operation"):
            end_fleet(fleet, actor=self.leader)

        row = self.stat(self.leader)
        self.assertEqual((row.fleet_count, row.eligible), (1, True))

    def test_a_credit_role_on_a_group_fleet_gives_the_credited_pilot_the_fleet(self):
        fleet = self.group_fleet(started_at=utc(2026, 3, 4, 18), type_obj=self.weighted)
        f.member_state(fleet, f.main_of(self.member), active=False)
        OperationRoleAssignment.objects.create(
            operation=fleet,
            role="backseat_fc",
            character_id=f.main_of(self.member).character_id,
            character_name="Pilot E",
            auth_user=self.member,
            grants_fc_credit=True,
        )

        rebuild_period(self.period)

        self.assertEqual(self.stat(self.member).total_points, Decimal("1.50"))


class EditPageTests(GroupRecordsBase):
    def setUp(self):
        super().setUp()
        self.fleet = self.group_fleet()
        self.url = reverse("fleetops:edit_operation", args=[self.fleet.uuid])

    def data(self, **changes):
        start = self.fleet.started_at.replace(second=0, microsecond=0)
        values = {
            "fleet_type": self.fleet.fleet_type_id,
            "doctrine_name": "",
            "formup": "Jita",
            "additional_message": "",
            "started_at": start.strftime("%Y-%m-%dT%H:%M"),
            "ended_at": self.fleet.ended_at.strftime("%Y-%m-%dT%H:%M"),
        }
        values.update(changes)
        return values

    def test_the_leader_sees_the_locked_type_with_the_reason(self):
        self.client.force_login(self.leader)

        page = self.client.get(self.url)

        self.assertContains(page, "Only FC leads can change the fleet type of a group fleet.")
        self.assertContains(page, 'id="fleet-type-help"')
        self.assertTrue(page.context["form"].fields["fleet_type"].disabled)

    def test_a_type_posted_by_a_leader_is_ignored(self):
        self.client.force_login(self.leader)

        response = self.client.post(self.url, self.data(fleet_type=self.stratop.pk, formup="Amarr"))

        self.assertEqual(response.status_code, 302)
        self.fleet.refresh_from_db()
        self.assertEqual((self.fleet.fleet_type, self.fleet.formup), (self.activity, "Amarr"))
        self.assertEqual(self.fleet.fleet_point_weight_snapshot, Decimal("0.00"))

    def test_a_credited_co_fc_cannot_change_it_either(self):
        co_fc = f.create_user("cofc", perms=f.FC_PERMS)
        OperationRoleAssignment.objects.create(
            operation=self.fleet, role="backseat_fc", character_id=f.main_of(co_fc).character_id,
            character_name="x", auth_user=co_fc, grants_fc_credit=True,
        )
        self.assertTrue(OperationEditForm(instance=self.fleet, user=fresh(co_fc)).fields["fleet_type"].disabled)

    def test_an_fc_lead_changes_the_type_and_the_weight_follows(self):
        self.client.force_login(self.lead)
        page = self.client.get(self.url)
        self.assertNotContains(page, "Only FC leads can change the fleet type")

        self.client.post(self.url, self.data(fleet_type=self.stratop.pk))

        self.fleet.refresh_from_db()
        self.assertEqual((self.fleet.fleet_type, self.fleet.fleet_point_weight_snapshot), (self.stratop, Decimal("2.00")))

    def test_an_alliance_fleet_keeps_an_editable_type(self):
        alliance = f.create_operation(self.fc, status=Status.CLOSED)
        self.client.force_login(self.fc)

        page = self.client.get(reverse("fleetops:edit_operation", args=[alliance.uuid]))

        self.assertFalse(page.context["form"].fields["fleet_type"].disabled)
        self.assertNotContains(page, 'id="fleet-type-help"')
