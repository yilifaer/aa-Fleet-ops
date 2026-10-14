"""Who may see and decide what in FleetOps SRP (fleetops.srp.access)."""

from django.contrib.auth.models import AnonymousUser
from django.test import TestCase

from fleetops.models import SrpClaim, SrpSettings
from fleetops.srp import access, review

from . import factories as f

STATS_ALL = f.MEMBER_PERMS + ["fleetops.view_all_stats"]
STATS_CORP = f.MEMBER_PERMS + ["fleetops.view_corp_stats"]
FC_REQUEST = f.FC_PERMS + ["fleetops.request_srp"]


def claim_ids(user):
    return set(access.viewable_claims(user).values_list("pk", flat=True))


class AccessBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        f.srp_settings()
        cls.plain = f.create_user("plain", perms=f.MEMBER_PERMS)
        cls.requester = f.create_user("requester", perms=f.SRP_MEMBER_PERMS)
        cls.other_requester = f.create_user("other-requester", perms=f.SRP_MEMBER_PERMS)
        cls.manager = f.create_user("manager", perms=f.SRP_MANAGER_PERMS)
        cls.second_manager = f.create_user("second-manager", perms=f.SRP_MANAGER_PERMS)
        cls.stats_all = f.create_user("stats-all", perms=STATS_ALL)
        cls.corp_viewer = f.create_user("corp-viewer", perms=STATS_CORP)
        cls.other_corp_viewer = f.create_user("other-corp-viewer", perms=STATS_CORP, corporation=f.OTHER_CORP)
        cls.fc = f.create_user("fc", perms=FC_REQUEST)
        cls.other_fc = f.create_user("other-fc", perms=FC_REQUEST)
        cls.operation = f.create_operation(cls.fc)
        cls.link = f.fleet_link(cls.operation)

        cls.own_out = f.srp_claim(cls.requester)
        cls.own_in = f.srp_claim(cls.requester, link=cls.link)
        cls.other_out = f.srp_claim(cls.other_requester)
        cls.other_corp_claim = f.srp_claim(f.create_user("far-pilot", corporation=f.OTHER_CORP, perms=f.SRP_MEMBER_PERMS))
        cls.manager_claim = f.srp_claim(cls.manager)
        cls.all_claims = [cls.own_out, cls.own_in, cls.other_out, cls.other_corp_claim, cls.manager_claim]


class PermissionHelperTests(AccessBase):
    def test_can_request_needs_request_srp(self):
        self.assertTrue(access.can_request(self.requester))
        self.assertTrue(access.can_request(self.manager))
        self.assertFalse(access.can_request(self.plain))
        self.assertFalse(access.can_request(self.stats_all))

    def test_is_manager_needs_manage_srp(self):
        self.assertTrue(access.is_manager(self.manager))
        self.assertFalse(access.is_manager(self.requester))
        self.assertFalse(access.is_manager(self.stats_all))
        self.assertFalse(access.is_manager(self.fc))

    def test_anonymous_and_inactive_users_have_nothing(self):
        anonymous = AnonymousUser()
        inactive = f.create_user("gone", perms=f.SRP_MANAGER_PERMS)
        inactive.is_active = False
        inactive.save()
        for user in (anonymous, inactive):
            with self.subTest(user=str(user)):
                self.assertFalse(access.can_request(user))
                self.assertFalse(access.is_manager(user))
                self.assertFalse(access.can_view_claim(user, self.own_out))
                self.assertFalse(access.can_view_alliance_summary(user))
                self.assertFalse(access.can_view_corporation_summary(user, f.DEFAULT_CORP[0]))
                self.assertFalse(access.can_view_fc_summary(user, self.fc.pk))
        self.assertEqual(claim_ids(anonymous), set())

    def test_internal_notes_are_for_managers_only(self):
        self.assertTrue(access.can_view_internal_notes(self.manager))
        for user in (self.requester, self.stats_all, self.corp_viewer, self.fc, self.plain):
            with self.subTest(user=user.username):
                self.assertFalse(access.can_view_internal_notes(user))


class CanReviewTests(AccessBase):
    def test_a_manager_decides_on_other_claims(self):
        self.assertTrue(access.can_review(self.manager, self.own_out))
        self.assertTrue(access.can_review(self.second_manager, self.manager_claim))

    def test_a_manager_does_not_decide_on_their_own_claim(self):
        self.assertFalse(access.can_review(self.manager, self.manager_claim))

    def test_the_setting_allows_self_review(self):
        f.srp_settings(allow_self_review=True)

        self.assertTrue(access.can_review(self.manager, self.manager_claim))

    def test_without_manage_srp_nobody_decides(self):
        f.srp_settings(allow_self_review=True)
        for user in (self.requester, self.stats_all, self.corp_viewer, self.fc, self.plain):
            with self.subTest(user=user.username):
                self.assertFalse(access.can_review(user, self.own_out))
                self.assertFalse(access.can_review(user, self.manager_claim))

    def test_a_claim_without_user_can_be_decided_by_any_manager(self):
        SrpClaim.objects.filter(pk=self.manager_claim.pk).update(user=None)
        self.manager_claim.refresh_from_db()

        self.assertTrue(access.can_review(self.manager, self.manager_claim))

    def test_settings_object_can_be_passed_in(self):
        settings_obj = SrpSettings.get_solo()
        self.manager.has_perm("fleetops.manage_srp")

        with self.assertNumQueries(0):
            self.assertFalse(access.can_review(self.manager, self.manager_claim, settings_obj=settings_obj))
        settings_obj.allow_self_review = True
        with self.assertNumQueries(0):
            self.assertTrue(access.can_review(self.manager, self.manager_claim, settings_obj=settings_obj))

    def test_review_module_uses_the_same_rule(self):
        self.assertFalse(review.can_review(self.manager, self.manager_claim))
        self.assertTrue(review.can_review(self.second_manager, self.manager_claim))
        self.assertFalse(review.can_review(self.requester, self.own_out))


class ViewClaimTests(AccessBase):
    def visible(self, user):
        return {claim.pk for claim in self.all_claims if access.can_view_claim(user, claim)}

    def test_the_claimant_sees_their_own_claims_only(self):
        self.assertEqual(self.visible(self.requester), {self.own_out.pk, self.own_in.pk})
        self.assertEqual(self.visible(self.other_requester), {self.other_out.pk})

    def test_the_claimant_needs_no_permission_to_see_their_history(self):
        # request_srp may be taken away later; the claims stay readable.
        self.assertEqual(self.visible(self.plain), set())
        claim = f.srp_claim(self.plain)
        self.assertTrue(access.can_view_claim(self.plain, claim))

    def test_managers_see_everything(self):
        self.assertEqual(self.visible(self.manager), {claim.pk for claim in self.all_claims})

    def test_view_all_stats_sees_everything(self):
        self.assertEqual(self.visible(self.stats_all), {claim.pk for claim in self.all_claims})

    def test_view_all_stats_is_read_only(self):
        for claim in self.all_claims:
            self.assertFalse(access.can_review(self.stats_all, claim))
        self.assertFalse(access.can_view_internal_notes(self.stats_all))

    def test_view_corp_stats_sees_the_claims_of_their_corporation(self):
        self.assertEqual(
            self.visible(self.corp_viewer),
            {self.own_out.pk, self.own_in.pk, self.other_out.pk, self.manager_claim.pk},
        )
        self.assertEqual(self.visible(self.other_corp_viewer), {self.other_corp_claim.pk})

    def test_the_fc_sees_the_in_fleet_claims_of_their_fleets(self):
        self.assertEqual(self.visible(self.fc), {self.own_in.pk})
        self.assertEqual(self.visible(self.other_fc), set())

    def test_an_out_of_fleet_claim_is_not_a_fleet_claim_of_the_fc(self):
        out_of_fleet = f.srp_claim(self.other_requester, fc_user=self.fc)

        self.assertEqual(out_of_fleet.scope, SrpClaim.Scope.OUT_OF_FLEET)
        self.assertFalse(access.can_view_claim(self.fc, out_of_fleet))

    def test_the_corporation_of_the_claim_is_the_one_stored_when_it_was_made(self):
        pilot = f.create_user("mover", perms=f.SRP_MEMBER_PERMS)
        claim = f.srp_claim(pilot)
        main = f.main_of(pilot)
        main.corporation_id, main.corporation_name = f.OTHER_CORP
        main.save()

        self.assertTrue(access.can_view_claim(self.corp_viewer, claim))
        self.assertFalse(access.can_view_claim(self.other_corp_viewer, claim))

    def test_view_corp_stats_without_a_corporation_sees_nothing_of_others(self):
        lost = f.create_user("lost", perms=STATS_CORP, with_main=False)

        self.assertEqual(self.visible(lost), set())
        self.assertEqual(claim_ids(lost), set())

    def test_member_without_stats_permissions_sees_nothing_of_others(self):
        self.assertEqual(self.visible(self.plain), set())

    def test_viewable_claims_match_the_single_claim_check(self):
        users = [
            self.plain,
            self.requester,
            self.other_requester,
            self.manager,
            self.stats_all,
            self.corp_viewer,
            self.other_corp_viewer,
            self.fc,
            self.other_fc,
        ]
        for user in users:
            with self.subTest(user=user.username):
                self.assertEqual(claim_ids(user), self.visible(user))


class ViewLimitsTests(AccessBase):
    """The monthly usage adds up more than the one claim: the FC of the fleet does not read it."""

    def limits(self, user, claim):
        return access.can_view_limits(user, claim)

    def test_the_claimant_sees_the_usage_of_their_claims(self):
        self.assertTrue(self.limits(self.requester, self.own_in))
        self.assertTrue(self.limits(self.requester, self.own_out))
        self.assertFalse(self.limits(self.requester, self.other_out))

    def test_managers_and_view_all_stats_see_it_on_every_claim(self):
        for user in (self.manager, self.stats_all):
            for claim in self.all_claims:
                with self.subTest(user=user.username, claim=claim.pk):
                    self.assertTrue(self.limits(user, claim))

    def test_view_corp_stats_sees_it_for_the_own_corporation_only(self):
        self.assertTrue(self.limits(self.corp_viewer, self.own_in))
        self.assertFalse(self.limits(self.corp_viewer, self.other_corp_claim))
        self.assertTrue(self.limits(self.other_corp_viewer, self.other_corp_claim))
        self.assertFalse(self.limits(self.other_corp_viewer, self.own_in))

    def test_the_fc_of_an_in_fleet_claim_does_not_see_it(self):
        self.assertTrue(access.can_view_claim(self.fc, self.own_in))
        self.assertFalse(self.limits(self.fc, self.own_in))

    def test_nobody_else_sees_it(self):
        for user in (self.plain, self.other_requester, self.other_fc, AnonymousUser()):
            with self.subTest(user=getattr(user, "username", "anonymous")):
                self.assertFalse(self.limits(user, self.own_in))

    def test_a_fc_who_is_also_in_the_corporation_of_the_claim_with_corp_stats_sees_it(self):
        both = f.create_user("fc-and-corp", perms=[*FC_REQUEST, "fleetops.view_corp_stats"])
        self.assertTrue(self.limits(both, self.own_in))


class SummaryAccessTests(AccessBase):
    def test_managers_and_view_all_stats_see_every_summary(self):
        for user in (self.manager, self.stats_all):
            with self.subTest(user=user.username):
                result = access.summary_access(user)
                self.assertTrue(result["alliance"] and result["corporations"] and result["fcs"])
                self.assertEqual(result["own_corporation"], f.DEFAULT_CORP)

    def test_view_corp_stats_sees_their_own_corporation_only(self):
        result = access.summary_access(self.corp_viewer)

        self.assertFalse(result["alliance"] or result["corporations"] or result["fcs"])
        self.assertEqual(result["own_corporation"], f.DEFAULT_CORP)
        self.assertEqual(access.summary_access(self.other_corp_viewer)["own_corporation"], f.OTHER_CORP)

    def test_view_corp_stats_without_a_main_has_no_corporation(self):
        lost = f.create_user("lost", perms=STATS_CORP, with_main=False)

        self.assertIsNone(access.summary_access(lost)["own_corporation"])

    def test_an_fc_with_an_srp_link_sees_their_fleets(self):
        result = access.summary_access(self.fc)

        self.assertTrue(result["own_fc"])
        self.assertFalse(result["alliance"] or result["corporations"] or result["fcs"])
        self.assertIsNone(result["own_corporation"])
        self.assertFalse(access.summary_access(self.other_fc)["own_fc"])

    def test_a_member_has_no_summary(self):
        self.assertEqual(
            access.summary_access(self.requester),
            {"alliance": False, "corporations": False, "fcs": False, "own_corporation": None, "own_fc": False},
        )

    def test_anonymous_has_no_summary(self):
        self.assertEqual(
            access.summary_access(AnonymousUser()),
            {"alliance": False, "corporations": False, "fcs": False, "own_corporation": None, "own_fc": False},
        )

    def test_summary_checks_for_single_objects(self):
        self.assertTrue(access.can_view_alliance_summary(self.manager))
        self.assertTrue(access.can_view_alliance_summary(self.stats_all))
        self.assertFalse(access.can_view_alliance_summary(self.corp_viewer))
        self.assertFalse(access.can_view_alliance_summary(self.fc))

        corporation = f.DEFAULT_CORP[0]
        other = f.OTHER_CORP[0]
        self.assertTrue(access.can_view_corporation_summary(self.manager, other))
        self.assertTrue(access.can_view_corporation_summary(self.stats_all, other))
        self.assertTrue(access.can_view_corporation_summary(self.corp_viewer, corporation))
        self.assertFalse(access.can_view_corporation_summary(self.corp_viewer, other))
        self.assertFalse(access.can_view_corporation_summary(self.requester, corporation))

        self.assertTrue(access.can_view_fc_summary(self.manager, self.fc.pk))
        self.assertTrue(access.can_view_fc_summary(self.stats_all, self.fc.pk))
        self.assertTrue(access.can_view_fc_summary(self.fc, self.fc.pk))
        self.assertFalse(access.can_view_fc_summary(self.other_fc, self.fc.pk))
        self.assertFalse(access.can_view_fc_summary(self.corp_viewer, self.fc.pk))


class NavTests(AccessBase):
    def nav(self, user):
        # Permissions are cached on the user after the first check, as during a request.
        user.has_perm("fleetops.basic_access")
        return access.srp_nav(user)

    def test_anonymous_sees_no_menu(self):
        self.assertEqual(
            access.srp_nav(AnonymousUser()),
            {"enabled": False, "request": False, "review": False, "pending_count": 0, "summary": False, "visible": False},
        )

    def test_a_member_without_srp_permissions_costs_no_query(self):
        self.plain.has_perm("fleetops.basic_access")

        with self.assertNumQueries(0):
            result = access.srp_nav(self.plain)

        self.assertFalse(result["visible"])

    def test_requesters_see_my_srp_when_srp_is_enabled(self):
        result = self.nav(self.requester)

        self.assertEqual(
            result,
            {"enabled": True, "request": True, "review": False, "pending_count": 0, "summary": False, "visible": True},
        )

    def test_my_srp_is_hidden_while_srp_is_disabled(self):
        f.srp_settings(enabled=False)

        result = self.nav(self.requester)

        self.assertFalse(result["enabled"])
        self.assertFalse(result["request"])
        self.assertFalse(result["visible"])

    def test_managers_still_review_while_srp_is_disabled(self):
        f.srp_settings(enabled=False)

        result = self.nav(self.manager)

        self.assertTrue(result["review"])
        self.assertFalse(result["request"])
        self.assertTrue(result["summary"])
        self.assertTrue(result["visible"])

    def test_requesters_cost_one_query(self):
        self.requester.has_perm("fleetops.basic_access")

        with self.assertNumQueries(1):
            access.srp_nav(self.requester)

    def test_managers_cost_two_queries(self):
        self.manager.has_perm("fleetops.basic_access")

        with self.assertNumQueries(2):
            access.srp_nav(self.manager)

    def test_pending_count_is_one_count_of_claims_the_manager_can_decide(self):
        # Pending: own_out, own_in, other_out, other_corp_claim and the manager's own claim.
        f.srp_claim(self.requester, status=SrpClaim.Status.APPROVED)
        f.srp_claim(self.requester, status=SrpClaim.Status.REJECTED)
        f.srp_claim(self.requester, status=SrpClaim.Status.WITHDRAWN)

        self.assertEqual(self.nav(self.manager)["pending_count"], 4)
        self.assertEqual(self.nav(self.second_manager)["pending_count"], 5)

    def test_pending_count_includes_own_claims_when_self_review_is_allowed(self):
        f.srp_settings(allow_self_review=True)

        self.assertEqual(self.nav(self.manager)["pending_count"], 5)

    def test_pending_count_is_for_managers_only(self):
        self.assertEqual(self.nav(self.requester)["pending_count"], 0)
        self.assertEqual(self.nav(self.stats_all)["pending_count"], 0)

    def test_summary_entries(self):
        self.assertTrue(self.nav(self.stats_all)["summary"])
        self.assertTrue(self.nav(self.corp_viewer)["summary"])
        self.assertTrue(self.nav(self.fc)["summary"])
        self.assertFalse(self.nav(self.other_fc)["summary"])
        self.assertFalse(self.nav(self.requester)["summary"])

    def test_view_corp_stats_without_a_corporation_has_no_summary(self):
        lost = f.create_user("lost", perms=STATS_CORP, with_main=False)

        self.assertFalse(self.nav(lost)["summary"])

    def test_view_all_stats_sees_the_summary_but_not_the_review(self):
        result = self.nav(self.stats_all)

        self.assertTrue(result["summary"])
        self.assertFalse(result["review"])
        self.assertFalse(result["request"])
        self.assertTrue(result["visible"])

    def test_an_fc_without_fleet_links_costs_one_more_query_and_sees_no_summary(self):
        self.other_fc.has_perm("fleetops.basic_access")

        with self.assertNumQueries(2):
            result = access.srp_nav(self.other_fc)

        self.assertFalse(result["summary"])
        self.assertTrue(result["request"])
