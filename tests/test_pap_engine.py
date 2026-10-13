"""The PAP engine: time in fleet from the tracking polls, and the PAP each character gets when a fleet closes."""

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from allianceauth.authentication.models import CharacterOwnership
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import AttendanceRecord, AuditLog, FleetMemberEvent, FleetMemberState, FleetOperation
from fleetops.providers import esi as esi_provider
from fleetops.services.attendance import create_manual_attendance, set_operation_attendance_multiplier
from fleetops.services.operations import (
    create_manual_fleet,
    end_fleet,
    finish_requested_end,
    record_start,
    request_fleet_end,
)
from fleetops.services.pap import compute_pap, finalize_pap, refresh_after_change
from fleetops.services.statistics import corporation_statistics, member_statistics
from fleetops.services.tracking import sync_operation

from . import factories as f

UTC = dt_timezone.utc
Status = FleetOperation.Status
AUTOMATIC = AttendanceRecord.Source.AUTOMATIC
CLICK = AttendanceRecord.Source.CLICK


def at(minute, second=0):
    """A fixed EVE time: 2026-10-12 19:00 plus ``minute`` minutes."""
    return datetime(2026, 10, 12, 19, 0, tzinfo=UTC) + timedelta(minutes=minute, seconds=second)


def row(operation, character):
    return AttendanceRecord.objects.get(
        operation=operation, character_id=getattr(character, "character_id", character),
        source__in=AttendanceRecord.MANAGED_SOURCES,
    )


def granted(operation):
    return set(
        AttendanceRecord.objects.filter(operation=operation, granted=True).values_list("character_id", flat=True)
    )


class TimeInFleetTests(TestCase):
    """Tracking adds the time between polls to each member; nothing is granted while the fleet runs."""

    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.pilot = f.create_user(main_name="Line Pilot")
        self.main = f.main_of(self.pilot)
        self.operation = f.create_operation(self.fc, started_at=at(0))

    def poll(self, moment, *rows):
        with mock.patch("fleetops.services.tracking.get_fleet_members", return_value=list(rows)), mock.patch(
            "django.utils.timezone.now", return_value=moment
        ):
            return sync_operation(self.operation)

    def seconds(self, character=None):
        character = character or self.main
        return FleetMemberState.objects.get(operation=self.operation, character_id=character.character_id).accumulated_seconds

    def test_first_poll_counts_from_the_fleet_start_at_the_earliest(self):
        self.poll(at(10), f.esi_member(self.main, join_time=at(-30)))

        self.assertEqual(self.seconds(), 600)

    def test_first_poll_counts_from_the_join_time_after_the_start(self):
        self.poll(at(10), f.esi_member(self.main, join_time=at(4)))

        self.assertEqual(self.seconds(), 360)

    def test_every_poll_adds_the_time_since_the_previous_one(self):
        member = f.esi_member(self.main, join_time=at(0))
        for minute in (1, 2, 5):
            self.poll(at(minute), member)

        self.assertEqual(self.seconds(), 300)

    def test_leaving_between_polls_and_coming_back_counts_from_the_new_join_time(self):
        self.poll(at(1), f.esi_member(self.main, join_time=at(0)))

        # Left after the first poll and joined again 40 seconds before the second.
        self.poll(at(2), f.esi_member(self.main, join_time=at(1, 20)))

        self.assertEqual(self.seconds(), 60 + 40)

    def test_a_member_who_left_is_counted_up_to_the_last_poll_that_saw_it(self):
        self.poll(at(1), f.esi_member(self.main, join_time=at(0)))
        self.poll(at(6))
        self.poll(at(9))

        state = FleetMemberState.objects.get(operation=self.operation, character_id=self.main.character_id)
        self.assertEqual((state.accumulated_seconds, state.is_active), (60, False))

    def test_returning_member_counts_from_its_join_time_but_not_before_the_previous_poll(self):
        self.poll(at(1), f.esi_member(self.main, join_time=at(0)))
        self.poll(at(2))

        # ESI still reports the first join time; the member was not there at the previous poll.
        self.poll(at(5), f.esi_member(self.main, join_time=at(0)))

        self.assertEqual(self.seconds(), 60 + 180)

    def test_a_long_gap_between_successful_polls_is_counted(self):
        member = f.esi_member(self.main, join_time=at(0))
        self.poll(at(1), member)

        # ESI was down for half an hour; the member was there before and after.
        self.poll(at(31), member)

        self.assertEqual(self.seconds(), 31 * 60)

    def test_member_without_join_time_counts_from_its_first_poll(self):
        self.poll(at(1), f.esi_member(f.main_of(self.fc), join_time=at(0)))
        member = f.esi_member(self.main, join_time=None)

        self.poll(at(3), f.esi_member(f.main_of(self.fc), join_time=at(0)), member)
        self.assertEqual(self.seconds(), 0)
        self.poll(at(4), f.esi_member(f.main_of(self.fc), join_time=at(0)), member)

        self.assertEqual(self.seconds(), 60)

    def test_join_time_in_text_form_is_read(self):
        self.poll(at(10), f.esi_member(self.main, join_time="2026-10-12T19:07:00Z"))

        self.assertEqual(self.seconds(), 180)
        self.assertEqual(
            FleetMemberEvent.objects.get(operation=self.operation).new_value, "2026-10-12T19:07:00Z"
        )

    def test_unreadable_join_time_counts_like_a_missing_one(self):
        self.poll(at(10), f.esi_member(self.main, join_time="soon"))

        self.assertEqual(self.seconds(), 0)
        self.assertEqual(FleetMemberEvent.objects.get(operation=self.operation).new_value, "")

    def test_a_poll_that_finished_later_never_counts_backwards(self):
        member = f.esi_member(self.main, join_time=at(0))
        self.poll(at(5), member)

        # A sync whose clock reading is older than the stored poll adds nothing and keeps the poll time.
        self.poll(at(4), member)

        self.assertEqual(self.seconds(), 300)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.last_esi_update, at(5))

    def test_an_older_read_that_finishes_later_changes_nothing(self):
        joiner = f.main_of(f.create_user(main_name="New Joiner"))
        self.poll(at(1), f.esi_member(self.main, join_time=at(0, 30)))
        # The sync after a kick read ESI at 19:02 and finished first.
        self.poll(at(2), f.esi_member(joiner, join_time=at(1, 30)))

        # A poll that read ESI at 19:01:59, when the kicked pilot was still listed, finishes now.
        count = self.poll(at(1, 59), f.esi_member(self.main, join_time=at(0, 30), solar_system_id=30002187))

        self.assertEqual(count, 1)
        states = FleetMemberState.objects.filter(operation=self.operation)
        self.assertEqual(
            dict(states.values_list("character_id", "is_active")),
            {self.main.character_id: False, joiner.character_id: True},
        )
        events = FleetMemberEvent.objects.filter(operation=self.operation).order_by("pk")
        self.assertEqual(list(events.values_list("event_type", flat=True)), ["join", "leave", "join"])
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.last_esi_update, at(2))

    def test_tracking_writes_no_pap_while_the_fleet_runs(self):
        self.poll(at(30), f.esi_member(self.main, join_time=at(0)))

        self.assertFalse(AttendanceRecord.objects.exists())

    def test_join_and_rejoin_events_store_the_join_time_and_the_page_shows_it(self):
        self.poll(at(1), f.esi_member(self.main, join_time=at(0, 30)))
        self.poll(at(2))
        self.poll(at(3), f.esi_member(self.main, join_time=at(2, 45)))

        values = list(
            FleetMemberEvent.objects.filter(operation=self.operation)
            .order_by("pk")
            .values_list("event_type", "new_value")
        )
        self.assertEqual(
            values,
            [("join", "2026-10-12T19:00:30Z"), ("leave", ""), ("rejoin", "2026-10-12T19:02:45Z")],
        )
        self.client.force_login(self.fc)
        page = self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))
        self.assertContains(page, "joined 19:00 EVE")
        self.assertContains(page, "joined 19:02 EVE")


class LateCloseTests(TestCase):
    """A close that runs later than its end request (a busy worker, the recovery) changes nothing."""

    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Fleet Boss")
        self.pilot = f.create_user(main_name="Line Pilot")
        self.main = f.main_of(self.pilot)

    def fleet(self, **extra):
        return f.create_operation(self.fc, started_at=at(0), **extra)

    def poll(self, operation, moment, *rows):
        with mock.patch("fleetops.services.tracking.get_fleet_members", return_value=list(rows)), mock.patch(
            "django.utils.timezone.now", return_value=moment
        ):
            return sync_operation(operation)

    def end(self, operation, requested, closed, *rows):
        with mock.patch("fleetops.tasks.finish_fleet_end.delay"), mock.patch(
            "django.utils.timezone.now", return_value=requested
        ):
            request_fleet_end(operation, actor=self.fc)
        operation.refresh_from_db()
        with mock.patch("fleetops.services.tracking.get_fleet_members", return_value=list(rows)), mock.patch(
            "django.utils.timezone.now", return_value=closed
        ):
            finish_requested_end(operation)
        operation.refresh_from_db()
        return operation

    def test_the_fleet_ends_at_its_end_request(self):
        operation = self.end(self.fleet(), at(89), at(95))

        self.assertEqual((operation.status, operation.ended_at), (Status.CLOSED, at(89)))
        self.assertEqual((operation.attendance_multiplier, operation.multiplier_pending), (1, False))

    def test_after_a_late_close_the_fc_cannot_choose_what_the_end_request_did_not_allow(self):
        operation = self.end(self.fleet(), at(89), at(95))
        self.client.force_login(self.fc)

        self.client.post(
            reverse("fleetops:set_attendance_multiplier", args=[operation.uuid]), {"attendance_multiplier": "2"}
        )

        operation.refresh_from_db()
        self.assertEqual(operation.attendance_multiplier, 1)
        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        self.assertEqual(page.context["multiplier_allowed"], [1])

    def test_a_late_close_measures_the_minimum_fleet_duration_to_the_end_request(self):
        f.settings(minimum_fleet_minutes=30)

        operation = self.end(self.fleet(), at(28), at(34))

        self.assertFalse(operation.counts_for_stats)

    def test_a_late_close_counts_no_time_after_the_end_request(self):
        operation = self.fleet(pap_minimum_minutes=20)
        member = f.esi_member(self.main, join_time=at(0))
        for minute in range(1, 17):
            self.poll(operation, at(minute), member)
        late = f.main_of(f.create_user(main_name="Late Joiner"))

        operation = self.end(operation, at(16, 30), at(22), member, f.esi_member(late, join_time=at(19)))

        state = FleetMemberState.objects.get(operation=operation, character_id=self.main.character_id)
        self.assertEqual(state.accumulated_seconds, 16 * 60 + 30)
        pilot_row = row(operation, self.main)
        self.assertEqual((pilot_row.granted, pilot_row.below_minimum), (False, True))
        self.assertFalse(FleetMemberState.objects.filter(operation=operation, character_id=late.character_id).exists())


class EngineTestBase(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Fleet Boss")
        self.fc_main = f.main_of(self.fc)
        self.pilot = f.create_user(main_name="Line Pilot")
        self.main = f.main_of(self.pilot)
        self.operation = self.closed()

    def closed(self, *, minimum=0, limit=None, **extra):
        started = timezone.now() - timedelta(hours=4)
        return f.create_operation(
            self.fc,
            status=Status.CLOSED,
            started_at=started,
            ended_at=started + timedelta(minutes=120),
            pap_minimum_minutes=minimum,
            pap_character_limit=limit,
            **extra,
        )

    def track(self, character, minutes, operation=None, **extra):
        return f.member_state(
            operation or self.operation, character, accumulated_seconds=int(minutes * 60), active=False, **extra
        )


class FinalizeTests(EngineTestBase):
    def test_close_grants_pap_from_the_tracked_time(self):
        operation = f.create_operation(self.fc, started_at=timezone.now() - timedelta(minutes=30), pap_minimum_minutes=20)
        f.member_state(operation, self.fc_main, accumulated_seconds=1800)
        f.member_state(operation, self.main, accumulated_seconds=600)

        with mock.patch("fleetops.services.operations.sync_operation"):
            end_fleet(operation, actor=self.fc)

        fc_row, pilot_row = row(operation, self.fc_main), row(operation, self.main)
        self.assertEqual((fc_row.source, fc_row.granted, fc_row.tracked_seconds), (AUTOMATIC, True, 1800))
        self.assertEqual((pilot_row.granted, pilot_row.below_minimum, pilot_row.tracked_seconds), (False, True, 600))
        self.assertEqual(pilot_row.attendance_value, 1)

    def test_minimum_is_reached_at_exactly_its_minutes(self):
        operation = self.closed(minimum=20)
        below = f.create_user()
        self.track(self.main, 20, operation)
        self.track(f.main_of(below), 20 - 1 / 60, operation)

        summary = finalize_pap(operation)

        self.assertTrue(row(operation, self.main).granted)
        below_row = row(operation, f.main_of(below))
        self.assertEqual((below_row.granted, below_row.below_minimum), (False, True))
        self.assertEqual((summary["granted"], summary["below_minimum"]), (1, 1))

    def test_minimum_zero_means_any_presence(self):
        self.track(self.main, 0)

        finalize_pap(self.operation)

        self.assertTrue(row(self.operation, self.main).granted)

    def test_limit_counts_characters_whatever_the_multiplier(self):
        alts = [f.add_alt(self.pilot) for _ in range(3)]
        for multiplier in (1, 3):
            with self.subTest(multiplier=multiplier):
                operation = self.closed(limit=2, attendance_multiplier=multiplier)
                self.track(self.main, 50, operation)
                for minutes, alt in zip((40, 30, 20), alts):
                    self.track(alt, minutes, operation)

                summary = finalize_pap(operation)

                self.assertEqual(granted(operation), {self.main.character_id, alts[0].character_id})
                self.assertEqual((summary["granted"], summary["capped"]), (2, 2))
                self.assertEqual(row(operation, self.main).attendance_value, multiplier)

    def test_limit_is_per_player(self):
        operation = self.closed(limit=1)
        other = f.create_user()
        self.track(self.main, 10, operation)
        self.track(f.add_alt(self.pilot), 10, operation)
        self.track(f.main_of(other), 10, operation)

        finalize_pap(operation)

        self.assertEqual(granted(operation), {self.main.character_id, f.main_of(other).character_id})

    def test_without_a_limit_every_qualifying_character_gets_pap(self):
        alts = [f.add_alt(self.pilot) for _ in range(3)]
        for character in (self.main, *alts):
            self.track(character, 5)

        finalize_pap(self.operation)

        self.assertEqual(len(granted(self.operation)), 4)

    def test_over_the_limit_the_fc_character_comes_first(self):
        fc_alt = f.add_alt(self.fc, "Boss Alt")
        operation = self.closed(limit=1, fc_character=fc_alt)
        self.track(self.fc_main, 90, operation)
        self.track(fc_alt, 10, operation)

        finalize_pap(operation)

        self.assertEqual(granted(operation), {fc_alt.character_id})
        self.assertTrue(row(operation, self.fc_main).capped)

    def test_over_the_limit_the_main_comes_before_alts_with_more_time(self):
        operation = self.closed(limit=1)
        alt = f.add_alt(self.pilot)
        self.track(self.main, 10, operation)
        self.track(alt, 90, operation)

        finalize_pap(operation)

        self.assertEqual(granted(operation), {self.main.character_id})

    def test_over_the_limit_alts_go_by_time_in_fleet_then_by_first_seen(self):
        operation = self.closed(limit=2)
        early, late, short = (f.add_alt(self.pilot, name) for name in ("Early Alt", "Late Alt", "Short Alt"))
        self.track(late, 30, operation, first_seen=operation.started_at + timedelta(minutes=10))
        self.track(early, 30, operation, first_seen=operation.started_at)
        self.track(short, 5, operation)

        finalize_pap(operation)

        self.assertEqual(granted(operation), {early.character_id, late.character_id})
        operation.pap_character_limit = 1
        operation.save()
        finalize_pap(operation)
        self.assertEqual(granted(operation), {early.character_id})

    def test_characters_below_the_minimum_take_no_place_under_the_limit(self):
        operation = self.closed(minimum=20, limit=1)
        alt = f.add_alt(self.pilot)
        self.track(self.main, 5, operation)
        self.track(alt, 25, operation)

        finalize_pap(operation)

        self.assertEqual(granted(operation), {alt.character_id})

    def test_a_replaced_character_keeps_its_place_under_the_limit(self):
        lead = f.create_user(perms=f.FC_LEAD_PERMS)
        operation = self.closed(limit=1)
        alt = f.add_alt(self.pilot)
        self.track(self.main, 30, operation)
        self.track(alt, 30, operation)
        finalize_pap(operation)

        create_manual_attendance(operation, actor=lead, character_id=self.main.character_id, duplicate_action="replace")

        main_row, alt_row = row(operation, self.main), row(operation, alt)
        self.assertEqual((main_row.replaced, main_row.granted), (True, False))
        self.assertEqual((alt_row.capped, alt_row.granted), (True, False))

    def test_a_merge_grants_over_the_limit_and_survives_multiplier_switches(self):
        lead = f.create_user(perms=f.FC_LEAD_PERMS)
        operation = self.closed(limit=1)
        alt = f.add_alt(self.pilot)
        self.track(self.main, 30, operation)
        self.track(alt, 10, operation)
        finalize_pap(operation)

        create_manual_attendance(operation, actor=lead, character_id=alt.character_id, duplicate_action="merge")
        for multiplier, value in ((3, 4), (1, 2), (2, 3)):
            set_operation_attendance_multiplier(operation, multiplier)
            alt_row = row(operation, alt)
            self.assertEqual((alt_row.granted, alt_row.attendance_value), (True, value), f"{multiplier}x")

    def test_characters_without_an_alliance_auth_account_get_no_row(self):
        stranger = f.create_character("Stranger")
        unknown = f.next_id()
        self.track(stranger, 30)
        self.track(unknown, 30, name="Unknown Pilot")
        self.track(self.main, 30)

        summary = finalize_pap(self.operation)
        plan = compute_pap(self.operation)

        self.assertEqual(granted(self.operation), {self.main.character_id})
        self.assertEqual(summary["unregistered"], 2)
        self.assertEqual(
            sorted(plan.unregistered), sorted([(stranger.character_id, "Stranger"), (unknown, "Unknown Pilot")])
        )

    def test_an_alt_added_to_alliance_auth_later_gets_pap_on_recalculation(self):
        late = f.create_character("Late Alt")
        self.track(late, 30)
        finalize_pap(self.operation)
        self.assertFalse(AttendanceRecord.objects.filter(operation=self.operation).exists())

        f.add_ownership(self.pilot, late)
        finalize_pap(self.operation)

        late_row = row(self.operation, late)
        self.assertEqual((late_row.granted, late_row.auth_user, late_row.main_character_id), (True, self.pilot, self.main.character_id))

    def test_a_character_that_lost_its_owner_keeps_its_pap(self):
        # Alliance Auth removes an ownership by itself (revoked tokens, a sold character); history stays.
        alt = f.add_alt(self.pilot)
        self.track(alt, 30)
        finalize_pap(self.operation)

        CharacterOwnership.objects.filter(character=alt).delete()
        summary = finalize_pap(self.operation)

        alt_row = row(self.operation, alt)
        self.assertEqual(
            (alt_row.granted, alt_row.auth_user, alt_row.main_character_id), (True, self.pilot, self.main.character_id)
        )
        self.assertEqual((summary["granted"], summary["unregistered"]), (1, 0))

    def totals(self, operation):
        start = operation.started_at
        return (
            member_statistics(self.pilot, start.year, start.month)["total"],
            corporation_statistics(self.main.corporation_id, start.year, start.month)["total_attendance"],
        )

    def test_ownership_changes_after_the_close_change_no_statistics(self):
        alt = f.add_alt(self.pilot)
        self.track(alt, 30)
        finalize_pap(self.operation)
        before = self.totals(self.operation)

        CharacterOwnership.objects.filter(character=alt).delete()
        set_operation_attendance_multiplier(self.operation, 2, actor=self.fc)
        set_operation_attendance_multiplier(self.operation, 1, actor=self.fc)

        self.assertEqual(before, (1, 1))
        self.assertEqual(self.totals(self.operation), before)

    def test_a_character_moved_to_another_player_stays_with_its_old_owner(self):
        operation = self.closed(limit=1)
        alt = f.add_alt(self.pilot)
        buyer = f.create_user(main_name="Buyer")
        self.track(alt, 30, operation)
        self.track(f.main_of(buyer), 60, operation)
        finalize_pap(operation)

        CharacterOwnership.objects.filter(character=alt).update(user=buyer)
        finalize_pap(operation)

        self.assertEqual(granted(operation), {alt.character_id, f.main_of(buyer).character_id})
        self.assertEqual(row(operation, alt).auth_user, self.pilot)
        self.assertEqual(self.totals(operation)[0], 1)

    def test_a_capped_alt_moved_to_another_player_stays_capped(self):
        operation = self.closed(limit=1)
        alt = f.add_alt(self.pilot)
        self.track(self.main, 60, operation)
        self.track(alt, 30, operation)
        finalize_pap(operation)

        CharacterOwnership.objects.filter(character=alt).update(user=f.create_user())
        finalize_pap(operation)

        self.assertEqual(granted(operation), {self.main.character_id})
        self.assertTrue(row(operation, alt).capped)

    def test_recalculation_gives_the_same_result(self):
        alt = f.add_alt(self.pilot)
        operation = self.closed(limit=1, minimum=10)
        self.track(self.main, 30, operation)
        self.track(alt, 30, operation)
        self.track(f.main_of(f.create_user()), 5, operation)

        first = finalize_pap(operation)
        rows = list(AttendanceRecord.objects.filter(operation=operation).order_by("pk").values())
        second = finalize_pap(operation)

        self.assertEqual(first, second)
        self.assertEqual(list(AttendanceRecord.objects.filter(operation=operation).order_by("pk").values()), rows)

    def test_open_fleets_are_not_finalized(self):
        running = f.create_operation(self.fc)
        self.track(self.main, 30, running)

        with self.assertRaises(ValueError):
            finalize_pap(running)
        self.assertIsNone(refresh_after_change(running))
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_history_is_kept_with_the_owner_and_corporation_it_was_granted_to(self):
        self.track(self.main, 30)
        finalize_pap(self.operation)
        new_owner = f.create_user(main_name="New Owner", corporation=f.OTHER_CORP)
        CharacterOwnership.objects.filter(character=self.main).update(user=new_owner)

        finalize_pap(self.operation)

        kept = row(self.operation, self.main)
        self.assertEqual((kept.auth_user, kept.corporation_id), (self.pilot, f.DEFAULT_CORP[0]))

    def test_the_engine_never_calls_esi(self):
        class NoClient:
            @property
            def client(self):
                raise AssertionError("ESI was called while PAP was finalized.")

        stranger = f.next_id()
        self.track(stranger, 30)
        self.track(self.main, 30)

        with mock.patch.object(esi_provider, "esi", NoClient()):
            finalize_pap(self.operation)
            compute_pap(self.operation)

        self.assertEqual(granted(self.operation), {self.main.character_id})


class RuleSnapshotTests(EngineTestBase):
    """A fleet keeps the PAP minimum and character limit it started with."""

    def test_a_fleet_started_from_a_schedule_takes_the_rules_at_its_start(self):
        f.settings(pap_minimum_minutes=15, attendance_limit=3)
        for stored in (None, 0):
            with self.subTest(stored=stored):
                # Fleets scheduled before the upgrade got 0 from the data migration.
                scheduled = f.create_scheduled_operation(self.fc, pap_minimum_minutes=stored)

                record_start(scheduled, self.fc, character=self.fc_main, from_schedule=True, handed_over=[])

                scheduled.refresh_from_db()
                self.assertEqual((scheduled.pap_minimum_minutes, scheduled.pap_character_limit), (15, 3))

    def test_a_restart_keeps_the_rules_it_has(self):
        operation = f.create_operation(self.fc, pap_minimum_minutes=5, pap_character_limit=None)
        f.settings(pap_minimum_minutes=30, attendance_limit=1)

        record_start(operation, self.fc, character=self.fc_main, from_schedule=False, handed_over=[])

        operation.refresh_from_db()
        self.assertEqual((operation.pap_minimum_minutes, operation.pap_character_limit), (5, None))

    def test_manual_fleets_take_the_rules_and_are_marked_manual(self):
        f.settings(pap_minimum_minutes=10, attendance_limit=2)
        started = timezone.now() - timedelta(hours=3)

        operation = create_manual_fleet(
            user=self.fc,
            cleaned_data={
                "started_at": started,
                "ended_at": started + timedelta(hours=1),
                "fleet_type": f.fleet_type(),
                "formup": "Jita",
            },
        )

        self.assertEqual((operation.pap_minimum_minutes, operation.pap_character_limit), (10, 2))
        self.assertEqual(operation.multiplier_source, FleetOperation.MultiplierSource.MANUAL)
        self.assertEqual(operation.review_status, "pending")
        self.assertEqual(AuditLog.objects.get(action="fleet.manual_create").new_value["review_status"], "pending")


class AltCreditTests(EngineTestBase):
    """PAP and statistics always go to the main of the character's owner."""

    def test_alt_in_another_corporation_and_alliance_is_credited_to_the_main(self):
        alt = f.add_alt(self.pilot, "Far Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        self.track(alt, 30)

        finalize_pap(self.operation)

        alt_row = row(self.operation, alt)
        self.assertEqual(alt_row.character_name, "Far Alt")
        self.assertEqual((alt_row.auth_user, alt_row.main_character_id), (self.pilot, self.main.character_id))
        self.assertEqual((alt_row.corporation_id, alt_row.corporation_name), f.DEFAULT_CORP)
        started = self.operation.started_at
        self.assertEqual(corporation_statistics(f.DEFAULT_CORP[0], started.year, started.month)["total_attendance"], 1)
        self.assertEqual(corporation_statistics(f.OTHER_CORP[0], started.year, started.month)["total_attendance"], 0)
        stats = member_statistics(self.pilot, started.year, started.month)
        self.assertEqual((stats["total"], stats["characters"][0]["character_name"]), (1, "Far Alt"))

    def test_member_list_shows_the_alts_own_corporation(self):
        alt = f.add_alt(self.pilot, "Far Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        operation = f.create_operation(self.fc, started_at=at(0))
        with mock.patch(
            "fleetops.services.tracking.get_fleet_members", return_value=[f.esi_member(alt, join_time=at(0))]
        ), mock.patch("django.utils.timezone.now", return_value=at(10)):
            sync_operation(operation)

        state = FleetMemberState.objects.get(operation=operation, character_id=alt.character_id)
        self.assertEqual((state.corporation_name, state.alliance_name), (f.OTHER_CORP[1], f.FOREIGN_ALLIANCE[1]))
        self.assertEqual(state.main_character_name, "Line Pilot")


class LinkRegistrationTests(EngineTestBase):
    def test_registered_character_qualifies_without_the_minimum(self):
        operation = self.closed(minimum=30)
        registered = f.registration(operation, self.main, link=f.pap_link(operation))

        finalize_pap(operation)

        click = row(operation, self.main)
        self.assertEqual((click.source, click.granted, click.tracked_seconds), (CLICK, True, 0))
        self.assertEqual((click.first_seen, click.last_seen), (registered.registered_at, registered.registered_at))

    def test_tracked_and_registered_character_has_one_tracked_row(self):
        self.track(self.main, 3)
        f.registration(self.operation, self.main)

        finalize_pap(self.operation)

        self.assertEqual(AttendanceRecord.objects.filter(operation=self.operation).count(), 1)
        self.assertEqual(row(self.operation, self.main).source, AUTOMATIC)

    def test_registrations_fall_under_the_character_limit(self):
        operation = self.closed(limit=1)
        alt = f.add_alt(self.pilot)
        self.track(self.main, 30, operation)
        f.registration(operation, alt)

        finalize_pap(operation)

        alt_row = row(operation, alt)
        self.assertEqual((alt_row.source, alt_row.capped, alt_row.granted), (CLICK, True, False))
        self.assertEqual(granted(operation), {self.main.character_id})


class LegacyFleetTests(EngineTestBase):
    """Fleets closed before the upgrade keep the selection they were closed with."""

    def setUp(self):
        super().setUp()
        self.legacy = self.closed(minimum=0, limit=1, pap_rules=FleetOperation.PapRules.LEGACY, attendance_multiplier=2)
        self.alt = f.add_alt(self.pilot)
        # The alt got the PAP when it was tracked; the main came later and was capped.
        f.add_attendance(self.legacy, self.pilot, self.alt, value=3, extra_value=1, legacy_extra_value=1)
        f.add_attendance(self.legacy, self.pilot, self.main, value=1, granted=False, capped=True)
        # Time in fleet that would select the main today.
        self.track(self.main, 90, self.legacy)
        self.track(self.alt, 10, self.legacy)

    def test_selection_stays_and_values_follow_the_multiplier(self):
        set_operation_attendance_multiplier(self.legacy, 3)

        alt_row, main_row = row(self.legacy, self.alt), row(self.legacy, self.main)
        self.assertEqual((alt_row.granted, alt_row.attendance_value), (True, 4))
        self.assertEqual((main_row.granted, main_row.capped), (False, True))

    def test_corrections_still_apply(self):
        lead = f.create_user(perms=f.FC_LEAD_PERMS)

        create_manual_attendance(self.legacy, actor=lead, character_id=self.main.character_id, duplicate_action="merge")

        main_row = row(self.legacy, self.main)
        self.assertEqual((main_row.granted, main_row.attendance_value), (True, 3))


class RecalculateViewTests(EngineTestBase):
    def url(self, operation=None):
        return reverse("fleetops:recalculate_pap", args=[(operation or self.operation).uuid])

    def test_recalculation_is_audited(self):
        late = f.create_character("Late Alt")
        self.track(late, 30)
        finalize_pap(self.operation)
        f.add_ownership(self.pilot, late)
        self.client.force_login(self.fc)

        response = self.client.post(self.url())

        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[self.operation.uuid]), fetch_redirect_response=False
        )
        self.assertTrue(row(self.operation, late).granted)
        entry = AuditLog.objects.get(action="pap.recalculate")
        self.assertEqual((entry.actor, entry.object_id), (self.fc, str(self.operation.pk)))
        self.assertEqual(entry.new_value["granted"], 1)

    def test_recalculating_keeps_pap_of_characters_that_changed_owner(self):
        # Through the button and through a manual row for somebody else, as through a multiplier change.
        lead = f.create_user(perms=f.FC_LEAD_PERMS)
        operation = self.closed(limit=1)
        lost, sold = f.add_alt(self.pilot, "Lost Alt"), f.add_alt(f.create_user(main_name="Seller"), "Sold Alt")
        buyer = f.create_user(main_name="Buyer")
        self.track(lost, 30, operation)
        self.track(sold, 30, operation)
        self.track(f.main_of(buyer), 60, operation)
        finalize_pap(operation)
        CharacterOwnership.objects.filter(character=lost).delete()
        CharacterOwnership.objects.filter(character=sold).update(user=buyer)
        self.client.force_login(lead)

        self.client.post(self.url(operation))
        create_manual_attendance(operation, actor=lead, character_id=self.fc_main.character_id)

        self.assertEqual(
            granted(operation),
            {lost.character_id, sold.character_id, f.main_of(buyer).character_id, self.fc_main.character_id},
        )
        self.assertEqual(row(operation, lost).auth_user, self.pilot)
        self.assertEqual(row(operation, sold).main_character_name, "Seller")

    def test_other_fcs_and_members_cannot_recalculate(self):
        for user in (f.create_user(perms=f.FC_PERMS), f.create_user(perms=f.MEMBER_PERMS)):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertIn(self.client.post(self.url()).status_code, (403, 404))
        self.assertFalse(AuditLog.objects.filter(action="pap.recalculate").exists())

    def test_running_fleets_are_not_recalculated(self):
        running = f.create_operation(self.fc)
        self.client.force_login(self.fc)

        self.client.post(self.url(running))

        self.assertFalse(AuditLog.objects.filter(action="pap.recalculate").exists())

    def test_closed_fleet_page_offers_the_button_and_shows_why_rows_are_not_granted(self):
        operation = self.closed(minimum=20, limit=1)
        alt = f.add_alt(self.pilot)
        self.track(self.main, 30, operation)
        self.track(alt, 25, operation)
        self.track(f.main_of(f.create_user()), 5, operation)
        finalize_pap(operation)
        self.client.force_login(self.fc)

        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

        self.assertContains(page, 'id="recalculate-pap"')
        self.assertContains(page, "Capped (limit)")
        self.assertContains(page, "Below minimum")
        self.assertContains(page, "Granted")
