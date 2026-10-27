# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The wording of PAP multipliers: one label per value ("2x", Chinese "2 倍"), lists that name only what the fleet
can get, and the conditions that are not reached yet."""

import re
from datetime import timedelta

from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone, translation

from fleetops.forms import FleetAttendanceMultiplierForm, ManualFleetForm, multiplier_choices
from fleetops.models import FleetOperation
from fleetops.services import multipliers

from . import factories as f

Status = FleetOperation.Status


def minutes(value, seconds=0):
    return timedelta(minutes=value, seconds=seconds)


def text_outside_attributes(html):
    """The text of an HTML page without tags (and so without attribute values) and without scripts."""
    html = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


class LabelTests(TestCase):
    def setUp(self):
        f.multiplier_tiers((10, 2), (20, 3))
        english = translation.override("en")
        english.__enter__()
        self.addCleanup(english.__exit__, None, None, None)

    def test_a_label_per_value_and_a_list_of_them(self):
        self.assertEqual(multipliers.multiplier_label(2), "2x")
        self.assertEqual(multipliers.multiplier_list_text([1, 2, 3]), "1x, 2x, 3x")
        self.assertEqual(multipliers.multiplier_list_text([]), "")

    def test_the_conditions_not_reached_yet(self):
        cases = [
            (minutes(5), "2x > 10 min, 3x > 20 min"),
            (minutes(10), "2x > 10 min, 3x > 20 min"),
            (minutes(10, 1), "3x > 20 min"),
            (minutes(15), "3x > 20 min"),
            (minutes(20), "3x > 20 min"),
            (minutes(25), ""),
            (None, "2x > 10 min, 3x > 20 min"),
        ]
        for duration, expected in cases:
            with self.subTest(duration=duration):
                self.assertEqual(multipliers.unmet_requirements_text(duration), expected)
        # The full list is unchanged.
        self.assertEqual(multipliers.requirements_text(), "2x > 10 min, 3x > 20 min")

    def test_the_form_choices_use_the_labels(self):
        self.assertEqual(multiplier_choices(), [(1, "1x"), (2, "2x"), (3, "3x")])
        self.assertEqual(multiplier_choices([1, 2]), [(1, "1x"), (2, "2x")])
        manual = ManualFleetForm()
        self.assertEqual([label for _value, label in manual.fields["attendance_multiplier"].choices], ["1x", "2x", "3x"])


class EndDialogTests(TestCase):
    """The End Fleet dialog names what the fleet can get and lists only the conditions it has not reached."""

    def setUp(self):
        f.multiplier_tiers((10, 2), (20, 3))
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.client.force_login(self.fc)

    def page(self, ran):
        operation = f.create_operation(self.fc, started_at=timezone.now() - ran)
        return self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

    def modal(self, page):
        html = page.content.decode()
        modal = html[html.index('id="endFleetModal"'):]
        return modal[: modal.index("</form>")]

    def test_a_fleet_past_every_tier_names_no_conditions(self):
        page = self.page(minutes(25, 10))

        self.assertEqual(page.context["end_prompt"]["allowed"], [1, 2, 3])
        modal = self.modal(page)
        self.assertIn("and can get 1x, 2x, 3x.", modal)
        self.assertNotIn("need", modal)
        self.assertEqual(page.context["end_prompt"]["requirements"], "")

    def test_a_fleet_between_tiers_lists_only_the_one_it_has_not_reached(self):
        page = self.page(minutes(15, 10))

        modal = self.modal(page)
        self.assertIn("and can get 1x, 2x. Higher multipliers need: 3x &gt; 20 min.", modal)
        self.assertNotIn("2x &gt; 10 min", modal)
        self.assertNotIn("Multipliers above 1x need", modal)

    def test_the_multiplier_choices_are_labelled_buttons(self):
        modal = self.modal(self.page(minutes(15, 10)))

        self.assertEqual(modal.count('type="radio" name="attendance_multiplier"'), 2)
        self.assertRegex(modal, r'<label for="id_attendance_multiplier_1">2x</label>')

    def test_the_closed_fleet_card_and_the_pending_choice_use_the_same_words(self):
        operation = f.create_operation(
            self.fc,
            status=Status.CLOSED,
            started_at=timezone.now() - timedelta(hours=2),
            ended_at=timezone.now() - timedelta(hours=2) + minutes(15, 10),
            multiplier_pending=True,
        )
        lead = f.create_user(perms=f.FC_LEAD_PERMS)
        self.client.force_login(lead)

        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

        self.assertContains(page, "The fleet ran 15 min, which allows 1x, 2x. Higher multipliers need: 3x &gt; 20 min.")
        self.assertContains(page, "Higher multipliers need: 3x &gt; 20 min.", count=2)
        self.assertNotContains(page, "Multipliers above 1x need")
        self.assertContains(page, "Keep 1x")
        self.assertContains(page, "3x (override)")
        self.assertEqual(
            [label for _value, label in page.context["multiplier_form"].fields["attendance_multiplier"].widget.choices],
            ["1x", "2x", "3x (override)"],
        )

    def test_the_conditions_of_a_fleet_that_has_them_all_are_left_out(self):
        operation = f.create_operation(
            self.fc,
            status=Status.CLOSED,
            started_at=timezone.now() - timedelta(hours=2),
            ended_at=timezone.now() - timedelta(hours=2) + minutes(25),
        )
        self.client.force_login(f.create_user(perms=f.FC_LEAD_PERMS))

        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

        self.assertContains(page, "which allows 1x, 2x, 3x.")
        self.assertNotContains(page, "Higher multipliers need")


@tag("catalog")
class ChineseWordingTests(TestCase):
    """In Chinese every multiplier reads "N 倍" and lists are joined with the Chinese enumeration mark."""

    def setUp(self):
        f.multiplier_tiers((10, 2), (20, 3))
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.lead = f.create_user(perms=f.FC_LEAD_PERMS)
        self.client.cookies["django_language"] = "zh-hans"

    def test_labels(self):
        with translation.override("zh-hans"):
            self.assertEqual(multipliers.multiplier_label(2), "2 倍")
            self.assertEqual(multipliers.multiplier_list_text([1, 2, 3]), "1 倍、2 倍、3 倍")
            self.assertEqual(multipliers.unmet_requirements_text(minutes(15)), "3 倍 > 20 分钟")
            self.assertEqual([label for _value, label in multiplier_choices()], ["1 倍", "2 倍", "3 倍"])

    def test_the_end_dialog_and_the_closed_fleet_page_have_no_english_multipliers(self):
        self.client.force_login(self.fc)
        running = f.create_operation(self.fc, started_at=timezone.now() - minutes(25, 10))
        closed = f.create_operation(
            self.fc,
            status=Status.CLOSED,
            started_at=timezone.now() - timedelta(hours=2),
            ended_at=timezone.now() - timedelta(hours=2) + minutes(15, 10),
            multiplier_pending=True,
        )

        dialog = self.client.get(reverse("fleetops:operation_detail", args=[running.uuid])).content.decode()
        self.assertIn("1 倍、2 倍、3 倍", dialog)
        self.client.force_login(self.lead)
        card = self.client.get(reverse("fleetops:operation_detail", args=[closed.uuid]))

        for html in (dialog, card.content.decode()):
            self.assertIsNone(re.search(r"\b\d+x\b", text_outside_attributes(html)), html[:0])
        self.assertContains(card, "3 倍（覆盖）")
        options = card.context["multiplier_form"].fields["attendance_multiplier"].widget.choices
        self.assertEqual([label for _value, label in options], ["1 倍", "2 倍", "3 倍（覆盖）"])

    def test_the_manual_fleet_form(self):
        self.client.force_login(self.lead)

        page = self.client.get(reverse("fleetops:manual_fleet"))

        self.assertIsNone(re.search(r"\b\d+x\b", text_outside_attributes(page.content.decode())))
        self.assertContains(page, ">1 倍<")

    def test_the_dashboard_card_of_pending_multipliers(self):
        self.client.force_login(self.fc)
        f.create_operation(
            self.fc,
            status=Status.CLOSED,
            started_at=timezone.now() - timedelta(hours=2),
            ended_at=timezone.now() - timedelta(hours=2) + minutes(25),
            multiplier_pending=True,
            attendance_multiplier=2,
        )

        page = self.client.get(reverse("fleetops:dashboard"))

        self.assertContains(page, 'id="pending-multipliers"')
        self.assertContains(page, '<td class="text-end">2 倍</td>')
        self.assertIsNone(re.search(r"\b\d+x\b", text_outside_attributes(page.content.decode())))

    def test_the_multiplier_form_labels(self):
        operation = f.create_operation(self.fc, status=Status.CLOSED, ended_at=timezone.now())
        with translation.override("zh-hans"):
            form = FleetAttendanceMultiplierForm(operation=operation, can_override=True)

        self.assertEqual([label for _value, label in form.fields["attendance_multiplier"].widget.choices][0], "1 倍")

