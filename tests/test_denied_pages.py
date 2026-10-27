# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FleetOps 403 and 404 pages: the reason a page is not open, 404 for objects out of scope, and the one neutral
404 for everything else a FleetOps page cannot show."""

import re
import uuid
from urllib.parse import urlsplit

from django.conf import settings
from django.shortcuts import resolve_url
from django.test import Client, TestCase, override_settings, tag
from django.urls import reverse
from django.utils import translation

from fleetops.models import FleetOpsSettings
from fleetops.views_errors import (
    LEADERBOARD_REASON,
    NOT_FOUND_REASON,
    OWN_CORPORATION_MEMBERS_REASON,
    OWN_CORPORATION_STATISTICS_REASON,
    OWN_FLEETS_SUMMARY_REASON,
    PERMISSION_REASONS,
    STARTER_REASON,
)

from . import factories as f

CARD = re.compile(r'<div class="card" id="fo-denied".*?</div>\s*</div>', re.S)
UNKNOWN_CORPORATION = 2_099_999


def card(response):
    """The denied card of a FleetOps 403/404 page."""
    found = CARD.search(response.content.decode())
    assert found, "no FleetOps denied card"
    return found.group(0)


class DeniedPageTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.member = f.create_user("member", perms=f.MEMBER_PERMS, main_name="Pilot A")
        self.corp = f.create_user("corp", perms=f.CORP_MANAGEMENT_PERMS, main_name="Pilot B")
        self.other = f.create_user("other", perms=f.MEMBER_PERMS, main_name="Pilot C", corporation=f.OTHER_CORP)
        self.colleague = f.create_user("colleague", perms=f.MEMBER_PERMS, main_name="Pilot D")
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Example FC")
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS, main_name="Example Lead")
        self.no_access = f.create_user("noaccess", main_name="Pilot E")

    def get(self, user, url, **params):
        client = Client()
        if user is not None:
            client.force_login(user)
        return client.get(url, params)

    def assertFleetOpsPage(self, response, status, reason):
        self.assertEqual(response.status_code, status)
        self.assertIn(str(reason), card(response))

    def assertNotFleetOpsPage(self, response, status):
        self.assertEqual(response.status_code, status)
        self.assertNotIn('id="fo-denied"', response.content.decode())


class DecoratorTests(DeniedPageTestCase):
    def test_a_page_without_the_permission_shows_the_reason_and_the_permission(self):
        response = self.get(self.member, reverse("fleetops:configuration_index"))

        self.assertFleetOpsPage(response, 403, PERMISSION_REASONS["fleetops.manage_configuration"])
        self.assertIn("fleetops.manage_configuration", card(response))
        self.assertIn("Not allowed", response.content.decode())

    def test_a_head_request_gets_the_same_status(self):
        response = Client()
        response.force_login(self.member)

        self.assertEqual(response.head(reverse("fleetops:configuration_index")).status_code, 403)

    def test_without_basic_access_every_page_explains_it(self):
        response = self.get(self.no_access, reverse("fleetops:dashboard"))

        self.assertFleetOpsPage(response, 403, PERMISSION_REASONS["fleetops.basic_access"])

    def test_posts_and_script_endpoints_keep_the_plain_answer(self):
        client = Client()
        client.force_login(self.member)
        operation = f.create_operation(self.fc)

        self.assertNotFleetOpsPage(client.post(reverse("fleetops:configuration_settings")), 403)
        self.assertNotFleetOpsPage(client.post(reverse("fleetops:incentive_review")), 403)
        self.assertNotFleetOpsPage(client.post(reverse("fleetops:preview_fleet")), 403)
        no_access = Client()
        no_access.force_login(self.no_access)
        self.assertNotFleetOpsPage(no_access.get(reverse("fleetops:fleet_state", args=[operation.uuid])), 403)
        self.assertNotFleetOpsPage(no_access.get(reverse("fleetops:live_pap")), 403)

    def test_the_group_decorators_show_their_reason(self):
        self.assertFleetOpsPage(self.get(self.member, reverse("fleetops:start_fleet")), 403, STARTER_REASON)

    def test_anonymous_users_are_sent_to_the_login_page(self):
        for name in ("dashboard", "configuration_index", "start_fleet", "send_ping", "leaderboard"):
            with self.subTest(name=name):
                response = self.get(None, reverse(f"fleetops:{name}"))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(urlsplit(response["Location"]).path, resolve_url(settings.LOGIN_URL))

    def test_a_denied_or_unknown_export_shows_the_fleetops_page(self):
        denied = self.get(self.member, reverse("fleetops:export", args=["members"]), format="csv")
        unknown = self.get(self.member, reverse("fleetops:export", args=["no-such-export"]), format="csv")

        self.assertEqual(denied.status_code, 403)
        self.assertIn('id="fo-denied"', denied.content.decode())
        self.assertFleetOpsPage(unknown, 404, NOT_FOUND_REASON)


class ObjectScopeTests(DeniedPageTestCase):
    """FB12: a corporation manager opens their own corporation by ID; any other ID, known or not, is one 404."""

    def setUp(self):
        super().setUp()
        f.srp_settings(enabled=True)
        self.own = f.DEFAULT_CORP[0]
        self.foreign = f.OTHER_CORP[0]

    def assertSameNotFound(self, user, name, reason, *, known, unknown):
        first = self.get(user, reverse(f"fleetops:{name}", args=[known]))
        second = self.get(user, reverse(f"fleetops:{name}", args=[unknown]))
        self.assertFleetOpsPage(first, 404, reason)
        self.assertFleetOpsPage(second, 404, reason)
        self.assertEqual(card(first), card(second))

    def test_corporation_statistics_by_id(self):
        own = self.get(self.corp, reverse("fleetops:corporation_statistics_detail", args=[self.own]))

        self.assertEqual(own.status_code, 200)
        self.assertSameNotFound(
            self.corp,
            "corporation_statistics_detail",
            OWN_CORPORATION_STATISTICS_REASON,
            known=self.foreign,
            unknown=UNKNOWN_CORPORATION,
        )

    def test_corporation_members_by_id(self):
        own = self.get(self.corp, reverse("fleetops:corporation_members_detail", args=[self.own]))

        self.assertEqual(own.status_code, 200)
        self.assertSameNotFound(
            self.corp,
            "corporation_members_detail",
            OWN_CORPORATION_STATISTICS_REASON,
            known=self.foreign,
            unknown=UNKNOWN_CORPORATION,
        )

    def test_member_details(self):
        own = self.get(self.corp, reverse("fleetops:member_detail", args=[self.colleague.pk]))

        self.assertEqual(own.status_code, 200)
        self.assertSameNotFound(
            self.corp, "member_detail", OWN_CORPORATION_MEMBERS_REASON, known=self.other.pk, unknown=9_999_999
        )

    def test_srp_corporation_summary_by_id(self):
        own = self.get(self.corp, reverse("fleetops:srp_summary_corporation", args=[self.own]))

        self.assertEqual(own.status_code, 200)
        self.assertSameNotFound(
            self.corp,
            "srp_summary_corporation",
            OWN_CORPORATION_STATISTICS_REASON,
            known=self.foreign,
            unknown=UNKNOWN_CORPORATION,
        )

    def test_srp_fc_summary_by_id(self):
        own = self.get(self.fc, reverse("fleetops:srp_summary_fc", args=[self.fc.pk]))

        self.assertEqual(own.status_code, 200)
        self.assertSameNotFound(
            self.fc, "srp_summary_fc", OWN_FLEETS_SUMMARY_REASON, known=self.lead.pk, unknown=9_999_999
        )

    def test_statistics_managers_still_open_every_corporation(self):
        stats = f.create_user("stats", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])
        f.closed_operation(self.fc)

        response = self.get(stats, reverse("fleetops:corporation_members_detail", args=[self.foreign]))

        self.assertEqual(response.status_code, 200)

    def test_members_without_statistics_permissions_get_the_reason(self):
        response = self.get(self.member, reverse("fleetops:corporation_statistics_detail", args=[self.own]))

        self.assertFleetOpsPage(response, 403, PERMISSION_REASONS["fleetops.view_corp_stats"])
        response = self.get(self.member, reverse("fleetops:corporation_members_detail", args=[self.own]))
        self.assertFleetOpsPage(response, 403, PERMISSION_REASONS["fleetops.view_corp_stats"])

    def test_a_hidden_leaderboard_explains_why(self):
        FleetOpsSettings.objects.filter(pk=FleetOpsSettings.get_solo().pk).update(
            leaderboard_visibility=FleetOpsSettings.LeaderboardVisibility.MANAGERS
        )

        response = self.get(self.member, reverse("fleetops:leaderboard"))

        self.assertFleetOpsPage(response, 403, LEADERBOARD_REASON)


class NeutralNotFoundTests(DeniedPageTestCase):
    """D19: every other 404 of a FleetOps page is the FleetOps page with one sentence that names nothing."""

    def test_a_pre_pinged_fleet_and_a_random_id_look_the_same_to_a_member(self):
        scheduled = f.create_scheduled_operation(self.fc)

        hidden = self.get(self.member, reverse("fleetops:operation_detail", args=[scheduled.uuid]))
        missing = self.get(self.member, reverse("fleetops:operation_detail", args=[uuid.uuid4()]))

        self.assertFleetOpsPage(hidden, 404, NOT_FOUND_REASON)
        self.assertFleetOpsPage(missing, 404, NOT_FOUND_REASON)
        self.assertEqual(card(hidden), card(missing))
        self.assertNotIn("FleetOperation", hidden.content.decode())
        self.assertIn("Not found", hidden.content.decode())

    def test_other_objects_answer_the_fleetops_404(self):
        claim = f.srp_claim(self.colleague)
        f.srp_settings(enabled=True)
        cases = {
            "another member's SRP claim": reverse("fleetops:srp_claim", args=[claim.uuid]),
            "an unknown PAP link": reverse("fleetops:pap_link_register", args=["unknown-link-code"]),
            "an unknown configuration section": reverse("fleetops:configuration_list", args=["no-such-section"]),
        }
        for label, url in cases.items():
            with self.subTest(case=label):
                user = self.lead if "configuration" in label else self.member
                self.assertFleetOpsPage(self.get(user, url), 404, NOT_FOUND_REASON)
        unknown_pk = reverse("fleetops:configuration_edit", args=["fleet-types", 9_999_999])
        self.assertFleetOpsPage(self.get(self.lead, unknown_pk), 404, NOT_FOUND_REASON)
        unknown_fleet = reverse("fleetops:edit_operation", args=[uuid.uuid4()])
        self.assertFleetOpsPage(self.get(self.fc, unknown_fleet), 404, NOT_FOUND_REASON)

    def test_json_and_post_endpoints_keep_their_plain_404(self):
        client = Client()
        client.force_login(self.fc)

        state = client.get(reverse("fleetops:fleet_state", args=[uuid.uuid4()]), HTTP_ACCEPT="application/json")
        retry = client.post(reverse("fleetops:retry_ping", args=[uuid.uuid4()]))

        self.assertNotFleetOpsPage(state, 404)
        self.assertNotFleetOpsPage(retry, 404)


@tag("catalog")
@override_settings(LANGUAGE_CODE="zh-hans")
class ChineseReasonTests(DeniedPageTestCase):
    """The reasons are shown in the viewer's language (needs the compiled catalog)."""

    def get(self, user, url, **params):
        client = Client(HTTP_ACCEPT_LANGUAGE="zh-hans")
        client.force_login(user)
        with translation.override("zh-hans"):
            return client.get(url, params)

    def test_the_administration_reason_is_chinese(self):
        response = self.get(self.member, reverse("fleetops:configuration_index"))

        self.assertEqual(response.status_code, 403)
        with translation.override("zh-hans"):
            expected = str(PERMISSION_REASONS["fleetops.manage_configuration"])
        self.assertNotEqual(expected, "Only FleetOps administrators can open the administration.")
        self.assertIn(expected, card(response))

    def test_the_neutral_not_found_sentence_is_chinese(self):
        response = self.get(self.member, reverse("fleetops:operation_detail", args=[uuid.uuid4()]))

        self.assertEqual(response.status_code, 404)
        self.assertIn("此页面不存在，或你无权打开。", card(response))

    def test_the_corporation_reason_is_chinese(self):
        response = self.get(self.corp, reverse("fleetops:corporation_statistics_detail", args=[f.OTHER_CORP[0]]))

        self.assertEqual(response.status_code, 404)
        with translation.override("zh-hans"):
            expected = str(OWN_CORPORATION_STATISTICS_REASON)
        self.assertNotEqual(expected, "You can only open the statistics of your own corporation.")
        self.assertIn(expected, card(response))
