# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FleetOps SRP in 0.2.0b2: translated blockers, no pointless confirmation, claims calculated with earlier
settings, the SRP menu of FCs and group leaders, one fleet type name, specific reason messages, the confirmations
before dangerous decisions, the undo notice of a payment and the ESI hint of the loss check."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone, translation

from fleetops import texts
from fleetops.forms_srp import ReasonForm
from fleetops.models import GroupAccess, SrpClaim, SrpCorporationLimit, SrpPayoutRule, SrpSettings
from fleetops.providers.esi import FleetESIError
from fleetops.srp import access, claims, killmails, policy
from fleetops.srp.types import SrpError
from fleetops.views_srp import REASON_CONFIRMATIONS

from . import factories as f
from .test_srp_config import settings_post
from .test_srp_review_views import ManagerPagesTestCase
from .test_srp_views import SrpPagesTestCase, make_preview, route

Status = SrpClaim.Status
MOA = 623
CONFIG_PERMS = f.MEMBER_PERMS + ["fleetops.manage_configuration"]


def zh(client):
    client.cookies["django_language"] = "zh-hans"
    return client


class TranslatedBlockerTests(SrpPagesTestCase):
    """FB06.3: the duplicate-claim blockers are stored English texts and are shown in the language of the viewer."""

    def setUp(self):
        super().setUp()
        self.patch_home_services()
        self.prepare = self.patch("fleetops.srp.claims.prepare_claim")

    def preview(self, user, blocker):
        self.prepare.return_value = make_preview(user, blockers=[blocker])
        return self.client_for(user).post(route("srp_check"), {"killmail": "https://zkillboard.com/kill/123456/"})

    def test_the_english_preview_is_unchanged(self):
        response = self.preview(self.member, claims.OWN_DUPLICATE_TEXT)

        self.assertContains(response, "You already submitted this loss.")

    def test_an_own_duplicate_is_in_chinese_on_a_chinese_page(self):
        self.prepare.return_value = make_preview(self.member, blockers=[claims.OWN_DUPLICATE_TEXT])
        client = zh(self.client_for(self.member))

        response = client.post(route("srp_check"), {"killmail": "https://zkillboard.com/kill/123456/"})

        self.assertContains(response, "你已经提交过这笔损失。")
        self.assertNotContains(response, "You already submitted this loss.")

    def test_another_pilots_claim_is_in_chinese_for_the_pilot_and_for_a_manager(self):
        for user, text, expected in (
            (self.member, claims.OTHER_DUPLICATE_TEXT, "这笔损失已被另一位飞行员申请过。"),
            (self.manager, texts.SRP_CLAIMED_BY(name="Pilot A"), "这笔损失已被 Pilot A 申请过。"),
        ):
            with self.subTest(user=user.username):
                self.prepare.return_value = make_preview(user, blockers=[text])
                client = zh(self.client_for(user))

                response = client.post(route("srp_check"), {"killmail": "https://zkillboard.com/kill/123456/"})

                self.assertContains(response, expected)

    def test_a_blocker_that_is_already_translated_is_left_alone(self):
        self.prepare.return_value = make_preview(self.member, blockers=[policy.BLOCK_CAPSULE])
        client = zh(self.client_for(self.member))

        response = client.post(route("srp_check"), {"killmail": "https://zkillboard.com/kill/123456/"})

        self.assertContains(response, "胶囊")


class WithoutAnAutomaticAmountTests(ManagerPagesTestCase):
    """FB10.1 on the page: nothing is asked about going above a policy that pays nothing."""

    def claim(self, **extra):
        return f.srp_claim(self.member, time=self.loss_time, amount=0, suggested_amount=0, limited_amount=0, **extra)

    def test_the_box_and_the_hint_follow_the_payout_basis(self):
        none = self.claim()
        rule = self.claim(payout_basis=SrpClaim.PayoutBasis.RULE)

        without = self.client.get(route("srp_claim", none.uuid))
        with_rule = self.client.get(route("srp_claim", rule.uuid))

        self.assertNotContains(without, "Pay more than the policy amount")
        self.assertContains(without, "There is no automatic amount for this claim. Enter the amount to pay and a reason.")
        self.assertContains(with_rule, "Pay more than the policy amount")

    def test_an_amount_and_a_reason_approve_the_claim_without_the_box(self):
        claim = self.claim()

        response = self.client.post(
            route("srp_claim_action", claim.uuid, "approve"),
            {"mode": "amount", "amount": "1,000,000", "reason": "Fit was worth it", "shown_status": "pending",
             "shown_suggested": "0", "shown_limited": "0"},
        )

        self.assertRedirects(response, route("srp_claim", claim.uuid), fetch_redirect_response=False)
        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.approved_amount), (Status.APPROVED, 1_000_000))

    def test_an_amount_without_a_reason_asks_for_the_reason(self):
        claim = self.claim()

        response = self.client.post(
            route("srp_claim_action", claim.uuid, "approve"), {"mode": "amount", "amount": "1,000,000", "reason": ""}
        )

        self.assertEqual(response.status_code, 200)
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PENDING)
        self.assertContains(response, "Give a reason for this amount.")


class OutdatedClaimsTests(TestCase):
    """FB10.2: pending claims keep the settings they were calculated with; the pages say how many are outdated."""

    @classmethod
    def setUpTestData(cls):
        cls.srp = f.srp_settings(percentage=Decimal("80"))
        cls.admin = f.create_user("srp-admin", perms=[*CONFIG_PERMS, *f.SRP_MANAGER_PERMS])
        cls.config_only = f.create_user("config-only", perms=CONFIG_PERMS)
        cls.pilot = f.create_user("srp-pilot", perms=f.SRP_MEMBER_PERMS)

    def submit(self, ship_type_id=MOA, user=None, **killmail):
        user = user or self.pilot
        km = f.srp_killmail(f.main_of(user), fitted="100000000", ship_type_id=ship_type_id, **killmail)
        from django.core import signing

        token = signing.dumps({"km": km.pk, "link": None, "u": user.pk}, salt=claims.PREVIEW_SALT)
        return claims.submit_claim(user, token)

    def change(self, **values):
        for name, value in values.items():
            setattr(self.srp, name, value)
        self.srp.save()

    def test_nothing_is_outdated_while_the_settings_are_unchanged(self):
        self.submit()
        self.submit()

        self.assertEqual(policy.outdated_pending_claim_count(), 0)

    def test_a_changed_setting_makes_every_pending_claim_outdated(self):
        first, second = self.submit(), self.submit()

        self.change(percentage=Decimal("50"))

        self.assertEqual(policy.outdated_pending_claim_count(), 2)
        self.assertCountEqual(policy.outdated_pending_claims(), [first.pk, second.pk])

    def test_decided_claims_are_not_counted(self):
        claim = self.submit()
        SrpClaim.objects.filter(pk=claim.pk).update(status=Status.APPROVED)

        self.change(percentage=Decimal("50"))

        self.assertEqual(policy.outdated_pending_claim_count(), 0)

    def test_every_setting_of_the_snapshot_counts(self):
        self.submit()
        for name, value in (
            ("in_fleet_loss_cap", 5_000_000),
            ("out_of_fleet_loss_cap", 5_000_000),
            ("personal_monthly_cap", 100_000_000),
            ("corporation_monthly_cap", 100_000_000),
            ("capsules_need_rule", not self.srp.capsules_need_rule),
            ("claim_deadline_days", 12),
            ("over_limit_action", SrpSettings.OverLimitAction.REJECT),
        ):
            with self.subTest(setting=name):
                original = getattr(self.srp, name)
                self.change(**{name: value})
                self.assertEqual(policy.outdated_pending_claim_count(), 1)
                self.change(**{name: original})
                self.assertEqual(policy.outdated_pending_claim_count(), 0)

    def test_a_corporation_limit_changes_only_the_claims_of_that_corporation(self):
        far = f.create_user("far-pilot", perms=f.SRP_MEMBER_PERMS, corporation=f.OTHER_CORP)
        near = self.submit()
        self.submit(user=far)

        SrpCorporationLimit.objects.create(corporation_id=near.corporation_id, corporation_name="Home", monthly_cap=1)

        self.assertEqual(policy.outdated_pending_claims(), [near.pk])

    def test_a_payout_rule_that_changed_or_went_away_makes_its_claims_outdated(self):
        rule = f.payout_rule(MOA, 60_000_000)
        claim = self.submit()
        self.assertEqual(claim.payout_basis, SrpClaim.PayoutBasis.RULE)
        self.assertEqual(policy.outdated_pending_claim_count(), 0)

        SrpPayoutRule.objects.filter(pk=rule.pk).update(amount=70_000_000)
        self.assertEqual(policy.outdated_pending_claim_count(), 1)

        SrpPayoutRule.objects.filter(pk=rule.pk).update(amount=60_000_000)
        self.assertEqual(policy.outdated_pending_claim_count(), 0)
        SrpPayoutRule.objects.filter(pk=rule.pk).update(is_active=False)
        self.assertEqual(policy.outdated_pending_claim_count(), 1)

    def test_the_count_costs_a_fixed_number_of_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        f.payout_rule(MOA, 60_000_000)
        for _ in range(3):
            self.submit()
        self.change(percentage=Decimal("50"))

        def cost():
            with CaptureQueriesContext(connection) as queries:
                # The claims made after the change were calculated with the new settings: only three are outdated.
                self.assertEqual(policy.outdated_pending_claim_count(self.srp), 3)
            return len(queries)

        small = cost()
        for _ in range(10):
            self.submit()
        self.assertEqual(cost(), small)
        self.assertLessEqual(small, 3)

    def pages(self):
        return {
            "srp": reverse("fleetops:configuration_list", args=["srp"]),
            "srp-rules": reverse("fleetops:configuration_list", args=["srp-rules"]),
            "srp-corporation-limits": reverse("fleetops:configuration_list", args=["srp-corporation-limits"]),
        }

    def test_the_notice_names_the_number_on_the_three_pages_and_offers_the_button_to_managers(self):
        self.submit()
        self.submit()
        self.change(percentage=Decimal("50"))
        self.client.force_login(self.admin)

        for section, url in self.pages().items():
            with self.subTest(section=section):
                response = self.client.get(url)
                self.assertContains(response, "2 pending claims were calculated with earlier SRP settings.")
                self.assertContains(response, 'id="srp-recalculate-outdated"')
                self.assertContains(response, f'value="{url}"')
                self.assertContains(response, 'data-fo-confirm="Recalculate every pending claim')

    def test_one_claim_is_in_the_singular(self):
        self.submit()
        self.change(percentage=Decimal("50"))
        self.client.force_login(self.admin)

        self.assertContains(
            self.client.get(self.pages()["srp"]), "1 pending claim was calculated with earlier SRP settings."
        )

    def test_without_manage_srp_the_notice_points_to_the_review_page(self):
        self.submit()
        self.change(percentage=Decimal("50"))
        self.client.force_login(self.config_only)

        for url in self.pages().values():
            response = self.client.get(url)
            self.assertContains(response, "1 pending claim was calculated with earlier SRP settings.")
            self.assertContains(response, "An SRP manager can recalculate them on the review page.")
            self.assertNotContains(response, 'id="srp-recalculate-outdated"')

    def test_no_notice_while_nothing_is_outdated(self):
        self.submit()
        self.client.force_login(self.admin)

        for url in self.pages().values():
            self.assertNotContains(self.client.get(url), "calculated with earlier SRP settings")

    def test_other_configuration_pages_have_no_notice(self):
        self.submit()
        self.change(percentage=Decimal("50"))
        self.client.force_login(self.admin)

        for section in ("fleet-types", "templates"):
            response = self.client.get(reverse("fleetops:configuration_list", args=[section]))
            self.assertNotContains(response, "calculated with earlier SRP settings")

    def test_saving_the_settings_stays_on_the_page_while_claims_are_outdated(self):
        self.submit()
        self.client.force_login(self.admin)

        response = self.client.post(self.pages()["srp"], settings_post(percentage="50"))

        self.assertRedirects(response, self.pages()["srp"], fetch_redirect_response=False)
        self.assertContains(self.client.get(self.pages()["srp"]), "calculated with earlier SRP settings")

    def test_saving_the_settings_goes_back_to_the_index_when_nothing_is_outdated(self):
        self.client.force_login(self.admin)

        response = self.client.post(self.pages()["srp"], settings_post(percentage="50"))

        self.assertRedirects(response, reverse("fleetops:configuration_index"), fetch_redirect_response=False)

    def test_the_button_recalculates_and_the_notice_goes_away(self):
        claim = self.submit()
        self.assertEqual(claim.suggested_amount, 80_000_000)
        self.change(percentage=Decimal("50"))
        self.client.force_login(self.admin)

        response = self.client.post(
            route("srp_review_recalculate"), {"next": self.pages()["srp"]}, follow=True
        )

        self.assertEqual(response.redirect_chain[-1][0], self.pages()["srp"])
        claim.refresh_from_db()
        self.assertEqual(claim.suggested_amount, 50_000_000)
        self.assertNotContains(response, "calculated with earlier SRP settings")
        self.assertEqual(policy.outdated_pending_claim_count(), 0)

    @tag("catalog")
    def test_the_notice_in_chinese(self):
        self.submit()
        self.submit()
        self.change(percentage=Decimal("50"))
        self.client.force_login(self.admin)

        with translation.override("zh-hans"):
            response = zh(self.client).get(self.pages()["srp"])

        self.assertContains(response, "有 2 笔待审核的申请是按之前的 SRP 设置计算的。")


class SummaryMenuTests(TestCase):
    """FB13.1 and FB13.2: every FC has "My fleets"; the alliance summary names who decides."""

    @classmethod
    def setUpTestData(cls):
        f.srp_settings()
        cls.fc = f.create_user("menu-fc", perms=f.FC_PERMS)
        cls.manager = f.create_user("menu-manager", perms=f.SRP_MANAGER_PERMS)
        cls.stats = f.create_user("menu-stats", perms=[*f.MEMBER_PERMS, "fleetops.view_all_stats"])

    def test_an_fc_with_only_pap_fleets_opens_the_page_and_sees_the_menu_entry(self):
        f.create_operation(self.fc)  # a fleet without an SRP link
        self.client.force_login(self.fc)

        response = self.client.get(route("srp_summary_own_fc"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["srp_nav"]["summary"])
        self.assertTrue(response.context["srp_nav"]["own_fleets"])
        self.assertTrue(response.context["summary_access"]["own_fc"])
        self.assertContains(response, ">My fleets</a>")

    def test_a_manager_sees_a_pointer_to_the_review_page_and_no_read_only_label(self):
        self.client.force_login(self.manager)

        response = self.client.get(route("srp_summary"))

        self.assertContains(response, f'<a href="{route("srp_review")}">Review</a> page.')
        self.assertNotContains(response, "read only")

    def test_view_all_stats_is_read_only(self):
        self.client.force_login(self.stats)

        response = self.client.get(route("srp_summary"))

        self.assertContains(response, '<span class="fo-pill fo-pill-neutral" id="srp-read-only">read only</span>')
        self.assertNotContains(response, "Decisions are made on the")

    def test_the_other_tabs_never_say_read_only(self):
        self.client.force_login(self.fc)

        self.assertNotContains(self.client.get(route("srp_summary_own_fc")), "read only")


class GroupLeaderMenuTests(TestCase):
    """G18: the leader of a group that runs fleets with SRP on is the FC of those fleets as far as SRP goes."""

    @classmethod
    def setUpTestData(cls):
        f.srp_settings()
        f.settings(srp_auto_create=False)
        cls.activity = f.fleet_type("Group Activity", "0.00")
        cls.leader = f.create_user("srp-leader", perms=f.MEMBER_PERMS, main_name="Pilot A")
        cls.outsider = f.create_user("srp-outsider", perms=f.MEMBER_PERMS, main_name="Pilot B")
        cls.pilot = f.create_user("srp-group-pilot", perms=f.SRP_MEMBER_PERMS, main_name="Pilot C")
        cls.fc = f.create_user("srp-alliance-fc", perms=f.FC_PERMS, main_name="Example FC")
        cls.group = f.aa_group("Example Fleet Group", leaders=[cls.leader])
        cls.access = f.group_access(cls.group, GroupAccess.Mode.FLEETS, types=[cls.activity], srp_enabled=True)
        cls.operation = f.create_operation(
            cls.leader, group=cls.group, group_name=cls.group.name, type_obj=cls.activity, status="closed"
        )
        cls.link = f.fleet_link(cls.operation)
        cls.claim = f.srp_claim(cls.pilot, link=cls.link, status=Status.PENDING)
        cls.alliance_operation = f.create_operation(cls.fc, status="closed")
        cls.alliance_link = f.fleet_link(cls.alliance_operation)
        cls.alliance_claim = f.srp_claim(cls.pilot, link=cls.alliance_link, status=Status.PENDING)

    def nav(self, user):
        user = get_user_model().objects.get(pk=user.pk)
        user.has_perm("fleetops.basic_access")
        return access.srp_nav(user)

    def test_the_leader_has_my_fleets_in_the_menu(self):
        result = self.nav(self.leader)

        self.assertTrue(result["own_fleets"])
        self.assertTrue(result["summary"])
        self.assertTrue(result["visible"])
        self.assertFalse(result["request"])
        self.assertFalse(result["review"])

    def test_the_menu_entry_comes_with_the_page(self):
        self.client.force_login(self.leader)

        response = self.client.get(route("srp_summary_own_fc"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["summary_access"]["own_fc"])
        self.assertContains(response, self.claim.reference)
        self.assertNotContains(response, self.alliance_claim.reference)

    def test_the_leader_opens_the_claim_of_their_group_fleet_but_not_the_alliance_fleets(self):
        self.client.force_login(self.leader)

        self.assertEqual(self.client.get(route("srp_claim", self.claim.uuid)).status_code, 200)
        self.assertEqual(self.client.get(route("srp_claim", self.alliance_claim.uuid)).status_code, 404)

    def test_another_fcs_summary_stays_closed_to_the_leader(self):
        self.client.force_login(self.leader)

        self.assertEqual(self.client.get(route("srp_summary_fc", self.fc.pk)).status_code, 404)

    def test_without_the_srp_switch_the_group_has_no_entry(self):
        GroupAccess.objects.filter(pk=self.access.pk).update(srp_enabled=False)

        result = self.nav(self.leader)

        self.assertFalse(result["own_fleets"])
        self.assertFalse(result["visible"])

    def test_a_ping_group_has_no_entry_even_with_the_switch_on(self):
        GroupAccess.objects.filter(pk=self.access.pk).update(mode=GroupAccess.Mode.PING)

        self.assertFalse(self.nav(self.leader)["own_fleets"])

    def test_a_deactivated_row_has_no_entry(self):
        GroupAccess.objects.filter(pk=self.access.pk).update(is_active=False)

        self.assertFalse(self.nav(self.leader)["own_fleets"])

    def test_somebody_who_leads_no_group_has_no_entry(self):
        result = self.nav(self.outsider)

        self.assertFalse(result["own_fleets"])
        self.assertFalse(result["visible"])

    def test_the_leader_lookup_is_shared_with_the_page(self):
        self.client.force_login(self.leader)
        self.client.get(route("srp_summary_own_fc"))  # warm caches
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            self.client.get(route("srp_summary_own_fc"))
        leader_lookups = [q for q in queries if "fleetops_groupaccess" in q["sql"] and "FROM" in q["sql"]]

        # One lookup of the grants (and one of the fleet types): the navigation and the page share them.
        self.assertLessEqual(len(leader_lookups), 2)


class FleetLabelTests(TestCase):
    """FB13.4: a claim names the fleet type as FleetOps shows it everywhere: its short name."""

    def test_the_label_starts_with_the_short_name(self):
        fc = f.create_user("label-fc", perms=f.FC_PERMS)
        fleet_type = f.fleet_type("Strategic Operation", "1.00")
        fleet_type.short_name = "StratOps"
        fleet_type.save()
        operation = f.create_operation(fc, type_obj=fleet_type)

        label = claims.fleet_label(operation)

        self.assertTrue(label.startswith("StratOps — "), label)
        self.assertNotIn("Strategic Operation", label)

    def test_a_type_without_a_short_name_keeps_its_name(self):
        fc = f.create_user("label-fc2", perms=f.FC_PERMS)
        operation = f.create_operation(fc, type_obj=f.fleet_type("Roam", "1.00"))

        self.assertTrue(claims.fleet_label(operation).startswith("Roam — "))


class PayoutLabelAndShipTests(TestCase):
    """FB13.5 and FB13.6 on the payout table."""

    @classmethod
    def setUpTestData(cls):
        f.srp_settings()
        cls.admin = f.create_user("payout-admin", perms=CONFIG_PERMS)

    def setUp(self):
        self.client.force_login(self.admin)

    def test_the_list_has_a_label_column_first(self):
        f.payout_rule(MOA, 60_000_000, label="Moa standard")

        response = self.client.get(reverse("fleetops:configuration_list", args=["srp-rules"]))

        self.assertEqual([label for label, _value in response.context["rows"][0]["values"]][:2], ["Label", "Ship"])
        self.assertContains(response, "Moa standard")
        self.assertContains(response, '<th scope="col">Label</th>')

    def test_the_label_help_names_the_table_and_the_calculation(self):
        response = self.client.get(reverse("fleetops:configuration_add", args=["srp-rules"]))

        self.assertContains(response, "Optional name, shown in the payout table and in a claim&#x27;s calculation.")

    def payload(self, **values):
        data = {"label": "", "ship": "", "doctrine_choice": "", "doctrine_custom": "", "scope": "any",
                "amount": "50m", "is_active": "on", "notes": ""}
        data.update(values)
        return data

    def test_an_unknown_ship_gets_only_the_ship_error(self):
        with mock.patch("fleetops.forms_config._esi_type_by_name", return_value=None):
            response = self.client.post(
                reverse("fleetops:configuration_add", args=["srp-rules"]), self.payload(ship="Nonexistent Hull")
            )

        form = response.context["form"]
        self.assertIn("ship", form.errors)
        self.assertEqual(form.non_field_errors(), [])
        self.assertNotContains(response, "Choose a ship type, a doctrine, or both.")

    def test_no_ship_and_no_doctrine_still_gets_the_general_error(self):
        response = self.client.post(reverse("fleetops:configuration_add", args=["srp-rules"]), self.payload())

        form = response.context["form"]
        self.assertEqual(form.non_field_errors(), ["Choose a ship type, a doctrine, or both."])
        self.assertNotIn("ship", form.errors)


class ReasonMessageTests(TestCase):
    """FB13.7: an empty reason is answered with the sentence of its decision."""

    SENTENCES = {
        "reason": "Give a reason.",
        "reason for the rejection": "Give a reason for the rejection.",
        "reason for revoking the approval": "Give a reason for revoking the approval.",
        "reason for undoing the payment": "Give a reason for undoing the payment.",
        "reason for reopening the claim": "Give a reason for reopening the claim.",
    }

    def test_each_form_has_its_own_required_message(self):
        for what, sentence in self.SENTENCES.items():
            with self.subTest(what=what):
                form = ReasonForm({"reason": "  "}, what=what)
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors["reason"], [sentence])

    def test_each_form_has_its_own_message_for_a_reason_that_is_too_long(self):
        form = ReasonForm({"reason": "x" * 1001}, what="reason for revoking the approval")

        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["reason"], ["The reason for revoking the approval is too long (at most 1000 characters)."]
        )

    def test_the_messages_are_chinese_on_a_chinese_page(self):
        with translation.override("zh-hans"):
            errors = {
                what: ReasonForm({"reason": " "}, what=what).errors["reason"][0]
                for what in ("reason for the rejection", "reason for revoking the approval")
            }

        self.assertEqual(errors["reason for the rejection"], "请填写拒绝原因。")
        self.assertEqual(errors["reason for revoking the approval"], "请填写撤销通过的原因。")

    def test_the_default_form_still_works_for_a_plain_reason(self):
        self.assertTrue(ReasonForm({"reason": "Because"}).is_valid())
        self.assertEqual(ReasonForm({"reason": ""}).errors["reason"], ["Give a reason."])


class DangerConfirmationTests(ManagerPagesTestCase):
    """D22: Reject, Revoke approval and Undo payment ask before they send; the reason is not the question."""

    def page(self, claim):
        return self.client.get(route("srp_claim", claim.uuid)).content.decode()

    def button(self, html, action):
        form = html.split(f'/{action}/"', 1)[1].split("</form>", 1)[0]
        return form.split("<button", 1)[1].split("</button>", 1)[0]

    def test_reject_asks_with_the_pilot_hint(self):
        html = self.page(self.pending())

        button = self.button(html, "reject")
        self.assertIn('data-fo-confirm="Reject this claim? The pilot sees the reason."', button)
        self.assertIn('data-fo-confirm-label="Reject"', button)
        self.assertIn("btn-outline-danger", button)
        self.assertNotIn("btn-danger", button)

    def test_revoke_and_undo_payment_ask(self):
        approved = f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.manager2, time=self.loss_time)
        paid = f.srp_claim(self.member, status=Status.PAID, decided_by=self.manager2, time=self.loss_time)

        self.assertIn('data-fo-confirm="Revoke the approval?"', self.button(self.page(approved), "revoke"))
        self.assertIn(
            'data-fo-confirm="Undo the payment? The claim goes back to approved."',
            self.button(self.page(paid), "unpaid"),
        )

    def test_approve_mark_paid_and_reopen_do_not_ask(self):
        pending = self.pending()
        approved = f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.manager2, time=self.loss_time)
        rejected = f.srp_claim(self.member, status=Status.REJECTED, decided_by=self.manager2, time=self.loss_time)

        self.assertNotIn("data-fo-confirm", self.page(pending).split("Reject claim", 1)[0])
        approved_page = self.page(approved)
        self.assertNotIn("data-fo-confirm", approved_page.split("Mark paid", 1)[1].split("</form>", 1)[0])
        self.assertNotIn("data-fo-confirm", self.button(self.page(rejected), "reopen"))

    def test_the_list_of_confirmations_is_the_documented_one(self):
        self.assertEqual(set(REASON_CONFIRMATIONS), {"reject", "revoke", "unpaid"})

    def test_the_reason_stays_required_next_to_the_question(self):
        html = self.page(self.pending())
        form = html.split("/reject/", 1)[1].split("</form>", 1)[0]

        self.assertIn("required", form.split("<textarea", 1)[1].split(">", 1)[0])

    def test_the_undo_payment_form_can_be_reached_from_the_notice(self):
        paid = f.srp_claim(self.member, status=Status.PAID, decided_by=self.manager2, time=self.loss_time)

        self.assertIn('id="undo-payment"', self.page(paid))

    def test_the_recalculate_all_button_asks_in_the_shared_dialog_without_a_red_button(self):
        self.pending()

        html = self.client.get(route("srp_review")).content.decode()

        form = html.split(f'action="{route("srp_review_recalculate")}"', 1)[1].split("</form>", 1)[0]
        self.assertNotIn("data-srp-confirm", html)
        self.assertIn('data-fo-confirm-danger="false"', html.split(f'action="{route("srp_review_recalculate")}"', 1)[1][:400])
        self.assertIn("btn-outline-secondary", form)


class UndoPaymentNoticeTests(ManagerPagesTestCase):
    """D11: a single payment is announced with a way back; every other decision keeps Alliance Auth's message."""

    def test_the_notice_is_shown_once_on_the_claim_page_with_the_link_to_the_form(self):
        claim = f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.manager2, time=self.loss_time)

        response = self.client.post(route("srp_claim_action", claim.uuid, "paid"), {"note": "Sent"}, follow=True)

        html = response.content.decode()
        self.assertIn('id="fo-notices"', html)
        self.assertIn(f"{claim.reference} marked paid.", html.split('id="fo-notices"', 1)[1].split("</script>", 1)[0])
        self.assertIn("Undo payment\\u2026", html.split('id="fo-notices"', 1)[1].split("</script>", 1)[0])
        # Shown once: the next page has none.
        self.assertNotIn('id="fo-notices"', self.client.get(route("srp_claim", claim.uuid)).content.decode())
        self.assertIn('id="undo-payment"', html)

    def test_the_bulk_payment_keeps_the_message(self):
        claim = f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.manager2, time=self.loss_time)

        response = self.client.post(route("srp_payments"), {"claim": [str(claim.uuid)], f"shown-{claim.uuid}": str(claim.approved_amount)})

        self.assertNotIn("fleetops_notices", self.client.session)
        self.assertTrue(any("marked paid" in text for text in [str(m) for m in response.wsgi_request._messages]))

    def test_another_decision_keeps_the_message(self):
        claim = self.pending()

        response = self.client.post(route("srp_claim_action", claim.uuid, "reject"), {"reason": "Not covered"})

        self.assertNotIn("fleetops_notices", self.client.session)
        self.assertTrue(any("rejected" in str(m) for m in response.wsgi_request._messages))


class LossCheckEsiHintTests(TestCase):
    """UD4.6: when ESI cannot be read the pilot is told what to do next, unless the message already says it."""

    def failing(self, code, message, status=502):
        error = FleetESIError(code, message, status)
        return mock.patch.object(killmails, "get_killmail", side_effect=error)

    def read(self):
        return killmails._read_and_store(1234, "a" * 40)

    def test_a_failed_read_says_to_try_again_in_a_minute(self):
        with self.failing("ESI_ERROR", texts.KILLMAIL_READ_FAILED(detail=texts.ESI_HTTP_PLAIN(status=502))):
            with self.assertRaises(SrpError) as caught:
                self.read()

        self.assertEqual(caught.exception.code, "esi_unavailable")
        self.assertEqual(
            caught.exception.message, "Could not read the killmail: ESI returned HTTP 502. Try again in a minute."
        )

    def test_a_message_that_already_says_so_is_not_repeated(self):
        with self.failing("ESI_ERROR", texts.KILLMAIL_READ_FAILED(detail=texts.ESI_ERROR_LIMIT_MINUTE()), 420):
            with self.assertRaises(SrpError) as caught:
                self.read()

        self.assertEqual(caught.exception.message.lower().count("try again"), 1)

    def test_a_killmail_that_does_not_exist_gets_no_hint(self):
        with self.failing("KILLMAIL_NOT_FOUND", texts.KILLMAIL_NOT_FOUND(), 404):
            with self.assertRaises(SrpError) as caught:
                self.read()

        self.assertEqual(caught.exception.code, "not_found")
        self.assertNotIn("Try again", caught.exception.message)

    @tag("catalog")
    def test_the_sentence_is_chinese_on_a_chinese_page(self):
        message = f"{texts.KILLMAIL_READ_FAILED(detail=texts.ESI_HTTP_PLAIN(status=502))} {texts.SRP_TRY_AGAIN()}"

        with translation.override("zh-hans"):
            shown = texts.translate_stored(message)

        self.assertTrue(shown.endswith("请一分钟后再试。"), shown)
        self.assertNotIn("Try again", shown)
