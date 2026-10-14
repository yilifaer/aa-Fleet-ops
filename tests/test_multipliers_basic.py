# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Multiplier tiers: which PAP multipliers a fleet's duration allows, and the tier form."""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from fleetops.forms_pap import MultiplierTierForm
from fleetops.models import MAX_MULTIPLIER, FleetOperation, MultiplierTier
from fleetops.services import multipliers

from . import factories as f


def minutes(value, seconds=0):
    return timedelta(minutes=value, seconds=seconds)


class AllowedMultiplierTests(TestCase):
    def setUp(self):
        f.multiplier_tiers((90, 2), (180, 3))

    def test_tiers_are_ordered_by_threshold(self):
        f.multiplier_tiers((180, 3), (60, 2))

        self.assertEqual([tier.threshold_minutes for tier in multipliers.tiers()], [60, 180])

    def test_a_tier_needs_strictly_more_than_its_threshold(self):
        cases = [
            (minutes(0), [1]),
            (minutes(89, 59), [1]),
            (minutes(90), [1]),
            (minutes(90, 1), [1, 2]),
            (minutes(180), [1, 2]),
            (minutes(180, 1), [1, 2, 3]),
            (timedelta(days=2), [1, 2, 3]),
        ]
        for duration, expected in cases:
            with self.subTest(duration=duration):
                self.assertEqual(multipliers.allowed_multipliers(duration), expected)

    def test_unknown_duration_allows_only_1x(self):
        self.assertEqual(multipliers.allowed_multipliers(None), [1])

    def test_without_tiers_only_1x_exists(self):
        MultiplierTier.objects.all().delete()

        self.assertEqual(multipliers.allowed_multipliers(timedelta(hours=10)), [1])
        self.assertEqual(multipliers.all_multipliers(), [1])
        self.assertEqual(multipliers.auto_multiplier(timedelta(hours=10)), 1)

    def test_auto_multiplier_is_the_highest_allowed(self):
        self.assertEqual(multipliers.auto_multiplier(minutes(90)), 1)
        self.assertEqual(multipliers.auto_multiplier(minutes(91)), 2)
        self.assertEqual(multipliers.auto_multiplier(minutes(181)), 3)

    def test_all_multipliers_lists_every_tier_and_1x(self):
        f.multiplier_tiers((60, 2), (120, 4), (240, MAX_MULTIPLIER))

        self.assertEqual(multipliers.all_multipliers(), [1, 2, 4, MAX_MULTIPLIER])

    def test_the_fc_chooses_only_when_more_than_1x_is_allowed(self):
        settings = f.settings(multiplier_mode="fc")

        self.assertFalse(multipliers.needs_choice(minutes(90), settings))
        self.assertTrue(multipliers.needs_choice(minutes(91), settings))
        self.assertTrue(multipliers.needs_choice(minutes(91)))

    def test_automatic_mode_never_asks(self):
        settings = f.settings(multiplier_mode="auto")

        self.assertFalse(multipliers.needs_choice(minutes(181), settings))
        self.assertFalse(multipliers.needs_choice(minutes(181)))


class EffectiveEndTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS)

    def test_end_request_wins_over_the_close(self):
        operation = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED)
        requested = operation.ended_at - timedelta(minutes=4)
        operation.end_requested_at = requested

        self.assertEqual(multipliers.effective_end(operation), requested)

    def test_closed_fleet_without_end_request_uses_its_end(self):
        operation = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED)

        self.assertEqual(multipliers.effective_end(operation), operation.ended_at)

    def test_running_fleet_uses_now(self):
        operation = f.create_operation(self.fc)
        now = timezone.now()

        with mock.patch("fleetops.services.multipliers.timezone.now", return_value=now):
            self.assertEqual(multipliers.effective_end(operation), now)


class MultiplierTierFormTests(TestCase):
    def setUp(self):
        self.tier_90, self.tier_180 = f.multiplier_tiers((90, 2), (180, 3))

    def form(self, threshold, multiplier, instance=None):
        return MultiplierTierForm({"threshold_minutes": threshold, "multiplier": multiplier}, instance=instance)

    def test_a_higher_tier_after_the_last_one_is_valid(self):
        form = self.form(240, 4)

        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(multipliers.all_multipliers(), [1, 2, 3, 4])

    def test_a_longer_fleet_cannot_get_less_or_the_same(self):
        for multiplier in (2, 3):
            with self.subTest(multiplier=multiplier):
                form = self.form(240, multiplier)

                self.assertFalse(form.is_valid())
                self.assertIn(
                    "Multipliers must rise with the time: fleets longer than 180 minutes already get 3x.",
                    form.errors["multiplier"],
                )

    def test_a_shorter_fleet_cannot_get_more_or_the_same(self):
        form = self.form(60, 2)

        self.assertFalse(form.is_valid())
        self.assertIn(
            "Multipliers must rise with the time: fleets longer than 90 minutes get only 2x.",
            form.errors["multiplier"],
        )

    def test_a_tier_between_two_tiers_needs_a_multiplier_between_them(self):
        self.assertFalse(self.form(120, 3).is_valid())
        self.assertTrue(self.form(120, 3, instance=self.tier_180).is_valid())

    def test_editing_a_tier_compares_it_with_the_others_only(self):
        form = self.form(100, 2, instance=self.tier_90)

        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.tier_90.refresh_from_db()
        self.assertEqual(self.tier_90.threshold_minutes, 100)

    def test_thresholds_are_unique(self):
        form = self.form(180, 4)

        self.assertFalse(form.is_valid())
        self.assertIn("threshold_minutes", form.errors)

    def test_multiplier_range(self):
        for multiplier in (1, MAX_MULTIPLIER + 1):
            with self.subTest(multiplier=multiplier):
                form = self.form(600, multiplier)

                self.assertFalse(form.is_valid())
                self.assertIn("multiplier", form.errors)
        self.assertTrue(self.form(600, MAX_MULTIPLIER).is_valid())
