# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The fleet presence check: the victim in the fleet at the loss time, by tracking events or PAP evidence."""

from datetime import datetime, timedelta, timezone as dt_timezone

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from fleetops.models import FleetMemberEvent, FleetOpsSettings
from fleetops.srp import display, presence

from . import factories as f

Event = FleetMemberEvent.EventType

BASE = datetime(2026, 9, 14, 18, 0, 0, tzinfo=dt_timezone.utc)


def at(minutes=0, seconds=0):
    return BASE + timedelta(minutes=minutes, seconds=seconds)


def iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def tracked(operation, character, event_type, when, *, new_value="", old_value=""):
    """A tracking event written at ``when`` (the column is set by the database, so it is moved afterwards)."""
    event = FleetMemberEvent.objects.create(
        operation=operation,
        character_id=character.character_id,
        character_name=character.character_name,
        event_type=event_type,
        old_value=old_value,
        new_value=new_value,
    )
    FleetMemberEvent.objects.filter(pk=event.pk).update(created_at=when)
    return event


class PresenceBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fc = f.create_user("fc", perms=f.FC_PERMS)
        cls.pilot = f.create_user("pilot")
        cls.character = f.main_of(cls.pilot)
        FleetOpsSettings.get_solo()

    def fleet(self, *, started=None, ended=None, **extra):
        started = started or at(-30)
        status = "closed" if ended else "active"
        return f.create_operation(self.fc, started_at=started, ended_at=ended, status=status, **extra)

    def check(self, operation, when, **kwargs):
        return presence.check_presence(operation, self.character.character_id, when, **kwargs)


class InFleetTests(PresenceBase):
    def test_loss_inside_a_stretch_is_in_fleet(self):
        operation = self.fleet(ended=at(60))
        tracked(operation, self.character, Event.JOIN, at(-20))
        tracked(operation, self.character, Event.LEAVE, at(40))
        result = self.check(operation, at(0))
        self.assertEqual(result.code, display.IN_FLEET)
        self.assertEqual(result.level, display.LEVEL_OK)
        self.assertEqual(result.params, {})
        self.assertEqual(result.text, "In the fleet at the time of the loss.")

    def test_stretch_ends_are_inclusive(self):
        operation = self.fleet(ended=at(60))
        tracked(operation, self.character, Event.JOIN, at(-20))
        tracked(operation, self.character, Event.LEAVE, at(40))
        self.assertEqual(self.check(operation, at(-20)).code, display.IN_FLEET)
        self.assertEqual(self.check(operation, at(40)).code, display.IN_FLEET)

    def test_open_stretch_of_a_running_fleet_lasts_until_now(self):
        operation = self.fleet()
        tracked(operation, self.character, Event.JOIN, at(-20))
        self.assertEqual(self.check(operation, timezone.now() - timedelta(seconds=5)).code, display.IN_FLEET)

    def test_open_stretch_of_a_closed_fleet_lasts_until_it_ended(self):
        operation = self.fleet(ended=at(60))
        tracked(operation, self.character, Event.JOIN, at(-20))
        self.assertEqual(self.check(operation, at(59)).code, display.IN_FLEET)
        self.assertEqual(self.check(operation, at(60, 119)).code, display.NEAR)
        self.assertEqual(self.check(operation, at(70)).code, display.NOT_IN_FLEET)

    def test_stored_join_time_starts_the_stretch_before_the_event(self):
        operation = self.fleet()
        # The poll saw the pilot at 18:05; ESI says the pilot joined at 17:50.
        tracked(operation, self.character, Event.JOIN, at(5), new_value=iso(at(-10)))
        tracked(operation, self.character, Event.LEAVE, at(30))
        self.assertEqual(self.check(operation, at(0)).code, display.IN_FLEET)
        self.assertEqual(self.check(operation, at(-10)).code, display.IN_FLEET)

    def test_join_time_after_the_event_is_not_believed(self):
        operation = self.fleet()
        tracked(operation, self.character, Event.JOIN, at(5), new_value=iso(at(25)))
        tracked(operation, self.character, Event.LEAVE, at(30))
        self.assertEqual(self.check(operation, at(10)).code, display.IN_FLEET)

    def test_unreadable_join_time_falls_back_to_the_event(self):
        operation = self.fleet()
        tracked(operation, self.character, Event.JOIN, at(5), new_value="yesterday")
        tracked(operation, self.character, Event.LEAVE, at(30))
        self.assertEqual(self.check(operation, at(10)).code, display.IN_FLEET)
        self.assertEqual(self.check(operation, at(0)).code, display.NOT_IN_FLEET)

    def test_rejoin_starts_a_second_stretch(self):
        operation = self.fleet(ended=at(90))
        tracked(operation, self.character, Event.JOIN, at(-20))
        tracked(operation, self.character, Event.LEAVE, at(10))
        tracked(operation, self.character, Event.REJOIN, at(30), new_value=iso(at(29)))
        tracked(operation, self.character, Event.LEAVE, at(50))
        self.assertEqual(self.check(operation, at(5)).code, display.IN_FLEET)
        self.assertEqual(self.check(operation, at(40)).code, display.IN_FLEET)
        self.assertEqual(self.check(operation, at(20)).code, display.NOT_IN_FLEET)

    def test_events_of_other_pilots_and_fleets_are_not_used(self):
        operation = self.fleet()
        other_fleet = self.fleet()
        other_pilot = f.main_of(f.create_user())
        tracked(operation, other_pilot, Event.JOIN, at(-20))
        tracked(other_fleet, self.character, Event.JOIN, at(-20))
        self.assertEqual(self.check(operation, at(0)).code, display.NEVER_SEEN)

    def test_join_without_second_join_is_one_stretch(self):
        operation = self.fleet()
        tracked(operation, self.character, Event.JOIN, at(-20))
        tracked(operation, self.character, Event.JOIN, at(-10))
        tracked(operation, self.character, Event.LEAVE, at(10))
        self.assertEqual(self.check(operation, at(-15)).code, display.IN_FLEET)


class GraceTests(PresenceBase):
    """The poll interval and a minute: 120 seconds at the default interval of 60."""

    def stretch(self):
        operation = self.fleet(ended=at(120))
        tracked(operation, self.character, Event.JOIN, at(0))
        tracked(operation, self.character, Event.LEAVE, at(30))
        return operation

    def test_just_before_the_join_is_near(self):
        result = self.check(self.stretch(), at(0, -90))
        self.assertEqual(result.code, display.NEAR)
        self.assertEqual(result.level, display.LEVEL_WARNING)
        self.assertEqual(result.params, {"seconds": 90, "direction": "join", "interval": 60})
        self.assertIn("within 90 s of joining", result.text)
        self.assertIn("polls every 60 s", result.text)

    def test_just_after_the_leave_is_near(self):
        result = self.check(self.stretch(), at(30, 45))
        self.assertEqual(result.code, display.NEAR)
        self.assertEqual(result.params["direction"], "leave")
        self.assertEqual(result.params["seconds"], 45)
        self.assertIn("of leaving", result.text)

    def test_grace_boundary_is_inclusive(self):
        operation = self.stretch()
        self.assertEqual(self.check(operation, at(0, -120)).code, display.NEAR)
        self.assertEqual(self.check(operation, at(0, -121)).code, display.NOT_IN_FLEET)
        self.assertEqual(self.check(operation, at(30, 120)).code, display.NEAR)
        self.assertEqual(self.check(operation, at(30, 121)).code, display.NOT_IN_FLEET)

    def test_far_from_the_stretch_is_not_in_fleet(self):
        result = self.check(self.stretch(), at(60))
        self.assertEqual(result.code, display.NOT_IN_FLEET)
        self.assertEqual(result.level, display.LEVEL_DANGER)
        self.assertEqual(result.text, "Not in the fleet at the time of the loss.")

    def test_grace_follows_the_tracking_interval(self):
        FleetOpsSettings.objects.update(tracking_interval=300)
        operation = self.stretch()
        self.assertEqual(presence.grace_seconds(), 360)
        self.assertEqual(self.check(operation, at(0, -360)).code, display.NEAR)
        self.assertEqual(self.check(operation, at(0, -361)).code, display.NOT_IN_FLEET)
        self.assertEqual(self.check(operation, at(0, -200)).params["interval"], 300)

    def test_fast_polling_still_allows_a_minute_and_a_minute(self):
        FleetOpsSettings.objects.update(tracking_interval=30)
        self.assertEqual(presence.grace_seconds(), 120)
        self.assertEqual(self.check(self.stretch(), at(0, -120)).code, display.NEAR)

    def test_nearest_boundary_is_reported(self):
        operation = self.fleet(ended=at(120))
        tracked(operation, self.character, Event.JOIN, at(0))
        tracked(operation, self.character, Event.LEAVE, at(10))
        tracked(operation, self.character, Event.REJOIN, at(12))
        tracked(operation, self.character, Event.LEAVE, at(30))
        result = self.check(operation, at(11, 20))
        self.assertEqual(result.code, display.NEAR)
        self.assertEqual((result.params["direction"], result.params["seconds"]), ("join", 40))


class NeverSeenAndFallbackTests(PresenceBase):
    def test_never_tracked_in_a_tracked_fleet(self):
        result = self.check(self.fleet(), at(0))
        self.assertEqual(result.code, display.NEVER_SEEN)
        self.assertEqual(result.level, display.LEVEL_DANGER)
        self.assertEqual(result.text, "This character was never seen in the fleet.")

    def test_pruned_events_fall_back_to_the_member_state(self):
        operation = self.fleet(ended=at(90))
        f.member_state(operation, self.character, active=False, first_seen=at(-20), left_at=at(40))
        self.assertEqual(self.check(operation, at(10)).code, display.IN_FLEET)
        self.assertEqual(self.check(operation, at(41)).code, display.NEAR)
        self.assertEqual(self.check(operation, at(60)).code, display.NOT_IN_FLEET)
        self.assertEqual(self.check(operation, at(-30)).code, display.NOT_IN_FLEET)

    def test_active_state_of_a_closed_fleet_lasts_until_the_fleet_ended(self):
        operation = self.fleet(ended=at(60))
        f.member_state(operation, self.character, active=True, first_seen=at(-20))
        self.assertEqual(self.check(operation, at(59)).code, display.IN_FLEET)
        self.assertEqual(self.check(operation, at(80)).code, display.NOT_IN_FLEET)

    def test_active_state_of_a_running_fleet_lasts_until_now(self):
        operation = self.fleet()
        f.member_state(operation, self.character, active=True, first_seen=at(-20))
        self.assertEqual(self.check(operation, timezone.now() - timedelta(seconds=3)).code, display.IN_FLEET)

    def test_leave_event_without_join_uses_the_state_stretch(self):
        operation = self.fleet(ended=at(90))
        f.member_state(operation, self.character, active=False, first_seen=at(-20), left_at=at(40))
        tracked(operation, self.character, Event.LEAVE, at(40))
        self.assertEqual(self.check(operation, at(10)).code, display.IN_FLEET)

    def test_events_win_over_the_state(self):
        operation = self.fleet(ended=at(90))
        f.member_state(operation, self.character, active=False, first_seen=at(-20), left_at=at(80))
        tracked(operation, self.character, Event.JOIN, at(-20))
        tracked(operation, self.character, Event.LEAVE, at(10))
        self.assertEqual(self.check(operation, at(50)).code, display.NOT_IN_FLEET)

    def test_no_fleet_is_not_applicable(self):
        result = presence.check_presence(None, self.character.character_id, at(0))
        self.assertEqual(result.code, display.NOT_APPLICABLE)
        self.assertEqual(result.level, display.LEVEL_OK)
        self.assertEqual(presence.check_presence(self.fleet(), 0, at(0)).code, display.NOT_APPLICABLE)


class UntrackedFleetTests(PresenceBase):
    def manual(self, **extra):
        return self.fleet(ended=at(60), is_manual=True, esi_fleet_id=None, **extra)

    def test_pap_registration_inside_the_window(self):
        operation = self.manual()
        f.registration(operation, self.character)
        result = self.check(operation, at(0))
        self.assertEqual(result.code, display.REGISTERED)
        self.assertEqual(result.level, display.LEVEL_WARNING)
        self.assertEqual(result.text, "No tracking data; registered for PAP in this fleet.")

    def test_window_has_a_grace_at_both_ends(self):
        operation = self.manual()
        f.registration(operation, self.character)
        self.assertEqual(self.check(operation, at(-30, -120)).code, display.REGISTERED)
        self.assertEqual(self.check(operation, at(-30, -121)).code, display.OUTSIDE_WINDOW)
        self.assertEqual(self.check(operation, at(60, 120)).code, display.REGISTERED)
        self.assertEqual(self.check(operation, at(60, 121)).code, display.OUTSIDE_WINDOW)

    def test_outside_window_is_a_danger(self):
        operation = self.manual()
        f.registration(operation, self.character)
        result = self.check(operation, at(300))
        self.assertEqual((result.code, result.level), (display.OUTSIDE_WINDOW, display.LEVEL_DANGER))

    def test_granted_attendance_counts_as_evidence(self):
        operation = self.manual()
        f.add_attendance(operation, self.pilot, self.character)
        self.assertEqual(self.check(operation, at(0)).code, display.REGISTERED)

    def test_attendance_that_was_not_granted_is_no_evidence(self):
        operation = self.manual()
        f.add_attendance(operation, self.pilot, self.character, granted=False)
        self.assertEqual(self.check(operation, at(0)).code, display.NEVER_SEEN)

    def test_link_only_fleets_use_registrations_too(self):
        operation = self.fleet(ended=at(60), link_only=True, esi_fleet_id=None)
        f.registration(operation, self.character)
        self.assertEqual(self.check(operation, at(0)).code, display.REGISTERED)

    def test_nobody_registered_is_never_seen(self):
        self.assertEqual(self.check(self.manual(), at(0)).code, display.NEVER_SEEN)

    def test_registration_of_another_pilot_does_not_count(self):
        operation = self.manual()
        f.registration(operation, f.main_of(f.create_user()))
        self.assertEqual(self.check(operation, at(0)).code, display.NEVER_SEEN)

    def test_tracking_events_are_ignored_for_untracked_fleets(self):
        operation = self.manual()
        tracked(operation, self.character, Event.JOIN, at(-20))
        self.assertEqual(self.check(operation, at(0)).code, display.NEVER_SEEN)

    def test_running_untracked_fleet_window_lasts_until_now(self):
        operation = self.fleet(is_manual=True, esi_fleet_id=None)
        f.registration(operation, self.character)
        self.assertEqual(self.check(operation, timezone.now() - timedelta(minutes=1)).code, display.REGISTERED)

    def test_registration_stands_in_when_a_tracked_fleet_has_no_tracking_data(self):
        operation = self.fleet(ended=at(60))
        f.registration(operation, self.character)
        self.assertEqual(self.check(operation, at(0)).code, display.REGISTERED)

    def test_tracking_data_wins_over_a_registration(self):
        operation = self.fleet(ended=at(60))
        f.registration(operation, self.character)
        tracked(operation, self.character, Event.JOIN, at(-20))
        tracked(operation, self.character, Event.LEAVE, at(-10))
        self.assertEqual(self.check(operation, at(30)).code, display.NOT_IN_FLEET)


class ShipHintTests(PresenceBase):
    MOA, CARACAL = 623, 621

    def fleet_with_ship(self, *changes, state_ship=None):
        """The pilot joined at -20 min; ``changes`` are ``(minute, old, new)`` ship change events."""
        operation = self.fleet(ended=at(90))
        tracked(operation, self.character, Event.JOIN, at(-20))
        for minute, old, new in changes:
            tracked(operation, self.character, Event.SHIP_CHANGE, at(minute), old_value=old, new_value=new)
        if state_ship:
            f.member_state(
                operation, self.character, ship_type_id=state_ship[0], ship_type_name=state_ship[1], first_seen=at(-20)
            )
        return operation

    def test_same_ship_gives_no_hint(self):
        operation = self.fleet_with_ship((-5, "Rifter", "Moa"))
        result = self.check(operation, at(0), ship_type_id=self.MOA, ship_name="Moa")
        self.assertEqual(result.code, display.IN_FLEET)
        self.assertIsNone(result.tracked_ship_type_id)
        self.assertEqual(result.tracked_ship_name, "")

    def test_other_ship_is_reported_by_name(self):
        operation = self.fleet_with_ship((-5, "Rifter", "Caracal"))
        result = self.check(operation, at(0), ship_type_id=self.MOA, ship_name="Moa")
        self.assertEqual(result.code, display.IN_FLEET)
        self.assertEqual(result.tracked_ship_name, "Caracal")

    def test_the_last_change_before_the_loss_decides(self):
        operation = self.fleet_with_ship((-10, "Rifter", "Caracal"), (-5, "Caracal", "Moa"), (5, "Moa", "Capsule"))
        self.assertEqual(self.check(operation, at(0), ship_type_id=self.MOA, ship_name="Moa").tracked_ship_name, "")
        result = self.check(operation, at(-7), ship_type_id=self.MOA, ship_name="Moa")
        self.assertEqual(result.tracked_ship_name, "Caracal")

    def test_ship_before_the_first_change_is_its_old_value(self):
        operation = self.fleet_with_ship((5, "Caracal", "Moa"))
        result = self.check(operation, at(0), ship_type_id=self.MOA, ship_name="Moa")
        self.assertEqual(result.tracked_ship_name, "Caracal")

    def test_name_comparison_ignores_case(self):
        operation = self.fleet_with_ship((-5, "Rifter", "moa"))
        result = self.check(operation, at(0), ship_type_id=self.MOA, ship_name="Moa")
        self.assertEqual(result.tracked_ship_name, "")

    def test_event_with_a_type_id_compares_by_id(self):
        operation = self.fleet_with_ship((-5, "587", str(self.CARACAL)))
        result = self.check(operation, at(0), ship_type_id=self.MOA)
        self.assertEqual(result.tracked_ship_type_id, self.CARACAL)
        same = self.check(operation, at(0), ship_type_id=self.CARACAL)
        self.assertIsNone(same.tracked_ship_type_id)

    def test_state_ship_is_used_without_ship_changes(self):
        operation = self.fleet_with_ship(state_ship=(self.CARACAL, "Caracal"))
        result = self.check(operation, at(0), ship_type_id=self.MOA, ship_name="Moa")
        self.assertEqual((result.tracked_ship_type_id, result.tracked_ship_name), (self.CARACAL, "Caracal"))
        self.assertIsNone(self.check(operation, at(0), ship_type_id=self.CARACAL).tracked_ship_type_id)

    def test_name_only_hint_needs_the_name_of_the_lost_ship(self):
        operation = self.fleet_with_ship((-5, "Rifter", "Caracal"))
        result = self.check(operation, at(0), ship_type_id=self.MOA)
        self.assertEqual(result.tracked_ship_name, "")

    def test_without_the_lost_ship_the_known_ship_is_returned(self):
        operation = self.fleet_with_ship((-5, "Rifter", "Caracal"))
        self.assertEqual(self.check(operation, at(0)).tracked_ship_name, "Caracal")

    def test_no_hint_when_the_pilot_was_not_in_the_fleet(self):
        operation = self.fleet_with_ship((-5, "Rifter", "Caracal"))
        tracked(operation, self.character, Event.LEAVE, at(30))
        result = self.check(operation, at(80), ship_type_id=self.MOA, ship_name="Moa")
        self.assertEqual(result.code, display.NOT_IN_FLEET)
        self.assertEqual(result.tracked_ship_name, "")

    def test_hint_also_for_a_near_result(self):
        operation = self.fleet(ended=at(90))
        tracked(operation, self.character, Event.JOIN, at(0))
        tracked(operation, self.character, Event.SHIP_CHANGE, at(1), old_value="Caracal", new_value="Moa")
        result = self.check(operation, at(0, -30), ship_type_id=self.MOA, ship_name="Moa")
        self.assertEqual(result.code, display.NEAR)
        self.assertEqual(result.tracked_ship_name, "Caracal")


class FindFleetTests(PresenceBase):
    def link_for(self, **fleet):
        operation = self.fleet(**fleet)
        return operation, f.fleet_link(operation)

    def test_finds_the_open_link_of_the_fleet_the_pilot_was_in(self):
        operation, link = self.link_for(ended=at(60))
        tracked(operation, self.character, Event.JOIN, at(-20))
        f.member_state(operation, self.character, active=False, first_seen=at(-20), left_at=at(40))
        self.assertEqual(presence.find_fleet_for_loss(self.character.character_id, at(0)), link)

    def test_closed_links_are_not_offered(self):
        operation, link = self.link_for(ended=at(60))
        link.is_open = False
        link.save()
        tracked(operation, self.character, Event.JOIN, at(-20))
        f.member_state(operation, self.character, active=False, first_seen=at(-20), left_at=at(40))
        self.assertIsNone(presence.find_fleet_for_loss(self.character.character_id, at(0)))

    def test_fleet_without_a_link_is_not_offered(self):
        operation = self.fleet(ended=at(60))
        tracked(operation, self.character, Event.JOIN, at(-20))
        f.member_state(operation, self.character, active=False, first_seen=at(-20), left_at=at(40))
        self.assertIsNone(presence.find_fleet_for_loss(self.character.character_id, at(0)))

    def test_pilot_who_left_before_the_loss_is_not_matched(self):
        operation, _link = self.link_for(ended=at(60))
        tracked(operation, self.character, Event.JOIN, at(-20))
        tracked(operation, self.character, Event.LEAVE, at(-10))
        f.member_state(operation, self.character, active=False, first_seen=at(-20), left_at=at(-10))
        self.assertIsNone(presence.find_fleet_for_loss(self.character.character_id, at(0)))

    def test_loss_just_after_the_fleet_ended_is_near(self):
        operation, link = self.link_for(ended=at(60))
        tracked(operation, self.character, Event.JOIN, at(-20))
        f.member_state(operation, self.character, active=True, first_seen=at(-20))
        self.assertEqual(presence.find_fleet_for_loss(self.character.character_id, at(60, 60)), link)

    def test_fleets_that_ended_long_before_are_not_candidates(self):
        operation, _link = self.link_for(ended=at(-60), started=at(-120))
        f.member_state(operation, self.character, active=False, first_seen=at(-120), left_at=at(-60))
        self.assertIsNone(presence.find_fleet_for_loss(self.character.character_id, at(0)))

    def test_running_fleet_is_a_candidate(self):
        operation, link = self.link_for()
        f.member_state(operation, self.character, active=True, first_seen=at(-20))
        tracked(operation, self.character, Event.JOIN, at(-20))
        self.assertEqual(presence.find_fleet_for_loss(self.character.character_id, timezone.now() - timedelta(minutes=1)), link)

    def test_other_pilots_do_not_match(self):
        operation, _link = self.link_for(ended=at(60))
        other = f.main_of(f.create_user())
        f.member_state(operation, other, active=False, first_seen=at(-20), left_at=at(40))
        self.assertIsNone(presence.find_fleet_for_loss(self.character.character_id, at(0)))

    def test_in_fleet_beats_near(self):
        near_op, near_link = self.link_for(started=at(-10), ended=at(10))
        tracked(near_op, self.character, Event.JOIN, at(-10))
        tracked(near_op, self.character, Event.LEAVE, at(-1))
        f.member_state(near_op, self.character, active=False, first_seen=at(-10), left_at=at(-1))
        in_op, in_link = self.link_for(started=at(-60), ended=at(30))
        tracked(in_op, self.character, Event.JOIN, at(-60))
        f.member_state(in_op, self.character, active=False, first_seen=at(-60), left_at=at(30))
        # The near fleet started later and is looked at first.
        self.assertEqual(presence.find_fleet_for_loss(self.character.character_id, at(0)), in_link)

    def test_near_is_offered_when_nothing_better_exists(self):
        operation, link = self.link_for(started=at(-10), ended=at(10))
        tracked(operation, self.character, Event.JOIN, at(-10))
        tracked(operation, self.character, Event.LEAVE, at(-1))
        f.member_state(operation, self.character, active=False, first_seen=at(-10), left_at=at(-1))
        self.assertEqual(presence.find_fleet_for_loss(self.character.character_id, at(0)), link)

    def test_only_three_fleets_are_checked_and_one_query_finds_them(self):
        for offset in range(4):
            operation, _link = self.link_for(started=at(-200 + offset), ended=at(60))
            f.member_state(operation, self.character, active=False, first_seen=at(-200), left_at=at(-100))
        with CaptureQueriesContext(connection) as queries:
            self.assertIsNone(presence.find_fleet_for_loss(self.character.character_id, at(0)))
        sql = [query["sql"] for query in queries.captured_queries]
        self.assertEqual(sum("fleetops_fleetmemberstate" in text and "JOIN" in text for text in sql), 1)
        self.assertEqual(sum("fleetops_fleetmemberevent" in text for text in sql), 3)
