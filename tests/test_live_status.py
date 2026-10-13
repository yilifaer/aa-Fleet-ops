"""Live PAP status: what pilots see about their characters in running fleets."""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import FleetOperation
from fleetops.services.pap import live_pap_status

from . import factories as f

Status = FleetOperation.Status


class LiveStatusTestBase(TestCase):
    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Fleet Boss")
        self.pilot = f.create_user("pilot", perms=f.MEMBER_PERMS, main_name="Line Pilot")
        self.main = f.main_of(self.pilot)
        self.alt = f.add_alt(self.pilot, "Pilot Alt")
        self.operation = f.create_operation(
            self.fc,
            started_at=timezone.now() - timedelta(minutes=40),
            type_obj=f.fleet_type("StratOp"),
            pap_minimum_minutes=20,
            pap_character_limit=1,
            last_esi_update=timezone.now(),
        )

    def track(self, character, minutes, operation=None, **extra):
        return f.member_state(operation or self.operation, character, accumulated_seconds=minutes * 60, **extra)

    def rows(self, user=None):
        return {row.character_name: row for row in live_pap_status(user or self.pilot)}

    def page(self, name="fleetops:dashboard", user=None):
        self.client.force_login(user or self.pilot)
        response = self.client.get(reverse(name))
        self.assertEqual(response.status_code, 200)
        return response


class LivePapStatusTests(LiveStatusTestBase):
    def test_only_the_users_own_characters_in_running_fleets(self):
        self.track(self.main, 37)
        self.track(f.main_of(self.fc), 40)
        closed = f.create_operation(self.fc, status=Status.CLOSED)
        self.track(self.alt, 30, closed, active=False)

        rows = self.rows()

        self.assertEqual(set(rows), {"Line Pilot"})
        row = rows["Line Pilot"]
        self.assertEqual((row.operation, row.fc_name, row.fleet_type), (self.operation, "Fleet Boss", "StratOp"))
        self.assertEqual((row.tracked_seconds, row.minimum_seconds, row.reached, row.outcome), (2220, 1200, True, "granted"))
        self.assertTrue(row.in_fleet)
        self.assertEqual(row.as_of, self.operation.last_esi_update)
        self.assertEqual(self.rows(f.create_user()), {})

    def test_reached_minimum_text(self):
        self.track(self.main, 37)

        page = self.page()

        self.assertContains(page, "Your character <strong>Line Pilot</strong> is in <strong>Fleet Boss</strong>'s fleet (StratOp): 37 min in fleet.")
        self.assertContains(page, "PAP minimum 20 min — reached.")

    def test_minutes_to_go_text(self):
        self.track(self.main, 7)

        row = self.rows()["Line Pilot"]
        self.assertEqual((row.reached, row.outcome, row.seconds_to_go), (False, "below_minimum", 13 * 60))
        self.assertContains(self.page(), "PAP minimum 20 min — not reached yet: 13 min to go.")

    def test_less_than_a_minute_short_is_one_minute_to_go(self):
        f.member_state(self.operation, self.main, accumulated_seconds=19 * 60 + 30)

        row = self.rows()["Line Pilot"]
        self.assertEqual((row.reached, row.seconds_to_go), (False, 60))
        page = self.page()
        self.assertContains(page, "PAP minimum 20 min — not reached yet: 1 min to go.")
        self.assertNotContains(page, "0 min to go")

    def test_over_the_limit_names_the_character_that_gets_the_pap(self):
        self.track(self.main, 37)
        self.track(self.alt, 30)

        rows = self.rows()

        self.assertEqual((rows["Pilot Alt"].outcome, rows["Pilot Alt"].capped_by), ("capped", "Line Pilot"))
        self.assertContains(
            self.page(), "Over the limit of 1 character per player: your character Line Pilot gets the PAP."
        )

    def test_a_character_that_left(self):
        self.track(self.main, 25, active=False)

        self.assertFalse(self.rows()["Line Pilot"].in_fleet)
        self.assertContains(
            self.page(), "Your character <strong>Line Pilot</strong> was in <strong>Fleet Boss</strong>'s fleet (StratOp) and left: 25 min in fleet."
        )

    def test_ending_fleet(self):
        self.track(self.main, 37)
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.ENDING)

        self.assertContains(self.page(), "The fleet is ending; PAP is being finalized.")

    def test_registration_by_pap_link(self):
        f.registration(self.operation, self.main)

        row = self.rows()["Line Pilot"]
        self.assertEqual((row.via_link, row.in_fleet, row.outcome), (True, False, "granted"))
        self.assertContains(self.page(), "Registered by PAP link: no minimum time needed.")

    def test_any_presence_counts_without_a_minimum(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(pap_minimum_minutes=0)
        self.track(self.main, 0)

        self.assertContains(self.page(), "Any time in fleet counts for PAP.")

    def test_shown_on_my_statistics_too(self):
        self.track(self.main, 37)

        page = self.page("fleetops:my_statistics")

        self.assertEqual([row.character_name for row in page.context["live_pap_rows"]], ["Line Pilot"])
        self.assertContains(page, 'id="live-pap-status"')

    def test_partial_refreshes_while_rows_exist(self):
        self.track(self.main, 37)

        dashboard = self.page()
        fragment = self.page("fleetops:live_pap")

        self.assertContains(dashboard, 'data-rows="1"')
        self.assertContains(dashboard, "setInterval(")
        self.assertTemplateUsed(fragment, "fleetops/partials/live_pap_status.html")
        self.assertContains(fragment, "Line Pilot")
        self.assertNotContains(fragment, "<script>")
        self.assertTrue(fragment.context["live_pap_rows"][0].can_open)

    def test_nothing_shown_without_running_fleets(self):
        page = self.page()

        self.assertNotContains(page, "PAP in running fleets")
        self.assertNotContains(page, 'data-rows="1"')


class MemberVisibilityTests(LiveStatusTestBase):
    """Members follow the running fleets they are tracked or registered in; VIS(2) still hides the member list."""

    def detail(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))

    def test_tracked_member_may_open_the_running_fleet(self):
        self.track(self.main, 37)

        page = self.detail(self.pilot)

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="composition-hidden"')
        self.assertIn(self.operation, list(self.page().context["active_operations"]))
        archive = self.page("fleetops:fleet_operations")
        self.assertIn(self.operation, list(archive.context["page"].object_list))

    def test_registered_member_may_open_the_running_fleet(self):
        f.registration(self.operation, self.alt)

        self.assertEqual(self.detail(self.pilot).status_code, 200)

    def test_others_may_not(self):
        self.track(self.main, 37)

        self.assertEqual(self.detail(f.create_user(perms=f.MEMBER_PERMS)).status_code, 404)

    def test_pap_preview_shows_members_only_their_own_rows(self):
        other = f.create_user("other", main_name="Other Pilot")
        self.track(self.main, 37)
        self.track(f.main_of(other), 30)
        self.track(f.next_id(), 30, name="Outside Pilot")

        member_page = self.detail(self.pilot)
        fc_page = self.detail(self.fc)

        self.assertEqual([c.character_name for c in member_page.context["pap_preview"]], ["Line Pilot"])
        self.assertEqual(member_page.context["pap_unregistered"], [])
        self.assertNotContains(member_page, "Other Pilot")
        self.assertNotContains(member_page, "Outside Pilot")
        self.assertEqual(
            [c.character_name for c in fc_page.context["pap_preview"]], ["Line Pilot", "Other Pilot"]
        )
        self.assertContains(fc_page, "Outside Pilot")
        self.assertContains(fc_page, 'id="pap-preview"')

    def test_fleet_viewers_see_the_whole_preview(self):
        viewer = f.create_user("viewer", perms=f.MEMBER_PERMS + ["fleetops.view_all_fleets"])
        self.track(self.main, 37)

        page = self.detail(viewer)

        self.assertEqual([c.character_name for c in page.context["pap_preview"]], ["Line Pilot"])
