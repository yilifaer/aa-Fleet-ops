"""The Fleet tab on the fleet page, its routes and their answers (JSON for scripts, redirects for forms)."""

from unittest import mock
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.messages import get_messages
from django.shortcuts import resolve_url
from django.test import Client, RequestFactory, SimpleTestCase
from django.urls import reverse

from fleetops import __version__
from fleetops.models import FleetOperation, FleetStructureTemplate, ShipFunction
from fleetops.services.fleet_access import FleetManageError
from fleetops.services.fleet_manage import FleetActionResult
from fleetops.views_fleet_common import wants_json

from . import factories as f
from .fleet_sim import FleetSim
from .test_views_permissions import FleetOpsViewTestCase, add_member_state

JSON = {"HTTP_ACCEPT": "application/json"}
BROWSER_FORM = {"HTTP_ACCEPT": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
WING_ID = 2_054_000_000_001
SQUAD_ID = 3_108_000_000_001

# (route name, extra URL kwargs, POST data that reaches the service)
MANAGE_POSTS = (
    ("fleet_refresh", {}, {}),
    ("fleet_wing_add", {}, {"name": "Logi"}),
    ("fleet_wing_rename", {"wing_id": WING_ID}, {"name": "Main"}),
    ("fleet_wing_delete", {"wing_id": WING_ID}, {}),
    ("fleet_squad_add", {"wing_id": WING_ID}, {"name": "Alpha"}),
    ("fleet_squad_rename", {"squad_id": SQUAD_ID}, {"name": "Bravo"}),
    ("fleet_squad_delete", {"squad_id": SQUAD_ID}, {}),
    (
        "fleet_members_move",
        {},
        {"character_ids": ["90000001"], "role": "squad_member", "wing_id": str(WING_ID), "squad_id": str(SQUAD_ID)},
    ),
    ("fleet_invite", {}, {"character_ids": ["90000001", "90000002"], "squad_id": str(SQUAD_ID)}),
    ("fleet_apply_template", {}, {"delete_extra": "1"}),
)
# The service behind each route, replaced by a fixed answer where a test checks the route itself.
SERVICES = {
    "fleet_wing_add": "fleetops.services.fleet_manage.add_wing",
    "fleet_wing_rename": "fleetops.services.fleet_manage.rename_wing",
    "fleet_wing_delete": "fleetops.services.fleet_manage.delete_wing",
    "fleet_squad_add": "fleetops.services.fleet_manage.add_squad",
    "fleet_squad_rename": "fleetops.services.fleet_manage.rename_squad",
    "fleet_squad_delete": "fleetops.services.fleet_manage.delete_squad",
    "fleet_members_move": "fleetops.services.fleet_manage.move_members",
    "fleet_invite": "fleetops.services.fleet_invites.invite_characters",
    "fleet_apply_template": "fleetops.services.fleet_templates.apply_template",
    "fleet_classify": "fleetops.services.composition_config.classify_type",
}
DONE = FleetActionResult(True, "Done.", data={"answer": 1})
FLEET_PARTIALS = (
    "_fleet.html",
    "_fleet_toolbar.html",
    "_fleet_status.html",
    "_fleet_bulk_bar.html",
    "_fleet_tree.html",
    "_fleet_wing_actions.html",
    "_fleet_composition.html",
    "_fleet_changes.html",
    "_fleet_invite_modal.html",
    "_fleet_template_modal.html",
)


class FleetTabTestCase(FleetOpsViewTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.template = FleetStructureTemplate.objects.create(name="Test layout", layout="Main: Alpha, Bravo")
        cls.function = ShipFunction.objects.create(name="Test function", sort_order=500)

    def setUp(self):
        super().setUp()
        # Fleet reads and writes reach an in-memory EVE, never ESI; the test fleets do not exist there.
        FleetSim().install(self)
        self.schedule_refresh = self.patch("fleetops.services.fleet_refresh.schedule_refresh")

    def url(self, name, operation, **kwargs):
        return reverse(f"fleetops:{name}", kwargs={"operation_uuid": operation.uuid, **kwargs})

    def patch_services(self, result=DONE):
        """Give every fleet service ``result``; returns the mocks by route name."""
        return {name: self.patch(target, return_value=result) for name, target in SERVICES.items()}

    def post_data(self, name, data):
        if name == "fleet_apply_template":
            return {**data, "template_id": str(self.template.pk)}
        return data

    def page(self, user, operation):
        response = self.client_for(user).get(self.detail_url(operation))
        self.assertEqual(response.status_code, 200)
        return response


class FleetRouteTests(FleetTabTestCase):
    def test_anonymous_users_are_sent_to_the_login_page(self):
        routes = [*((name, kwargs) for name, kwargs, _data in MANAGE_POSTS), ("fleet_state", {}),
                  ("fleet_invite_candidates", {}), ("fleet_classify", {})]
        for name, kwargs in routes:
            for method in ("get", "post"):
                with self.subTest(name=name, method=method):
                    response = getattr(Client(), method)(self.url(name, self.own_active, **kwargs))
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(urlsplit(response["Location"]).path, resolve_url(settings.LOGIN_URL))

    def test_members_without_management_rights_are_refused(self):
        member = self.client_for(self.member)
        for name, kwargs, data in MANAGE_POSTS:
            with self.subTest(name=name):
                response = member.post(self.url(name, self.own_active, **kwargs), self.post_data(name, data), **JSON)
                self.assertEqual(response.status_code, 403)
        self.assertEqual(member.get(self.url("fleet_invite_candidates", self.own_active)).status_code, 403)
        classify = member.post(self.url("fleet_classify", self.own_active), {"type_id": "11987"})
        self.assertEqual(classify.status_code, 403)

    def test_other_fcs_get_not_found_for_fleets_they_do_not_manage(self):
        other = self.client_for(self.other_fc)
        for name, kwargs, data in MANAGE_POSTS:
            with self.subTest(name=name):
                response = other.post(self.url(name, self.own_active, **kwargs), self.post_data(name, data), **JSON)
                self.assertEqual(response.status_code, 404)
        self.assertEqual(other.get(self.url("fleet_invite_candidates", self.own_active)).status_code, 404)

    def test_manager_reaches_every_service(self):
        services = self.patch_services()
        fc = self.client_for(self.fc)
        for name, kwargs, data in MANAGE_POSTS:
            with self.subTest(name=name):
                response = fc.post(self.url(name, self.own_active, **kwargs), self.post_data(name, data), **JSON)
                self.assertIn("no-store", response["Cache-Control"])
                body = response.json()
                self.assertEqual(set(body), {"ok", "message", "level", "data", "state"})
                self.assertEqual(body["state"]["status"], "active")
                self.assertIn("tree_html", body["state"])
                if name in services:
                    services[name].assert_called_once()
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        {key: body[key] for key in ("ok", "message", "level", "data")},
                        {"ok": True, "message": "Done.", "level": "success", "data": {"answer": 1}},
                    )
                else:
                    # The Refresh button reads the fleet itself.
                    self.assertNotIn(response.status_code, (403, 404, 405))

    def test_fleet_leads_manage_every_fleet(self):
        services = self.patch_services()

        response = self.client_for(self.lead).post(self.url("fleet_wing_add", self.own_active), **JSON)

        self.assertEqual(response.status_code, 200)
        services["fleet_wing_add"].assert_called_once_with(self.own_active, actor=self.lead, name="")

    def test_post_routes_refuse_get(self):
        fc = self.client_for(self.fc)
        for name, kwargs, _data in MANAGE_POSTS:
            with self.subTest(name=name):
                self.assertEqual(fc.get(self.url(name, self.own_active, **kwargs)).status_code, 405)
        self.assertEqual(self.client_for(self.lead).get(self.url("fleet_classify", self.own_active)).status_code, 405)
        self.assertEqual(fc.post(self.url("fleet_state", self.own_active)).status_code, 405)
        self.assertEqual(fc.post(self.url("fleet_invite_candidates", self.own_active)).status_code, 405)

    def test_form_posts_return_to_the_fleet_tab_with_a_message(self):
        self.patch_services(FleetActionResult(False, "That wing no longer exists.", level="warning", status=404))

        response = self.client_for(self.fc).post(self.url("fleet_wing_add", self.own_active), **BROWSER_FORM)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{self.detail_url(self.own_active)}#fleet")
        self.assertIn("no-store", response["Cache-Control"])
        messages = list(get_messages(response.wsgi_request))
        self.assertEqual([(str(m), m.level_tag) for m in messages], [("That wing no longer exists.", "warning")])

    def test_service_results_and_errors_are_answered_alike(self):
        fc = self.client_for(self.fc)
        url = self.url("fleet_wing_add", self.own_active)
        created = FleetActionResult(True, "Wing created.", data={"wing_id": WING_ID})
        with mock.patch("fleetops.services.fleet_manage.add_wing", return_value=created) as add_wing:
            response = fc.post(url, {"name": "Logi"}, **JSON)
        add_wing.assert_called_once_with(self.own_active, actor=self.fc, name="Logi")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {key: value for key, value in response.json().items() if key != "state"},
            {"ok": True, "message": "Wing created.", "level": "success", "data": {"wing_id": WING_ID}},
        )

        busy = FleetManageError("Another change to this fleet is in progress. Try again in a moment.", 409)
        with mock.patch("fleetops.services.fleet_manage.add_wing", side_effect=busy):
            response = fc.post(url, **JSON)
            form = fc.post(url, **BROWSER_FORM)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["level"], "error")
        self.assertEqual(response.json()["message"], busy.message)
        self.assertEqual(form.status_code, 302)
        self.assertEqual([str(m) for m in get_messages(form.wsgi_request)], [busy.message])

    def test_ids_and_positions_are_checked_before_the_service(self):
        fc = self.client_for(self.fc)
        move = self.url("fleet_members_move", self.own_active)
        with mock.patch("fleetops.services.fleet_manage.move_members") as move_members:
            for data, message in (
                ({"role": "squad_member"}, "Choose the members to move."),
                ({"character_ids": ["x"], "role": "squad_member"}, "Choose the members to move."),
                ({"character_ids": ["90000001"], "role": "boss"}, "Choose a fleet position."),
                (
                    {"character_ids": ["90000001"], "role": "squad_member", "wing_id": "abc"},
                    "That wing does not exist.",
                ),
            ):
                with self.subTest(data=data):
                    response = fc.post(move, data, **JSON)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["message"], message)
            move_members.assert_not_called()
            fc.post(move, {"character_ids": ["90000001", "90000001", "90000002"], "role": "wing_commander",
                           "wing_id": str(WING_ID), "squad_id": "-1"}, **JSON)
        moves = move_members.call_args.args[1]
        self.assertEqual([move.character_id for move in moves], [90000001, 90000002])
        positions = {(move.role, move.wing_id, move.squad_id) for move in moves}
        self.assertEqual(positions, {("wing_commander", WING_ID, None)})

        too_big = self.url("fleet_wing_rename", self.own_active, wing_id=2**63)
        self.assertEqual(fc.post(too_big, {"name": "Main"}, **JSON).status_code, 404)

    def test_template_must_exist_and_be_active(self):
        fc = self.client_for(self.fc)
        url = self.url("fleet_apply_template", self.own_active)
        inactive = FleetStructureTemplate.objects.create(name="Old", layout="Main: Alpha", is_active=False)
        for template_id in ("", "abc", str(inactive.pk)):
            with self.subTest(template_id=template_id):
                response = fc.post(url, {"template_id": template_id}, **JSON)
                self.assertEqual(response.status_code, 404)
        with mock.patch("fleetops.services.fleet_templates.apply_template") as apply_template:
            apply_template.return_value = FleetActionResult(True, "Applying template Test layout.", level="info")
            fc.post(url, {"template_id": str(self.template.pk)}, **JSON)
        apply_template.assert_called_once_with(self.own_active, self.template, actor=self.fc, delete_extra=False)

    def test_invite_candidates(self):
        fc = self.client_for(self.fc)
        with mock.patch("fleetops.services.fleet_invites.invite_candidates", return_value=[]) as candidates:
            response = fc.get(self.url("fleet_invite_candidates", self.own_active), {"q": " Mem "})

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response.json(), {"query": "Mem", "candidates": []})
        candidates.assert_called_once_with("Mem", actor=self.fc, operation=self.own_active)

    def test_quick_classify_needs_configuration_rights(self):
        services = self.patch_services()
        data = {"type_id": "11987", "function_id": str(self.function.pk)}
        self.assertEqual(
            self.client_for(self.fc).post(self.url("fleet_classify", self.own_active), data, **JSON).status_code, 403
        )
        response = self.client_for(self.lead).post(self.url("fleet_classify", self.own_active), data, **JSON)
        self.assertEqual(response.status_code, 200)
        services["fleet_classify"].assert_called_once_with(11987, self.function.pk, actor=self.lead)
        missing = self.client_for(self.lead).post(self.url("fleet_classify", self.own_active), {"type_id": "1"}, **JSON)
        self.assertEqual(missing.status_code, 400)


class FleetStateTests(FleetTabTestCase):
    def test_state_follows_the_fleet_tab_visibility(self):
        url = self.url("fleet_state", self.own_active)

        self.assertEqual(self.client_for(self.fc).get(url).status_code, 200)
        # view_all_fleets sees a fleet it does not manage, read-only.
        self.assertEqual(self.client_for(self.other_fc).get(url).status_code, 200)
        self.assertEqual(self.client_for(self.member).get(url).status_code, 404)
        self.assertEqual(self.client_for(self.no_perms).get(url).status_code, 403)
        # After the close everybody who can open the fleet page sees its composition.
        self.assertEqual(self.client_for(self.outsider).get(self.url("fleet_state", self.own_closed)).status_code, 200)

    def test_unchanged_state_carries_no_html(self):
        fc = self.client_for(self.fc)
        url = self.url("fleet_state", self.own_active)

        full = fc.get(url).json()
        self.assertFalse(full["unchanged"])
        for key in ("version", "server_time", "status", "last_esi_update", "structure_read_at", "stale",
                    "member_count", "max_members", "write", "job", "status_html", "tree_html",
                    "composition_html", "changes_html", "members_html", "pap_html"):
            self.assertIn(key, full)
        self.assertTrue(full["write"]["allowed"])
        self.assertEqual(full["max_members"], 256)

        short = fc.get(url, {"v": full["version"], "live": "1"})
        self.assertIn("no-store", short["Cache-Control"])
        self.assertTrue(short.json()["unchanged"])
        self.assertNotIn("tree_html", short.json())
        self.assertFalse(fc.get(url, {"v": "old"}).json()["unchanged"])


class FleetTabPageTests(FleetTabTestCase):
    def test_manager_of_an_active_fleet_gets_the_fleet_tab_first(self):
        response = self.page(self.fc, self.own_active)

        for name in FLEET_PARTIALS:
            with self.subTest(partial=name):
                self.assertTemplateUsed(response, f"fleetops/operation/{name}")
        for text in (
            'class="nav-link active" data-bs-toggle="tab" data-bs-target="#fleet" id="fleet-tab">Fleet</button>',
            'class="tab-pane fade show active" id="fleet"',
            'id="fleet-root"',
            'data-status="active"',
            'data-can-manage="1" data-can-write="1"',
            f'data-state-url="{self.url("fleet_state", self.own_active)}"',
            'id="fleet-toolbar"', 'id="fleet-status"', 'id="fleet-tree"', 'id="fleet-composition"',
            'id="fleet-changes"', 'id="fleet-flash"', 'id="fleet-confirm"', 'id="fleet-eve-clock"',
            'data-node="fleet"', 'data-drop-role="fleet_commander"', 'id="fleet-refresh-form"',
            f'action="{self.url("fleet_wing_add", self.own_active)}"',
            f"fleetops/fleet_live.js?v={__version__}", f"fleetops/fleet_actions.js?v={__version__}",
            f"fleetops/fleet_tools.js?v={__version__}", f"fleetops/fleet.css?v={__version__}",
            "Also delete empty wings and squads that are not in the template",
            'id="stat-characters"', 'id="stat-last-esi"', 'id="fleet-reload-hint"',
            'id="members-fragment"', 'id="pap-preview-fragment"',
        ):
            with self.subTest(text=text):
                self.assertContains(response, text)
        # The Members tab is no longer the first one, and the page does not reload itself.
        self.assertContains(response, 'class="tab-pane fade" id="members"')
        self.assertNotContains(response, "location.reload")
        # Q3: the delete option of a structure template starts unticked.
        self.assertNotContains(response, 'name="delete_extra" value="1" id="fleet-template-delete-extra" checked')

    def test_fleet_tab_is_hidden_from_members_of_an_open_fleet(self):
        response = self.page(self.member, self.own_active)

        self.assertNotContains(response, 'id="fleet-root"')
        self.assertNotContains(response, 'data-bs-target="#fleet"')
        self.assertNotContains(response, 'id="fleet-reload-hint"')
        self.assertContains(response, 'class="nav-link active" data-bs-toggle="tab" data-bs-target="#members"')
        self.assertContains(response, 'id="composition-hidden"')
        self.assertNotContains(response, "location.reload")

    def test_viewers_of_all_fleets_see_the_fleet_read_only(self):
        response = self.page(self.other_fc, self.own_active)

        self.assertContains(response, 'data-can-manage="0" data-can-write="0"')
        self.assertNotContains(response, 'id="fleet-refresh-form"')
        self.assertNotContains(response, 'id="fleet-invite-modal"')
        self.assertNotContains(response, self.url("fleet_wing_add", self.own_active))

    def test_closed_fleet_shows_its_composition(self):
        for user in (self.fc, self.outsider):
            with self.subTest(user=user.username):
                response = self.page(user, self.own_closed)
                self.assertContains(response, 'data-bs-target="#fleet" id="fleet-tab">Composition</button>')
                self.assertContains(response, 'class="nav-link active" data-bs-toggle="tab" data-bs-target="#members"')
                self.assertContains(response, 'data-status="closed"')
                self.assertContains(response, 'id="fleet-composition"')
                self.assertContains(response, "Last known ships of everyone tracked in this fleet.")
                self.assertNotContains(response, 'id="fleet-tree"')
                self.assertNotContains(response, 'id="fleet-status"')

    def test_ending_fleet_keeps_reloading_until_it_closed(self):
        ending = f.create_operation(self.fc, status=FleetOperation.Status.ENDING)
        add_member_state(ending, self.fc_main, self.fc, fleet_role="fleet_commander")

        response = self.page(self.fc, ending)

        self.assertContains(response, 'class="nav-link active" data-bs-toggle="tab" data-bs-target="#fleet"')
        self.assertContains(response, "setTimeout(()=>location.reload(),5000);")
        self.assertNotContains(response, 'id="fleet-refresh-form"')

    def test_fleets_without_an_esi_fleet_have_no_fleet_tab(self):
        manual = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED, is_manual=True, esi_fleet_id=None)
        link_only = f.create_operation(self.fc, link_only=True)
        for operation in (manual, link_only):
            with self.subTest(operation=operation.pk):
                response = self.page(self.fc, operation)
                self.assertNotContains(response, 'data-bs-target="#fleet"')
                self.assertContains(response, 'class="nav-link active" data-bs-toggle="tab" data-bs-target="#members"')

    def test_active_fleets_without_a_fleet_tab_reload_for_their_managers(self):
        link_only = f.create_operation(self.fc, link_only=True)

        # No poller updates a PAP-link fleet: its FC sees registrations come in by a reload every minute,
        # which waits while a dialog is open or a form is being filled in.
        response = self.page(self.fc, link_only)
        self.assertContains(response, "setTimeout(function reloadPage(){ if(document.querySelector('.modal.show') || formInUse())")
        # ESI fleets update in place, and members of an open fleet reload the page themselves.
        self.assertNotContains(self.page(self.fc, self.own_active), "location.reload")
        self.assertNotContains(self.page(self.member, self.own_active), "location.reload")


class FleetConfigurationTests(FleetTabTestCase):
    def test_new_configuration_sections(self):
        lead = self.client_for(self.lead)
        rule = self.function.rules.create(kind="type", target_id=11987, label="Guardian")
        objects = {"ship-functions": self.function, "ship-function-rules": rule, "structure-templates": self.template}
        for section, obj in objects.items():
            with self.subTest(section=section):
                listing = lead.get(reverse("fleetops:configuration_list", args=[section]))
                self.assertContains(listing, str(obj.pk))
                self.assertEqual(lead.get(reverse("fleetops:configuration_add", args=[section])).status_code, 200)
                edit = lead.get(reverse("fleetops:configuration_edit", args=[section, obj.pk]))
                self.assertEqual(edit.status_code, 200)
                fc_listing = self.client_for(self.fc).get(reverse("fleetops:configuration_list", args=[section]))
                self.assertEqual(fc_listing.status_code, 403)
        self.assertContains(lead.get(reverse("fleetops:configuration_index")), "Fleet Structure Templates")

    def test_structure_template_from_the_configuration_page(self):
        response = self.client_for(self.lead).post(
            reverse("fleetops:configuration_add", args=["structure-templates"]),
            {
                "name": "Small",
                "layout": "Main: Alpha\nLogi: Logi",
                "description": "",
                "is_active": "on",
                "sort_order": "0",
            },
        )

        self.assertRedirects(response, reverse("fleetops:configuration_list", args=["structure-templates"]))
        template = FleetStructureTemplate.objects.get(name="Small")
        self.assertEqual(template.wings[1], {"name": "Logi", "squads": ["Logi"]})

    def test_settings_page_shows_the_fleet_tab_settings(self):
        response = self.client_for(self.lead).get(reverse("fleetops:configuration_settings"))

        self.assertContains(response, 'id="settings-fleet-tab"')
        self.assertContains(response, 'name="fleet_live_refresh_seconds"')
        self.assertContains(response, 'name="bridge_excluded_group_ids"')


class WantsJsonTests(SimpleTestCase):
    def test_only_explicit_json_requests_get_json(self):
        factory = RequestFactory()
        cases = (
            ({}, False),
            (BROWSER_FORM, False),
            ({"HTTP_ACCEPT": "*/*"}, False),
            (JSON, True),
            ({"HTTP_ACCEPT": "application/json, text/plain, */*"}, True),
            ({"HTTP_ACCEPT": "text/html, application/json"}, False),
            ({"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}, True),
        )
        for headers, expected in cases:
            with self.subTest(headers=headers):
                self.assertIs(wants_json(factory.post("/", **headers)), expected)
