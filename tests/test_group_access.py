# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Group leaders read from Alliance Auth, their grants, the channel and start rules, the navigation and the
group decorators."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from fleetops import texts
from fleetops.models import GroupAccess
from fleetops.services import group_access
from fleetops.views_errors import FLEET_MANAGER_REASON, PING_SENDER_REASON, STARTER_REASON

from . import factories as f

PING = GroupAccess.Mode.PING
FLEETS = GroupAccess.Mode.FLEETS


def fresh(user):
    """The user as the next request loads it: no cached permissions, no cached leadership."""
    return get_user_model().objects.get(pk=user.pk)


class GroupFixture(TestCase):
    """Two groups with access rows, a leader group, an alliance channel and a channel per group."""

    def setUp(self):
        super().setUp()
        self.activity = f.fleet_type("Group Activity", "0.00")
        self.stratop = f.fleet_type("StratOp", "1.00")
        self.member = f.create_user("member", perms=f.MEMBER_PERMS)
        self.leader = f.create_user("leader", perms=f.MEMBER_PERMS, main_name="Pilot A")
        self.via = f.create_user("via", perms=f.MEMBER_PERMS, main_name="Pilot B")
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Example FC")
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS, main_name="Example Lead")
        self.leader_group = f.aa_group("Example Fleet Leaders")
        self.via.groups.add(self.leader_group)
        self.fleet_group = f.aa_group("Example Fleet Group", leaders=[self.leader], leader_groups=[self.leader_group])
        self.ping_group = f.aa_group("Example Ping Group", leaders=[self.leader])
        self.fleet_access = f.group_access(self.fleet_group, FLEETS, types=[self.activity])
        self.ping_access = f.group_access(self.ping_group, PING)
        self.alliance = f.ping_channel("Alliance Pings")
        self.fleet_channel = f.group_channel("Fleet Group Pings", groups=[self.fleet_group])
        self.ping_channel = f.group_channel("Ping Group Pings", groups=[self.ping_group])


class LeaderAccessTests(GroupFixture):
    def test_a_direct_leader_has_the_grants_of_the_groups_they_lead(self):
        access = group_access.leader_access(fresh(self.leader))

        self.assertEqual(access.group_ids, {self.fleet_group.pk, self.ping_group.pk})
        self.assertEqual(access.fleet_group_ids, {self.fleet_group.pk})
        grant = access.grant(self.fleet_group.pk)
        self.assertEqual(grant.group_name, "Example Fleet Group")
        self.assertTrue(grant.runs_fleets)
        self.assertEqual(grant.fleet_type_ids, {self.activity.pk})
        self.assertFalse(grant.srp_enabled)
        self.assertFalse(access.grant(self.ping_group.pk).runs_fleets)

    def test_leaders_are_added_to_alliance_auth_by_primary_key(self):
        # Alliance Auth 5.4 and later point the leader fields at proxy models, which refuse a plain User or Group
        # instance; primary keys work on every supported version.
        field = self.fleet_group.authgroup._meta.get_field("group_leaders")
        self.assertTrue(self.fleet_group.authgroup.group_leaders.filter(pk=self.leader.pk).exists())
        self.assertTrue(self.fleet_group.authgroup.group_leader_groups.filter(pk=self.leader_group.pk).exists())
        if field.related_model is not get_user_model():
            with self.assertRaises(TypeError), transaction.atomic():
                self.ping_group.authgroup.group_leaders.add(fresh(self.member))
        self.ping_group.authgroup.group_leaders.add(self.member.pk)
        self.assertIn(self.ping_group.pk, group_access.leader_access(fresh(self.member)).group_ids)

    def test_a_member_of_a_leader_group_leads_the_group(self):
        access = group_access.leader_access(fresh(self.via))

        self.assertEqual(access.group_ids, {self.fleet_group.pk})

    def test_a_leader_both_directly_and_through_a_leader_group_has_one_grant(self):
        self.leader.groups.add(self.leader_group)

        access = group_access.leader_access(fresh(self.leader))

        self.assertEqual(len([grant for grant in access.grants if grant.group_id == self.fleet_group.pk]), 1)

    def test_members_of_the_group_itself_and_other_users_lead_nothing(self):
        self.member.groups.add(self.fleet_group)

        self.assertEqual(group_access.leader_access(fresh(self.member)).grants, ())
        self.assertEqual(group_access.leader_access(fresh(self.fc)).grants, ())

    def test_a_group_without_an_access_row_gives_nothing(self):
        f.aa_group("Example Unconfigured", leaders=[self.member])

        self.assertEqual(group_access.leader_access(fresh(self.member)).grants, ())

    def test_an_inactive_access_row_gives_nothing(self):
        GroupAccess.objects.filter(pk=self.fleet_access.pk).update(is_active=False)

        self.assertEqual(group_access.leader_access(fresh(self.leader)).group_ids, {self.ping_group.pk})
        self.assertEqual(group_access.leader_access(fresh(self.via)).grants, ())

    def test_the_group_management_permission_alone_makes_nobody_a_leader(self):
        content_type = ContentType.objects.get_for_model(Group)
        permission, _created = Permission.objects.get_or_create(
            codename="group_management", content_type=content_type, defaults={"name": "group_management"}
        )
        self.member.user_permissions.add(permission)
        member = fresh(self.member)
        self.assertTrue(member.has_perm("auth.group_management"))

        self.assertEqual(group_access.leader_access(member).grants, ())

    def test_without_basic_access_or_an_active_account_there_is_no_grant_and_no_query(self):
        outsider = f.create_user("outsider")
        self.fleet_group.authgroup.group_leaders.add(outsider.pk)
        inactive = f.member("inactive", perms=f.MEMBER_PERMS, active=False)
        self.fleet_group.authgroup.group_leaders.add(inactive.pk)

        for user in (fresh(outsider), fresh(inactive)):
            with self.subTest(user=user.username):
                user.has_perm("fleetops.basic_access")  # permissions are loaded by the page anyway
                with self.assertNumQueries(0):
                    self.assertEqual(group_access.leader_access(user).grants, ())

    def test_a_deleted_group_gives_nothing(self):
        self.ping_group.delete()

        self.assertEqual(group_access.leader_access(fresh(self.leader)).group_ids, {self.fleet_group.pk})
        self.assertFalse(GroupAccess.objects.filter(pk=self.ping_access.pk).exists())

    def test_a_leader_removed_in_alliance_auth_has_nothing_from_the_next_request_on(self):
        user = fresh(self.via)
        self.assertTrue(group_access.leader_access(user).grants)

        self.via.groups.remove(self.leader_group)

        # The same request keeps its answer; the next one reads Alliance Auth again.
        self.assertTrue(group_access.leader_access(user).grants)
        group_access.forget(user)
        self.assertEqual(group_access.leader_access(user).grants, ())
        self.assertEqual(group_access.leader_access(fresh(self.via)).grants, ())

    def test_only_active_fleet_types_are_granted(self):
        self.activity.is_active = False
        self.activity.save()

        grant = group_access.leader_access(fresh(self.leader)).grant(self.fleet_group.pk)

        self.assertEqual(grant.fleet_type_ids, frozenset())

    def test_queries_none_for_a_leader_of_nothing_one_and_two_for_leaders_and_cached_afterwards(self):
        only_ping = f.create_user("onlyping", perms=f.MEMBER_PERMS)
        self.ping_group.authgroup.group_leaders.add(only_ping.pk)
        for user, queries in ((fresh(self.member), 1), (fresh(only_ping), 1), (fresh(self.leader), 2)):
            with self.subTest(user=user.username):
                user.has_perm("fleetops.basic_access")
                with self.assertNumQueries(queries):
                    group_access.leader_access(user)
                with self.assertNumQueries(0):
                    group_access.leader_access(user)

    def test_the_lookup_does_not_grow_with_groups_or_types(self):
        user = fresh(self.leader)
        user.has_perm("fleetops.basic_access")
        with self.assertNumQueries(2):
            group_access.leader_access(user)
        for number in range(4):
            group = f.aa_group(f"Example Extra {number}", leaders=[self.leader])
            f.group_access(group, FLEETS, types=[self.activity, self.stratop])
        user = fresh(self.leader)
        user.has_perm("fleetops.basic_access")
        with self.assertNumQueries(2):
            self.assertEqual(len(group_access.leader_access(user).grants), 6)

    def test_start_and_ping_rights(self):
        cases = (
            (self.member, False, False),
            (self.via, True, True),
            (self.fc, True, True),
            (self.lead, True, True),
        )
        only_ping = f.create_user("onlyping", perms=f.MEMBER_PERMS)
        self.ping_group.authgroup.group_leaders.add(only_ping.pk)
        cases += ((only_ping, False, True),)
        for user, starts, pings in cases:
            with self.subTest(user=user.username):
                user = fresh(user)
                self.assertEqual(group_access.can_start_fleets(user), starts)
                self.assertEqual(group_access.can_send_pings(user), pings)


class ChannelRuleTests(GroupFixture):
    def refusal(self, user, channel, group=None):
        return group_access.channel_refusal(fresh(user), channel, group_id=group.pk if group else None)

    def test_no_channel_is_never_refused_and_an_inactive_one_always(self):
        self.assertEqual(self.refusal(self.member, None), "")
        self.alliance.is_active = False
        self.alliance.save()
        for user in (self.fc, self.lead, self.leader):
            with self.subTest(user=user.username):
                self.assertEqual(
                    self.refusal(user, self.alliance), texts.PING_CHANNEL_DISABLED(name="Alliance Pings")
                )

    def test_fcs_use_alliance_channels_and_channels_open_to_them(self):
        open_group_channel = f.group_channel("Open Group Pings", groups=[self.fleet_group], fcs_allowed=True)

        self.assertEqual(self.refusal(self.fc, self.alliance), "")
        self.assertEqual(self.refusal(self.fc, open_group_channel), "")
        self.assertEqual(
            self.refusal(self.fc, self.fleet_channel), texts.CHANNEL_NOT_ALLOWED(name="Fleet Group Pings")
        )

    def test_a_channel_without_groups_is_an_alliance_channel_whatever_its_switch_says(self):
        stored_off = f.ping_channel("Stored Off", fcs_allowed=False)

        self.assertEqual(self.refusal(self.fc, stored_off), "")
        self.assertIn(stored_off, group_access.alliance_channels(fresh(self.fc)))

    def test_fc_leads_post_everywhere(self):
        for channel in (self.alliance, self.fleet_channel, self.ping_channel):
            with self.subTest(channel=channel.name):
                self.assertEqual(self.refusal(self.lead, channel), "")
                self.assertEqual(self.refusal(self.lead, channel, self.fleet_group), "")

    def test_alliance_pings_need_fc_permissions(self):
        self.assertEqual(self.refusal(self.member, self.alliance), texts.CHANNEL_NOT_ALLOWED(name="Alliance Pings"))
        self.assertEqual(self.refusal(self.leader, self.alliance), texts.CHANNEL_NOT_ALLOWED(name="Alliance Pings"))
        # Retries of their own fleets by FCs who no longer start fleets keep working.
        own_fleets = f.create_user("ownfleets", perms=["fleetops.basic_access", "fleetops.manage_own_fleet"])
        self.assertEqual(self.refusal(own_fleets, self.alliance), "")

    def test_leaders_post_to_their_groups_channels_only(self):
        self.assertEqual(self.refusal(self.leader, self.fleet_channel, self.fleet_group), "")
        self.assertEqual(self.refusal(self.via, self.fleet_channel, self.fleet_group), "")
        self.assertEqual(
            self.refusal(self.leader, self.alliance, self.fleet_group),
            texts.GROUP_CHANNEL_NOT_ASSIGNED(name="Alliance Pings", group="Example Fleet Group"),
        )
        # A leader of two groups cannot post the channel of one group as the other group.
        self.assertEqual(
            self.refusal(self.leader, self.ping_channel, self.fleet_group),
            texts.GROUP_CHANNEL_NOT_ASSIGNED(name="Ping Group Pings", group="Example Fleet Group"),
        )
        self.assertEqual(self.refusal(self.leader, self.ping_channel, self.ping_group), "")

    def test_a_user_who_does_not_lead_the_group_is_refused(self):
        self.assertEqual(
            self.refusal(self.via, self.ping_channel, self.ping_group),
            texts.GROUP_NOT_LED(group="Example Ping Group"),
        )
        self.assertEqual(
            self.refusal(self.fc, self.fleet_channel, self.fleet_group),
            texts.GROUP_NOT_LED(group="Example Fleet Group"),
        )

    def test_a_leader_who_is_also_an_fc(self):
        self.fleet_group.authgroup.group_leaders.add(self.fc.pk)

        self.assertEqual(self.refusal(self.fc, self.alliance), "")
        self.assertEqual(
            self.refusal(self.fc, self.fleet_channel), texts.CHANNEL_NOT_ALLOWED(name="Fleet Group Pings")
        )
        self.assertEqual(self.refusal(self.fc, self.fleet_channel, self.fleet_group), "")
        self.assertEqual(
            self.refusal(self.fc, self.alliance, self.fleet_group),
            texts.GROUP_CHANNEL_NOT_ASSIGNED(name="Alliance Pings", group="Example Fleet Group"),
        )

    def test_channel_lists(self):
        def names(queryset):
            return sorted(channel.name for channel in queryset)

        self.assertEqual(names(group_access.alliance_channels(fresh(self.fc))), ["Alliance Pings"])
        self.assertEqual(
            names(group_access.alliance_channels(fresh(self.lead))),
            ["Alliance Pings", "Fleet Group Pings", "Ping Group Pings"],
        )
        self.assertEqual(names(group_access.alliance_channels(fresh(self.leader))), [])
        self.assertEqual(names(group_access.group_channels([self.fleet_group.pk])), ["Fleet Group Pings"])
        self.assertEqual(
            names(group_access.group_channels([self.fleet_group.pk, self.ping_group.pk])),
            ["Fleet Group Pings", "Ping Group Pings"],
        )
        self.assertEqual(names(group_access.group_channels([])), [])
        both = f.group_channel("Both Groups", groups=[self.fleet_group, self.ping_group])
        self.assertEqual(
            list(group_access.group_channels([self.fleet_group.pk, self.ping_group.pk]).filter(pk=both.pk)), [both]
        )


class FleetStartRuleTests(GroupFixture):
    def refusal(self, user, *, group=None, fleet_type=None, channel=None, sends=True, pre_ping=False):
        return group_access.fleet_start_refusal(
            fresh(user),
            group_id=group.pk if group else None,
            fleet_type=fleet_type or self.activity,
            channel=channel,
            sends=sends,
            pre_ping=pre_ping,
        )

    def test_alliance_fleets_need_start_fleet_and_a_channel_fcs_may_use(self):
        self.assertEqual(self.refusal(self.leader, channel=self.alliance), ("fleet_group", texts.GROUP_REQUIRED()))
        self.assertEqual(self.refusal(self.fc, fleet_type=self.stratop, channel=self.alliance), ("", ""))
        self.assertEqual(
            self.refusal(self.fc, channel=self.fleet_channel),
            ("ping_channel", texts.CHANNEL_NOT_ALLOWED(name="Fleet Group Pings")),
        )
        # Without a post (PAP only, copy only) the channel does not matter.
        self.assertEqual(self.refusal(self.fc, channel=self.fleet_channel, sends=False), ("", ""))
        self.assertEqual(self.refusal(self.fc, channel=None), ("", ""))
        # A channel switched off is never posted to; the start records that instead of refusing.
        self.fleet_channel.is_active = False
        self.fleet_channel.save()
        self.assertEqual(self.refusal(self.fc, fleet_type=self.stratop, channel=self.fleet_channel), ("", ""))
        self.assertEqual(
            group_access.channel_refusal(fresh(self.fc), self.fleet_channel),
            texts.PING_CHANNEL_DISABLED(name="Fleet Group Pings"),
        )

    def test_group_fleets(self):
        self.assertEqual(self.refusal(self.leader, group=self.fleet_group, channel=self.fleet_channel), ("", ""))
        self.assertEqual(self.refusal(self.via, group=self.fleet_group, channel=self.fleet_channel), ("", ""))
        self.assertEqual(
            self.refusal(self.leader, group=self.ping_group, channel=self.ping_channel),
            ("fleet_group", texts.GROUP_NO_FLEETS(group="Example Ping Group")),
        )
        self.assertEqual(
            self.refusal(self.via, group=self.ping_group),
            ("fleet_group", texts.GROUP_NOT_LED(group="Example Ping Group")),
        )
        self.assertEqual(
            self.refusal(self.leader, group=self.fleet_group, channel=self.fleet_channel, pre_ping=True),
            ("fleet_timing", texts.GROUP_NO_PRE_PING()),
        )
        self.assertEqual(
            self.refusal(self.leader, group=self.fleet_group, fleet_type=self.stratop, channel=self.fleet_channel),
            (
                "fleet_type",
                texts.GROUP_FLEET_TYPE_NOT_ALLOWED(type="StratOp", group="Example Fleet Group"),
            ),
        )
        self.assertEqual(
            self.refusal(self.leader, group=self.fleet_group, channel=self.alliance),
            ("ping_channel", texts.GROUP_CHANNEL_NOT_ASSIGNED(name="Alliance Pings", group="Example Fleet Group")),
        )
        self.assertEqual(
            self.refusal(self.leader, group=self.fleet_group, channel=self.alliance, sends=False), ("", "")
        )

    def test_an_fc_who_leads_no_group_cannot_start_a_group_fleet(self):
        self.assertEqual(
            self.refusal(self.fc, group=self.fleet_group, channel=self.fleet_channel),
            ("fleet_group", texts.GROUP_NOT_LED(group="Example Fleet Group")),
        )


class GroupFleetHelperTests(GroupFixture):
    def test_the_leader_who_started_a_group_fleet_manages_it_while_they_lead_the_group(self):
        group_fleet = f.create_operation(self.leader, group=self.fleet_group, group_name=self.fleet_group.name)
        alliance_fleet = f.create_operation(self.leader)

        self.assertTrue(group_access.manages_group_fleet(fresh(self.leader), group_fleet))
        self.assertFalse(group_access.manages_group_fleet(fresh(self.via), group_fleet))
        self.assertFalse(group_access.manages_group_fleet(fresh(self.leader), alliance_fleet))
        GroupAccess.objects.filter(pk=self.fleet_access.pk).update(is_active=False)
        self.assertFalse(group_access.manages_group_fleet(fresh(self.leader), group_fleet))

    def test_alliance_fleets_cost_no_query(self):
        alliance_fleet = f.create_operation(self.leader)
        user = fresh(self.leader)
        with self.assertNumQueries(0):
            self.assertFalse(group_access.manages_group_fleet(user, alliance_fleet))
            self.assertTrue(group_access.group_srp_allowed(alliance_fleet))

    def test_visible_groups_are_the_groups_led_now(self):
        self.assertEqual(group_access.visible_group_ids(fresh(self.via)), {self.fleet_group.pk})
        self.assertEqual(group_access.visible_group_ids(fresh(self.member)), set())

    def test_srp_for_group_fleets_follows_the_group_switch(self):
        group_fleet = f.create_operation(self.leader, group=self.fleet_group, group_name=self.fleet_group.name)

        self.assertFalse(group_access.group_srp_allowed(group_fleet))
        GroupAccess.objects.filter(pk=self.fleet_access.pk).update(srp_enabled=True)
        self.assertTrue(group_access.group_srp_allowed(group_fleet))
        GroupAccess.objects.filter(pk=self.fleet_access.pk).delete()
        self.assertFalse(group_access.group_srp_allowed(group_fleet))

    def test_a_deactivated_access_row_stops_srp_like_every_other_group_power(self):
        group_fleet = f.create_operation(self.leader, group=self.fleet_group, group_name=self.fleet_group.name)
        GroupAccess.objects.filter(pk=self.fleet_access.pk).update(srp_enabled=True, is_active=False)

        self.assertFalse(group_access.group_srp_allowed(group_fleet))
        GroupAccess.objects.filter(pk=self.fleet_access.pk).update(is_active=True)
        self.assertTrue(group_access.group_srp_allowed(group_fleet))

    def test_a_group_fleet_stays_a_group_fleet_after_its_alliance_auth_group_is_deleted(self):
        group_fleet = f.create_operation(self.leader, group=self.fleet_group, group_name=self.fleet_group.name)
        GroupAccess.objects.filter(pk=self.fleet_access.pk).update(srp_enabled=True)

        self.fleet_group.delete()
        group_fleet.refresh_from_db()

        self.assertIsNone(group_fleet.group_id)
        self.assertEqual(group_fleet.group_name, "Example Fleet Group")
        self.assertTrue(group_access.is_group_fleet(group_fleet))
        self.assertFalse(group_access.group_srp_allowed(group_fleet))
        self.assertFalse(group_access.is_group_fleet(f.create_operation(self.leader)))

    def test_leaders_of_lists_direct_leaders_and_leader_group_members(self):
        outsider = f.create_user("outsider", main_name="Pilot C")
        self.leader_group.user_set.add(outsider)
        empty = f.aa_group("Example Empty")

        rows = group_access.leaders_of([self.fleet_group.pk, self.ping_group.pk, empty.pk])

        fleet = {row.name: row for row in rows[self.fleet_group.pk]}
        self.assertEqual(set(fleet), {"Pilot A", "Pilot B", "Pilot C"})
        self.assertEqual(fleet["Pilot A"].via_group, "")
        self.assertEqual(fleet["Pilot B"].via_group, "Example Fleet Leaders")
        self.assertTrue(fleet["Pilot A"].has_access)
        self.assertFalse(fleet["Pilot C"].has_access)
        self.assertEqual([row.name for row in rows[self.ping_group.pk]], ["Pilot A"])
        self.assertEqual(rows[empty.pk], [])

    def test_leaders_of_costs_the_same_with_more_leaders(self):
        with CaptureQueriesContext(connection) as small:
            group_access.leaders_of([self.fleet_group.pk])
        for number in range(5):
            extra = f.create_user(f"extra{number}", perms=f.MEMBER_PERMS)
            self.fleet_group.authgroup.group_leaders.add(extra.pk)
            self.leader_group.user_set.add(f.create_user(f"via{number}"))
        with CaptureQueriesContext(connection) as large:
            rows = group_access.leaders_of([self.fleet_group.pk, self.ping_group.pk])
        self.assertEqual(len(large), len(small))
        self.assertEqual(len(rows[self.fleet_group.pk]), 12)


def group_queries(captured):
    return [query["sql"] for query in captured if "fleetops_groupaccess" in query["sql"]]


class NavigationTests(GroupFixture):
    """The "Start Fleet" and "Send Ping" items follow the rights matrix; FCs cost no group lookup."""

    def nav(self, user):
        self.client.force_login(user)
        with CaptureQueriesContext(connection) as captured:
            html = self.client.get(reverse("fleetops:my_statistics")).content.decode()
        start = f'href="{reverse("fleetops:start_fleet")}"' in html
        ping = f'href="{reverse("fleetops:send_ping")}"' in html
        return start, ping, len(group_queries(captured))

    def test_items_per_role(self):
        only_ping = f.create_user("onlyping", perms=f.MEMBER_PERMS)
        self.ping_group.authgroup.group_leaders.add(only_ping.pk)
        fc_leader = f.create_user("fcleader", perms=f.FC_PERMS)
        self.fleet_group.authgroup.group_leaders.add(fc_leader.pk)
        cases = (
            ("member", self.member, (False, False, 1)),
            ("ping leader", only_ping, (False, True, 1)),
            ("fleets leader", self.via, (True, True, 2)),
            ("fc", self.fc, (True, True, 0)),
            ("fc and leader", fc_leader, (True, True, 0)),
            ("fc lead", self.lead, (True, True, 0)),
        )
        for label, user, expected in cases:
            with self.subTest(role=label):
                self.assertEqual(self.nav(user), expected)


class GroupDecoratorTests(GroupFixture):
    """The three group decorators: who passes, the FleetOps 403 page for pages, a plain 403 otherwise."""

    def setUp(self):
        super().setUp()
        self.only_ping = f.create_user("onlyping", perms=f.MEMBER_PERMS)
        self.ping_group.authgroup.group_leaders.add(self.only_ping.pk)

    def as_user(self, user, method, url, data=None):
        self.client.force_login(user)
        return getattr(self.client, method)(url, data or {})

    def assertDeniedPage(self, response, reason):
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, 'id="fo-denied"', status_code=403)
        self.assertContains(response, str(reason), status_code=403)

    def assertPlainDenied(self, response):
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, 'id="fo-denied"', status_code=403)

    def test_fleet_starter_required(self):
        start = reverse("fleetops:start_fleet")
        for user in (self.member, self.only_ping):
            with self.subTest(user=user.username):
                self.assertDeniedPage(self.as_user(user, "get", start), STARTER_REASON)
                self.assertPlainDenied(self.as_user(user, "post", start))
                self.assertPlainDenied(self.as_user(user, "post", reverse("fleetops:preview_fleet")))
                self.assertDeniedPage(self.as_user(user, "get", reverse("fleetops:preset_list")), STARTER_REASON)
                self.assertPlainDenied(self.as_user(user, "post", reverse("fleetops:preset_save")))
        for user in (self.via, self.fc):
            with self.subTest(user=user.username):
                self.assertEqual(self.as_user(user, "get", start).status_code, 200)

    def test_ping_sender_required(self):
        page = reverse("fleetops:send_ping")
        preview = reverse("fleetops:preview_ping")
        self.assertDeniedPage(self.as_user(self.member, "get", page), PING_SENDER_REASON)
        self.assertPlainDenied(self.as_user(self.member, "post", preview))
        for user in (self.only_ping, self.via, self.fc, self.lead):
            with self.subTest(user=user.username):
                self.assertEqual(self.as_user(user, "get", page).status_code, 200)
                response = self.as_user(user, "post", preview)
                self.assertEqual(response.status_code, 200)
                self.assertIs(response.json()["ok"], False)

    def test_a_leader_of_an_inactive_row_is_refused(self):
        GroupAccess.objects.update(is_active=False)

        self.assertDeniedPage(self.as_user(self.only_ping, "get", reverse("fleetops:send_ping")), PING_SENDER_REASON)
        self.assertDeniedPage(self.as_user(self.via, "get", reverse("fleetops:start_fleet")), STARTER_REASON)

    def test_fleet_manager_required(self):
        operation = f.create_operation(self.fc)
        edit = reverse("fleetops:edit_operation", args=[operation.uuid])
        end = reverse("fleetops:end_fleet", args=[operation.uuid])

        self.assertDeniedPage(self.as_user(self.member, "get", edit), FLEET_MANAGER_REASON)
        self.assertPlainDenied(self.as_user(self.member, "post", end))
        # A leader passes the decorator; the fleet check then answers 404 for a fleet they do not manage.
        self.assertContains(self.as_user(self.only_ping, "get", edit), 'id="fo-denied"', status_code=404)
        self.assertEqual(self.as_user(self.only_ping, "post", end).status_code, 404)
        self.assertEqual(self.as_user(self.fc, "get", edit).status_code, 200)

    def test_the_decorator_needs_parentheses(self):
        from fleetops.decorators import fleet_manager_required

        with self.assertRaises(TypeError):
            fleet_manager_required(lambda request: None)
