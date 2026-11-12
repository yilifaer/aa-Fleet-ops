# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fleet type colours and the dashboard cards: the colour of a type (theme colour or custom #rrggbb), where it is
shown, the contrast rules, the edit page, the audit entries and the "Show on dashboard" switch."""

import re
from decimal import Decimal

from django.contrib.messages import get_messages
from django.template import Context, Template
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from fleetops import ui
from fleetops.forms_config import FleetTypeForm
from fleetops.models import AuditLog, FleetType
from fleetops.services.dashboard import dashboard_metrics

from . import factories as f

CONFIG_PERMS = f.MEMBER_PERMS + ["fleetops.manage_configuration"]


def render(source, **context):
    return Template("{% load fleetops_ui %}" + source).render(Context(context))


def payload(**overrides):
    data = {"name": "Roam", "short_name": "RM", "point_weight": "1.50", "is_active": "on", "sort_order": "3"}
    data.update(overrides)
    return data


class ColorHelperTests(SimpleTestCase):
    def test_a_colour_is_stored_as_empty_a_theme_name_or_six_digit_hex(self):
        cases = {
            "": "",
            "  ": "",
            None: "",
            "theme-1": "theme-1",
            "theme-6": "theme-6",
            "#E67E22": "#e67e22",
            "#abc": "#aabbcc",
            " #AbC ": "#aabbcc",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(ui.normalize_type_color(value), expected)

    def test_anything_else_is_refused(self):
        for value in (
            "red", "e67e22", "#12", "#1234", "#12345", "#1234567", "#ggg", "theme-0", "theme-7", "theme-10",
            "rgb(1,2,3)", "#fff; background:url(x)", "#fff}", "url(x)", "var(--x)", "#e67e22 #fff", "<b>",
            "#ffffff00", "١٢٣",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ui.normalize_type_color(value)

    def test_contrast_ratios_are_the_wcag_ones(self):
        self.assertAlmostEqual(ui.contrast_ratio("#000000", "#ffffff"), 21.0, places=2)
        self.assertAlmostEqual(ui.contrast_ratio("#ffffff", "#ffffff"), 1.0, places=2)
        self.assertAlmostEqual(ui.contrast_ratio("#777777", "#ffffff"), 4.48, places=2)

    def test_the_pill_text_is_black_on_light_and_white_on_dark_backgrounds(self):
        for color, ink in (
            ("#ffffff", "#000000"), ("#ffff00", "#000000"), ("#e67e22", "#000000"),
            ("#000000", "#ffffff"), ("#0000ff", "#ffffff"), ("#222222", "#ffffff"), ("#fff", "#000000"),
        ):
            with self.subTest(color=color):
                self.assertEqual(ui.ink_for(color), ink)

    def test_the_ink_always_has_at_least_the_contrast_of_the_other_choice(self):
        for color in ("#808080", "#767676", "#7f7f7f", "#ff0000", "#00ff00", "#1e90ff", "#8a2be2"):
            with self.subTest(color=color):
                ink = ui.ink_for(color)
                other = "#ffffff" if ink == "#000000" else "#000000"
                self.assertGreaterEqual(ui.contrast_ratio(color, ink), ui.contrast_ratio(color, other))
                self.assertGreaterEqual(ui.contrast_ratio(color, ink), 4.5)

    def test_poor_contrast_names_the_theme_the_dot_hardly_stands_out_from(self):
        self.assertEqual(ui.poor_contrast_themes("#ffffff"), ["light"])
        self.assertEqual(ui.poor_contrast_themes("#f5f5f5"), ["light"])
        self.assertEqual(ui.poor_contrast_themes("#222222"), ["dark"])
        self.assertEqual(ui.poor_contrast_themes("#000000"), ["dark"])
        self.assertEqual(ui.poor_contrast_themes("#e67e22"), [])
        self.assertEqual(ui.poor_contrast_themes("#808080"), [])
        # Both themes at once: a mid grey close to both backgrounds does not exist, but a colour can fail one only.
        self.assertEqual(ui.poor_contrast_themes("#2a2a2a"), ["dark"])


class ColorOfATypeTests(SimpleTestCase):
    def test_without_a_colour_the_automatic_colour_is_unchanged(self):
        # Same numbers as before the colour existed (pk % 6 + 1).
        self.assertEqual([ui.type_tone(FleetType(pk=pk)) for pk in range(1, 8)], [2, 3, 4, 5, 6, 1, 2])
        self.assertEqual(ui.type_css_color(FleetType(pk=9)), "var(--fo-type-4)")
        self.assertEqual(ui.type_custom_color(FleetType(pk=9)), "")

    def test_a_theme_colour_wins_over_the_primary_key(self):
        fleet_type = FleetType(pk=9, color="theme-2")
        self.assertEqual(ui.type_tone(fleet_type), 2)
        self.assertEqual(ui.type_css_color(fleet_type), "var(--fo-type-2)")
        self.assertEqual(ui.type_custom_color(fleet_type), "")

    def test_a_custom_colour_is_used_as_it_is_and_has_no_theme_dot(self):
        fleet_type = FleetType(pk=9, color="#e67e22")
        self.assertEqual(ui.type_tone(fleet_type), 0)
        self.assertEqual(ui.type_css_color(fleet_type), "#e67e22")
        self.assertEqual(ui.type_custom_color(fleet_type), "#e67e22")

    def test_a_stored_value_that_is_not_valid_is_ignored_when_it_is_shown(self):
        # Defence in depth: whatever is in the column (a manual edit, an old import), it never reaches a page.
        for bad in ("red", "#fff", "#12345g", "#e67e22;x:y", "theme-9", "url(javascript:1)", "<script>"):
            with self.subTest(bad=bad):
                fleet_type = FleetType(pk=9, color=bad)
                self.assertEqual(ui.type_tone(fleet_type), 4)
                self.assertEqual(ui.type_custom_color(fleet_type), "")
                self.assertEqual(ui.type_css_color(fleet_type), "var(--fo-type-4)")

    def test_a_type_without_a_key_has_a_neutral_dot(self):
        self.assertEqual(ui.type_css_color(None), "")
        self.assertEqual(ui.type_css_color(FleetType()), "")


class TemplateOutputTests(SimpleTestCase):
    def test_the_type_pill_of_an_automatic_type_is_as_before(self):
        self.assertEqual(
            render("{{ t|type_pill }}", t=FleetType(pk=2, name="StratOp")),
            '<span class="fo-pill fo-pill-type fo-type-3">StratOp</span>',
        )

    def test_the_type_pill_of_a_theme_colour_uses_that_theme_dot(self):
        self.assertEqual(
            render("{{ t|type_pill }}", t=FleetType(pk=2, name="StratOp", color="theme-5")),
            '<span class="fo-pill fo-pill-type fo-type-5">StratOp</span>',
        )

    def test_the_type_pill_of_a_custom_colour_carries_the_colour_in_the_dot_only(self):
        html = render("{{ t|type_pill }}", t=FleetType(pk=2, name="StratOp", color="#e67e22"))
        self.assertEqual(html, '<span class="fo-pill fo-pill-type" style="--fo-dot: #e67e22">StratOp</span>')

    def test_a_name_with_markup_is_escaped_and_an_invalid_colour_writes_no_style(self):
        html = render("{{ t|type_pill }}", t=FleetType(pk=2, name="<b>x</b>", color='#fff" onmouseover="x'))
        self.assertNotIn("style=", html)
        self.assertNotIn("onmouseover", html)
        self.assertIn("&lt;b&gt;", html)

    def test_the_colour_filters(self):
        custom = FleetType(pk=3, color="#e67e22")
        theme = FleetType(pk=3, color="theme-1")
        self.assertEqual(render("{{ t|type_color }}", t=custom), "#e67e22")
        self.assertEqual(render("{{ t|type_color }}", t=theme), "var(--fo-type-1)")
        self.assertEqual(render("{{ t|type_color }}", t=FleetType(pk=3)), "var(--fo-type-4)")
        self.assertEqual(render("{{ t|type_color }}", t=None), "")
        self.assertEqual(render("{{ t|type_dot_style }}", t=custom), "--fo-dot: #e67e22")
        self.assertEqual(render("{{ t|type_dot_style }}", t=theme), "")


class FleetTypeFormTests(TestCase):
    def form(self, instance=None, **overrides):
        return FleetTypeForm(payload(**overrides), instance=instance)

    def test_no_choice_means_automatic_and_stores_nothing(self):
        form = self.form()
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().color, "")

    def test_a_theme_colour_is_stored(self):
        form = self.form(color_choice="theme-3")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().color, "theme-3")

    def test_a_custom_colour_is_stored_normalised(self):
        for typed, stored in (("#E67E22", "#e67e22"), ("#f80", "#ff8800"), (" #ABCDEF ", "#abcdef")):
            with self.subTest(typed=typed):
                FleetType.objects.all().delete()
                form = self.form(color_choice="custom", custom_color=typed)
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.save().color, stored)

    def test_an_invalid_custom_colour_is_refused_and_nothing_is_saved(self):
        for bad in ("", "red", "e67e22", "#12", "#12345", "rgb(1,2,3)", "#fff;}", "#ggg", "url(x)", "#1234567"):
            with self.subTest(bad=bad):
                form = self.form(color_choice="custom", custom_color=bad)
                self.assertFalse(form.is_valid())
                self.assertIn("custom_color", form.errors)
        self.assertFalse(FleetType.objects.exists())

    def test_an_unknown_choice_is_refused(self):
        for bad in ("theme-7", "#e67e22", "red", "theme-0"):
            with self.subTest(bad=bad):
                form = self.form(color_choice=bad)
                self.assertFalse(form.is_valid())
                self.assertIn("color_choice", form.errors)

    def test_the_typed_colour_is_ignored_unless_custom_is_chosen(self):
        form = self.form(color_choice="theme-2", custom_color="not a colour")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().color, "theme-2")
        form = self.form(instance=FleetType.objects.get(), color_choice="auto", custom_color="#123456")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().color, "")

    def test_the_form_shows_the_stored_colour(self):
        theme = FleetType.objects.create(name="A", color="theme-4")
        custom = FleetType.objects.create(name="B", color="#e67e22")
        plain = FleetType.objects.create(name="C")
        self.assertEqual(FleetTypeForm(instance=theme).initial["color_choice"], "theme-4")
        self.assertEqual(FleetTypeForm(instance=custom).initial["color_choice"], "custom")
        self.assertEqual(FleetTypeForm(instance=custom).initial["custom_color"], "#e67e22")
        self.assertEqual(FleetTypeForm(instance=plain).initial["color_choice"], "auto")

    def test_the_colour_field_of_the_model_cannot_be_posted_directly(self):
        form = self.form(color="#e67e22")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().color, "")

    def test_a_colour_that_is_hard_to_see_gives_a_warning_per_theme(self):
        form = self.form(color_choice="custom", custom_color="#ffffff")
        form.is_valid()
        form.save()
        self.assertEqual(len(form.warnings), 1)
        self.assertIn("light", str(form.warnings[0]))
        FleetType.objects.all().delete()
        form = self.form(color_choice="custom", custom_color="#202020")
        form.is_valid()
        form.save()
        self.assertIn("dark", str(form.warnings[0]))
        FleetType.objects.all().delete()
        form = self.form(color_choice="custom", custom_color="#e67e22")
        form.is_valid()
        form.save()
        self.assertEqual(form.warnings, [])


class FleetTypePagesTests(TestCase):
    def setUp(self):
        self.admin = f.create_user(perms=CONFIG_PERMS)
        self.client.force_login(self.admin)
        self.add = reverse("fleetops:configuration_add", args=["fleet-types"])

    def edit(self, obj):
        return reverse("fleetops:configuration_edit", args=["fleet-types", obj.pk])

    def test_the_edit_page_has_the_colour_fields_the_switch_and_the_preview(self):
        html = self.client.get(self.add).content.decode()
        for needle in ('name="color_choice"', 'name="custom_color"', 'name="show_on_dashboard"', "fleet-type-preview", "fleet_type_preview.js"):
            self.assertIn(needle, html)
        self.assertIn('value="theme-1"', html)
        self.assertIn('value="custom"', html)

    def test_creating_a_type_with_a_custom_colour_saves_and_audits_it(self):
        response = self.client.post(self.add, payload(color_choice="custom", custom_color="#E67E22", show_on_dashboard="on"))
        self.assertEqual(response.status_code, 302)
        fleet_type = FleetType.objects.get(name="Roam")
        self.assertEqual(fleet_type.color, "#e67e22")
        self.assertTrue(fleet_type.show_on_dashboard)
        entry = AuditLog.objects.get(action="configuration.create", object_id=str(fleet_type.pk))
        self.assertEqual(entry.new_value["color"], "#e67e22")
        self.assertIs(entry.new_value["show_on_dashboard"], True)

    def test_changing_the_colour_and_the_switch_is_audited_with_old_and_new_values(self):
        fleet_type = FleetType.objects.create(name="Roam", short_name="RM", point_weight=Decimal("1.50"), sort_order=3)
        self.client.post(self.edit(fleet_type), payload(color_choice="theme-4", is_active="on"))
        fleet_type.refresh_from_db()
        self.assertEqual((fleet_type.color, fleet_type.show_on_dashboard), ("theme-4", False))
        entry = AuditLog.objects.filter(action="configuration.update", object_id=str(fleet_type.pk)).latest("pk")
        self.assertEqual((entry.old_value["color"], entry.new_value["color"]), ("", "theme-4"))
        self.assertEqual(
            (entry.old_value["show_on_dashboard"], entry.new_value["show_on_dashboard"]), (True, False)
        )

    def test_a_rejected_colour_saves_and_audits_nothing_and_the_page_says_why(self):
        response = self.client.post(self.add, payload(color_choice="custom", custom_color="red; x"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(FleetType.objects.exists())
        self.assertFalse(AuditLog.objects.filter(action="configuration.create").exists())
        self.assertIn("custom_color", response.context["form"].errors)
        self.assertContains(response, "hexadecimal")

    def test_a_poor_contrast_colour_is_saved_with_a_warning_message(self):
        response = self.client.post(self.add, payload(color_choice="custom", custom_color="#ffffff"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(FleetType.objects.get().color, "#ffffff")
        self.assertTrue(any("light theme" in str(m) for m in get_messages(response.wsgi_request)))

    def test_the_edit_page_of_a_type_shows_its_colour(self):
        fleet_type = FleetType.objects.create(name="Roam", color="#e67e22")
        html = self.client.get(self.edit(fleet_type)).content.decode()
        self.assertIn('value="#e67e22"', html)
        self.assertRegex(html, r'<option value="custom" selected>')

    def test_the_list_shows_the_colour_and_the_dashboard_switch(self):
        FleetType.objects.create(name="Roam", color="#e67e22", show_on_dashboard=True)
        FleetType.objects.create(name="Auto", show_on_dashboard=False)
        FleetType.objects.create(name="Themed", color="theme-1")
        html = self.client.get(reverse("fleetops:configuration_list", args=["fleet-types"])).content.decode()
        self.assertIn("--fo-dot: #e67e22", html)
        self.assertIn(">Automatic<", html)
        self.assertIn(">Teal<", html)
        self.assertIn("On dashboard", html)

    def test_only_a_configuration_manager_can_change_colours(self):
        user = f.create_user(perms=f.MEMBER_PERMS)
        self.client.force_login(user)
        response = self.client.post(self.add, payload(color_choice="theme-1"))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(FleetType.objects.exists())


class DashboardCardsTests(TestCase):
    def setUp(self):
        self.pilot = f.member()
        self.types = [
            FleetType.objects.create(name=f"Type {letter}", sort_order=order)
            for letter, order in zip("ABCDEF", (10, 20, 30, 40, 50, 60))
        ]

    def names(self):
        return [card["fleet_type"].name for card in dashboard_metrics(self.pilot)["cards"]]

    def test_the_cards_are_the_types_with_the_switch_on_in_sort_order(self):
        FleetType.objects.filter(name__in=("Type A", "Type B")).update(show_on_dashboard=False)
        self.assertEqual(self.names(), ["Type C", "Type D", "Type E", "Type F"])

    def test_more_than_four_cards_are_shown_when_the_switch_is_on(self):
        self.assertEqual(self.names(), [f"Type {letter}" for letter in "ABCDEF"])

    def test_a_type_with_the_switch_off_has_no_card_and_a_new_one_is_on_by_default(self):
        FleetType.objects.filter(name="Type A").update(show_on_dashboard=False)
        self.assertNotIn("Type A", self.names())
        self.assertTrue(FleetType.objects.create(name="New").show_on_dashboard)

    def test_an_inactive_type_has_no_card_even_with_the_switch_on(self):
        FleetType.objects.filter(name="Type A").update(is_active=False)
        self.assertNotIn("Type A", self.names())

    def test_the_order_is_sort_order_then_name(self):
        FleetType.objects.filter(name="Type F").update(sort_order=10)
        FleetType.objects.filter(name="Type A").update(sort_order=10)
        self.assertEqual(self.names()[:3], ["Type A", "Type F", "Type B"])

    def test_no_type_with_the_switch_on_gives_no_cards(self):
        FleetType.objects.update(show_on_dashboard=False)
        self.assertEqual(self.names(), [])

    def test_the_dashboard_page_draws_each_card_with_the_colour_of_its_type_and_wraps(self):
        FleetType.objects.filter(name="Type A").update(color="#e67e22")
        FleetType.objects.filter(name="Type B").update(color="theme-6")
        self.client.force_login(f.create_user(perms=f.MEMBER_PERMS))
        response = self.client.get(reverse("fleetops:dashboard"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        dots = re.findall(r'<span class="fo-metric-dot" style="--fo-dot: ([^"]+)"', html)
        self.assertEqual(len(dots), 6)
        self.assertEqual(dots[0], "#e67e22")
        self.assertEqual(dots[1], "var(--fo-type-6)")
        self.assertEqual(dots[2], f"var(--fo-type-{self.types[2].pk % 6 + 1})")
        self.assertEqual(html.count('class="col-6 col-xl-3"'), 6)


class TemplatesUseTheColourTests(SimpleTestCase):
    def test_no_template_builds_a_type_colour_from_the_dot_number_alone(self):
        # A custom colour has the dot number 0, so a template that writes var(--fo-type-{{ ...|type_tone }}) would
        # lose it. Colours are written with type_color, type_dot_style or type_pill.
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "fleetops" / "templates"
        offenders = [
            str(path.relative_to(root))
            for path in root.rglob("*.html")
            if re.search(r"var\(--fo-type-\{\{[^}]*type_tone", path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_the_statistics_label_and_the_member_bar_carry_a_custom_colour(self):
        root = __import__("pathlib").Path(__file__).resolve().parent.parent / "fleetops" / "templates" / "fleetops"
        labels = (root / "all_corporation_statistics.html").read_text(encoding="utf-8")
        bars = (root / "statistics" / "member_detail.html").read_text(encoding="utf-8")
        self.assertIn("fleet_type|type_dot_style", labels)
        self.assertIn("fleet_type|type_color", bars)


class FleetTypeColourReviewTests(TestCase):
    def setUp(self):
        self.admin = f.create_user(perms=CONFIG_PERMS)
        self.client.force_login(self.admin)

    def test_the_model_refuses_colours_that_are_not_stored_form(self):
        from django.core.exceptions import ValidationError

        for bad in ("red;x:", "theme-9", "#ABC", "#E67E22"):
            with self.assertRaises(ValidationError, msg=bad):
                FleetType(name="X", short_name="X", color=bad).full_clean()
        for good in ("", "theme-3", "#e67e22"):
            FleetType(name="X", short_name="X", color=good).full_clean()

    def test_the_django_admin_refuses_an_invalid_colour(self):
        from django.contrib import admin

        model_admin = admin.site._registry[FleetType]
        form_class = model_admin.get_form(None)
        data = {"name": "Adm", "short_name": "AD", "point_weight": "1", "sort_order": "0", "color": "#ABC"}
        form = form_class(data)
        form.is_valid()
        self.assertIn("color", form.errors)
        data["color"] = "#abcdef"
        self.assertNotIn("color", form_class(data).errors)

    def test_the_preview_automatic_tone_ignores_the_stored_colour(self):
        for color in ("", "theme-2", "#e67e22"):
            obj = FleetType.objects.create(name="T" + color, short_name="T", color=color)
            self.assertEqual(ui.type_auto_tone(obj), obj.pk % 6 + 1)
            html = self.client.get(reverse("fleetops:configuration_edit", args=["fleet-types", obj.pk])).content.decode()
            self.assertIn('data-auto-tone="%d"' % (obj.pk % 6 + 1), html)
        self.assertEqual(ui.type_auto_tone(FleetType()), 0)
        html = self.client.get(reverse("fleetops:configuration_add", args=["fleet-types"])).content.decode()
        self.assertIn('data-auto-tone="0"', html)
