# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Owner feedback on 0.3.0b5: the member view of the fleet page, the rank card and its scope (the CharLink item has
its tests in test_charlink_hook.py)."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import get_resolver, reverse
from django.utils import timezone

from fleetops.models import FleetOperation, FleetOpsSettings, GroupAccess, OperationRoleAssignment
from fleetops.services import fleet_access

from . import factories as f

CLOSED = FleetOperation.Status.CLOSED
ACTIVE = FleetOperation.Status.ACTIVE
REQUEST_SRP = "fleetops.request_srp"


def fresh(user):
    return get_user_model().objects.get(pk=user.pk)


def assign_role(operation, user, *, credit=False):
    main = f.main_of(user)
    return OperationRoleAssignment.objects.create(
        operation=operation,
        role=OperationRoleAssignment.Role.LOGI_ANCHOR,
        character_id=main.character_id,
        character_name=main.character_name,
        main_character_id=main.character_id,
        main_character_name=main.character_name,
        auth_user=user,
        corporation_id=main.corporation_id,
        corporation_name=main.corporation_name,
        grants_fc_credit=credit,
    )


class MemberViewFixture(TestCase):
    """A closed fleet with an FC, a participant, another participant and the people who see everything."""

    def setUp(self):
        super().setUp()
        f.srp_settings(enabled=True)
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Example FC")
        self.member = f.create_user("member", perms=f.MEMBER_PERMS + [REQUEST_SRP], main_name="Example Member")
        self.other = f.create_user("other", perms=f.MEMBER_PERMS, main_name="Example Other")
        self.outsider = f.create_user("outsider", perms=f.MEMBER_PERMS, main_name="Example Outsider")
        started = timezone.now() - timedelta(hours=3)
        self.operation = f.closed_operation(
            self.fc,
            started_at=started,
            ended_at=started + timedelta(hours=2, minutes=5),
            doctrine_name="Example Doctrine",
            send_ping=True,
            ping_text="@Example Ping Fleet is forming, FC Example FC. Join the channel.",
            motd_text="Example MOTD text that members do not see",
        )
        self.own_alt = f.add_alt(self.member, "Example Member Alt")
        for user in (self.member, self.other):
            f.member_state(self.operation, f.main_of(user), accumulated_seconds=7000, active=False)
            f.add_attendance(self.operation, user, value=2)
        f.member_state(self.operation, self.own_alt, accumulated_seconds=600, active=False, user=self.member)
        f.add_attendance(self.operation, self.member, self.own_alt, value=0, granted=False, below_minimum=True)
        self.link = f.fleet_link(self.operation)

    def get(self, user, url):
        client = Client()
        client.force_login(user)
        return client.get(url)

    def page(self, user, operation=None):
        return self.get(user, reverse("fleetops:operation_detail", args=[(operation or self.operation).uuid]))


class MemberViewPredicateTests(MemberViewFixture):
    def test_a_participant_gets_the_member_view(self):
        self.assertTrue(fleet_access.member_view_only(self.member, self.operation))

    def test_the_people_who_see_everything_do_not(self):
        lead = f.create_user("lead", perms=f.MEMBER_PERMS + ["fleetops.view_all_fleets"])
        manager = f.create_user("manager", perms=f.MEMBER_PERMS + ["fleetops.manage_fleets"])
        admin = get_user_model().objects.create_superuser("root", "root@example.com", "pw")
        for user in (self.fc, lead, manager, admin):
            with self.subTest(user=user.username):
                self.assertFalse(fleet_access.member_view_only(user, self.operation))

    def test_a_credited_fc_without_the_manage_permission_does_not(self):
        credited = f.create_user("credited", perms=f.MEMBER_PERMS)
        assign_role(self.operation, credited, credit=True)

        self.assertFalse(fleet_access.member_view_only(credited, self.operation))

    def test_a_special_role_without_fc_credit_counts_as_a_member(self):
        logi = f.create_user("logi", perms=f.MEMBER_PERMS)
        assign_role(self.operation, logi)

        self.assertTrue(fleet_access.member_view_only(logi, self.operation))
        response = self.page(logi)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "fleetops/operation_detail_member.html")

    def test_the_leader_of_the_fleets_group_does_not(self):
        leader = f.create_user("leader", perms=f.MEMBER_PERMS)
        group = f.aa_group("Example Group", leaders=[leader])
        f.group_access(group, GroupAccess.Mode.FLEETS)
        operation = f.closed_operation(self.fc, group_id=group.pk, group_name=group.name)

        self.assertFalse(fleet_access.member_view_only(fresh(leader), operation))
        # The same person is a plain member of a fleet that is not of their group.
        f.member_state(self.operation, f.main_of(leader), active=False, user=leader)
        self.assertTrue(fleet_access.member_view_only(fresh(leader), self.operation))

    def test_a_scheduled_fleet_has_no_member_view(self):
        scheduled = f.create_scheduled_operation(self.fc)
        self.assertFalse(fleet_access.member_view_only(self.fc, scheduled))


class MemberPageTests(MemberViewFixture):
    def test_the_member_opens_the_fleet_and_sees_the_slim_page(self):
        response = self.page(self.member)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "fleetops/operation_detail_member.html")
        self.assertTemplateNotUsed(response, "fleetops/operation_detail.html")
        html = response.content.decode()
        self.assertIn("Example Doctrine", html)
        self.assertIn("Example FC", html)
        self.assertIn("2 h 05 min", html)
        self.assertIn("Ended at", html)
        self.assertIn("Example Ping Fleet is forming", html)
        self.assertIn("My attendance", html)
        # Own characters, with their time and PAP state.
        self.assertIn("Example Member", html)
        self.assertIn("Example Member Alt", html)
        self.assertIn("Granted", html)
        self.assertIn("Below minimum", html)

    def test_the_page_has_no_other_character_names_and_none_of_the_hidden_parts(self):
        html = self.page(self.member).content.decode()

        self.assertNotIn("Example Other", html)
        self.assertNotIn("Example Outsider", html)
        # The fleet's own FC is in the header and the ping, which is not more than the ping says.
        for hidden in (
            'id="members"', 'id="map"', 'id="events"', 'id="roles"', 'id="messages"', 'id="fleet"',
            "Example MOTD text", "3-Jump Map", "Special Roles", "Ping / MOTD", "Composition", "Operation Actions",
            "Characters</span>", "Players</span>", "Form Up", "Last ESI", "Recalculate PAP", "Edit Operation",
            "fleet/state", "Retry", "recalculate", "operation-tabs", "fleet-command-bar is-sticky",
        ):
            with self.subTest(hidden=hidden):
                self.assertNotIn(hidden, html)
        # No fleet id, no references of the SRP provider and no UUID of the fleet.
        self.assertNotIn(str(self.operation.uuid), html.replace(reverse("fleetops:operation_detail", args=[self.operation.uuid]), ""))

    def test_srp_button_while_the_link_is_open_and_the_member_may_request(self):
        html = self.page(self.member).content.decode()

        self.assertIn("Request SRP for this fleet", html)
        self.assertIn(self.link.code, html)
        # No provider, reference or retry.
        self.assertNotIn("Retry SRP", html)
        self.assertNotIn("Mark SRP link complete", html)
        self.assertNotIn("srp-link-url", html)

    def test_no_srp_button_without_the_request_permission(self):
        f.member_state(self.operation, f.main_of(self.outsider), active=False)

        html = self.page(self.outsider).content.decode()

        self.assertNotIn("Request SRP for this fleet", html)
        self.assertNotIn(self.link.code, html)

    def test_a_closed_link_says_when_it_closed(self):
        closed_at = timezone.now() - timedelta(hours=1)
        self.link.is_open = False
        self.link.closed_at = closed_at
        self.link.save()

        html = self.page(self.member).content.decode()

        self.assertNotIn("Request SRP for this fleet", html)
        self.assertIn("Closed ", html)
        self.assertIn(closed_at.strftime("%Y-%m-%d %H:%M"), html)

    def test_a_fleet_without_a_link_shows_no_srp_card(self):
        self.link.delete()

        html = self.page(self.member).content.decode()

        self.assertNotIn('id="member-srp"', html)

    def test_an_open_fleet_shows_only_the_own_preview(self):
        operation = f.create_operation(self.fc, status=ACTIVE, send_ping=True, ping_text="Open fleet ping")
        f.member_state(operation, f.main_of(self.member), accumulated_seconds=1800)
        f.member_state(operation, f.main_of(self.other), accumulated_seconds=1800)

        response = self.page(self.member, operation)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Example Member", html)
        self.assertNotIn("Example Other", html)
        self.assertIn("Open fleet ping", html)

    def test_the_fc_and_the_viewers_of_all_fleets_still_see_the_full_page(self):
        lead = f.create_user("lead", perms=f.MEMBER_PERMS + ["fleetops.view_all_fleets"])
        for user in (self.fc, lead):
            with self.subTest(user=user.username):
                response = self.page(user)
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "fleetops/operation_detail.html")
                self.assertContains(response, "Example Other")

    def test_the_group_leader_still_sees_the_full_page(self):
        leader = f.create_user("leader", perms=f.MEMBER_PERMS)
        group = f.aa_group("Example Group", leaders=[leader])
        f.group_access(group, GroupAccess.Mode.FLEETS)
        operation = f.closed_operation(self.fc, group_id=group.pk, group_name=group.name)

        response = self.page(fresh(leader), operation)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "fleetops/operation_detail.html")

    def test_a_fleet_the_person_did_not_take_part_in_is_still_404(self):
        self.assertEqual(self.page(self.outsider).status_code, 404)

    def test_the_pap_history_link_of_a_fleet_of_another_fc_opens_the_slim_page(self):
        history = self.get(self.member, reverse("fleetops:attendance_history_me"))
        self.assertContains(history, reverse("fleetops:operation_detail", args=[self.operation.uuid]))

        response = self.page(self.member)

        self.assertTemplateUsed(response, "fleetops/operation_detail_member.html")

    def test_my_pap_history_calls_the_fleet_column_fc(self):
        history = self.get(self.member, reverse("fleetops:attendance_history_me"))

        html = history.content.decode()
        self.assertIn("<th>FC</th>", html)
        self.assertNotIn("<th>Fleet</th>", html)
        # The link is unchanged: the name of the FC opens the fleet.
        self.assertIn(f'>{self.operation.fc_character_name}</a>', html)


class FleetEndpointsAreClosedToMemberViewTests(MemberViewFixture):
    """Every fleet related GET address except the fleet page answers 404 to a member who took part."""

    def operation_urls(self):
        found = []
        for pattern in self.walk(get_resolver().url_patterns):
            found.append(pattern)
        return found

    def walk(self, patterns, prefix=""):
        for entry in patterns:
            if hasattr(entry, "url_patterns"):
                yield from self.walk(entry.url_patterns, prefix + str(entry.pattern))
            else:
                yield prefix + str(entry.pattern), entry.name

    def test_every_fleet_get_url_is_404_except_the_page(self):
        urls = {}
        for route, name in self.operation_urls():
            if "<uuid:operation_uuid>" not in route or name in (None, "operation_detail"):
                continue
            urls[name] = route
        self.assertGreater(len(urls), 20, "the URL walk found too few fleet routes")
        fillers = {
            "<uuid:operation_uuid>": str(self.operation.uuid),
            "<int:character_id>": "1",
            "<int:wing_id>": "1",
            "<int:squad_id>": "1",
        }
        client = Client()
        client.force_login(self.member)
        for name, route in sorted(urls.items()):
            url = "/" + route.lstrip("^").rstrip("$")
            for key, value in fillers.items():
                url = url.replace(key, value)
            with self.subTest(name=name):
                response = client.get(url, HTTP_ACCEPT="application/json")
                # A route that needs a permission the member lacks answers 403 or 404; none may answer 200 or send
                # the member to a page with the fleet's data.
                self.assertIn(response.status_code, (403, 404, 405), (name, url, response.status_code))

    def test_the_fleet_state_is_404_for_the_member_view_and_200_for_the_fc(self):
        url = reverse("fleetops:fleet_state", args=[self.operation.uuid])
        self.operation.esi_fleet_id = 99001
        self.operation.save(update_fields=["esi_fleet_id"])

        self.assertEqual(self.get(self.member, url).status_code, 404)
        self.assertEqual(self.get(self.fc, url).status_code, 200)

    def test_the_fleet_tab_is_not_offered_to_the_member_view(self):
        self.assertFalse(fleet_access.can_view_fleet_tab(self.member, self.operation))
        self.assertTrue(fleet_access.can_view_fleet_tab(self.fc, self.operation))

    def test_an_endpoint_that_reads_the_fleet_for_a_non_manager_is_404_for_the_member_view(self):
        classify = reverse("fleetops:fleet_classify", args=[self.operation.uuid])
        admin = f.create_user("configurer", perms=f.MEMBER_PERMS + ["fleetops.manage_configuration"])
        f.member_state(self.operation, f.main_of(admin), active=False)

        client = Client()
        client.force_login(admin)
        response = client.post(classify, {"type_id": "1", "function_id": "1"})

        self.assertEqual(response.status_code, 404)


class RankCardTests(TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.member("fc", perms=f.FC_PERMS)
        self.op = f.closed_operation(self.fc)
        f.fleet_type()
        self.alice = f.member("alice", perms=f.MEMBER_PERMS)
        self.bob = f.member("bob", perms=f.MEMBER_PERMS)
        self.carol = f.member("carol", perms=f.MEMBER_PERMS, corporation=f.OTHER_CORP)
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        f.add_attendance(self.op, self.alice, value=5)
        f.add_attendance(self.op, self.bob, value=3)
        f.add_attendance(self.op, self.carol, value=4)

    def page(self, user):
        client = Client()
        client.force_login(user)
        return client.get(reverse("fleetops:my_statistics"))

    def test_the_rank_card_is_shown_with_a_managers_only_leaderboard_but_without_the_link(self):
        f.settings(leaderboard_visibility="managers")

        response = self.page(self.bob)

        card = response.context["rank_card"]
        self.assertEqual((card["rank"], card["of"], card["scope"]), (3, 3, "alliance"))
        self.assertContains(response, 'id="pap-rank"')
        self.assertFalse(response.context["can_open_leaderboard"])
        self.assertNotContains(response, reverse("fleetops:leaderboard"))

    def test_the_leaderboard_page_is_refused_then(self):
        f.settings(leaderboard_visibility="managers")
        client = Client()
        client.force_login(self.bob)

        self.assertIn(client.get(reverse("fleetops:leaderboard")).status_code, (403, 404))

    def test_the_link_follows_the_leaderboard_setting(self):
        f.settings(leaderboard_visibility="members")

        response = self.page(self.bob)

        self.assertTrue(response.context["can_open_leaderboard"])
        self.assertContains(response, reverse("fleetops:leaderboard"))

    def test_statistics_managers_see_the_link_with_a_managers_board(self):
        f.settings(leaderboard_visibility="managers")
        manager = f.member("manager", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])
        f.add_attendance(self.op, manager, value=1)

        response = self.page(manager)

        self.assertTrue(response.context["can_open_leaderboard"])
        self.assertContains(response, reverse("fleetops:leaderboard"))
        self.assertIsNotNone(response.context["rank_card"])

    def test_the_default_scope_is_the_alliance(self):
        self.assertEqual(FleetOpsSettings.get_solo().member_rank_scope, "alliance")

    def test_the_corporation_scope_ranks_in_the_own_corporation(self):
        f.settings(member_rank_scope="corporation")

        card = self.page(self.bob).context["rank_card"]
        response = self.page(self.carol)

        self.assertEqual((card["rank"], card["of"], card["scope"]), (2, 2, "corporation"))
        self.assertEqual((response.context["rank_card"]["rank"], response.context["rank_card"]["of"]), (1, 1))
        self.assertContains(self.page(self.bob), "Among the members of your corporation with PAP this month.")

    def test_the_alliance_text_follows_the_scope(self):
        self.assertContains(self.page(self.bob), "Among the alliance members with PAP this month.")

    def test_not_shown_has_no_card(self):
        f.settings(member_rank_scope="none")

        response = self.page(self.bob)

        self.assertIsNone(response.context["rank_card"])
        self.assertNotContains(response, 'id="pap-rank"')

    def test_no_pap_no_card(self):
        idle = f.member("idle", perms=f.MEMBER_PERMS)

        self.assertIsNone(self.page(idle).context["rank_card"])

    def test_the_card_shows_numbers_only(self):
        html = self.page(self.bob).content.decode()
        card = html[html.index('id="pap-rank"'):]
        card = card[: card.index("</small>")]

        for name in ("alice", "carol", "Alice", "Carol"):
            self.assertNotIn(name, card)


class MemberSrpCardTests(MemberViewFixture):
    def test_an_open_link_the_member_cannot_request_shows_no_empty_card(self):
        # The participant lacks request_srp: the open link has nothing to offer.
        f.member_state(self.operation, f.main_of(self.outsider), active=False)
        f.add_attendance(self.operation, self.outsider, value=1)

        html = self.page(self.outsider).content.decode()

        self.assertNotIn('id="member-srp"', html)

    def test_an_open_link_with_srp_switched_off_shows_no_empty_card(self):
        f.srp_settings(enabled=False)

        html = self.page(self.member).content.decode()

        self.assertNotIn('id="member-srp"', html)
        self.assertNotIn("Request SRP for this fleet", html)

    def test_the_card_stays_for_the_request_button_and_for_a_closed_link(self):
        self.assertIn('id="member-srp"', self.page(self.member).content.decode())
        self.link.is_open = False
        self.link.closed_at = timezone.now() - timedelta(hours=1)
        self.link.save()
        f.member_state(self.operation, f.main_of(self.outsider), active=False)
        self.assertIn('id="member-srp"', self.page(self.outsider).content.decode())


class ListAndDashboardHideMemberViewDataTests(MemberViewFixture):
    FORMUP = "Example Formup Station"
    ESI_ID = 9876543210

    def setUp(self):
        super().setUp()
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            formup=self.FORMUP,
            esi_fleet_id=self.ESI_ID,
            end_reason=FleetOperation.EndReason.FLEET_GONE,
        )
        self.operation.refresh_from_db()
        self.list_url = reverse("fleetops:fleet_operations")

    def test_the_list_hides_the_fleet_data_from_the_member_view(self):
        html = self.get(self.member, self.list_url).content.decode()

        self.assertIn("Example Doctrine", html)
        self.assertNotIn(self.FORMUP, html)
        self.assertNotIn(str(self.ESI_ID), html)
        self.assertNotIn(self.operation.end_reason_text, html)

    def test_the_list_keeps_the_data_for_the_fc(self):
        html = self.get(self.fc, self.list_url).content.decode()

        self.assertIn(self.FORMUP, html)
        self.assertIn(str(self.ESI_ID), html)
        self.assertIn(self.operation.end_reason_text, html)

    def test_the_list_hides_the_not_counted_reason_from_the_member_view(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            counts_for_stats=False, stats_mode="exclude", stats_note="Example excluded reason"
        )

        member_html = self.get(self.member, self.list_url).content.decode()
        fc_html = self.get(self.fc, self.list_url).content.decode()

        self.assertIn("Example excluded reason", fc_html)
        self.assertNotIn("Example excluded reason", member_html)

    def test_the_dashboard_hides_member_counts_and_esi_time_from_the_member_view(self):
        running = f.create_operation(self.fc, doctrine_name="Example Running Doctrine")
        f.member_state(running, f.main_of(self.member), active=True)
        f.member_state(running, f.main_of(self.other), active=True)
        FleetOperation.objects.filter(pk=running.pk).update(last_esi_update=timezone.now())

        member_html = self.get(self.member, reverse("fleetops:dashboard")).content.decode()
        fc_html = self.get(self.fc, reverse("fleetops:dashboard")).content.decode()

        self.assertIn(str(running.uuid), member_html)
        self.assertNotIn("2 members", member_html)
        active_card = member_html.split("Active Fleets")[1].split("My FC Month")[0]
        self.assertNotRegex(active_card, r"\d\d:\d\d EVE")
        self.assertIn("2 members", fc_html)

    def test_last_participations_hides_the_member_count_from_the_member_view(self):
        f.add_attendance(self.operation, self.fc, value=1)
        html = self.get(self.member, reverse("fleetops:dashboard")).content.decode()
        fc_html = self.get(self.fc, reverse("fleetops:dashboard")).content.decode()

        self.assertIn(str(self.operation.uuid), html)
        self.assertNotRegex(html, r"\d+ members?<")
        self.assertRegex(fc_html, r"\d+ members?<")
