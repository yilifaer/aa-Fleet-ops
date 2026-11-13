# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fixes found by the re-test of 0.3.0b3: the dead-end warning after a save, the ping cooldown numbers, the hint of the
MOTD template page, the custom doctrine name as a way around restricted doctrines, one audit entry per refused scheduled
start, the unused class of custom colours and the saves that change nothing."""

import uuid
from datetime import timedelta
from unittest import mock

from allianceauth.notifications.models import Notification
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation

from fleetops.forms import ScheduledFleetForm, StartFleetForm
from fleetops.forms_groups import SendPingForm
from fleetops.models import (
    AuditLog,
    FleetOperation,
    FleetStartPreset,
    MessageTemplate,
    SrpSettings,
    StandalonePing,
)
from fleetops.services import doctrine_access
from fleetops.services.operations import ping_cooldown_problem
from fleetops.services.presets import PresetRefused, load_preset, save_preset
from fleetops.services.scheduling import start_scheduled_fleet

from . import factories as f
from . import test_doctrine_access as da
from .test_configuration_audit import settings_payload
from .test_scheduling import SchedulingTestBase
from .test_srp_config import CONFIG_PERMS, settings_post


def flashed(response):
    return [(message.level_tag, str(message)) for message in get_messages(response.wsgi_request)]


class DeadEndWarningAfterSaveTests(TestCase):
    def test_a_save_that_leaves_the_dead_end_in_place_ends_with_the_warning(self):
        admin = f.create_user("b4_dead_end_admin", perms=CONFIG_PERMS + f.SRP_MEMBER_PERMS)
        self.client.force_login(admin)
        f.srp_settings(out_of_fleet_category_required=True, claim_deadline_days=7)
        url = reverse("fleetops:configuration_list", args=["srp"])

        response = self.client.post(
            url, settings_post(out_of_fleet_category_required="on", claim_deadline_days="30")
        )

        self.assertEqual(response.status_code, 302)
        warnings = [text for level, text in flashed(response) if level == "warning"]
        self.assertTrue(any("Members cannot submit a loss outside a fleet right now" in text for text in warnings))
        # The same text as the warning on the settings page.
        self.assertContains(self.client.get(url), warnings[0].split(",")[0][:40])

    def test_the_warning_is_not_shown_twice_when_the_redirect_lands_on_the_srp_page(self):
        admin = f.create_user("b4_dead_end_twice", perms=CONFIG_PERMS + f.SRP_MEMBER_PERMS)
        self.client.force_login(admin)
        f.srp_settings(out_of_fleet_category_required=True, claim_deadline_days=7)
        url = reverse("fleetops:configuration_list", args=["srp"])

        with mock.patch("fleetops.views_config.srp_policy.outdated_pending_claim_count", return_value=2):
            response = self.client.post(
                url, settings_post(out_of_fleet_category_required="on", claim_deadline_days="30")
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], url)
        self.assertFalse([text for level, text in flashed(response) if "Members cannot submit" in text])
        self.assertContains(self.client.get(url), 'id="srp-category-dead-end"')

    def test_a_working_configuration_gets_no_such_warning(self):
        admin = f.create_user("b4_ok_admin", perms=CONFIG_PERMS + f.SRP_MEMBER_PERMS)
        self.client.force_login(admin)
        f.srp_settings(out_of_fleet_category_required=False)

        response = self.client.post(self.url_srp(), settings_post(claim_deadline_days="30"))

        self.assertFalse([text for level, text in flashed(response) if "Members cannot submit" in text])

    @staticmethod
    def url_srp():
        return reverse("fleetops:configuration_list", args=["srp"])


class PingCooldownNumbersTests(TestCase):
    def test_the_two_numbers_never_contradict_each_other(self):
        fc = f.create_user("b4_cool_fc", perms=f.FC_PERMS)
        channel = f.ping_channel("RcCool")
        now = timezone.now()
        ping = f.standalone_ping(sender=fc, channel=channel, status=StandalonePing.Status.SENT)
        StandalonePing.objects.filter(pk=ping.pk).update(created_at=now)

        # Elapsed time is rounded down (1 to 59), the wait up and never shorter than what remains.
        expected = {0.2: (1, 60), 1.0: (1, 59), 59.5: (59, 1), 59.9: (59, 1)}
        for elapsed, (seconds, wait) in expected.items():
            with self.subTest(elapsed=elapsed):
                text = ping_cooldown_problem(channel, now=now + timedelta(seconds=elapsed))
                self.assertIn(f"{seconds} second", text)
                self.assertTrue(text.endswith(f"Wait {wait} second{'' if wait == 1 else 's'}."), text)
                self.assertNotIn("60 seconds ago", text)
                self.assertGreaterEqual(seconds + wait, 60)


class TemplateHintTests(TestCase):
    def setUp(self):
        self.admin = f.create_user("b4_hint_admin", perms=f.MEMBER_PERMS + ["fleetops.manage_configuration"])
        self.client.force_login(self.admin)

    def page(self, template_type):
        template = MessageTemplate.objects.create(name=f"Own {template_type}", template_type=template_type, content="x")
        return self.client.get(reverse("fleetops:configuration_edit", args=["templates", template.pk]))

    def test_a_ping_template_page_has_no_visible_hint(self):
        page = self.page(MessageTemplate.TemplateType.PING)
        # The text is only the data of the script, not a visible line under the field.
        self.assertNotContains(page, '<div class="form-text">MOTD templates:')

    def test_a_standalone_template_page_has_no_visible_hint(self):
        page = self.page(MessageTemplate.TemplateType.STANDALONE)
        self.assertNotContains(page, '<div class="form-text">MOTD templates:')

    def test_a_motd_template_page_shows_the_hint(self):
        page = self.page(MessageTemplate.TemplateType.MOTD)
        self.assertContains(page, '<div class="form-text">MOTD templates:')

    def test_a_new_template_shows_the_hint_until_the_type_is_known(self):
        page = self.client.get(reverse("fleetops:configuration_add", args=["templates"]))
        self.assertContains(page, '<div class="form-text">MOTD templates:')

    def test_the_page_script_follows_the_type(self):
        page = self.page(MessageTemplate.TemplateType.PING)
        self.assertContains(page, 'select[name="template_type"]')
        self.assertContains(page, "data-motd-hint")


def names_shown(testcase, rows=da.BOTH):
    """The names of the doctrines of the doctrine app that the typed-name check compares with."""
    patcher = mock.patch(
        "fleetops.services.doctrine_access._all_doctrines", side_effect=lambda rows=rows: list(rows)
    )
    patcher.start()
    testcase.addCleanup(patcher.stop)


class RestrictedNameBase(da.DoctrineAccessBase):
    """A restricted flagship doctrine, a custom name open to everybody and an FC outside the group."""

    def setUp(self):
        super().setUp()
        names_shown(self)
        da.restrict(da.FLAGSHIP, self.caplead)


class NameRuleTests(RestrictedNameBase):
    def refused(self, user, name, source="custom"):
        return doctrine_access.refusal(user, {"doctrine_source": source, "doctrine_name": name})

    def test_the_name_of_a_restricted_doctrine_is_refused(self):
        self.assertEqual(self.refused(self.fc, "Flagship Doctrine"), da.NOT_ALLOWED)

    def test_case_and_white_space_do_not_matter(self):
        for name in ("flagship doctrine", "  FLAGSHIP   Doctrine ", "Flagship\tDoctrine"):
            with self.subTest(name=name):
                self.assertEqual(self.refused(self.fc, name), da.NOT_ALLOWED)

    def test_markup_zero_width_and_full_width_variants_are_refused(self):
        for name in (
            "Flagship<b></b> Doctrine",
            "<i>Flagship Doctrine</i>",
            "Flagship<br>Doctrine",
            "Flagship\u200b Doctrine",
            "Fl\u2060agship Doctrine",
            "\uff26lagship Doctrine",
            "\uff26\uff4c\uff41\uff47\uff53\uff48\uff49\uff50\u3000Doctrine",
        ):
            with self.subTest(name=name):
                self.assertEqual(self.refused(self.fc, name), da.NOT_ALLOWED)
        self.assertEqual(doctrine_access.normalize_name("Cap<b></b>lead"), "caplead")

    def test_a_running_fleet_rename_with_markup_is_refused(self):
        operation = f.create_operation(self.fc, doctrine_name="Subcap Doctrine", doctrine_source=da.SOURCE, doctrine_external_id="13")
        self.client.force_login(self.fc)
        data = {
            "fleet_type": operation.fleet_type_id,
            "doctrine_name": "<i>Flagship</i> Doctrine",
            "formup": operation.formup,
            "additional_message": "",
            "started_at": operation.started_at.strftime("%Y-%m-%dT%H:%M"),
        }

        response = self.client.post(reverse("fleetops:edit_operation", args=[operation.uuid]), data)

        self.assertContains(response, da.NOT_ALLOWED)

    def test_it_is_refused_with_no_source_too(self):
        self.assertEqual(self.refused(self.fc, "Flagship Doctrine", source="none"), da.NOT_ALLOWED)

    def test_another_name_and_the_name_of_an_open_doctrine_are_accepted(self):
        self.assertEqual(self.refused(self.fc, "Flagship Doctrine 2"), "")
        self.assertEqual(self.refused(self.fc, "Subcap Doctrine"), "")

    def test_a_member_of_the_group_and_administrators_may(self):
        self.assertEqual(self.refused(self.join(self.fc, self.caplead), "Flagship Doctrine"), "")
        admin = f.create_user("b4_name_admin", perms=da.ADMIN_PERMS)
        self.assertEqual(self.refused(admin, "Flagship Doctrine"), "")
        superuser = f.create_user("b4_name_su")
        superuser.is_superuser = True
        superuser.save()
        self.assertEqual(self.refused(da.fresh(superuser), "Flagship Doctrine"), "")

    def test_a_doctrine_that_no_longer_exists_does_not_count(self):
        # The restriction row stays, but the doctrine left the doctrine app.
        names_shown(self, [da.SUBCAP])
        self.assertEqual(self.refused(self.fc, "Flagship Doctrine"), "")

    def test_a_doctrine_without_groups_does_not_count(self):
        da.restrict(da.FLAGSHIP)
        self.assertEqual(self.refused(self.fc, "Flagship Doctrine"), "")


class NameEntryPointTests(RestrictedNameBase):
    def test_start_fleet(self):
        data = da.StartFormTests.data(self, custom_doctrine="flagship  doctrine")
        form = StartFleetForm(data, user=self.fc)
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["custom_doctrine"], [da.NOT_ALLOWED])

    def test_start_fleet_accepts_another_name(self):
        data = da.StartFormTests.data(self, custom_doctrine="Something Else")
        self.assertTrue(StartFleetForm(data, user=self.fc).is_valid())

    def test_the_start_service_refuses_it_too(self):
        from fleetops.services.operations import start_refusal

        data = {"doctrine_source": "custom", "doctrine_name": "Flagship Doctrine"}
        self.assertEqual(start_refusal(self.fc, data, full_mode=False), da.NOT_ALLOWED)

    def test_ping_without_a_fleet(self):
        form = SendPingForm(
            {
                "request_id": str(uuid.uuid4()),
                "ping_channel": str(f.ping_channel("RcPing", allow_here=True).pk),
                "ping_mentions": ["none"],
                "additional_message": "Form up",
                "custom_doctrine": "Flagship Doctrine",
            },
            user=self.fc,
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["custom_doctrine"], [da.NOT_ALLOWED])

    def test_running_fleet_name_change(self):
        operation = f.create_operation(self.fc, doctrine_name="Subcap Doctrine", doctrine_source=da.SOURCE, doctrine_external_id="13")
        self.client.force_login(self.fc)
        data = {
            "fleet_type": operation.fleet_type_id,
            "doctrine_name": "Flagship Doctrine",
            "formup": operation.formup,
            "additional_message": "",
            "started_at": operation.started_at.strftime("%Y-%m-%dT%H:%M"),
        }

        response = self.client.post(reverse("fleetops:edit_operation", args=[operation.uuid]), data)

        self.assertContains(response, da.NOT_ALLOWED)
        operation.refresh_from_db()
        self.assertEqual(operation.doctrine_name, "Subcap Doctrine")

    def test_preset_save(self):
        data = da.PresetTests.data(self, custom_doctrine="Flagship Doctrine")
        with self.assertRaises(PresetRefused) as caught:
            save_preset(self.fc, "Typed", data)
        self.assertEqual(str(caught.exception), da.NOT_ALLOWED)
        self.assertFalse(FleetStartPreset.objects.exists())

    def test_preset_load_leaves_the_name_out_and_says_why(self):
        preset = FleetStartPreset.objects.create(
            owner=self.fc, name="Stored", values={"fields": {"doctrine_choice": "", "custom_doctrine": "Flagship Doctrine"}}
        )

        initial, warnings = load_preset(preset, StartFleetForm, self.fc)

        self.assertEqual(initial.get("custom_doctrine", ""), "")
        self.assertTrue(any(da.NOT_ALLOWED in warning for warning in warnings), warnings)

    def test_preset_load_does_not_turn_a_gone_restricted_choice_into_its_name(self):
        # A doctrine value that is no longer offered but whose name is a restricted doctrine's name.
        preset = FleetStartPreset.objects.create(
            owner=self.fc,
            name="Stored",
            values={
                "fields": {"doctrine_choice": "fittings:fittings.doctrine|99|Flagship Doctrine", "custom_doctrine": ""},
                "labels": {"doctrine_choice": "Flagship Doctrine"},
            },
        )

        initial, _warnings = load_preset(preset, StartFleetForm, self.fc)

        self.assertEqual(initial.get("custom_doctrine", ""), "")


class GroupFleetNameTests(da.DoctrineAccessBase):
    def setUp(self):
        super().setUp()
        names_shown(self)

    def test_a_group_leader_cannot_type_the_name_of_a_restricted_doctrine(self):
        helper = da.GroupFleetTests
        group = f.aa_group("Example Fleet Group")
        leader = f.create_user("b4_lead", perms=f.MEMBER_PERMS, main_name="Group Leader")
        group.authgroup.group_leaders.add(leader.pk)
        f.group_access(group, "fleets", types=[self.fleet_type])
        leader = da.fresh(leader)
        da.restrict(da.FLAGSHIP, self.caplead)
        self.group, self.leader = group, leader

        form = StartFleetForm(helper.data(self, custom_doctrine="Flagship Doctrine"), user=leader)

        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["custom_doctrine"], [da.NOT_ALLOWED])


class ScheduledNameTests(SchedulingTestBase):
    def setUp(self):
        super().setUp()
        for patcher in da.doctrines_shown():
            patcher.start()
            self.addCleanup(patcher.stop)
        self.caplead = f.aa_group("Example Caplead")
        self.fc = da.fresh(self.fc)
        names_shown(self)
        da.restrict(da.FLAGSHIP, self.caplead)

    def test_saving_a_scheduled_fleet_with_the_name_is_refused(self):
        response = self.post_schedule(custom_doctrine="Flagship Doctrine")

        self.assertContains(response, da.NOT_ALLOWED)
        self.assertFalse(FleetOperation.objects.exists())

    def test_starting_a_scheduled_fleet_with_the_name_is_refused(self):
        operation = self.scheduled(doctrine_source="custom", doctrine_name="Flagship Doctrine")

        with self.assertRaises(PermissionError):
            start_scheduled_fleet(operation, user=self.fc, character_id=self.fc_char.character_id, send_ping=False)

        operation.refresh_from_db()
        self.assertEqual(operation.status, FleetOperation.Status.SCHEDULED)
        entry = AuditLog.objects.get(action="fleet.schedule_start_refused")
        self.assertEqual(entry.new_value["reason"], "doctrine")


class SettingsPageWarningTests(da.DoctrineAccessBase):
    def setUp(self):
        super().setUp()
        self.admin = f.create_user("b4_settings_admin", perms=da.ADMIN_PERMS)
        self.client.force_login(self.admin)
        self.url = reverse("fleetops:doctrine_orders")

    def test_a_restricted_doctrine_with_an_open_custom_name_shows_the_warning(self):
        da.restrict(da.FLAGSHIP, self.caplead)
        page = self.client.get(self.url)
        self.assertContains(page, 'id="doctrine-custom-open-warning"')
        self.assertContains(page, "Only the chosen groups")

    def test_no_warning_without_a_restricted_doctrine(self):
        self.assertNotContains(self.client.get(self.url), "doctrine-custom-open-warning")

    def test_no_warning_when_the_custom_name_is_limited_to_groups(self):
        da.restrict(da.FLAGSHIP, self.caplead)
        da.restrict_custom(self.fcs)
        self.assertNotContains(self.client.get(self.url), "doctrine-custom-open-warning")

    def test_a_restriction_of_a_doctrine_that_is_gone_does_not_count(self):
        da.restrict(da.FLAGSHIP, self.caplead)
        for patcher in da.doctrines_shown([da.SUBCAP]):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertNotContains(self.client.get(self.url), "doctrine-custom-open-warning")

    def test_the_warning_is_translated(self):
        da.restrict(da.FLAGSHIP, self.caplead)
        with translation.override("zh-hans"):
            page = self.client.get(self.url, HTTP_ACCEPT_LANGUAGE="zh-hans")
        self.assertContains(page, "自定义建制名")
        self.assertNotContains(page, "can still use a custom doctrine name")


class RefusedStartIsReportedOnceTests(SchedulingTestBase):
    def setUp(self):
        super().setUp()
        for patcher in da.doctrines_shown():
            patcher.start()
            self.addCleanup(patcher.stop)
        self.caplead = f.aa_group("Example Caplead")
        self.fc = da.fresh(self.fc)
        da.restrict(da.FLAGSHIP, self.caplead)
        self.operation = self.scheduled(
            doctrine_source=da.SOURCE, doctrine_external_id="12", doctrine_name="Flagship Doctrine"
        )

    def press(self):
        with self.assertRaises(PermissionError) as caught:
            start_scheduled_fleet(
                self.operation, user=da.fresh(self.fc), character_id=self.fc_char.character_id, send_ping=False
            )
        return str(caught.exception)

    def counts(self):
        return (
            AuditLog.objects.filter(action="fleet.schedule_start_refused").count(),
            Notification.objects.filter(user=self.fc).count(),
        )

    def test_repeated_presses_write_one_entry_and_one_notification_but_always_answer(self):
        first = self.press()
        second = self.press()
        third = self.press()

        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        self.assertIn("not started", first)

    def test_an_edit_of_the_fleet_allows_a_new_entry(self):
        self.press()
        # The fleet is saved after the first refusal (the clock of the test does not move between the steps).
        AuditLog.objects.update(created_at=timezone.now() - timedelta(hours=1))
        FleetOperation.objects.filter(pk=self.operation.pk).update(updated_at=timezone.now())
        self.operation.refresh_from_db()

        self.press()
        self.press()

        self.assertEqual(self.counts(), (2, 2))

    def test_a_changed_doctrine_is_a_new_refusal(self):
        self.press()
        da.restrict(da.SUBCAP, self.caplead)
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            doctrine_external_id="13", doctrine_name="Subcap Doctrine"
        )
        self.operation.refresh_from_db()

        self.press()

        self.assertEqual(self.counts(), (2, 2))

    def test_the_custom_name_reason_is_deduplicated_too(self):
        da.restrict_custom(f.aa_group("Example Typing Group"))
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            doctrine_source="custom", doctrine_external_id="", doctrine_name="Typed"
        )
        self.operation.refresh_from_db()

        self.press()
        self.press()

        self.assertEqual(self.counts(), (1, 1))

    def test_a_failing_notification_does_not_make_the_next_press_write_again(self):
        with mock.patch("fleetops.services.scheduling.notify.warning", side_effect=RuntimeError("down")):
            self.press()
        self.press()
        self.assertEqual(AuditLog.objects.filter(action="fleet.schedule_start_refused").count(), 1)


class CustomColourClassTests(TestCase):
    def pill(self, fleet_type):
        """The label of a fleet type as the statistics page of all corporations writes it (its own template text)."""
        import pathlib
        import re

        from django.template import Context, Template

        source = (
            pathlib.Path(__file__).resolve().parent.parent
            / "fleetops/templates/fleetops/all_corporation_statistics.html"
        ).read_text(encoding="utf-8")
        match = re.search(r"\{% if pap %\}(<span class=\"fo-pill fo-pill-type.*?</span>)\{% endif %\}", source)
        self.assertIsNotNone(match)
        return Template("{% load fleetops_ui %}" + match.group(1)).render(
            Context({"fleet_type": fleet_type, "pap": 3})
        )

    def test_a_custom_colour_emits_no_tone_class(self):
        custom = f.fleet_type("CustomColour", "1.00")
        custom.color = "#e67e22"
        custom.save()

        html = self.pill(custom)

        self.assertNotIn("fo-type-", html)
        self.assertIn("--fo-dot: #e67e22", html)

    def test_a_type_without_a_custom_colour_keeps_its_tone_class(self):
        plain = f.fleet_type("PlainType", "1.00")

        self.assertRegex(self.pill(plain), r"fo-type-[1-6]")


class SavesThatChangeNothingTests(TestCase):
    def setUp(self):
        self.admin = f.create_user("b4_nochange_admin", perms=f.MEMBER_PERMS + ["fleetops.manage_configuration"])
        self.client.force_login(self.admin)

    def updates(self):
        return AuditLog.objects.filter(action="configuration.update").count()

    def post_twice(self, url, data):
        first = self.client.post(url, data)
        self.assertEqual(first.status_code, 302, first.content[:300])
        self.client.get(first["Location"])  # shows (and so clears) the messages of the first save
        before = self.updates()
        second = self.client.post(url, data)
        self.assertEqual(second.status_code, 302)
        return before, second

    def test_the_general_settings(self):
        url = reverse("fleetops:configuration_settings")

        before, second = self.post_twice(url, settings_payload(tracking_interval="300"))

        self.assertEqual(self.updates(), before)
        self.assertIn(("info", "Nothing was changed."), flashed(second))
        self.assertNotIn("FleetOps settings updated.", [text for _level, text in flashed(second)])

    def test_a_real_change_of_the_general_settings_is_still_audited(self):
        url = reverse("fleetops:configuration_settings")
        self.client.post(url, settings_payload(tracking_interval="300"))
        before = self.updates()

        response = self.client.post(url, settings_payload(tracking_interval="120"))

        self.assertEqual(self.updates(), before + 1)
        self.assertIn(("success", "FleetOps settings updated."), flashed(response))

    def test_the_srp_settings(self):
        from fleetops.forms_config import SrpSettingsForm

        url = reverse("fleetops:configuration_list", args=["srp"])
        self.client.post(url, settings_post(claim_deadline_days="30"))
        before = self.updates()
        # What a browser posts: every field of the form with the value it shows.
        form = SrpSettingsForm(instance=SrpSettings.get_solo())
        data = {}
        for name, value in form.initial.items():
            if value is True:
                data[name] = "on"
            elif value is not None and value is not False:
                data[name] = value

        second = self.client.post(url, data)

        self.assertEqual(second.status_code, 302, getattr(second, "context", None) and second.context["form"].errors)
        self.assertEqual(self.updates(), before)
        self.assertIn(("info", "Nothing was changed."), flashed(second))
        self.assertEqual(SrpSettings.get_solo().claim_deadline_days, 30)

    def test_a_real_change_of_the_srp_settings_is_still_audited(self):
        url = reverse("fleetops:configuration_list", args=["srp"])
        self.client.post(url, settings_post(claim_deadline_days="30"))
        before = self.updates()

        response = self.client.post(url, settings_post(claim_deadline_days="31"))

        self.assertEqual(self.updates(), before + 1)
        self.assertIn(("success", "SRP settings updated."), flashed(response))

    def test_a_list_page_edit_form(self):
        template = MessageTemplate.objects.create(
            name="Own MOTD", template_type=MessageTemplate.TemplateType.MOTD, content="{{ fc }}", is_active=True
        )
        url = reverse("fleetops:configuration_edit", args=["templates", template.pk])
        data = {"name": "Own MOTD", "template_type": "motd", "content": "{{ fc }}", "is_active": "on"}

        before = self.updates()
        response = self.client.post(url, data)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.updates(), before)
        self.assertIn(("info", "Nothing was changed."), flashed(response))

    def test_a_list_page_edit_that_changes_something_is_audited(self):
        template = MessageTemplate.objects.create(
            name="Own MOTD", template_type=MessageTemplate.TemplateType.MOTD, content="{{ fc }}", is_active=True
        )
        url = reverse("fleetops:configuration_edit", args=["templates", template.pk])
        data = {"name": "Own MOTD 2", "template_type": "motd", "content": "{{ fc }}", "is_active": "on"}

        before = self.updates()
        response = self.client.post(url, data)

        self.assertEqual(self.updates(), before + 1)
        self.assertEqual([level for level, _text in flashed(response)], ["success"])

    def test_the_message_is_translated(self):
        template = MessageTemplate.objects.create(
            name="Own MOTD", template_type=MessageTemplate.TemplateType.MOTD, content="x", is_active=True
        )
        url = reverse("fleetops:configuration_edit", args=["templates", template.pk])
        data = {"name": "Own MOTD", "template_type": "motd", "content": "x", "is_active": "on"}

        response = self.client.post(url, data, HTTP_ACCEPT_LANGUAGE="zh-hans")

        self.assertIn("没有任何改动。", " ".join(text for _level, text in flashed(response)))
