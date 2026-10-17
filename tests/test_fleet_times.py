# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Date sanity of manual fleets and of edited fleet times: no future, no dates the daily clean-up would undo, and a
confirmation for very old fleets."""

from datetime import timedelta

from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone, translation

from fleetops.forms import ManualFleetForm, OperationEditForm
from fleetops.models import FleetOperation

from . import factories as f

Status = FleetOperation.Status
FORMAT = "%Y-%m-%dT%H:%M"


def stamp(value):
    return value.strftime(FORMAT)


class ManualFleetTimesTests(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(second=0, microsecond=0)
        self.fleet_type = f.fleet_type("StratOp")

    def form(self, started, ended=None, **extra):
        data = {
            "fleet_type": self.fleet_type.pk,
            "formup": "Jita",
            "started_at": started if isinstance(started, str) else stamp(started),
            "ended_at": ended if isinstance(ended, str) else stamp(ended or self.now - timedelta(minutes=1)),
            "attendance_multiplier": "1",
            **extra,
        }
        return ManualFleetForm(data)

    def test_a_recent_fleet_passes(self):
        form = self.form(self.now - timedelta(hours=3), self.now - timedelta(hours=2))

        self.assertTrue(form.is_valid(), form.errors)

    def test_years_that_cannot_be_right_are_refused(self):
        for year in ("0001", "1999", "2099", "9999"):
            with self.subTest(year=year):
                form = self.form(f"{year}-06-01T10:00", f"{year}-06-01T11:00")

                self.assertFalse(form.is_valid())
                self.assertIn("started_at", form.errors)

    def test_the_future_is_refused_for_the_start_and_for_the_end(self):
        later = self.now + timedelta(hours=2)

        form = self.form(later, later + timedelta(hours=1))
        self.assertEqual(
            form.errors["started_at"], ["A fleet record cannot start or end in the future (EVE time)."]
        )
        self.assertEqual(form.errors["ended_at"], ["A fleet record cannot start or end in the future (EVE time)."])

        only_end = self.form(self.now - timedelta(hours=1), self.now + timedelta(hours=1))
        self.assertNotIn("started_at", only_end.errors)
        self.assertEqual(only_end.errors["ended_at"], ["A fleet record cannot start or end in the future (EVE time)."])

    def test_local_time_typed_as_eve_time_is_caught(self):
        # A site at UTC+10:30 types its local "now" (10 hours ahead of EVE time) as the end of a fleet that just ended.
        form = self.form(self.now + timedelta(hours=9), self.now + timedelta(hours=10, minutes=30))

        self.assertFalse(form.is_valid())

    def test_a_few_minutes_of_clock_difference_are_tolerated(self):
        form = self.form(self.now - timedelta(hours=1), self.now + timedelta(minutes=4))

        self.assertTrue(form.is_valid(), form.errors)
        refused = self.form(self.now - timedelta(hours=1), self.now + timedelta(minutes=7))
        self.assertFalse(refused.is_valid())

    def test_older_than_the_retention_is_refused(self):
        f.settings(data_retention_days=365)

        form = self.form(self.now - timedelta(days=366), self.now - timedelta(days=366) + timedelta(hours=1))

        self.assertEqual(
            form.errors["started_at"],
            ["Fleets older than the PAP retention (365 days) would be deleted again; check the date."],
        )
        self.assertNotIn("confirm_old_fleet", form.errors)

    def test_up_to_the_retention_a_fleet_older_than_a_year_needs_the_box(self):
        f.settings(data_retention_days=730)
        started = self.now - timedelta(days=400)

        form = self.form(started, started + timedelta(hours=1))
        self.assertFalse(form.is_valid())
        self.assertIn("confirm_old_fleet", form.errors)
        self.assertNotIn("started_at", form.errors)

        confirmed = self.form(started, started + timedelta(hours=1), confirm_old_fleet="on")
        self.assertTrue(confirmed.is_valid(), confirmed.errors)

        beyond = self.form(self.now - timedelta(days=731), self.now - timedelta(days=731) + timedelta(hours=1), confirm_old_fleet="on")
        self.assertIn("started_at", beyond.errors)

    def test_364_days_need_no_confirmation(self):
        f.settings(data_retention_days=730)
        started = self.now - timedelta(days=364)

        self.assertTrue(self.form(started, started + timedelta(hours=1)).is_valid())

    def test_with_the_retention_set_to_forever_a_year_before_eve_is_still_refused(self):
        f.settings(data_retention_days=999_999)

        form = self.form("0001-01-01T00:00", "0001-01-01T01:00", confirm_old_fleet="on")

        self.assertIn("started_at", form.errors)

    def test_the_view_refuses_and_shows_the_error(self):
        lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.client.force_login(lead)

        response = self.client.post(
            reverse("fleetops:manual_fleet"),
            {
                "fleet_type": self.fleet_type.pk,
                "formup": "Jita",
                "started_at": "9999-01-01T10:00",
                "ended_at": "9999-01-01T11:00",
                "attendance_multiplier": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A fleet record cannot start or end in the future (EVE time).")
        self.assertFalse(FleetOperation.objects.filter(is_manual=True).exists())

    def test_the_page_has_the_hidden_box_for_old_fleets(self):
        self.client.force_login(f.create_user("lead", perms=f.FC_LEAD_PERMS))

        page = self.client.get(reverse("fleetops:manual_fleet"))

        self.assertContains(page, 'id="confirm-old-fleet-wrap"')
        self.assertContains(page, "This fleet really took place more than a year ago")


class EditedTimesTests(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(second=0, microsecond=0)
        f.settings(data_retention_days=1000)
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        # Stored two years ago, before the retention was shortened; it must stay editable.
        self.started = self.now - timedelta(days=730)
        self.operation = f.create_operation(
            self.fc, status=Status.CLOSED, started_at=self.started, ended_at=self.started + timedelta(hours=1)
        )

    def form(self, **changes):
        data = {
            "fleet_type": self.operation.fleet_type_id,
            "doctrine_name": self.operation.doctrine_name,
            "formup": self.operation.formup,
            "additional_message": "",
            "started_at": stamp(self.started),
            "ended_at": stamp(self.started + timedelta(hours=1)),
            **changes,
        }
        return OperationEditForm(data, instance=self.operation, user=self.fc)

    def test_an_unrelated_edit_of_an_old_record_passes(self):
        form = self.form(formup="Amarr", additional_message="Fixed the form-up")

        self.assertTrue(form.is_valid(), form.errors)

    def test_a_changed_start_is_checked_again(self):
        f.settings(data_retention_days=365)

        form = self.form(started_at=stamp(self.started + timedelta(minutes=30)))

        self.assertIn("started_at", form.errors)

    def test_a_start_moved_to_another_old_date_needs_the_box(self):
        form = self.form(started_at=stamp(self.started - timedelta(days=2)))
        self.assertIn("confirm_old_fleet", form.errors)

        confirmed = self.form(started_at=stamp(self.started - timedelta(days=2)), confirm_old_fleet="on")
        self.assertTrue(confirmed.is_valid(), confirmed.errors)

    def test_an_end_in_the_future_is_refused_but_the_stored_end_is_not_checked(self):
        form = self.form(ended_at=stamp(self.now + timedelta(days=1)))
        self.assertEqual(form.errors["ended_at"], ["A fleet record cannot start or end in the future (EVE time)."])

        # A record that was stored with an end in the future stays editable while that end is not changed.
        FleetOperation.objects.filter(pk=self.operation.pk).update(ended_at=self.now + timedelta(days=1))
        self.operation.refresh_from_db()
        unchanged = self.form(ended_at=stamp(self.operation.ended_at), formup="Amarr")
        self.assertNotIn("ended_at", unchanged.errors)

    def test_the_edit_view_saves_an_unrelated_change_of_an_old_record(self):
        self.client.force_login(self.fc)

        response = self.client.post(
            reverse("fleetops:edit_operation", args=[self.operation.uuid]),
            {
                "fleet_type": self.operation.fleet_type_id,
                "doctrine_name": "",
                "formup": "Amarr",
                "additional_message": "",
                "started_at": stamp(self.started),
                "ended_at": stamp(self.started + timedelta(hours=1)),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.formup, "Amarr")

    def test_the_page_has_the_hidden_box_for_old_fleets(self):
        self.client.force_login(self.fc)

        page = self.client.get(reverse("fleetops:edit_operation", args=[self.operation.uuid]))

        self.assertContains(page, 'id="confirm-old-fleet-wrap"')


@tag("catalog")
class ChineseTests(TestCase):
    def test_the_messages_are_translated(self):
        now = timezone.now().replace(second=0, microsecond=0)
        with translation.override("zh-hans"):
            form = ManualFleetForm(
                {
                    "fleet_type": f.fleet_type("StratOp").pk,
                    "formup": "Jita",
                    "started_at": stamp(now + timedelta(hours=1)),
                    "ended_at": stamp(now + timedelta(hours=2)),
                    "attendance_multiplier": "1",
                }
            )
            # The message is formatted when it is read, so it is read while the language is active.
            message = str(form.errors["started_at"][0])

        self.assertNotIn("A fleet record cannot start", message)
        self.assertTrue(any(ord(char) > 127 for char in message))
