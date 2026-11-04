# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""CAT-7: what a member sees and chooses: the category list of the form for a loss outside a fleet, the category on
the pages, and the personal limit of each category on My SRP."""

from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import translation

from fleetops.forms_srp import ClaimInputForm
from fleetops.models import SrpClaim
from fleetops.providers import zkillboard
from fleetops.srp import claims, quota

from . import factories as f



class MemberCategoryBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.srp = f.srp_settings(percentage=Decimal("100"), value_source="zkill_total", out_of_fleet_enabled=True)
        cls.user = f.create_user("pilot", perms=f.SRP_MEMBER_PERMS)
        cls.manager = f.create_user("manager", perms=f.SRP_MANAGER_PERMS)
        cls.fleet_category = f.fleet_category()

    def setUp(self):
        super().setUp()
        cache.delete(f"fleetops:srp:check:{self.user.pk}")
        patcher = mock.patch.object(
            zkillboard.requests, "get", side_effect=zkillboard.requests.ConnectionError("offline")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def killmail(self, **kwargs):
        kwargs.setdefault("total", "300000000")
        return f.srp_killmail(f.main_of(self.user), **kwargs)

    def check(self, km, data=None, user=None):
        """POST the pasted killmail to the check view; returns the response."""
        self.client.force_login(user or self.user)
        text = f"<url=killReport:{km.killmail_id}:{km.killmail_hash}>Loss</url>"
        with mock.patch.object(claims.killmails, "fetch_killmail", return_value=km):
            return self.client.post(reverse("fleetops:srp_check"), {"killmail": text, **(data or {})})


class CategoryFieldTests(MemberCategoryBase):
    def test_without_categories_the_form_is_the_old_one(self):
        form = ClaimInputForm(with_category=True)

        self.assertEqual(list(form.fields), ["killmail"])

    def test_a_category_nobody_can_choose_adds_no_field(self):
        f.srp_category("Hidden", member_selectable=False)
        f.srp_category("Off", is_active=False)

        self.assertNotIn("category", ClaimInputForm(with_category=True).fields)

    def test_the_form_without_the_option_never_has_the_field(self):
        f.srp_category("Solo PVP")

        self.assertNotIn("category", ClaimInputForm().fields)

    def test_only_active_categories_members_can_choose_are_listed(self):
        solo = f.srp_category("Solo PVP", sort_order=2)
        pve = f.srp_category("Roams", sort_order=1)
        f.srp_category("Hidden", member_selectable=False)
        f.srp_category("Off", is_active=False)

        field = ClaimInputForm(with_category=True).fields["category"]

        self.assertEqual(list(field.queryset), [pve, solo])
        self.assertFalse(field.required)
        self.assertEqual(field.empty_label, "No category (general rules)")

    def test_the_setting_makes_the_choice_required(self):
        f.srp_category("Solo PVP")
        f.srp_settings(out_of_fleet_category_required=True)

        form = ClaimInputForm({"killmail": "123"}, with_category=True)

        self.assertTrue(form.fields["category"].required)
        self.assertFalse(form.is_valid())
        self.assertIn("category", form.errors)

    def test_the_default_category_is_preselected_and_leaves_no_empty_choice(self):
        solo = f.srp_category("Solo PVP")
        f.srp_settings(default_category=solo)

        field = ClaimInputForm(with_category=True).fields["category"]

        self.assertEqual(field.initial, solo.pk)
        self.assertIsNone(field.empty_label)

    def test_a_default_category_members_cannot_choose_is_not_preselected(self):
        hidden = f.srp_category("Hidden", member_selectable=False)
        f.srp_category("Solo PVP")
        f.srp_settings(default_category=hidden)

        field = ClaimInputForm(with_category=True).fields["category"]

        self.assertIsNone(field.initial)

    def test_a_category_that_cannot_be_chosen_is_refused_by_the_form(self):
        f.srp_category("Solo PVP")
        hidden = f.srp_category("Hidden", member_selectable=False)

        form = ClaimInputForm({"killmail": "123", "category": hidden.pk}, with_category=True)

        self.assertFalse(form.is_valid())
        self.assertIn("category", form.errors)

    def test_the_built_in_category_is_named_in_the_language_of_the_viewer(self):
        f.srp_category("Solo PVP")
        self.fleet_category.member_selectable = True
        self.fleet_category.save()

        with translation.override("zh-hans"):
            labels = [label for _value, label in ClaimInputForm(with_category=True).fields["category"].choices]

        self.assertNotIn("Fleet", labels)


class HomePageTests(MemberCategoryBase):
    def test_the_form_has_no_category_list_without_categories(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("fleetops:srp_home"))

        self.assertContains(response, 'id="srp-claim-form"')
        self.assertNotContains(response, 'name="category"')

    def test_the_form_lists_the_categories(self):
        f.srp_category("Solo PVP")
        self.client.force_login(self.user)

        response = self.client.get(reverse("fleetops:srp_home"))

        self.assertContains(response, 'name="category"')
        self.assertContains(response, "Solo PVP")

    def test_the_link_page_has_no_category_list(self):
        f.srp_category("Solo PVP")
        operation = f.create_operation(self.manager)
        link = f.fleet_link(operation)
        self.client.force_login(self.user)

        response = self.client.get(reverse("fleetops:srp_link", args=[link.code]))

        self.assertNotContains(response, 'name="category"')


class CheckAndSubmitTests(MemberCategoryBase):
    def test_a_required_category_must_be_chosen(self):
        f.srp_category("Solo PVP")
        f.srp_settings(out_of_fleet_category_required=True)

        response = self.check(self.killmail())

        self.assertNotIn("preview", response.context)
        self.assertIn("category", response.context["claim_form"].errors)

    def test_the_chosen_category_decides_the_preview(self):
        solo = f.srp_category(
            "Solo PVP", loss_cap_mode="custom", loss_cap=250_000_000, percentage=Decimal("100")
        )
        f.srp_settings(out_of_fleet_category_required=True)

        response = self.check(self.killmail(total="400000000"), {"category": solo.pk})

        preview = response.context["preview"]
        self.assertEqual(preview.category, solo)
        self.assertEqual(preview.calculation.limited, 250_000_000)
        self.assertContains(response, "Solo PVP")

    def test_a_forged_category_is_refused(self):
        f.srp_category("Solo PVP")
        hidden = f.srp_category("Hidden", member_selectable=False)

        for value in (hidden.pk, self.fleet_category.pk, 999_999, "abc"):
            with self.subTest(value=value):
                cache.delete(f"fleetops:srp:check:{self.user.pk}")
                response = self.check(self.killmail(), {"category": value})
                self.assertNotIn("preview", response.context)
                self.assertIn("category", response.context["claim_form"].errors)

    def test_the_check_without_a_choice_uses_the_default_category(self):
        solo = f.srp_category("Solo PVP")
        f.srp_settings(default_category=solo)

        response = self.check(self.killmail())

        self.assertEqual(response.context["preview"].category, solo)

    def test_a_category_switched_off_after_the_check_gives_a_clear_error(self):
        solo = f.srp_category("Solo PVP")
        response = self.check(self.killmail(), {"category": solo.pk})
        token = response.context["preview"].token
        solo.is_active = False
        solo.save()

        submitted = self.client.post(reverse("fleetops:srp_submit"), {"token": token}, follow=True)

        self.assertFalse(SrpClaim.objects.exists())
        self.assertContains(submitted, "category")
        self.assertRedirects(
            self.client.post(reverse("fleetops:srp_submit"), {"token": token}),
            reverse("fleetops:srp_home"),
            fetch_redirect_response=False,
        )

    def test_the_submitted_claim_has_the_category_and_shows_it(self):
        solo = f.srp_category("Solo PVP")
        response = self.check(self.killmail(), {"category": solo.pk})
        token = response.context["preview"].token

        self.client.post(reverse("fleetops:srp_submit"), {"token": token})

        claim = SrpClaim.objects.get()
        self.assertEqual(claim.category, solo)
        page = self.client.get(reverse("fleetops:srp_claim", args=[claim.uuid]))
        self.assertContains(page, "Solo PVP")


class CategoryOnPagesTests(MemberCategoryBase):
    def setUp(self):
        super().setUp()
        self.solo = f.srp_category("Solo PVP")
        self.claim = f.srp_claim(self.user, category=self.solo)

    def test_my_srp_table_shows_the_category(self):
        self.client.force_login(self.user)

        self.assertContains(self.client.get(reverse("fleetops:srp_home")), "Solo PVP")

    def test_the_review_queue_shows_the_category(self):
        self.client.force_login(self.manager)

        self.assertContains(self.client.get(reverse("fleetops:srp_review")), "Solo PVP")

    def test_the_payment_page_shows_the_category(self):
        paid = f.srp_claim(self.user, category=self.solo, status=SrpClaim.Status.APPROVED)
        self.client.force_login(self.manager)

        response = self.client.get(reverse("fleetops:srp_payments"))

        self.assertContains(response, "Solo PVP")
        self.assertContains(response, paid.reference)

    def test_the_claim_page_shows_the_category(self):
        self.client.force_login(self.user)

        self.assertContains(self.client.get(reverse("fleetops:srp_claim", args=[self.claim.uuid])), "Solo PVP")

    def test_a_claim_in_the_built_in_category_adds_no_extra_line(self):
        operation = f.create_operation(self.manager)
        link = f.fleet_link(operation)
        f.srp_claim(f.create_user("second", perms=f.SRP_MEMBER_PERMS), link=link)
        self.client.force_login(self.manager)

        response = self.client.get(reverse("fleetops:srp_review"))

        self.assertNotContains(response, '<div class="small text-muted">Fleet</div>')

    def test_the_number_of_queries_does_not_grow_with_the_claims(self):
        self.client.force_login(self.manager)
        urls = (reverse("fleetops:srp_review"), reverse("fleetops:srp_payments"))
        for url in urls:
            self.client.get(url)  # warm the caches
        counts = []
        for extra in (0, 5):
            for number in range(extra):
                f.srp_claim(self.user, category=self.solo, amount=1_000_000 + number)
                f.srp_claim(self.user, category=self.solo, status=SrpClaim.Status.APPROVED)
            with CaptureQueriesContext(connection) as captured:
                for url in urls:
                    self.client.get(url)
            counts.append(len(captured))
        self.assertEqual(counts[0], counts[1])


class LimitRowTests(MemberCategoryBase):
    def test_every_active_category_with_a_personal_cap_gets_a_row(self):
        capped = f.srp_category("Solo PVP", personal_monthly_cap=100_000_000)
        f.srp_category("No cap")
        f.srp_category("Switched off", personal_monthly_cap=50_000_000, is_active=False)
        f.srp_claim(
            self.user,
            category=capped,
            status=SrpClaim.Status.APPROVED,
            amount=40_000_000,
            counts_personal=False,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("fleetops:srp_home"))

        rows = response.context["category_limits"]
        self.assertEqual([row.category_name for row in rows], ["Solo PVP"])
        self.assertEqual((rows[0].state.cap, rows[0].state.used, rows[0].state.remaining), (100_000_000, 40_000_000, 60_000_000))
        self.assertContains(response, "Personal monthly limit of this category.")

    def test_without_capped_categories_the_page_is_unchanged(self):
        f.srp_category("No cap")
        self.client.force_login(self.user)

        response = self.client.get(reverse("fleetops:srp_home"))

        self.assertEqual(response.context["category_limits"], ())
        self.assertNotContains(response, "Personal monthly limit of this category.")

    def test_the_rows_count_only_the_users_own_claims(self):
        capped = f.srp_category("Solo PVP", personal_monthly_cap=100_000_000)
        f.srp_claim(self.manager, category=capped, status=SrpClaim.Status.APPROVED, amount=90_000_000)

        rows = quota.category_personal_limits(user_id=self.user.pk, killmail_time=f.srp_claim(self.user).killmail_time)

        self.assertEqual(rows[0].state.used, 0)
