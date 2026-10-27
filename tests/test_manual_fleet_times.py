# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Manual fleet records: start and end times in EVE time."""

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from fleetops.forms import ManualFleetForm
from fleetops.models import FleetOperation

from . import factories as f

UTC = dt_timezone.utc


class ManualFleetTimeTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.fleet_type = f.fleet_type("Roam")

    def form(self, started="2026-10-11T19:00", ended="2026-10-11T21:00", **extra):
        data = {
            "fleet_type": self.fleet_type.pk,
            "formup": "Amamake",
            "started_at": started,
            "ended_at": ended,
            "attendance_multiplier": "1",
        }
        data.update(extra)
        return ManualFleetForm(data=data)

    def test_labels_name_eve_time(self):
        form = ManualFleetForm()

        self.assertEqual(form.fields["started_at"].label, "Start Time — EVE time (UTC)")
        self.assertEqual(form.fields["ended_at"].label, "End Time — EVE time (UTC)")

    def test_end_time_is_required(self):
        form = self.form(ended="")

        self.assertFalse(form.is_valid())
        self.assertIn("ended_at", form.errors)

    @override_settings(TIME_ZONE="Australia/Adelaide")
    def test_times_are_read_as_utc_whatever_the_time_zone(self):
        with timezone.override("America/Los_Angeles"):
            form = self.form()
            self.assertTrue(form.is_valid(), form.errors)

        self.assertEqual(form.cleaned_data["started_at"], datetime(2026, 10, 11, 19, 0, tzinfo=UTC))
        self.assertEqual(form.cleaned_data["ended_at"], datetime(2026, 10, 11, 21, 0, tzinfo=UTC))

    def test_explicit_offsets_are_respected(self):
        form = self.form(started="2026-10-11T21:00+02:00")

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["started_at"], datetime(2026, 10, 11, 19, 0, tzinfo=UTC))

    @override_settings(TIME_ZONE="Australia/Adelaide")
    def test_stored_times_are_shown_in_utc(self):
        form = ManualFleetForm(initial={"started_at": datetime(2026, 10, 11, 19, 0, tzinfo=UTC)})

        self.assertIn('value="2026-10-11T19:00"', str(form["started_at"]))

    def test_end_must_be_after_start(self):
        for ended in ("2026-10-11T19:00", "2026-10-11T18:59"):
            with self.subTest(ended=ended):
                form = self.form(ended=ended)
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors["ended_at"], ["End Time must be after Start Time."])

    def test_more_than_six_hours_needs_the_confirmation(self):
        form = self.form(ended="2026-10-12T08:51")

        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["confirm_long_fleet"],
            [
                "This fleet would last 13 h 51 min. Check the times, or tick this box if the fleet really ran "
                "for more than 6 hours."
            ],
        )
        self.assertTrue(self.form(ended="2026-10-12T08:51", confirm_long_fleet="on").is_valid())
        self.assertTrue(self.form(ended="2026-10-12T01:00").is_valid())

    def test_page_shows_eve_time_and_the_duration(self):
        self.client.force_login(self.fc)
        moment = datetime(2026, 10, 12, 8, 41, tzinfo=UTC)

        with mock.patch("django.utils.timezone.now", return_value=moment), override_settings(TIME_ZONE="Asia/Shanghai"):
            page = self.client.get(reverse("fleetops:manual_fleet"))

        self.assertContains(page, '<strong id="eve-time-now">2026-10-12 08:41</strong>')
        self.assertContains(page, 'id="manual-fleet-duration"')
        # The duration text is a translated message that the script fills in.
        self.assertContains(page, "Duration: %(duration)s")
        self.assertEqual(page.context["form"]["ended_at"].value(), None)

    def test_long_fleet_is_saved_after_confirming(self):
        self.client.force_login(self.fc)
        data = {
            "fleet_type": self.fleet_type.pk,
            "formup": "Amamake",
            "started_at": "2026-10-11T19:21",
            "ended_at": "2026-10-12T08:51",
            "attendance_multiplier": "1",
        }

        rejected = self.client.post(reverse("fleetops:manual_fleet"), data)
        self.assertEqual(rejected.status_code, 200)
        self.assertContains(rejected, "This fleet would last 13 h 30 min.")
        self.assertFalse(FleetOperation.objects.exists())

        accepted = self.client.post(reverse("fleetops:manual_fleet"), {**data, "confirm_long_fleet": "on"})
        self.assertEqual(accepted.status_code, 302)
        operation = FleetOperation.objects.get()
        self.assertEqual(operation.started_at, datetime(2026, 10, 11, 19, 21, tzinfo=UTC))
        self.assertEqual(operation.ended_at - operation.started_at, timedelta(hours=13, minutes=30))
