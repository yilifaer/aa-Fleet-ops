"""The fleet tree of the Fleet tab (F1, #33a): placement, markers, pending changes and the view version."""

import time
from datetime import timedelta

from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import TestCase
from django.utils import timezone

from fleetops.models import AuditLog, FleetMemberEvent, FleetOperation
from fleetops.services import fleet_jobs, fleet_overlay
from fleetops.services.audit import audit
from fleetops.services.fleet_rules import MAX_SQUADS_PER_WING, MAX_WINGS
from fleetops.services.fleet_view import build_fleet_view, fleet_panel, recent_changes
from fleetops.templatetags.fleetops_fleet import change_text, fleet_event_change, member_position, role_label

from . import factories as f

Status = FleetOperation.Status
JITA = 30000142
GUARDIAN, CAPSULE = 11987, 670
# Wing and squad ids as ESI allocates them.
MAIN, LOGI = 2_054_000_000_011, 2_054_000_000_012
ALPHA, BRAVO, LOGI_SQUAD = 3_108_000_000_021, 3_108_000_000_022, 3_108_000_000_023


class FleetViewTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        read_at = timezone.now() - timedelta(seconds=30)
        self.operation = f.create_operation(
            self.fc,
            fleet_wings=f.wings((MAIN, "Main", ((ALPHA, "Alpha"), (BRAVO, "Bravo"))), (LOGI, "Logi", ((LOGI_SQUAD, "Logi"),))),
            fleet_wings_read_at=read_at,
            last_esi_update=read_at,
        )
        self.boss = f.member_state(
            self.operation, self.fc_char, role="fleet_commander", wing_id=-1, squad_id=-1, ship_type_id=GUARDIAN,
            ship_type_name="Guardian", solar_system_id=JITA, solar_system_name="Jita", accumulated_seconds=1800,
        )

    def pilot(self, name, *, role="squad_member", wing_id=MAIN, squad_id=ALPHA, in_auth=True, **extra):
        if in_auth:
            character = f.main_of(f.create_user(main_name=name))
        else:
            character = f.next_id()
        defaults = {"ship_type_id": GUARDIAN, "ship_type_name": "Guardian", "solar_system_id": JITA, "solar_system_name": "Jita"}
        return f.member_state(
            self.operation, character, name=name, role=role, wing_id=wing_id, squad_id=squad_id, **{**defaults, **extra}
        )

    def view(self):
        self.operation.refresh_from_db()
        return build_fleet_view(self.operation)


class PlacementTests(FleetViewTestCase):
    def test_positions_ordering_and_counts(self):
        self.pilot("Zed", squad_id=ALPHA)
        self.pilot("adam", squad_id=ALPHA)
        self.pilot("Squad Lead", role="squad_commander", squad_id=ALPHA)
        self.pilot("Wing Lead", role="wing_commander", wing_id=LOGI, squad_id=-1)
        self.pilot("Logi One", wing_id=LOGI, squad_id=LOGI_SQUAD, station_id=60003760, takes_fleet_warp=False)
        self.pilot("Gone", active=False)

        view = self.view()

        self.assertEqual(view.commander.name, "Boss Main")
        self.assertTrue(view.commander.is_boss)
        self.assertTrue(view.commander.protected)
        self.assertEqual(view.boss_name, "Boss Main")
        main, logi = view.wings
        self.assertEqual((main.id, main.name, logi.name), (MAIN, "Main", "Logi"))
        alpha, bravo = main.squads
        self.assertEqual(alpha.commander.name, "Squad Lead")
        # The commander first, then the members by name whatever their case.
        self.assertEqual([member.name for member in alpha.members], ["adam", "Zed"])
        self.assertEqual((alpha.count, alpha.is_empty, bravo.count, bravo.is_empty), (3, False, 0, True))
        self.assertEqual((main.count, main.is_empty, main.commander), (3, False, None))
        self.assertEqual(logi.commander.name, "Wing Lead")
        self.assertEqual((logi.count, logi.is_empty), (2, False))
        logi_one = logi.squads[0].members[0]
        self.assertEqual((logi_one.docked, logi_one.takes_fleet_warp, logi_one.wing_id, logi_one.squad_id),
                         (True, False, LOGI, LOGI_SQUAD))
        self.assertFalse(logi_one.protected)
        self.assertEqual(view.member_count, 6)
        self.assertEqual(view.unplaced, [])
        self.assertTrue(view.structure_known)
        self.assertEqual((view.commander.wing_id, view.commander.squad_id, view.commander.seconds_in_fleet), (None, None, 1800))

    def test_member_details(self):
        outsider = self.pilot("Outsider", in_auth=False, ship_type_id=CAPSULE, ship_type_name="", solar_system_name="")

        member = self.view().wings[0].squads[0].members[0]

        self.assertEqual(member.character_id, outsider.character_id)
        self.assertFalse(member.in_auth)
        self.assertEqual(member.ship_name, "")
        self.assertEqual(member.system_name, "")
        # The seeded "Capsules" function has a type rule for capsules.
        self.assertEqual((member.function_name, member.function_color), ("Capsules", "#6c757d"))
        self.assertTrue(self.view().commander.in_auth)
        self.assertEqual(self.view().commander.function_name, "Fleet Command")

    def test_the_boss_and_the_fc_character_are_protected(self):
        alt = self.pilot("New Boss", role="squad_commander", squad_id=BRAVO)
        FleetOperation.objects.filter(pk=self.operation.pk).update(fleet_boss_character_id=alt.character_id)

        view = self.view()

        bravo_lead = view.wings[0].squads[1].commander
        self.assertTrue(bravo_lead.is_boss)
        self.assertTrue(bravo_lead.protected)
        self.assertFalse(view.commander.is_boss)
        self.assertTrue(view.commander.protected)
        self.assertEqual(view.boss_name, "New Boss")

    def test_members_outside_the_snapshot_wait_for_the_next_wings_read(self):
        self.pilot("New Wing Pilot", wing_id=2_054_000_000_099, squad_id=3_108_000_000_099)
        self.pilot("Wrong Wing", wing_id=LOGI, squad_id=ALPHA)
        self.pilot("Second Squad Lead", role="squad_commander", squad_id=ALPHA)
        self.pilot("Third Squad Lead", role="squad_commander", squad_id=ALPHA)
        self.pilot("Unknown Position", role="", wing_id=None, squad_id=None)

        view = self.view()

        self.assertEqual(
            [member.name for member in view.unplaced],
            ["New Wing Pilot", "Third Squad Lead", "Unknown Position", "Wrong Wing"],
        )
        self.assertEqual(view.wings[0].squads[0].commander.name, "Second Squad Lead")
        self.assertEqual(view.member_count, 6)

    def test_without_a_wings_read_everyone_but_the_fleet_commander_is_unplaced(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(fleet_wings=[], fleet_wings_read_at=None)
        self.pilot("Pilot")

        view = self.view()

        self.assertFalse(view.structure_known)
        self.assertEqual(view.wings, [])
        self.assertEqual([member.name for member in view.unplaced], ["Pilot"])
        self.assertEqual(view.commander.name, "Boss Main")

    def test_limits_of_wings_and_squads(self):
        self.assertTrue(self.view().can_add_wing)
        full = f.wings(*((f"W{number}", [f"S{index}" for index in range(MAX_SQUADS_PER_WING)]) for number in range(MAX_WINGS)))
        FleetOperation.objects.filter(pk=self.operation.pk).update(fleet_wings=full)

        view = self.view()

        self.assertFalse(view.can_add_wing)
        self.assertFalse(view.wings[0].can_add_squad)
        self.assertTrue(self.view().wings[0].squads[0].is_empty)

    def test_boss_without_a_tracked_row(self):
        self.boss.delete()
        FleetOperation.objects.filter(pk=self.operation.pk).update(fleet_boss_character_id=4242)

        view = self.view()

        self.assertIsNone(view.commander)
        self.assertEqual((view.boss_id, view.boss_name), (4242, "Character 4242"))


class PendingChangeTests(FleetViewTestCase):
    def test_expected_changes_show_as_pending(self):
        pilot = self.pilot("Mover", squad_id=ALPHA)
        op = self.operation.pk
        fleet_overlay.expect(op, {"kind": "wing_created", "wing_id": 2_054_000_000_013, "name": ""})
        fleet_overlay.expect(op, {"kind": "squad_renamed", "squad_id": BRAVO, "name": "Charlie"})
        fleet_overlay.expect(
            op,
            {"kind": "member_moved", "character_id": pilot.character_id, "role": "squad_member", "wing_id": MAIN,
             "squad_id": BRAVO},
        )

        view = self.view()

        new_wing = view.wings[2]
        self.assertEqual((new_wing.name, new_wing.pending, new_wing.squads, new_wing.is_empty), ("Wing", True, [], True))
        bravo = view.wings[0].squads[1]
        self.assertEqual((bravo.name, bravo.pending), ("Charlie", True))
        self.assertEqual([(member.name, member.pending) for member in bravo.members], [("Mover", True)])
        self.assertTrue(view.wings[0].squads[0].is_empty)
        self.assertFalse(view.wings[0].pending)

    def test_invitations_until_the_character_joins(self):
        op = self.operation.pk
        fleet_overlay.expect(op, {"kind": "invited", "character_id": 77, "name": "Invited Alt", "role": "squad_member",
                                  "wing_id": LOGI, "squad_id": LOGI_SQUAD})
        fleet_overlay.expect(op, {"kind": "invited", "character_id": self.fc_char.character_id, "name": "Already In",
                                  "role": "squad_member"})

        invitations = self.view().invitations

        self.assertEqual([(item["character_id"], item["name"], item["where"]) for item in invitations],
                         [(77, "Invited Alt", "Logi / Logi")])

    def test_tree_html_marks_pending_units_and_members(self):
        pilot = self.pilot("Mover", squad_id=ALPHA)
        fleet_overlay.expect(
            self.operation.pk,
            {"kind": "member_moved", "character_id": pilot.character_id, "role": "squad_member", "wing_id": MAIN,
             "squad_id": BRAVO},
        )
        fleet_overlay.expect(self.operation.pk, {"kind": "wing_renamed", "wing_id": LOGI, "name": "<b>Logi</b>"})
        self.operation.refresh_from_db()

        html = render_to_string("fleetops/operation/_fleet_tree.html", fleet_panel(self.operation, self.fc))

        self.assertIn(f'data-node="wing" data-wing-id="{LOGI}" data-pending="1"', html)
        self.assertIn("&lt;b&gt;Logi&lt;/b&gt;", html)
        self.assertNotIn("<b>Logi</b>", html)
        self.assertIn(f'class="fleet-member is-pending" data-character-id="{pilot.character_id}"', html)
        self.assertIn("No members", html)


class TreeHtmlTests(FleetViewTestCase):
    def html(self, user=None):
        self.operation.refresh_from_db()
        return render_to_string("fleetops/operation/_fleet_tree.html", fleet_panel(self.operation, user or self.fc))

    def test_member_rows(self):
        outsider = self.pilot("Outsider", in_auth=False, station_id=60003760, takes_fleet_warp=False)

        html = self.html()

        self.assertIn(f"Fleet {self.operation.esi_fleet_id}</strong> · Boss Boss Main", html)
        self.assertIn('<span id="fleet-member-count" title="Members in the fleet; EVE allows 256">2 / 256</span>', html)
        self.assertIn('title="Fleet boss"', html)
        self.assertIn('<span class="badge fleet-position" title="Fleet Commander">FC</span>', html)
        self.assertIn('title="This character has no Alliance Auth account.">Not in Alliance Auth</span>', html)
        self.assertIn(f'href="https://evewho.com/character/{outsider.character_id}"', html)
        self.assertIn(f"https://images.evetech.net/types/{GUARDIAN}/icon?size=32", html)
        self.assertIn('title="Docked"', html)
        self.assertIn('title="Not taking fleet warp"', html)
        self.assertIn(f'data-character-id="{self.fc_char.character_id}" data-role="fleet_commander" data-wing-id="" data-squad-id=""', html)
        self.assertIn('data-protected="1"', html)
        self.assertIn('draggable="true"', html)
        self.assertIn('input class="form-check-input fleet-select"', html)
        self.assertIn("in fleet 30 min", html)
        for toggle in ("fleet", f"wing:{MAIN}", f"squad:{ALPHA}"):
            self.assertIn(f'data-fleet-toggle="{toggle}"', html)

    def test_viewers_get_no_controls(self):
        viewer = f.create_user(perms=f.MEMBER_PERMS + ["fleetops.view_all_fleets"])

        html = self.html(viewer)

        self.assertNotIn('draggable="true"', html)
        self.assertNotIn("fleet-select", html)
        self.assertNotIn("<form", html)

    def test_unplaced_and_unknown_structure(self):
        self.pilot("Lost", wing_id=1, squad_id=2)
        self.assertIn("Not yet placed — waiting for the next wings read", self.html())

        FleetOperation.objects.filter(pk=self.operation.pk).update(fleet_wings=[])
        html = self.html()
        self.assertIn("Wings and squads have not been read from ESI yet.", html)


class VersionTests(FleetViewTestCase):
    def version(self, user=None):
        self.operation.refresh_from_db()
        return fleet_panel(self.operation, user or self.fc)["fleet_version"]

    def test_the_version_is_stable_and_follows_every_visible_change(self):
        pilot = self.pilot("Pilot")
        first = self.version()
        self.assertEqual(first, self.version())

        changes = (
            lambda: FleetOperation.objects.filter(pk=self.operation.pk).update(
                fleet_wings=f.wings((MAIN, "Primary", ((ALPHA, "Alpha"), (BRAVO, "Bravo"))))
            ),
            lambda: type(pilot).objects.filter(pk=pilot.pk).update(ship_type_id=CAPSULE),
            lambda: type(pilot).objects.filter(pk=pilot.pk).update(accumulated_seconds=60),
            lambda: fleet_overlay.expect(self.operation.pk, {"kind": "squad_renamed", "squad_id": ALPHA, "name": "A"}),
            lambda: fleet_jobs.claim_job(self.operation, kind="move", label="Move 2 members", actor=self.fc, total=2),
            lambda: audit(self.fc, "fleet.wing_rename", self.operation, None, {"result": "ok", "summary": "Renamed"}),
            lambda: f.add_attendance(self.operation, self.fc, source="manual"),
            lambda: FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.ENDING),
        )
        seen = {first}
        for number, change in enumerate(changes):
            with self.subTest(change=number):
                change()
                version = self.version()
                self.assertNotIn(version, seen)
                seen.add(version)

    def test_write_state_is_part_of_the_version(self):
        manager = self.version()
        viewer = self.version(f.create_user(perms=f.MEMBER_PERMS + ["fleetops.view_all_fleets"]))

        self.assertNotEqual(manager, viewer)


class RecentChangesTests(FleetViewTestCase):
    def test_only_fleet_changes_newest_first(self):
        audit(self.fc, "fleet.start", self.operation, None, {})
        audit(self.fc, "fleet.wing_create", self.operation, None, {"result": "ok", "summary": "Created wing Logi"})
        audit(self.fc, "fleet.kick_member", self.operation, None, {"character_id": 5})
        audit(self.fc, "fleet.member_move", self.operation, None, {"result": "failed", "summary": "Could not move Bob"})
        audit(self.fc, "fleet.invite_job", f.create_operation(self.fc), None, {"summary": "Other fleet"})

        changes = recent_changes(self.operation)

        self.assertEqual([entry.action for entry in changes], ["fleet.member_move", "fleet.kick_member", "fleet.wing_create"])
        self.assertEqual([change_text(entry) for entry in changes], ["Could not move Bob", "Kicked a member", "Created wing Logi"])
        html = render_to_string("fleetops/operation/_fleet_changes.html", fleet_panel(self.operation, self.fc))
        self.assertIn("Boss Main · Could not move Bob", html.replace("</span>", ""))
        self.assertIn('class="list-group-item text-danger"', html)

    def test_viewers_do_not_see_the_changes(self):
        audit(self.fc, "fleet.wing_create", self.operation, None, {"summary": "Created wing Logi"})
        viewer = f.create_user(perms=f.MEMBER_PERMS + ["fleetops.view_all_fleets"])

        panel = fleet_panel(self.operation, viewer)

        self.assertEqual(panel["fleet_changes"], [])
        self.assertEqual(render_to_string("fleetops/operation/_fleet_changes.html", panel).strip(), "")
        self.assertTrue(AuditLog.objects.filter(action="fleet.wing_create").exists())


class PositionNameTests(FleetViewTestCase):
    """#33a: wing and squad names instead of raw ids; -1 is the fleet command position."""

    def test_member_positions(self):
        cases = (
            ({"fleet_role": "fleet_commander", "wing_id": -1, "squad_id": -1}, ("Fleet Command", "—")),
            ({"fleet_role": "wing_commander", "wing_id": LOGI, "squad_id": -1}, ("Logi", "Wing Command")),
            ({"fleet_role": "squad_member", "wing_id": MAIN, "squad_id": BRAVO}, ("Main", "Bravo")),
            ({"fleet_role": "squad_member", "wing_id": 7, "squad_id": 8}, ("Wing 7", "Squad 8")),
            ({"fleet_role": "squad_member", "wing_id": None, "squad_id": None}, ("—", "—")),
        )
        for values, expected in cases:
            with self.subTest(values=values):
                member = type(self.boss)(**values)
                self.assertEqual(member_position(member, self.operation), expected)

    def test_event_values(self):
        def event(kind, old, new):
            return FleetMemberEvent(event_type=kind, old_value=old, new_value=new)

        Event = FleetMemberEvent.EventType
        cases = (
            (event(Event.WING_CHANGE, str(MAIN), str(LOGI)), ("Main", "Logi")),
            (event(Event.WING_CHANGE, str(MAIN), "-1"), ("Main", "Fleet Command")),
            (event(Event.WING_CHANGE, "99", str(MAIN)), ("Wing 99", "Main")),
            (event(Event.SQUAD_CHANGE, str(ALPHA), str(LOGI_SQUAD)), ("Alpha", "Logi")),
            (event(Event.SQUAD_CHANGE, str(ALPHA), "-1"), ("Alpha", "No squad")),
            (event(Event.SQUAD_CHANGE, "", "5"), ("—", "Squad 5")),
            (event(Event.ROLE_CHANGE, "squad_member", "wing_commander"), ("Squad Member", "Wing Commander")),
            (event(Event.SHIP_CHANGE, "Guardian", "Capsule"), ("Guardian", "Capsule")),
        )
        for item, expected in cases:
            with self.subTest(event=item.event_type, old=item.old_value):
                self.assertEqual(fleet_event_change(item, self.operation), expected)
        self.assertEqual(role_label("squad_commander"), "Squad Commander")
        self.assertEqual(role_label("other"), "other")

    def test_names_follow_the_latest_snapshot(self):
        item = FleetMemberEvent(event_type=FleetMemberEvent.EventType.WING_CHANGE, old_value=str(MAIN), new_value=str(LOGI))
        self.operation.fleet_wings = f.wings((MAIN, "Primary", ()), (LOGI, "Logi", ()))

        self.assertEqual(fleet_event_change(item, self.operation), ("Primary", "Logi"))


class StaleExpectationTests(FleetViewTestCase):
    def test_a_late_read_replaces_the_expected_change(self):
        fleet_overlay.expect(self.operation.pk, {"kind": "wing_renamed", "wing_id": MAIN, "name": "New", "at": time.time() - 10})
        self.assertEqual(self.view().wings[0].name, "New")

        # A wings read made at least ESI_READ_LAG seconds after the change shows ESI's own name.
        FleetOperation.objects.filter(pk=self.operation.pk).update(fleet_wings_read_at=timezone.now())
        self.assertEqual(self.view().wings[0].name, "Main")
