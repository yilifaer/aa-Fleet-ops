# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""REV-10: the insurance estimate next to SRP amounts."""

from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase

from fleetops import tasks
from fleetops.models import SrpClaim, SrpInsurancePrice, SrpSettings
from fleetops.providers import esi_killmails
from fleetops.providers.esi import FleetESIError
from fleetops.srp import insurance

from . import factories as f
from .test_srp_views import SrpPagesTestCase, route

PRICES = {
    587: {
        "basic": (Decimal("10.5"), Decimal("100.5")),
        "gold": (Decimal("500000"), Decimal("900000")),
        "platinum": (Decimal("750000"), Decimal("1500000")),
    },
    588: {"platinum": (Decimal("1"), Decimal("3"))},
}


class RefreshTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_the_whole_table_is_replaced(self):
        SrpInsurancePrice.objects.create(ship_type_id=999, levels={})
        with mock.patch.object(esi_killmails, "get_insurance_prices", return_value=PRICES):
            stored = insurance.refresh_prices()

        self.assertEqual(stored, 2)
        self.assertEqual(set(SrpInsurancePrice.objects.values_list("pk", flat=True)), {587, 588})
        row = SrpInsurancePrice.objects.get(pk=587)
        self.assertEqual(row.levels["platinum"], {"cost": "750000", "payout": "1500000"})
        self.assertEqual(row.levels["basic"]["cost"], "10.5")

    def test_a_second_refresh_removes_ship_types_that_are_gone(self):
        with mock.patch.object(esi_killmails, "get_insurance_prices", return_value=PRICES):
            insurance.refresh_prices()
        with mock.patch.object(esi_killmails, "get_insurance_prices", return_value={588: PRICES[588]}):
            insurance.refresh_prices()
        self.assertEqual(list(SrpInsurancePrice.objects.values_list("pk", flat=True)), [588])

    def test_a_failed_read_keeps_the_old_prices_and_the_task_logs_one_line(self):
        SrpInsurancePrice.objects.create(ship_type_id=587, levels={"platinum": {"cost": "1", "payout": "2"}})
        error = FleetESIError("ESI_ERROR", "down")
        with mock.patch.object(esi_killmails, "get_insurance_prices", side_effect=error):
            with self.assertLogs("extensions", "WARNING") as logs:
                self.assertEqual(tasks.refresh_srp_insurance(), 0)
        self.assertEqual(SrpInsurancePrice.objects.count(), 1)
        self.assertEqual(len([line for line in logs.output if "insurance" in line.lower()]), 1)
        # The next try is an hour away, not a day.
        self.assertTrue(cache.get(insurance.REFRESH_KEY))

    def test_an_empty_answer_keeps_the_old_prices(self):
        SrpInsurancePrice.objects.create(ship_type_id=587, levels={})
        with mock.patch.object(esi_killmails, "get_insurance_prices", return_value={}):
            self.assertEqual(insurance.refresh_prices(), 0)
        self.assertEqual(SrpInsurancePrice.objects.count(), 1)

    def test_a_database_error_while_storing_rolls_the_delete_back(self):
        SrpInsurancePrice.objects.create(ship_type_id=587, levels={})
        with mock.patch.object(esi_killmails, "get_insurance_prices", return_value=PRICES):
            with mock.patch.object(SrpInsurancePrice.objects, "bulk_create", side_effect=RuntimeError("full")):
                with self.assertRaises(RuntimeError):
                    insurance.refresh_prices()
        self.assertEqual(list(SrpInsurancePrice.objects.values_list("pk", flat=True)), [587])


class QueueTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        f.srp_settings(show_insurance=True)

    def test_it_is_queued_once_a_day(self):
        with mock.patch.object(tasks.refresh_srp_insurance, "delay") as delay:
            self.assertTrue(insurance.queue_refresh())
            self.assertFalse(insurance.queue_refresh())
            self.assertFalse(insurance.queue_refresh())
        delay.assert_called_once_with()

    def test_the_minute_beat_queues_it(self):
        with mock.patch.object(tasks.refresh_srp_insurance, "delay") as delay:
            tasks.schedule_active_fleet_tracking()
            tasks.schedule_active_fleet_tracking()
        delay.assert_called_once_with()

    def test_nothing_is_queued_or_read_while_it_is_off(self):
        for values in ({"show_insurance": False}, {"enabled": False}):
            with self.subTest(values=values):
                cache.clear()
                f.srp_settings(**{"show_insurance": True, "enabled": True, **values})
                with mock.patch.object(tasks.refresh_srp_insurance, "delay") as delay:
                    self.assertFalse(insurance.queue_refresh())
                delay.assert_not_called()

    def test_a_broker_problem_lets_the_next_beat_try_again(self):
        with mock.patch.object(tasks.refresh_srp_insurance, "delay", side_effect=OSError("broker")):
            self.assertFalse(insurance.queue_refresh())
        with mock.patch.object(tasks.refresh_srp_insurance, "delay") as delay:
            self.assertTrue(insurance.queue_refresh())
        delay.assert_called_once_with()


class EstimateTests(TestCase):
    def setUp(self):
        SrpInsurancePrice.objects.create(
            ship_type_id=587,
            levels={
                "basic": {"cost": "10.5", "payout": "100.5"},
                "gold": {"cost": "500000", "payout": "900000"},
                "platinum": {"cost": "750000", "payout": "1500000"},
            },
        )

    def test_the_net_payout_is_payout_minus_cost_of_the_chosen_level(self):
        srp = f.srp_settings(insurance_level="platinum")
        line = insurance.estimate(587, 80_000_000, srp)
        self.assertEqual(line, {"compensation": 80_000_000, "insurance": 750_000, "total": 80_750_000, "level": "platinum"})

    def test_another_level_changes_the_line(self):
        srp = f.srp_settings(insurance_level="gold")
        self.assertEqual(insurance.estimate(587, 1_000_000, srp)["insurance"], 400_000)
        srp = f.srp_settings(insurance_level="basic")
        self.assertEqual(insurance.estimate(587, 1_000_000, srp)["insurance"], 90)

    def test_a_ship_without_data_shows_no_insurance_and_the_total_is_the_compensation(self):
        srp = f.srp_settings()
        line = insurance.estimate(12345, 5_000_000, srp)
        self.assertEqual((line["insurance"], line["total"]), (None, 5_000_000))

    def test_a_level_the_ship_does_not_have_counts_as_no_data(self):
        srp = f.srp_settings(insurance_level="silver")
        self.assertIsNone(insurance.estimate(587, 1, srp)["insurance"])

    def test_nothing_while_it_is_off(self):
        self.assertIsNone(insurance.estimate(587, 1, f.srp_settings(show_insurance=False)))

    def test_damaged_stored_values_count_as_no_data(self):
        for levels in ({"platinum": "x"}, {"platinum": {"cost": "a", "payout": "b"}}, {"platinum": {"cost": "1"}}, "x", []):
            with self.subTest(levels=levels):
                SrpInsurancePrice.objects.filter(pk=587).update(levels=levels)
                self.assertIsNone(insurance.estimate(587, 1, f.srp_settings())["insurance"])

    def test_the_settings_know_the_six_levels(self):
        field = SrpSettings._meta.get_field("insurance_level")
        self.assertEqual([value for value, _label in field.choices], ["basic", "standard", "bronze", "silver", "gold", "platinum"])
        self.assertEqual(field.default, "platinum")
        self.assertTrue(SrpSettings._meta.get_field("show_insurance").default)


class DisplayTests(SrpPagesTestCase):
    def setUp(self):
        super().setUp()
        SrpInsurancePrice.objects.create(
            ship_type_id=f.RIFTER, levels={"platinum": {"cost": "750000", "payout": "1500000"}, "gold": {"cost": "1", "payout": "1000001"}}
        )
        self.claim = f.srp_claim(self.member, amount=80_000_000, time=self.loss_time)

    def test_the_claimant_and_staff_see_the_line_on_the_claim_page(self):
        for user in (self.member, self.manager):
            with self.subTest(user=user.username):
                page = self.client_for(user).get(route("srp_claim", self.claim.uuid))
                self.assertContains(page, "srp-insurance-line")
                self.assertContains(page, "80,750,000 ISK")
                self.assertContains(page, "compensation 80,000,000 ISK")
                self.assertContains(page, "insurance payout 750,000 ISK")
                self.assertContains(page, "only paid out if the pilot insured the ship")

    def test_an_approved_claim_uses_the_approved_amount(self):
        claim = f.srp_claim(self.member, status=SrpClaim.Status.APPROVED, amount=80_000_000, approved_amount=60_000_000, decided_by=self.manager)
        page = self.client_for(self.member).get(route("srp_claim", claim.uuid))
        self.assertContains(page, "60,750,000 ISK")

    def test_the_level_of_the_settings_decides(self):
        SrpSettings.objects.filter(pk=1).update(insurance_level="gold")
        page = self.client_for(self.member).get(route("srp_claim", self.claim.uuid))
        self.assertContains(page, "insurance payout 1,000,000 ISK")

    def test_a_ship_without_prices_shows_a_dash_and_the_page_works(self):
        SrpInsurancePrice.objects.all().delete()
        page = self.client_for(self.member).get(route("srp_claim", self.claim.uuid))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "srp-insurance-line")
        self.assertNotContains(page, "80,750,000")

    def test_nothing_is_shown_when_it_is_off(self):
        SrpSettings.objects.filter(pk=1).update(show_insurance=False)
        page = self.client_for(self.member).get(route("srp_claim", self.claim.uuid))
        self.assertNotContains(page, "srp-insurance-line")

    def test_it_changes_no_amount_and_is_not_in_the_exports_or_the_payment_page(self):
        approved = f.srp_claim(self.member, status=SrpClaim.Status.APPROVED, amount=80_000_000, decided_by=self.manager)
        before = (self.claim.suggested_amount, self.claim.limited_amount, approved.approved_amount)

        page = self.client_for(self.manager).get(route("srp_payments"))
        self.claim.refresh_from_db()
        approved.refresh_from_db()

        self.assertEqual((self.claim.suggested_amount, self.claim.limited_amount, approved.approved_amount), before)
        self.assertNotContains(page, "srp-insurance-line")
        self.assertEqual(page.context["to_pay_total"], 80_000_000)

    def test_the_preview_shows_the_line(self):
        from fleetops.srp.types import Calculation, ClaimPreview

        km = f.srp_killmail(f.main_of(self.member))
        preview = ClaimPreview(
            killmail=km,
            scope=SrpClaim.Scope.OUT_OF_FLEET,
            link=None,
            user=self.member,
            calculation=Calculation(suggested=10_000_000, limited=10_000_000),
        )
        from django.test import RequestFactory

        from fleetops.views_srp import preview_response

        request = RequestFactory().get("/")
        request.user = self.member
        with mock.patch("fleetops.views_srp.srp_render") as render:
            preview_response(request, preview, settings_obj=SrpSettings.get_solo())
        context = render.call_args.args[2]
        self.assertEqual(context["insurance"]["total"], 10_750_000)
