"""Tests for the statistics access rules."""

from django.test import TestCase

from fleetops.models import FleetOpsSettings
from fleetops.services import stats_access
from fleetops.services.stats_access import (
    can_view_corporation,
    can_view_member,
    leaderboard_access,
    leaderboard_visible,
    own_corporation,
)

from . import factories as f

MEMBER = f.MEMBER_PERMS
CORP = f.CORP_MANAGEMENT_PERMS
ALL_STATS = f.MEMBER_PERMS + ["fleetops.view_all_stats"]


def set_visibility(value):
    f.settings(leaderboard_visibility=value)


def set_alliances(*alliances):
    f.settings(history_alliance_ids=",".join(str(alliance[0]) for alliance in alliances))


class OwnCorporationTests(TestCase):
    def test_corporation_of_the_main(self):
        user = f.member("alice", perms=MEMBER, corporation=f.OTHER_CORP)
        f.add_alt(user, corporation=f.DEFAULT_CORP)
        self.assertEqual(own_corporation(user), f.OTHER_CORP)

    def test_without_a_main(self):
        self.assertIsNone(own_corporation(f.member("nomain", perms=MEMBER, with_main=False)))

    def test_applies_even_outside_the_member_alliances(self):
        user = f.member("alice", perms=CORP, alliance=f.FOREIGN_ALLIANCE)
        set_alliances(f.DEFAULT_ALLIANCE)
        self.assertEqual(own_corporation(user), f.DEFAULT_CORP)


class CanViewCorporationTests(TestCase):
    def test_roles(self):
        member = f.member("member", perms=MEMBER)
        manager = f.member("manager", perms=CORP)
        lead = f.member("lead", perms=ALL_STATS)
        for corporation_id, expected in ((f.DEFAULT_CORP[0], (False, True, True)), (f.OTHER_CORP[0], (False, False, True))):
            with self.subTest(corporation=corporation_id):
                self.assertEqual(
                    tuple(can_view_corporation(user, corporation_id) for user in (member, manager, lead)), expected
                )

    def test_manager_without_a_main_sees_no_corporation(self):
        manager = f.member("manager", perms=CORP, with_main=False)
        self.assertFalse(can_view_corporation(manager, f.DEFAULT_CORP[0]))


class CanViewMemberTests(TestCase):
    def setUp(self):
        self.target = f.member("target", perms=MEMBER)
        self.colleague = f.member("colleague", perms=CORP)
        self.manager_elsewhere = f.member("elsewhere", perms=CORP, corporation=f.OTHER_CORP)
        self.lead = f.member("lead", perms=ALL_STATS)
        self.plain = f.member("plain", perms=MEMBER)

    def test_everybody_may_see_themselves(self):
        for user in (self.target, self.colleague, self.lead, self.plain):
            with self.subTest(user=user.username):
                self.assertTrue(can_view_member(user, user))

    def test_a_member_cannot_see_others(self):
        self.assertFalse(can_view_member(self.plain, self.target))

    def test_corporation_manager_sees_their_own_corporation(self):
        self.assertTrue(can_view_member(self.colleague, self.target))

    def test_corporation_manager_does_not_see_other_corporations(self):
        self.assertFalse(can_view_member(self.manager_elsewhere, self.target))

    def test_view_all_stats_sees_every_member(self):
        self.assertTrue(can_view_member(self.lead, self.target))
        self.assertTrue(can_view_member(self.lead, self.manager_elsewhere))

    def test_a_user_id_is_accepted(self):
        self.assertTrue(can_view_member(self.lead, self.target.pk))
        self.assertFalse(can_view_member(self.plain, self.target.pk))

    def test_unknown_user(self):
        self.assertFalse(can_view_member(self.lead, 999_999))

    def test_member_outside_the_population_has_no_page_for_others(self):
        for label, user in {
            "disabled": f.member("gone", active=False),
            "no main": f.member("nomain", with_main=False),
            "biomassed": f.member("biomassed", corporation=f.DOOMHEIM_CORP),
        }.items():
            with self.subTest(label):
                self.assertFalse(can_view_member(self.lead, user))
                self.assertFalse(can_view_member(self.colleague, user))

    def test_departed_member_is_hidden_when_member_alliances_are_set(self):
        leaver = f.member("leaver", alliance=f.FOREIGN_ALLIANCE)
        self.assertTrue(can_view_member(self.lead, leaver))
        set_alliances(f.DEFAULT_ALLIANCE)
        self.assertFalse(can_view_member(self.lead, leaver))
        self.assertFalse(can_view_member(self.colleague, leaver))

    def test_a_user_outside_the_population_still_sees_themselves(self):
        leaver = f.member("leaver", alliance=f.FOREIGN_ALLIANCE, perms=MEMBER)
        set_alliances(f.DEFAULT_ALLIANCE)
        self.assertTrue(can_view_member(leaver, leaver))

    def test_corporation_follows_the_current_main(self):
        mover = f.member("mover", perms=MEMBER, corporation=f.OTHER_CORP)
        self.assertFalse(can_view_member(self.colleague, mover))
        new_main = f.add_alt(mover, "Mover New Main")
        profile = mover.profile
        profile.main_character = new_main
        profile.save()
        self.assertTrue(can_view_member(self.colleague, mover))


class LeaderboardAccessTests(TestCase):
    def setUp(self):
        self.member = f.member("member", perms=MEMBER)
        self.manager = f.member("manager", perms=CORP)
        self.lead = f.member("lead", perms=ALL_STATS)
        self.outsider = f.member("outsider", perms=[])

    def access(self, user):
        return leaderboard_access(user)

    def test_default_is_visible_to_members(self):
        self.assertEqual(FleetOpsSettings.get_solo().leaderboard_visibility, "members")
        self.assertEqual(FleetOpsSettings.get_solo().leaderboard_size, 20)

    def test_members_visibility(self):
        set_visibility("members")
        self.assertEqual(
            self.access(self.member),
            {"alliance": True, "own_corporation": f.DEFAULT_CORP, "any_corporation": False},
        )
        self.assertEqual(
            self.access(self.manager),
            {"alliance": True, "own_corporation": f.DEFAULT_CORP, "any_corporation": False},
        )
        self.assertEqual(
            self.access(self.lead),
            {"alliance": True, "own_corporation": f.DEFAULT_CORP, "any_corporation": True},
        )

    def test_managers_visibility(self):
        set_visibility("managers")
        self.assertEqual(
            self.access(self.member),
            {"alliance": False, "own_corporation": None, "any_corporation": False},
        )
        self.assertEqual(
            self.access(self.manager),
            {"alliance": False, "own_corporation": f.DEFAULT_CORP, "any_corporation": False},
        )
        self.assertEqual(
            self.access(self.lead),
            {"alliance": True, "own_corporation": None, "any_corporation": True},
        )

    def test_without_fleetops_access_nothing_is_visible_to_members(self):
        for visibility in ("members", "managers"):
            with self.subTest(visibility=visibility):
                set_visibility(visibility)
                self.assertEqual(
                    self.access(self.outsider),
                    {"alliance": False, "own_corporation": None, "any_corporation": False},
                )

    def test_member_without_a_main_has_no_corporation_board(self):
        user = f.member("nomain", perms=MEMBER, with_main=False)
        access = self.access(user)
        self.assertTrue(access["alliance"])
        self.assertIsNone(access["own_corporation"])

    def test_visible_for_the_navigation(self):
        set_visibility("members")
        self.assertTrue(leaderboard_visible(self.member))
        self.assertTrue(leaderboard_visible(self.manager))
        self.assertTrue(leaderboard_visible(self.lead))
        self.assertFalse(leaderboard_visible(self.outsider))
        set_visibility("managers")
        self.assertFalse(leaderboard_visible(self.member))
        self.assertTrue(leaderboard_visible(self.manager))
        self.assertTrue(leaderboard_visible(self.lead))
        self.assertFalse(leaderboard_visible(self.outsider))

    def test_corporation_manager_without_a_main_has_no_board_when_managers_only(self):
        set_visibility("managers")
        manager = f.member("nomain", perms=CORP, with_main=False)
        self.assertFalse(leaderboard_visible(manager))
        self.assertEqual(self.access(manager), {"alliance": False, "own_corporation": None, "any_corporation": False})

    def test_visible_agrees_with_access(self):
        users = (self.member, self.manager, self.lead, self.outsider)
        for visibility in ("members", "managers"):
            set_visibility(visibility)
            for user in users:
                with self.subTest(visibility=visibility, user=user.username):
                    access = self.access(user)
                    self.assertEqual(
                        leaderboard_visible(user),
                        bool(access["alliance"] or access["own_corporation"] or access["any_corporation"]),
                    )


class ModuleConstantsTests(TestCase):
    def test_permission_names_exist(self):
        from django.contrib.auth.models import Permission

        for name in (stats_access.BASIC_ACCESS, stats_access.VIEW_CORP_STATS, stats_access.VIEW_ALL_STATS):
            with self.subTest(name=name):
                self.assertTrue(Permission.objects.filter(codename=name.split(".")[1]).exists())
