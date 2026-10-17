# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tracking reads the fleet's wings and squads too (F1, #33a), and the member details the tree shows."""

from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import FleetMemberEvent, FleetMemberState, FleetOperation
from fleetops.services import fleet_refresh, tracking
from fleetops.services.tracking import sync_operation

from . import factories as f
from .fleet_sim import FleetSim

Event = FleetMemberEvent.EventType
EndReason = FleetOperation.EndReason
JITA_STATION = 60003760


class WingsTrackingTestCase(TestCase):
    response_style = "dict"

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Wing Boss")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.sim = FleetSim(response_style=self.response_style)
        self.sim.install(self)
        self.fleet = self.sim.create_fleet(
            self.fc_char.character_id, wings=(("Main", ("Alpha", "Bravo")), ("Logi", ("Logi",)))
        )
        self.pilot_char = f.main_of(f.create_user(main_name="Line Pilot"))
        self.fleet.add_member(self.pilot_char.character_id)
        self.operation = f.create_operation(self.fc, esi_fleet_id=self.fleet.fleet_id)
        self.names = self.patch("fleetops.services.tracking.queue_name_resolution")

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def sync(self, **kwargs):
        self.sim.expire_cache()
        result = sync_operation(self.operation, **kwargs)
        self.operation.refresh_from_db()
        return result

    def state(self, character=None):
        character_id = (character or self.pilot_char).character_id
        return FleetMemberState.objects.get(operation=self.operation, character_id=character_id)

    def page(self):
        self.client.force_login(self.fc)
        response = self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def wing_events(self):
        return list(
            FleetMemberEvent.objects.filter(
                operation=self.operation,
                character_id=self.pilot_char.character_id,
                event_type__in=[Event.WING_CHANGE, Event.SQUAD_CHANGE],
            )
            .order_by("pk")
            .values_list("event_type", "old_value", "new_value")
        )


class WingsSnapshotTests(WingsTrackingTestCase):
    def test_snapshot_is_stored_with_its_read_time(self):
        before = timezone.now()

        self.assertEqual(self.sync(), 2)

        self.assertEqual(self.operation.fleet_wings, self.fleet.structure())
        self.assertEqual([wing["name"] for wing in self.operation.fleet_wings], ["Main", "Logi"])
        self.assertGreaterEqual(self.operation.fleet_wings_read_at, before)
        self.assertEqual(self.operation.fleet_wings_read_at, self.operation.last_esi_update)
        self.assertEqual(len(self.sim.requests_of("GetFleetsFleetIdWings")), 1)

    def test_a_fresh_sync_reads_the_wings_past_the_cache(self):
        self.sync()
        self.fleet.rename_wing(self.fleet.wing_id("Main"), "Primary")

        sync_operation(self.operation)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.fleet_wings[0]["name"], "Main")

        sync_operation(self.operation, fresh=True)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.fleet_wings[0]["name"], "Primary")

    def test_failed_wings_read_keeps_the_members_sync_and_the_old_snapshot(self):
        self.sync()
        snapshot, read_at = self.operation.fleet_wings, self.operation.fleet_wings_read_at
        self.fleet.update_member(self.pilot_char.character_id, ship_type_id=670)
        self.sim.fail("GetFleetsFleetIdWings", status=502)

        with self.assertLogs("extensions", "INFO") as logs:
            self.assertEqual(self.sync(), 2)

        self.assertEqual(self.state().ship_type_id, 670)
        self.assertEqual((self.operation.fleet_wings, self.operation.fleet_wings_read_at), (snapshot, read_at))
        self.assertGreater(self.operation.last_esi_update, read_at)
        self.assertEqual(self.operation.last_error, "")
        self.assertTrue(any("wings of operation" in line and "ESI_ERROR" in line for line in logs.output))
        self.assertFalse(any(line.startswith("ERROR:") for line in logs.output))

    def test_unexpected_wings_errors_are_reported_without_failing_the_sync(self):
        with mock.patch.object(tracking, "get_fleet_wings", side_effect=RuntimeError("token=secret")), self.assertLogs(
            "extensions", "ERROR"
        ) as logs:
            self.assertEqual(self.sync(), 2)

        self.assertEqual(self.operation.fleet_wings, [])
        self.assertIsNone(self.operation.fleet_wings_read_at)
        self.assertIn("Reading the fleet wings failed", logs.output[0])
        self.assertNotIn("token=secret", "\n".join(logs.output))

    def test_a_late_read_writes_no_snapshot(self):
        later = timezone.now() + timedelta(minutes=1)
        FleetOperation.objects.filter(pk=self.operation.pk).update(last_esi_update=later)

        self.sync()

        self.assertEqual(self.operation.fleet_wings, [])

    def test_member_details_are_stored_without_events(self):
        self.sync()
        self.assertEqual((self.state().station_id, self.state().takes_fleet_warp), (None, True))

        self.fleet.update_member(self.pilot_char.character_id, station_id=JITA_STATION, takes_fleet_warp=False)
        self.sync()

        self.assertEqual((self.state().station_id, self.state().takes_fleet_warp), (JITA_STATION, False))
        types = set(FleetMemberEvent.objects.filter(operation=self.operation).values_list("event_type", flat=True))
        self.assertEqual(types, {Event.JOIN})

    def test_a_successful_read_clears_the_fleet_read_problems(self):
        cache.set(f"fleetops:fleet:{self.operation.pk}:read_error", "Could not read fleet members: ESI returned HTTP 502.")
        cache.set(f"fleetops:fleet:{self.operation.pk}:boss", [EndReason.FLEET_GONE, "", None])

        self.sync()

        self.assertEqual(fleet_refresh.read_problem(self.operation.pk), {"boss": None, "read_error": ""})

    def test_refresh_reads_the_wings_too(self):
        self.fleet.add_wing("Tackle")

        self.assertEqual(fleet_refresh.refresh_now(self.operation, min_interval=0), (True, fleet_refresh.REFRESHED))

        self.operation.refresh_from_db()
        self.assertEqual([wing["name"] for wing in self.operation.fleet_wings], ["Main", "Logi", "Tackle"])


class ModelResponseTests(WingsTrackingTestCase):
    response_style = "model"

    def test_response_models_are_read(self):
        self.fleet.update_member(self.pilot_char.character_id, station_id=JITA_STATION)

        self.sync()

        self.assertEqual(self.operation.fleet_wings, self.fleet.structure())
        self.assertEqual(self.state().station_id, JITA_STATION)


class ObjectResponseTests(ModelResponseTests):
    response_style = "object"


class PositionEventTests(WingsTrackingTestCase):
    """Position events keep ESI's raw ids; the events tab names them from the latest snapshot."""

    def move(self, role, wing=None, squad=None):
        wing_id = self.fleet.wing_id(wing) if wing else None
        squad_id = self.fleet.squad_id(squad, wing) if squad else None
        self.fleet.move_member(self.pilot_char.character_id, role, wing_id, squad_id)

    def test_raw_ids_are_stored_and_named_on_the_page(self):
        self.sync()
        alpha, logi_squad = self.fleet.squad_id("Alpha"), self.fleet.squad_id("Logi", "Logi")
        main, logi = self.fleet.wing_id("Main"), self.fleet.wing_id("Logi")

        self.move("squad_member", "Logi", "Logi")
        self.sync()
        self.move("wing_commander", "Logi")
        self.sync()
        # The boss steps down from the fleet commander position, the pilot takes it.
        self.fleet.move_member(self.fc_char.character_id, "squad_member", main, alpha)
        self.move("fleet_commander")
        self.sync()

        self.assertEqual(
            self.wing_events(),
            [
                (Event.WING_CHANGE, str(main), str(logi)),
                (Event.SQUAD_CHANGE, str(alpha), str(logi_squad)),
                (Event.SQUAD_CHANGE, str(logi_squad), "-1"),
                (Event.WING_CHANGE, str(logi), "-1"),
            ],
        )

        page = self.page()
        for text in ("<code>Main → Logi</code>", "<code>Main / Alpha → Logi / Logi</code>", "<code>Logi / Logi → No squad</code>",
                     "<code>Logi → Fleet Command</code>", "<code>Squad Member → Wing Commander</code>",
                     "<code>Wing Commander → Fleet Commander</code>"):
            self.assertIn(text, page)

    def test_renamed_and_deleted_units(self):
        self.sync()
        self.move("squad_member", "Logi", "Logi")
        self.sync()
        logi = self.fleet.wing_id("Logi")
        self.fleet.rename_wing(self.fleet.wing_id("Main"), "Primary")
        # In game: back to Alpha, then the Logi wing goes.
        self.move("squad_member", "Primary", "Alpha")
        self.fleet.delete_wing(logi)
        self.sync(fresh=True)

        page = self.page()

        self.assertIn("<code>Primary → Wing ", page)
        self.assertIn(f"<code>Wing {logi} → Primary</code>", page)
