# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Squad changes in the Events tab name the wing: "DPS / 111 → FAX / 111", not "111 → 111" (FB04)."""

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import translation

from fleetops.models import FleetMemberEvent, FleetOperation
from fleetops.services.fleet_rules import normalize_wings, squad_path_label
from fleetops.templatetags.fleetops_fleet import fleet_event_change

from . import factories as f

Event = FleetMemberEvent.EventType
DPS, FAX = 2_054_000_000_101, 2_054_000_000_102
DPS_SQUAD, FAX_SQUAD, SOLO_SQUAD = 3_108_000_000_101, 3_108_000_000_102, 3_108_000_000_103
WINGS = f.wings(
    (DPS, "DPS", [(DPS_SQUAD, "111"), (SOLO_SQUAD, "222")]),
    (FAX, "FAX", [(FAX_SQUAD, "111")]),
)


class SquadPathTests(TestCase):
    def test_a_squad_is_named_with_its_wing(self):
        self.assertEqual(squad_path_label(DPS_SQUAD, WINGS), "DPS / 111")
        self.assertEqual(squad_path_label(FAX_SQUAD, WINGS), "FAX / 111")
        self.assertEqual(squad_path_label(str(SOLO_SQUAD), WINGS), "DPS / 222")

    def test_a_squad_that_is_gone_keeps_its_number(self):
        self.assertEqual(squad_path_label(999, WINGS), "Squad 999")
        self.assertEqual(squad_path_label(999, []), "Squad 999")

    def test_units_without_a_name_are_called_by_their_number(self):
        wings = normalize_wings([{"id": 7, "name": "", "squads": [{"id": 8, "name": ""}]}])

        self.assertEqual(squad_path_label(8, wings), "Wing 7 / Squad 8")

    def test_the_separator_is_not_translated(self):
        with translation.override("zh-hans"):
            self.assertEqual(squad_path_label(DPS_SQUAD, WINGS), "DPS / 111")


class EventsTabTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("squad-fc", perms=f.FC_PERMS, main_name="Example FC")
        self.pilot = f.create_character("Pilot A")
        self.operation = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED)
        FleetOperation.objects.filter(pk=self.operation.pk).update(fleet_wings=WINGS)
        self.operation.refresh_from_db()

    def event(self, old, new):
        return FleetMemberEvent.objects.create(
            operation=self.operation,
            character_id=self.pilot.character_id,
            character_name=self.pilot.character_name,
            event_type=Event.SQUAD_CHANGE,
            old_value=old,
            new_value=new,
        )

    def events_html(self, **headers):
        client = Client(**headers)
        client.force_login(self.fc)
        response = client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode().split('id="events"', 1)[1].split('id="attendance"', 1)[0]

    def test_two_wings_with_the_same_squad_name_are_told_apart(self):
        self.event(str(DPS_SQUAD), str(FAX_SQUAD))

        self.assertIn("<code>DPS / 111 → FAX / 111</code>", self.events_html())

    def test_an_unknown_squad_keeps_its_number_and_no_squad_stays(self):
        self.event(str(FAX_SQUAD), "999")
        self.event(str(SOLO_SQUAD), "-1")

        html = self.events_html()

        self.assertIn("<code>FAX / 111 → Squad 999</code>", html)
        self.assertIn("<code>DPS / 222 → No squad</code>", html)

    def test_wing_changes_are_unchanged(self):
        item = FleetMemberEvent(event_type=Event.WING_CHANGE, old_value=str(DPS), new_value=str(FAX))

        self.assertEqual(fleet_event_change(item, self.operation), ("DPS", "FAX"))

    @override_settings(LANGUAGE_CODE="zh-hans")
    def test_the_fixed_words_follow_the_language_and_the_names_do_not(self):
        # "Squad 999" and "No squad" are catalog entries of earlier releases; the wing and squad names are data.
        self.event(str(DPS_SQUAD), "999")
        self.event(str(FAX_SQUAD), "-1")

        html = self.events_html(HTTP_ACCEPT_LANGUAGE="zh-hans")

        with translation.override("zh-hans"):
            self.assertIn("<code>DPS / 111 → 中队 999</code>", html)
            self.assertIn("<code>FAX / 111 → 无中队</code>", html)
