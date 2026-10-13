"""The month of the statistics pages: EVE time, neighbour month links, kept filters and the navigation."""

import html
import re
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

import fleetops
from fleetops.models import FleetType
from fleetops.views import _period_choices, _period_render_context, _year_month

from . import factories as f

UTC = dt_timezone.utc


def at(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def clock(moment):
    """The site clock stands at ``moment``."""
    return mock.patch("django.utils.timezone.now", return_value=moment)


def query_of(href):
    return parse_qs(urlsplit(html.unescape(href)).query)


class YearMonthTests(TestCase):
    def month(self, query=""):
        return _year_month(RequestFactory().get(f"/fleetops/statistics/me/{query}"))

    @override_settings(TIME_ZONE="Asia/Shanghai")
    def test_default_is_the_current_eve_month_whatever_the_site_time_zone(self):
        # 04:00 on 1 April in Shanghai is still 31 March in EVE time.
        with clock(at(2026, 3, 31, 20)):
            self.assertEqual(self.month(), (2026, 3))
        with clock(at(2026, 12, 31, 23, 30)):
            self.assertEqual(self.month(), (2026, 12))

    @override_settings(TIME_ZONE="Pacific/Auckland")
    def test_default_follows_eve_time_for_a_zone_behind_utc_too(self):
        # Already 1 January in Auckland, 31 December in EVE time.
        with clock(at(2026, 12, 31, 12)):
            self.assertEqual(self.month(), (2026, 12))
        with clock(at(2027, 1, 1, 0, 0)):
            self.assertEqual(self.month(), (2027, 1))

    def test_month_from_the_query(self):
        with clock(at(2026, 10, 13)):
            self.assertEqual(self.month("?year=2025&month=7"), (2025, 7))
            self.assertEqual(self.month("?year=2025"), (2025, 10))
            self.assertEqual(self.month("?month=2"), (2026, 2))

    def test_invalid_values_fall_back_to_the_current_month(self):
        with clock(at(2026, 10, 13)):
            for query in (
                "?year=abc&month=3",
                "?year=2026&month=xyz",
                "?year=2026&month=13",
                "?year=2026&month=0",
                "?year=2002&month=5",
                "?year=2201&month=5",
                "?year=99999&month=1",
                "?year=&month=",
            ):
                with self.subTest(query=query):
                    self.assertEqual(self.month(query), (2026, 10))


class PeriodChoicesTests(TestCase):
    def test_years_run_from_the_first_fleet_to_the_current_year(self):
        fc = f.create_user("fc", perms=f.FC_PERMS)
        f.closed_operation(fc, started_at=at(2024, 12, 31, 23, 30))
        with clock(at(2026, 10, 13)):
            choices = _period_choices(2026, 10)

        self.assertEqual(choices["year_options"], [2026, 2025, 2024])
        self.assertEqual([number for number, _ in choices["month_options"]], list(range(1, 13)))
        self.assertEqual((choices["selected_year"], choices["selected_month"]), (2026, 10))

    def test_a_selected_year_outside_the_range_is_still_offered(self):
        with clock(at(2026, 10, 13)):
            choices = _period_choices(2031, 1)

        self.assertEqual(choices["year_options"][0], 2031)
        self.assertIn(2026, choices["year_options"])


class PeriodContextTests(TestCase):
    def context(self, year, month, now, query="", **kwargs):
        request = RequestFactory().get(f"/fleetops/statistics/members/{query}")
        with clock(now):
            return _period_render_context(year, month, request=request, **kwargs)

    def test_running_month(self):
        context = self.context(2026, 3, at(2026, 3, 15))

        ref = context["month_ref"]
        self.assertTrue(ref.is_current)
        self.assertEqual((ref.days_elapsed, ref.days_in_month), (15, 31))
        self.assertEqual((context["year"], context["month"]), (2026, 3))
        self.assertEqual(context["previous_period"], {"year": 2026, "month": 2, "label": "2026-02"})
        # There is no link to a month that has not begun.
        self.assertIsNone(context["next_period"])

    def test_finished_month_links_both_ways(self):
        context = self.context(2026, 1, at(2026, 3, 15))

        self.assertTrue(context["month_ref"].is_finished)
        self.assertEqual(context["previous_period"], {"year": 2025, "month": 12, "label": "2025-12"})
        self.assertEqual(context["next_period"], {"year": 2026, "month": 2, "label": "2026-02"})

    def test_year_change_forwards(self):
        context = self.context(2026, 12, at(2027, 2, 3))

        self.assertEqual(context["next_period"], {"year": 2027, "month": 1, "label": "2027-01"})

    def test_future_month_has_no_next_link(self):
        context = self.context(2026, 6, at(2026, 3, 15))

        self.assertTrue(context["month_ref"].is_future)
        self.assertEqual(context["previous_period"]["month"], 5)
        self.assertIsNone(context["next_period"])

    def test_no_link_before_the_first_supported_year(self):
        context = self.context(2003, 1, at(2026, 3, 15))

        self.assertIsNone(context["previous_period"])
        self.assertEqual(context["next_period"]["month"], 2)

    def test_filters_are_kept_without_the_month_and_the_page(self):
        context = self.context(
            2026,
            3,
            at(2026, 3, 15),
            "?status=inactive&q=&sort=pap&page=3&year=2026&month=3&other=x&status=active",
            preserve=("status", "q", "sort", "page", "year", "month", "corporation"),
        )

        self.assertEqual(context["preserved_params"], [("status", "inactive"), ("status", "active"), ("sort", "pap")])

    def test_nothing_is_kept_without_a_request(self):
        with clock(at(2026, 3, 15)):
            context = _period_render_context(2026, 3, preserve=("status",))

        self.assertEqual(context["preserved_params"], [])

    def test_page_values_override_the_period_values(self):
        context = self.context(2026, 3, at(2026, 3, 15), stats="stats", month_ref="mine")

        self.assertEqual((context["stats"], context["month_ref"]), ("stats", "mine"))


class MonthSelectorTemplateTests(TestCase):
    """The shared month selector, rendered the way the statistics pages include it."""

    def render(self, query, year, month, now, **kwargs):
        request = RequestFactory().get(f"/fleetops/statistics/members/{query}")
        with clock(now):
            context = _period_render_context(year, month, request=request, **kwargs)
            return render_to_string("fleetops/month_selector.html", context, request=request)

    def test_labels_the_month_as_eve_time(self):
        page = self.render("", 2026, 3, at(2026, 3, 15))

        self.assertIn("Month (EVE time)", page)
        self.assertIn('<option value="3" selected>March</option>', page)
        self.assertIn('<option value="2026" selected>2026</option>', page)

    def test_filters_come_back_with_the_new_month(self):
        page = self.render(
            "?status=inactive&q=Pilot+One&sort=pap&page=4&year=2026&month=3",
            2026,
            3,
            at(2026, 10, 13),
            preserve=("status", "q", "sort", "corporation"),
        )

        self.assertIn('<input type="hidden" name="status" value="inactive">', page)
        self.assertIn('<input type="hidden" name="q" value="Pilot One">', page)
        self.assertIn('<input type="hidden" name="sort" value="pap">', page)
        self.assertNotIn('name="page"', page)
        self.assertNotIn('name="corporation"', page)

    def test_a_filter_value_cannot_break_out_of_the_form(self):
        page = self.render('?q="><script>alert(1)</script>', 2026, 3, at(2026, 10, 13), preserve=("q",))

        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&quot;&gt;&lt;script&gt;", page)

    def test_neighbour_month_links_keep_the_filters_and_drop_the_page(self):
        page = self.render(
            "?status=inactive&sort=pap&page=4&year=2026&month=3", 2026, 3, at(2026, 10, 13), preserve=("status",)
        )

        previous, following = [query_of(link) for link in re.findall(r'href="(\?[^"]*)"', page)]
        self.assertEqual(previous, {"status": ["inactive"], "sort": ["pap"], "year": ["2026"], "month": ["2"]})
        self.assertEqual(following, {"status": ["inactive"], "sort": ["pap"], "year": ["2026"], "month": ["4"]})
        self.assertIn("Previous month", page)
        self.assertIn("Next month", page)

    def test_running_month_says_how_far_it_is_and_has_no_next_link(self):
        page = self.render("", 2026, 10, at(2026, 10, 13))

        self.assertIn("Month in progress", page)
        self.assertIn("day 13 of 31", page)
        self.assertIn("Previous month", page)
        self.assertNotIn("Next month", page)

    def test_finished_month_has_no_progress_note(self):
        page = self.render("", 2026, 3, at(2026, 10, 13))

        self.assertNotIn("Month in progress", page)

    def test_future_month_says_so(self):
        page = self.render("", 2027, 1, at(2026, 10, 13))

        self.assertIn("has not begun yet", page)
        self.assertNotIn("Next month", page)


class MonthSelectorPageTests(TestCase):
    def test_incentive_page_shows_the_selector_with_the_running_month(self):
        f.settings(incentive_enabled=True)
        lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)

        with clock(at(2026, 10, 13)):
            self.client.force_login(lead)
            response = self.client.get(reverse("fleetops:incentive_review"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Month (EVE time)")
        self.assertContains(response, "day 13 of 31")
        self.assertEqual((response.context["year"], response.context["month"]), (2026, 10))
        self.assertEqual(response.context["previous_period"]["label"], "2026-09")

    @override_settings(TIME_ZONE="Asia/Shanghai")
    def test_incentive_page_opens_on_the_eve_month(self):
        f.settings(incentive_enabled=True)
        lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)

        # 04:00 on 1 November in Shanghai.
        with clock(at(2026, 10, 31, 20)):
            self.client.force_login(lead)
            response = self.client.get(reverse("fleetops:incentive_review"))

        self.assertEqual((response.context["year"], response.context["month"]), (2026, 10))


class NavigationTests(TestCase):
    def setUp(self):
        self.member = f.create_user("member", perms=f.MEMBER_PERMS)
        self.corp = f.create_user("corp", perms=f.CORP_MANAGEMENT_PERMS)
        self.stats = f.create_user("stats", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])

    def nav(self, user):
        self.client.force_login(user)
        response = self.client.get(reverse("fleetops:fleet_operations"))
        self.assertEqual(response.status_code, 200)
        return response

    def assertLink(self, response, name, args=(), query="", present=True):
        href = reverse(f"fleetops:{name}", args=args) + query
        method = self.assertContains if present else self.assertNotContains
        method(response, f'href="{href}"')

    def test_member_sees_the_leaderboard_but_no_statistics_manager_menus(self):
        response = self.nav(self.member)

        self.assertLink(response, "leaderboard")
        self.assertNotContains(response, ">Corporation</a>")
        self.assertNotContains(response, ">Alliance</a>")
        self.assertTrue(response.context["leaderboard_visible"])

    def test_corporation_menu(self):
        response = self.nav(self.corp)

        self.assertContains(response, ">Corporation</a>")
        self.assertLink(response, "corporation_statistics")
        self.assertLink(response, "corporation_members")
        self.assertLink(response, "corporation_members", query="?status=inactive")
        self.assertLink(response, "attendance_history_corporation")
        self.assertNotContains(response, ">Alliance</a>")

    def test_alliance_menu(self):
        response = self.nav(self.stats)

        self.assertContains(response, ">Alliance</a>")
        self.assertLink(response, "all_corporation_statistics")
        self.assertLink(response, "alliance_members")
        self.assertLink(response, "alliance_members", query="?status=inactive")
        self.assertLink(response, "all_fc_statistics")
        self.assertLink(response, "attendance_history_alliance")
        self.assertContains(response, ">PAP History</a>")
        self.assertNotContains(response, ">Corporation</a>")

    def test_no_afat_comparison(self):
        for user in (self.member, self.corp, self.stats):
            with self.subTest(user=user.username):
                self.assertNotContains(self.nav(user), "AFAT")

    def test_leaderboard_item_follows_the_visibility_setting(self):
        f.settings(leaderboard_visibility="managers")

        self.assertLink(self.nav(self.member), "leaderboard", present=False)
        self.assertLink(self.nav(self.corp), "leaderboard")
        self.assertLink(self.nav(self.stats), "leaderboard")
        self.assertFalse(self.nav(self.member).context["leaderboard_visible"])


class PeriodSelectorLayoutTests(TestCase):
    def test_the_selects_are_sized_by_their_text(self):
        """Bootstrap gives each select of an input group the same 1% width, which cuts "September" and "All retained"."""
        css = (Path(fleetops.__file__).parent / "static" / "fleetops" / "fleetops.css").read_text(encoding="utf-8")

        rule = re.search(r"\.fleetops-period-selector \.input-group>\.form-select\{([^}]*)\}", css)

        self.assertIsNotNone(rule)
        self.assertIn("width:auto", rule.group(1))
        self.assertIn("flex:0 0 auto", rule.group(1))

    def test_both_selectors_put_the_selects_in_an_input_group_of_the_styled_form(self):
        for name in ("fleetops/month_selector.html", "fleetops/attendance_history.html"):
            with self.subTest(template=name):
                source = Path(fleetops.__file__).parent.joinpath("templates", name).read_text(encoding="utf-8")
                self.assertIn("fleetops-period-selector", source)
                self.assertIn('<div class="input-group">', source)


class DashboardEmptyStateTests(TestCase):
    def test_the_dashboard_without_fleet_types_says_pap_cards(self):
        FleetType.objects.all().delete()
        user = f.member("viewer", perms=f.MEMBER_PERMS)
        self.client.force_login(user)

        response = self.client.get(reverse("fleetops:dashboard"))

        self.assertContains(response, "Create Fleet Types to populate the dashboard PAP cards.")
        self.assertNotContains(response, "attendance cards")
