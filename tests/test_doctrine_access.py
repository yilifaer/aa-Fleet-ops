# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Who may ping which doctrine and who may use a custom doctrine name: the rule, every entry point (the dropdown and
the server check of a forged id), scheduled fleets that lose the right before they start, presets, and the
administration page that sets it all."""

import uuid
from unittest import mock

from allianceauth.notifications.models import Notification
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from fleetops.forms import ScheduledFleetForm, StartFleetForm
from fleetops.forms_groups import SendPingForm
from fleetops.models import (
    AuditLog,
    DoctrineAccess,
    DoctrineSettings,
    FleetOperation,
    FleetStartPreset,
    StandalonePing,
)
from fleetops.providers.doctrines import DoctrineChoice
from fleetops.services import doctrine_access, standalone_pings
from fleetops.services.operations import start_refusal
from fleetops.services.presets import PresetRefused, load_preset, save_preset
from fleetops.services.scheduling import schedule_fleet, start_scheduled_fleet

from . import factories as f
from .test_scheduling import SchedulingTestBase, in_two_hours

SOURCE = "fittings:fittings.doctrine"
FLAGSHIP = DoctrineChoice(external_id="12", name="Flagship Doctrine", source=SOURCE)
SUBCAP = DoctrineChoice(external_id="13", name="Subcap Doctrine", source=SOURCE)
BOTH = [FLAGSHIP, SUBCAP]
FLAGSHIP_VALUE = f"{SOURCE}|12|Flagship Doctrine"
SUBCAP_VALUE = f"{SOURCE}|13|Subcap Doctrine"
NOT_ALLOWED = "You may not ping this doctrine, ask an administrator."
CUSTOM_NOT_ALLOWED = "You may not use a custom doctrine name, ask an administrator."
ADMIN_PERMS = f.FC_PERMS + ["fleetops.manage_configuration"]


def doctrines_shown(rows=BOTH):
    """Every entry point reads the doctrines of the doctrine app through its own import of ``get_doctrines``."""
    patches = [
        mock.patch(f"{module}.get_doctrines", side_effect=lambda user=None, rows=rows: list(rows))
        for module in ("fleetops.forms", "fleetops.forms_groups", "fleetops.views_doctrine_settings")
    ]
    return patches


def restrict(choice, *groups):
    row, _created = DoctrineAccess.objects.get_or_create(
        doctrine_source=choice.source, doctrine_external_id=choice.external_id
    )
    row.groups.set(groups)
    return row


def restrict_custom(*groups):
    row = DoctrineSettings.get_solo()
    row.custom_name_restricted = True
    row.save()
    row.custom_name_groups.set(groups)
    return row


def fresh(user):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.get(pk=user.pk)


def flashed(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class DoctrineAccessBase(TestCase):
    def setUp(self):
        super().setUp()
        for patcher in doctrines_shown():
            patcher.start()
            self.addCleanup(patcher.stop)
        self.caplead = f.aa_group("Example Caplead")
        self.fcs = f.aa_group("Example FC")
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Example FC")
        self.fleet_type = f.fleet_type("StratOp", "1.00")
        self.channel = f.ping_channel("STR", allow_everyone=True, allow_here=True)

    def join(self, user, *groups):
        user.groups.add(*groups)
        return fresh(user)


class RuleTests(DoctrineAccessBase):
    def test_a_doctrine_without_a_row_is_open(self):
        self.assertTrue(doctrine_access.can_use_doctrine(self.fc, SOURCE, "12"))

    def test_a_row_without_groups_is_open(self):
        restrict(FLAGSHIP)
        self.assertTrue(doctrine_access.can_use_doctrine(self.fc, SOURCE, "12"))

    def test_a_restricted_doctrine_is_closed_to_a_user_outside_its_groups(self):
        restrict(FLAGSHIP, self.caplead)
        self.assertFalse(doctrine_access.can_use_doctrine(self.fc, SOURCE, "12"))
        # The other doctrine has no row.
        self.assertTrue(doctrine_access.can_use_doctrine(self.fc, SOURCE, "13"))

    def test_a_member_of_an_allowed_group_may(self):
        restrict(FLAGSHIP, self.caplead)
        self.assertTrue(doctrine_access.can_use_doctrine(self.join(self.fc, self.caplead), SOURCE, "12"))

    def test_one_allowed_group_among_several_is_enough(self):
        restrict(FLAGSHIP, self.caplead, self.fcs)
        other = f.aa_group("Example Other")
        user = self.join(self.fc, other, self.fcs)
        self.assertTrue(doctrine_access.can_use_doctrine(user, SOURCE, "12"))

    def test_other_groups_do_not_help(self):
        restrict(FLAGSHIP, self.caplead)
        user = self.join(self.fc, self.fcs)
        self.assertFalse(doctrine_access.can_use_doctrine(user, SOURCE, "12"))

    def test_superusers_are_never_restricted(self):
        restrict(FLAGSHIP, self.caplead)
        root = f.create_user("root", perms=f.FC_PERMS)
        root.is_superuser = True
        root.save()
        self.assertTrue(doctrine_access.can_use_doctrine(fresh(root), SOURCE, "12"))

    def test_fleetops_administrators_are_never_restricted(self):
        restrict(FLAGSHIP, self.caplead)
        admin = f.create_user("admin", perms=ADMIN_PERMS)
        self.assertTrue(doctrine_access.can_use_doctrine(admin, SOURCE, "12"))

    def test_the_permission_of_the_configuration_decides_who_is_an_administrator(self):
        self.assertEqual(doctrine_access.MANAGE_CONFIGURATION, "fleetops.manage_configuration")

    def test_an_inactive_user_is_not_an_administrator(self):
        admin = f.create_user("gone", perms=ADMIN_PERMS)
        admin.is_active = False
        admin.save()
        self.assertFalse(doctrine_access.is_administrator(fresh(admin)))

    def test_a_typed_name_or_no_doctrine_is_not_a_doctrine(self):
        restrict(FLAGSHIP, self.caplead)
        for source in ("", "custom", "none", "manual"):
            with self.subTest(source=source):
                self.assertTrue(doctrine_access.can_use_doctrine(self.fc, source, ""))

    def test_a_row_of_a_doctrine_that_left_the_doctrine_app_still_applies_and_does_not_fail(self):
        restrict(DoctrineChoice(external_id="99", name="Gone", source=SOURCE), self.caplead)
        self.assertFalse(doctrine_access.can_use_doctrine(self.fc, SOURCE, "99"))
        self.assertEqual([c.name for c in doctrine_access.allowed_doctrines(self.fc, BOTH)], ["Flagship Doctrine", "Subcap Doctrine"])

    def test_filtering_does_not_add_a_query_per_doctrine(self):
        restrict(DoctrineChoice(external_id="1", name="D1", source=SOURCE), self.caplead)
        few = [DoctrineChoice(external_id=str(n), name=f"D{n}", source=SOURCE) for n in range(3)]
        many = [DoctrineChoice(external_id=str(n), name=f"D{n}", source=SOURCE) for n in range(60)]
        # The permission lookup of the user is cached after the first question; the rules cost the same for any number.
        doctrine_access.is_administrator(self.fc)
        with self.assertNumQueries(3):
            doctrine_access.Scope(self.fc).allowed(few)
        with self.assertNumQueries(3):
            doctrine_access.Scope(self.fc).allowed(many)

    def test_the_custom_name_is_open_by_default(self):
        self.assertTrue(doctrine_access.can_use_custom_name(self.fc))

    def test_the_custom_name_can_be_limited_to_groups(self):
        restrict_custom(self.fcs)
        self.assertFalse(doctrine_access.can_use_custom_name(self.fc))
        self.assertTrue(doctrine_access.can_use_custom_name(self.join(self.fc, self.fcs)))

    def test_the_custom_name_switched_off_again_is_open_even_with_groups_stored(self):
        row = restrict_custom(self.fcs)
        row.custom_name_restricted = False
        row.save()
        self.assertTrue(doctrine_access.can_use_custom_name(self.fc))

    def test_administrators_may_always_use_the_custom_name(self):
        restrict_custom(self.fcs)
        self.assertTrue(doctrine_access.can_use_custom_name(f.create_user("admin", perms=ADMIN_PERMS)))

    def test_a_limit_without_any_group_leaves_the_custom_name_to_administrators(self):
        restrict_custom()
        self.assertFalse(doctrine_access.can_use_custom_name(self.fc))
        self.assertTrue(doctrine_access.can_use_custom_name(f.create_user("admin", perms=ADMIN_PERMS)))

    def test_the_models_have_no_default_permissions(self):
        self.assertEqual(DoctrineAccess._meta.default_permissions, ())
        self.assertEqual(DoctrineSettings._meta.default_permissions, ())


class StartFormTests(DoctrineAccessBase):
    def data(self, **extra):
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fleet_timing": "now",
            "fc_character_id": str(f.main_of(self.fc).character_id),
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Jita",
            "ping_channel": str(self.channel.pk),
            "ping_mentions": ["everyone"],
        }
        data.update(extra)
        return data

    def values(self, form):
        return [value for value, _label in form.fields["doctrine_choice"].choices]

    def test_the_dropdown_lists_only_the_doctrines_the_user_may_ping(self):
        restrict(FLAGSHIP, self.caplead)

        form = StartFleetForm(user=self.fc)

        self.assertEqual(self.values(form), ["", SUBCAP_VALUE])

    def test_the_dropdown_lists_the_doctrine_for_a_member_of_its_group(self):
        restrict(FLAGSHIP, self.caplead)

        form = StartFleetForm(user=self.join(self.fc, self.caplead))

        self.assertEqual(self.values(form), ["", FLAGSHIP_VALUE, SUBCAP_VALUE])

    def test_a_forged_id_is_refused_with_the_message_of_the_feature(self):
        restrict(FLAGSHIP, self.caplead)

        form = StartFleetForm(self.data(doctrine_choice=FLAGSHIP_VALUE), user=self.fc)

        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["doctrine_choice"], [NOT_ALLOWED])

    def test_a_forged_id_with_another_name_is_refused_too(self):
        restrict(FLAGSHIP, self.caplead)

        form = StartFleetForm(self.data(doctrine_choice=f"{SOURCE}|12|Something Else"), user=self.fc)

        self.assertEqual(form.errors["doctrine_choice"], [NOT_ALLOWED])

    def test_an_unknown_id_keeps_the_ordinary_message(self):
        form = StartFleetForm(self.data(doctrine_choice=f"{SOURCE}|777|Nothing"), user=self.fc)

        self.assertFalse(form.is_valid())
        self.assertNotIn(NOT_ALLOWED, form.errors["doctrine_choice"])

    def test_an_allowed_doctrine_is_accepted(self):
        restrict(FLAGSHIP, self.caplead)

        form = StartFleetForm(self.data(doctrine_choice=FLAGSHIP_VALUE), user=self.join(self.fc, self.caplead))

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["doctrine_external_id"], "12")

    def test_an_open_doctrine_is_accepted(self):
        restrict(FLAGSHIP, self.caplead)

        form = StartFleetForm(self.data(doctrine_choice=SUBCAP_VALUE), user=self.fc)

        self.assertTrue(form.is_valid(), form.errors)

    def test_the_refusal_is_translated(self):
        restrict(FLAGSHIP, self.caplead)
        with translation.override("zh-hans"):
            form = StartFleetForm(self.data(doctrine_choice=FLAGSHIP_VALUE), user=self.fc)
            form.is_valid()
            text = form.errors["doctrine_choice"][0]

        self.assertNotEqual(text, NOT_ALLOWED)
        self.assertIn("管理员", text)

    def test_administrators_see_and_may_use_every_doctrine(self):
        restrict(FLAGSHIP, self.caplead)
        admin = f.create_user("admin", perms=ADMIN_PERMS)

        form = StartFleetForm(user=admin)

        self.assertEqual(self.values(form), ["", FLAGSHIP_VALUE, SUBCAP_VALUE])

    def test_the_server_check_of_the_start_service_refuses_a_doctrine_that_skipped_the_form(self):
        restrict(FLAGSHIP, self.caplead)
        cleaned = {"fleet_type": self.fleet_type, "doctrine_source": SOURCE, "doctrine_external_id": "12"}

        self.assertEqual(start_refusal(self.fc, cleaned, full_mode=False), NOT_ALLOWED)

    def test_the_start_page_does_not_offer_the_restricted_doctrine(self):
        restrict(FLAGSHIP, self.caplead)
        self.client.force_login(self.fc)

        page = self.client.get(reverse("fleetops:start_fleet")).content.decode()

        self.assertNotIn("Flagship Doctrine", page)
        self.assertIn("Subcap Doctrine", page)

    def test_the_start_view_refuses_a_forged_id_and_starts_nothing(self):
        restrict(FLAGSHIP, self.caplead)
        self.client.force_login(self.fc)

        response = self.client.post(reverse("fleetops:start_fleet"), self.data(doctrine_choice=FLAGSHIP_VALUE))

        self.assertContains(response, NOT_ALLOWED)
        self.assertFalse(FleetOperation.objects.exists())


class CustomNameStartFormTests(StartFormTests):
    """The custom name setting on the start form, in both values."""

    def test_everybody_sees_none_custom_by_default(self):
        form = StartFleetForm(user=self.fc)

        self.assertIn("", self.values(form))
        self.assertTrue(form.custom_name_allowed)

    def test_a_user_outside_the_groups_does_not_get_none_custom_nor_the_text_box(self):
        restrict_custom(self.fcs)

        form = StartFleetForm(user=self.fc)

        # No blank "None / Custom" entry; the placeholder makes the user choose.
        self.assertEqual(self.values(form), ["", FLAGSHIP_VALUE, SUBCAP_VALUE])
        self.assertFalse(form.custom_name_allowed)
        self.client.force_login(self.fc)
        page = self.client.get(reverse("fleetops:start_fleet")).content.decode()
        self.assertNotIn("custom-doctrine-wrap", page)
        self.assertNotIn("None / Custom", page)

    def test_a_member_of_a_chosen_group_keeps_the_option(self):
        restrict_custom(self.fcs)

        form = StartFleetForm(user=self.join(self.fc, self.fcs))

        self.assertIn("", self.values(form))
        self.assertTrue(form.custom_name_allowed)

    def test_a_typed_name_is_refused_on_submit(self):
        restrict_custom(self.fcs)

        form = StartFleetForm(self.data(custom_doctrine="My Doctrine"), user=self.fc)

        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["doctrine_choice"], [CUSTOM_NOT_ALLOWED])

    def test_no_doctrine_at_all_is_refused_when_there_is_a_doctrine_to_choose(self):
        restrict_custom(self.fcs)

        form = StartFleetForm(self.data(), user=self.fc)

        self.assertFalse(form.is_valid())
        self.assertIn("Choose a doctrine from the list.", form.errors["doctrine_choice"])

    def test_a_real_doctrine_is_fine_for_a_user_who_may_not_type_a_name(self):
        restrict_custom(self.fcs)

        form = StartFleetForm(self.data(doctrine_choice=SUBCAP_VALUE), user=self.fc)

        self.assertTrue(form.is_valid(), form.errors)

    def test_a_member_of_a_chosen_group_may_type_a_name(self):
        restrict_custom(self.fcs)

        form = StartFleetForm(self.data(custom_doctrine="My Doctrine"), user=self.join(self.fc, self.fcs))

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["doctrine_source"], "custom")

    def test_administrators_are_not_limited(self):
        restrict_custom(self.fcs)
        admin = f.create_user("admin", perms=ADMIN_PERMS)
        data = self.data(custom_doctrine="My Doctrine", fc_character_id=str(f.main_of(admin).character_id))

        form = StartFleetForm(data, user=admin)

        self.assertTrue(form.is_valid(), form.errors)

    def test_nothing_is_lost_when_there_is_no_doctrine_to_choose(self):
        restrict_custom(self.fcs)
        for patcher in doctrines_shown([]):
            patcher.start()
            self.addCleanup(patcher.stop)

        form = StartFleetForm(self.data(), user=self.fc)

        self.assertIn("", self.values(form))
        self.assertTrue(form.is_valid(), form.errors)
        typed = StartFleetForm(self.data(custom_doctrine="My Doctrine"), user=self.fc)
        self.assertFalse(typed.is_valid())

    def test_the_service_check_refuses_a_typed_name(self):
        restrict_custom(self.fcs)
        cleaned = {"fleet_type": self.fleet_type, "doctrine_source": "custom", "doctrine_name": "Typed"}

        self.assertEqual(start_refusal(self.fc, cleaned, full_mode=False), CUSTOM_NOT_ALLOWED)

    def test_the_two_settings_do_not_depend_on_each_other(self):
        restrict(FLAGSHIP, self.caplead)
        restrict_custom(self.fcs)

        form = StartFleetForm(user=self.join(self.fc, self.fcs))

        self.assertEqual(self.values(form), ["", SUBCAP_VALUE])


class GroupFleetTests(DoctrineAccessBase):
    """A group leader who starts a group fleet is checked by their own Alliance Auth groups."""

    def setUp(self):
        super().setUp()
        self.group = f.aa_group("Example Fleet Group")
        self.leader = f.create_user("lead", perms=f.MEMBER_PERMS, main_name="Group Leader")
        self.group.authgroup.group_leaders.add(self.leader.pk)
        f.group_access(self.group, "fleets", types=[self.fleet_type])
        self.leader = fresh(self.leader)

    def data(self, **extra):
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fleet_timing": "now",
            "fleet_group": str(self.group.pk),
            "fc_character_id": str(f.main_of(self.leader).character_id),
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Jita",
            "ping_mentions": ["none"],
        }
        data.update(extra)
        return data

    def test_the_leader_without_an_allowed_group_does_not_get_the_doctrine(self):
        restrict(FLAGSHIP, self.caplead)

        form = StartFleetForm(user=self.leader)

        self.assertNotIn(FLAGSHIP_VALUE, [value for value, _l in form.fields["doctrine_choice"].choices])

    def test_a_forged_id_is_refused_for_a_group_fleet(self):
        restrict(FLAGSHIP, self.caplead)

        form = StartFleetForm(self.data(doctrine_choice=FLAGSHIP_VALUE), user=self.leader)

        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["doctrine_choice"], [NOT_ALLOWED])

    def test_the_leader_who_is_in_an_allowed_group_may_use_it(self):
        restrict(FLAGSHIP, self.caplead)
        leader = self.join(self.leader, self.caplead)

        form = StartFleetForm(self.data(doctrine_choice=FLAGSHIP_VALUE), user=leader)

        self.assertTrue(form.is_valid(), form.errors)

    def test_the_group_the_fleet_is_for_does_not_grant_the_doctrine(self):
        # The leader is only a leader of the group; the doctrine rule looks at the groups they belong to.
        restrict(FLAGSHIP, self.group)

        form = StartFleetForm(self.data(doctrine_choice=FLAGSHIP_VALUE), user=self.leader)

        self.assertFalse(form.is_valid())

    def test_a_typed_name_follows_the_custom_name_setting(self):
        restrict_custom(self.fcs)

        form = StartFleetForm(self.data(custom_doctrine="Typed"), user=self.leader)

        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["doctrine_choice"], [CUSTOM_NOT_ALLOWED])


class SendPingTests(DoctrineAccessBase):
    def setUp(self):
        super().setUp()
        self.channel = f.ping_channel("Alliance Pings", allow_here=True)

    def data(self, **extra):
        data = {
            "request_id": str(uuid.uuid4()),
            "ping_channel": str(self.channel.pk),
            "ping_mentions": ["none"],
            "additional_message": "Form up",
        }
        data.update(extra)
        return data

    def send(self, user, data):
        self.client.force_login(user)
        with mock.patch("fleetops.providers.pings.requests.post", return_value=mock.Mock(status_code=204, text="")):
            return self.client.post(reverse("fleetops:send_ping"), data)

    def test_the_dropdown_lists_only_the_allowed_doctrines(self):
        restrict(FLAGSHIP, self.caplead)

        form = SendPingForm(user=self.fc)

        self.assertEqual([v for v, _l in form.fields["doctrine_choice"].choices], ["", SUBCAP_VALUE])

    def test_a_forged_id_is_refused_and_nothing_is_posted(self):
        restrict(FLAGSHIP, self.caplead)

        response = self.send(self.fc, self.data(doctrine_choice=FLAGSHIP_VALUE))

        self.assertContains(response, NOT_ALLOWED)
        self.assertFalse(StandalonePing.objects.exists())

    def test_an_allowed_doctrine_is_sent(self):
        restrict(FLAGSHIP, self.caplead)

        self.send(self.join(self.fc, self.caplead), self.data(doctrine_choice=FLAGSHIP_VALUE))

        self.assertEqual(StandalonePing.objects.get().doctrine_external_id, "12")

    def test_the_preview_refuses_a_forged_id_too(self):
        restrict(FLAGSHIP, self.caplead)
        self.client.force_login(self.fc)

        response = self.client.post(reverse("fleetops:preview_ping"), self.data(doctrine_choice=FLAGSHIP_VALUE))

        self.assertFalse(response.json()["ok"])

    def test_the_service_refuses_a_doctrine_that_skipped_the_form(self):
        restrict(FLAGSHIP, self.caplead)
        cleaned = {
            "request_id": uuid.uuid4(),
            "ping_channel": self.channel,
            "doctrine_source": SOURCE,
            "doctrine_external_id": "12",
            "doctrine_name": "Flagship Doctrine",
            "additional_message": "x",
        }

        with self.assertRaises(standalone_pings.PingRefused) as caught:
            standalone_pings.send_standalone_ping(user=self.fc, cleaned_data=cleaned)

        self.assertEqual(str(caught.exception), NOT_ALLOWED)
        self.assertFalse(StandalonePing.objects.exists())

    def test_a_typed_name_is_not_offered_and_is_refused(self):
        restrict_custom(self.fcs)
        self.client.force_login(self.fc)

        page = self.client.get(reverse("fleetops:send_ping")).content.decode()
        response = self.send(self.fc, self.data(custom_doctrine="Typed"))

        self.assertNotIn("custom-doctrine-wrap", page)
        self.assertContains(response, CUSTOM_NOT_ALLOWED)
        self.assertFalse(StandalonePing.objects.exists())

    def test_use_again_of_a_doctrine_the_user_may_not_ping_is_not_turned_into_a_typed_name(self):
        restrict(FLAGSHIP, self.caplead)

        form = SendPingForm(user=self.fc, initial={"doctrine_choice": FLAGSHIP_VALUE})

        self.assertEqual(form.initial["doctrine_choice"], "")
        self.assertNotIn("custom_doctrine", form.initial)
        self.assertEqual(form.fields["doctrine_choice"].help_text, NOT_ALLOWED)

    def test_administrators_are_not_limited(self):
        restrict(FLAGSHIP, self.caplead)
        restrict_custom(self.fcs)
        admin = f.create_user("admin", perms=ADMIN_PERMS)

        self.send(admin, self.data(custom_doctrine="Typed"))

        self.assertEqual(StandalonePing.objects.get().doctrine_name, "Typed")


class ScheduledFleetTests(SchedulingTestBase):
    def setUp(self):
        super().setUp()
        for patcher in doctrines_shown():
            patcher.start()
            self.addCleanup(patcher.stop)
        self.caplead = f.aa_group("Example Caplead")
        self.fc = fresh(self.fc)

    def test_a_forged_id_is_not_scheduled(self):
        restrict(FLAGSHIP, self.caplead)

        response = self.post_schedule(doctrine_choice=FLAGSHIP_VALUE)

        self.assertContains(response, NOT_ALLOWED)
        self.assertFalse(FleetOperation.objects.exists())

    def test_the_service_refuses_a_forged_doctrine(self):
        restrict(FLAGSHIP, self.caplead)

        with self.assertRaises(PermissionError) as caught:
            schedule_fleet(
                user=self.fc,
                cleaned_data={
                    "fc_character_id": str(self.fc_char.character_id),
                    "fleet_type": self.fleet_type,
                    "formup": "Jita",
                    "scheduled_at": in_two_hours(),
                    "doctrine_source": SOURCE,
                    "doctrine_external_id": "12",
                },
            )

        self.assertEqual(str(caught.exception), NOT_ALLOWED)

    def test_the_edit_page_does_not_offer_the_restricted_doctrine_for_another_doctrine(self):
        restrict(FLAGSHIP, self.caplead)
        operation = self.scheduled(
            doctrine_source=SOURCE, doctrine_external_id="13", doctrine_name="Subcap Doctrine"
        )

        form = ScheduledFleetForm(operation=operation, user=self.fc)

        self.assertEqual([v for v, _l in form.fields["doctrine_choice"].choices], ["", SUBCAP_VALUE])

    def test_the_edit_refuses_a_forged_switch_to_a_restricted_doctrine(self):
        restrict(FLAGSHIP, self.caplead)
        operation = self.scheduled(
            doctrine_source=SOURCE, doctrine_external_id="13", doctrine_name="Subcap Doctrine"
        )
        self.client.force_login(self.fc)
        data = self.form_data(doctrine_choice=FLAGSHIP_VALUE)
        data.pop("operation_mode")
        data.pop("fleet_timing")
        data.pop("request_id")

        response = self.client.post(reverse("fleetops:edit_scheduled_fleet", args=[operation.uuid]), data)

        self.assertContains(response, NOT_ALLOWED)
        operation.refresh_from_db()
        self.assertEqual(operation.doctrine_external_id, "13")

    def test_the_edit_keeps_the_doctrine_the_fleet_has_even_if_the_editor_may_not_ping_it(self):
        restrict(FLAGSHIP, self.caplead)
        operation = self.scheduled(
            doctrine_source=SOURCE, doctrine_external_id="12", doctrine_name="Flagship Doctrine"
        )

        form = ScheduledFleetForm(operation=operation, user=self.fc)

        self.assertIn(FLAGSHIP_VALUE, [v for v, _l in form.fields["doctrine_choice"].choices])
        when = in_two_hours()
        data = {
            "fleet_type": str(self.fleet_type.pk),
            "doctrine_choice": FLAGSHIP_VALUE,
            "formup": "Amarr",
            "fleet_date": when.strftime("%Y-%m-%d"),
            "fleet_clock": when.strftime("%H:%M"),
            "ping_channel": str(self.channel.pk),
            "ping_mentions": ["everyone"],
        }
        self.assertTrue(ScheduledFleetForm(data, operation=operation, user=self.fc).is_valid())

    def test_a_scheduled_fleet_is_not_started_when_the_right_was_withdrawn(self):
        restrict(FLAGSHIP, self.caplead)
        self.fc = self.join_group(self.fc, self.caplead)
        self.post_schedule(user=self.fc, doctrine_choice=FLAGSHIP_VALUE)
        operation = FleetOperation.objects.get()
        self.assertEqual(operation.doctrine_external_id, "12")
        # The group is taken away before the fleet starts.
        self.fc.groups.remove(self.caplead)
        calls = len(self.esi.calls)

        response = self.client.post(
            reverse("fleetops:start_scheduled_fleet", args=[operation.uuid]),
            {"fc_character_id": str(self.fc_char.character_id), "send_ping": "on"},
        )

        self.assertContains(response, "The scheduled fleet was not started: you may not ping its doctrine any more.")
        operation.refresh_from_db()
        self.assertEqual(operation.status, FleetOperation.Status.SCHEDULED)
        self.assertEqual(len(self.esi.calls), calls)
        entry = AuditLog.objects.get(action="fleet.schedule_start_refused")
        self.assertEqual((entry.actor, entry.object_id), (self.fc, str(operation.pk)))
        self.assertEqual(entry.new_value["reason"], "doctrine")
        self.assertEqual(entry.new_value["doctrine_external_id"], "12")
        notice = Notification.objects.get(user=self.fc)
        self.assertEqual(notice.title, "Scheduled fleet not started")
        self.assertIn("you may not ping its doctrine any more", notice.message)
        self.assertFalse(AuditLog.objects.filter(action="fleet.start").exists())

    def test_the_notice_follows_the_language_of_the_scheduler(self):
        restrict(FLAGSHIP, self.caplead)
        operation = self.scheduled(
            doctrine_source=SOURCE, doctrine_external_id="12", doctrine_name="Flagship Doctrine"
        )
        self.fc.profile.language = "zh-hans"
        self.fc.profile.save()

        with self.assertRaises(PermissionError):
            start_scheduled_fleet(
                operation, user=fresh(self.fc), character_id=self.fc_char.character_id, send_ping=True
            )

        notice = Notification.objects.get(user=self.fc)
        self.assertNotEqual(notice.title, "Scheduled fleet not started")
        self.assertIn("预约", notice.title + notice.message)

    def test_a_scheduled_fleet_still_starts_when_the_user_is_allowed(self):
        restrict(FLAGSHIP, self.caplead)
        operation = self.scheduled(
            doctrine_source=SOURCE, doctrine_external_id="12", doctrine_name="Flagship Doctrine"
        )
        user = self.join_group(self.fc, self.caplead)

        started = start_scheduled_fleet(
            operation, user=user, character_id=self.fc_char.character_id, send_ping=False
        )

        self.assertEqual(started.status, FleetOperation.Status.ACTIVE)
        self.assertFalse(AuditLog.objects.filter(action="fleet.schedule_start_refused").exists())

    def test_a_typed_name_is_checked_again_when_the_fleet_starts(self):
        operation = self.scheduled(doctrine_source="custom", doctrine_name="Typed Doctrine")
        restrict_custom(f.aa_group("Example Typing Group"))

        with self.assertRaises(PermissionError) as caught:
            start_scheduled_fleet(
                operation, user=fresh(self.fc), character_id=self.fc_char.character_id, send_ping=False
            )

        self.assertIn("custom doctrine name any more", str(caught.exception))
        operation.refresh_from_db()
        self.assertEqual(operation.status, FleetOperation.Status.SCHEDULED)
        entry = AuditLog.objects.get(action="fleet.schedule_start_refused")
        self.assertEqual(entry.new_value["reason"], "custom_doctrine_name")
        self.assertEqual(Notification.objects.filter(user=self.fc).count(), 1)

    def test_a_typed_name_cannot_be_scheduled_without_the_right(self):
        restrict_custom(f.aa_group("Example Typing Group"))

        response = self.post_schedule(custom_doctrine="Typed")

        self.assertContains(response, CUSTOM_NOT_ALLOWED)
        self.assertFalse(FleetOperation.objects.exists())

    def test_a_fleet_without_a_doctrine_starts_whatever_the_rules_are(self):
        restrict_custom(f.aa_group("Example Typing Group"))
        operation = self.scheduled()

        started = start_scheduled_fleet(
            operation, user=fresh(self.fc), character_id=self.fc_char.character_id, send_ping=False
        )

        self.assertEqual(started.status, FleetOperation.Status.ACTIVE)

    def test_a_failing_notification_does_not_stop_the_audit_or_the_refusal(self):
        restrict(FLAGSHIP, self.caplead)
        operation = self.scheduled(
            doctrine_source=SOURCE, doctrine_external_id="12", doctrine_name="Flagship Doctrine"
        )

        with mock.patch("fleetops.services.scheduling.notify.warning", side_effect=RuntimeError("boom")):
            with self.assertRaises(PermissionError):
                start_scheduled_fleet(
                    operation, user=fresh(self.fc), character_id=self.fc_char.character_id, send_ping=False
                )

        self.assertTrue(AuditLog.objects.filter(action="fleet.schedule_start_refused").exists())

    def join_group(self, user, group):
        user.groups.add(group)
        return fresh(user)


class PresetTests(DoctrineAccessBase):
    def data(self, **extra):
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Jita",
            "ping_channel": str(self.channel.pk),
            "ping_mentions": ["everyone"],
        }
        data.update(extra)
        return data

    def test_saving_a_restricted_doctrine_is_refused(self):
        restrict(FLAGSHIP, self.caplead)

        with self.assertRaises(PresetRefused) as caught:
            save_preset(self.fc, "Flagship", self.data(doctrine_choice=FLAGSHIP_VALUE))

        self.assertEqual(str(caught.exception), NOT_ALLOWED)
        self.assertFalse(FleetStartPreset.objects.exists())

    def test_saving_a_typed_name_is_refused_without_the_right(self):
        restrict_custom(self.fcs)

        with self.assertRaises(PresetRefused) as caught:
            save_preset(self.fc, "Typed", self.data(custom_doctrine="Typed"))

        self.assertEqual(str(caught.exception), CUSTOM_NOT_ALLOWED)

    def test_the_save_view_says_why_a_preset_was_not_saved(self):
        restrict(FLAGSHIP, self.caplead)
        self.client.force_login(self.fc)
        data = {**self.data(doctrine_choice=FLAGSHIP_VALUE), "preset-name": "Flagship"}

        response = self.client.post(
            reverse("fleetops:preset_save"), data, HTTP_ACCEPT="application/json"
        )

        self.assertFalse(response.json()["ok"])
        self.assertIn(NOT_ALLOWED, response.json()["message"])
        self.assertFalse(FleetStartPreset.objects.exists())

    def test_saving_an_allowed_doctrine_works(self):
        restrict(FLAGSHIP, self.caplead)
        user = self.join(self.fc, self.caplead)

        preset = save_preset(user, "Flagship", self.data(doctrine_choice=FLAGSHIP_VALUE))

        self.assertEqual(preset.values["fields"]["doctrine_choice"], FLAGSHIP_VALUE)

    def stored(self, choice_value="", custom=""):
        return FleetStartPreset.objects.create(
            owner=self.fc,
            name="Stored",
            values={
                "fields": {"doctrine_choice": choice_value, "custom_doctrine": custom},
                "labels": {"doctrine_choice": choice_value.split("|")[-1]} if choice_value else {},
            },
        )

    def test_a_preset_whose_doctrine_is_no_longer_allowed_says_so_and_is_not_switched(self):
        preset = self.stored(FLAGSHIP_VALUE)
        restrict(FLAGSHIP, self.caplead)

        initial, warnings = load_preset(preset, StartFleetForm, self.fc)

        self.assertEqual(initial["doctrine_choice"], "")
        self.assertEqual(initial.get("custom_doctrine", ""), "")
        self.assertEqual(warnings, [f"Doctrine 'Flagship Doctrine' of this preset: {NOT_ALLOWED}"])

    def test_the_start_page_shows_that_warning_and_the_dropdown_is_not_switched_to_another_doctrine(self):
        preset = self.stored(FLAGSHIP_VALUE)
        restrict(FLAGSHIP, self.caplead)
        self.client.force_login(self.fc)

        page = self.client.get(reverse("fleetops:start_fleet") + f"?preset={preset.pk}").content.decode()

        self.assertIn(NOT_ALLOWED, page)
        self.assertNotIn('value="fittings:fittings.doctrine|13|Subcap Doctrine" selected', page)

    def test_without_the_custom_name_right_the_refused_preset_doctrine_leaves_a_disabled_selected_placeholder(self):
        preset = self.stored(FLAGSHIP_VALUE)
        restrict(FLAGSHIP, self.caplead)
        restrict_custom(self.caplead)
        self.client.force_login(self.fc)

        page = self.client.get(reverse("fleetops:start_fleet") + f"?preset={preset.pk}").content.decode()

        self.assertIn(NOT_ALLOWED, page)
        self.assertRegex(page, r'<option value="" (selected disabled|disabled selected)>Choose a doctrine</option>')
        self.assertNotIn("None / Custom", page)

    def test_a_preset_with_a_typed_name_the_user_may_not_use_says_so(self):
        preset = self.stored(custom="Typed")
        restrict_custom(self.fcs)

        initial, warnings = load_preset(preset, StartFleetForm, self.fc)

        self.assertEqual(initial.get("custom_doctrine", ""), "")
        self.assertEqual(warnings, [f"Custom doctrine name 'Typed' of this preset: {CUSTOM_NOT_ALLOWED}"])

    def test_a_preset_with_a_deleted_doctrine_still_becomes_a_typed_name_for_those_who_may_type(self):
        preset = self.stored(f"{SOURCE}|99|Old Doctrine")

        initial, warnings = load_preset(preset, StartFleetForm, self.fc)

        self.assertEqual(initial["custom_doctrine"], "Old Doctrine")
        self.assertEqual(len(warnings), 1)

    def test_a_deleted_doctrine_of_a_preset_is_not_typed_in_for_those_who_may_not_type(self):
        preset = self.stored(f"{SOURCE}|99|Old Doctrine")
        restrict_custom(self.fcs)

        initial, warnings = load_preset(preset, StartFleetForm, self.fc)

        self.assertEqual(initial.get("custom_doctrine", ""), "")
        self.assertEqual(warnings, ["Doctrine 'Old Doctrine' is no longer available."])

    def test_a_preset_with_an_allowed_doctrine_loads_it(self):
        preset = self.stored(SUBCAP_VALUE)
        restrict(FLAGSHIP, self.caplead)

        initial, warnings = load_preset(preset, StartFleetForm, self.fc)

        self.assertEqual(initial["doctrine_choice"], SUBCAP_VALUE)
        self.assertEqual(warnings, [])


class LinkFleetTests(DoctrineAccessBase):
    def test_the_fleet_with_a_pap_link_and_its_srp_link_form_is_not_restricted(self):
        # forms_links is no ping: no Discord post, no MOTD (the specification leaves it out on purpose).
        from fleetops.forms_links import LinkOnlyFleetForm

        restrict(FLAGSHIP, self.caplead)
        restrict_custom(self.fcs)
        with mock.patch("fleetops.forms_links.get_doctrines", return_value=list(BOTH)):
            form = LinkOnlyFleetForm(user=self.fc)

        values = [value for value, _label in form.fields["doctrine_choice"].choices]
        self.assertIn(FLAGSHIP_VALUE, values)


class SettingsPageTests(DoctrineAccessBase):
    def setUp(self):
        super().setUp()
        self.admin = f.create_user("doc_admin", perms=ADMIN_PERMS)
        self.client.force_login(self.admin)
        self.url = reverse("fleetops:doctrine_orders")

    def post(self, rows=(), custom_mode="everybody", custom_groups=(), **extra):
        data = {"custom_mode": custom_mode, "custom_groups": [str(g.pk) for g in custom_groups]}
        for index, (key, groups) in enumerate(rows):
            data[f"key_{index}"] = key
            data[f"groups_{index}"] = [str(g.pk) for g in groups]
        data.update(extra)
        return self.client.post(self.url, data)

    def test_the_page_lists_every_doctrine_with_the_groups_and_marks_the_restricted_ones(self):
        restrict(FLAGSHIP, self.caplead)

        page = self.client.get(self.url).content.decode()

        self.assertIn("Doctrine Settings", page)
        self.assertIn("Flagship Doctrine", page)
        self.assertIn("Subcap Doctrine", page)
        self.assertIn("Restricted", page)
        self.assertEqual(page.count("Restricted"), 1)
        self.assertIn("Example Caplead", page)

    def test_a_record_of_a_doctrine_that_no_longer_exists_is_kept_and_marked(self):
        restrict(DoctrineChoice(external_id="99", name="Gone", source=SOURCE), self.caplead)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Doctrine no longer exists")
        self.assertContains(response, "Doctrines that no longer exist")
        self.assertTrue(DoctrineAccess.objects.filter(doctrine_external_id="99").exists())

    def test_saving_a_doctrines_groups_stores_them_and_audits_old_and_new(self):
        response = self.post(rows=[(f"{SOURCE}|12", [self.caplead])])

        self.assertRedirects(response, self.url)
        row = DoctrineAccess.objects.get()
        self.assertEqual(list(row.groups.all()), [self.caplead])
        entry = AuditLog.objects.get(action="configuration.create")
        self.assertEqual((entry.actor, entry.object_type), (self.admin, "DoctrineAccess"))
        self.assertEqual(entry.new_value["groups"], [self.caplead.pk])
        self.assertEqual(entry.new_value["group_names"], ["Example Caplead"])
        self.assertEqual(entry.new_value["doctrine_name"], "Flagship Doctrine")

        self.post(rows=[(f"{SOURCE}|12", [self.fcs])])

        entry = AuditLog.objects.get(action="configuration.update", object_type="DoctrineAccess")
        self.assertEqual(entry.old_value["groups"], [self.caplead.pk])
        self.assertEqual(entry.new_value["groups"], [self.fcs.pk])

    def test_clearing_the_groups_opens_the_doctrine_and_keeps_the_record(self):
        restrict(FLAGSHIP, self.caplead)

        self.post(rows=[(f"{SOURCE}|12", [])])

        self.assertEqual(DoctrineAccess.objects.get().groups.count(), 0)
        self.assertTrue(doctrine_access.can_use_doctrine(self.fc, SOURCE, "12"))
        self.assertEqual(AuditLog.objects.filter(action="configuration.update").count(), 1)

    def test_an_unchanged_form_writes_nothing(self):
        restrict(FLAGSHIP, self.caplead)

        response = self.post(rows=[(f"{SOURCE}|12", [self.caplead]), (f"{SOURCE}|13", [])])

        self.assertRedirects(response, self.url)
        self.assertFalse(AuditLog.objects.exists())
        self.assertIn("Nothing was changed.", flashed(response))

    def test_a_forged_doctrine_key_is_ignored(self):
        self.post(rows=[("fittings:other|1", [self.caplead])])

        self.assertFalse(DoctrineAccess.objects.exists())
        self.assertFalse(AuditLog.objects.exists())

    def test_a_group_that_does_not_exist_stores_nothing(self):
        response = self.client.post(
            self.url, {"custom_mode": "everybody", "key_0": f"{SOURCE}|12", "groups_0": ["999999"]}
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DoctrineAccess.objects.exists())
        self.assertFalse(AuditLog.objects.exists())

    def test_a_bad_group_in_the_custom_setting_rolls_back_the_doctrine_rows_too(self):
        response = self.client.post(
            self.url,
            {
                "custom_mode": "groups",
                "custom_groups": ["999999"],
                "key_0": f"{SOURCE}|12",
                "groups_0": [str(self.caplead.pk)],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DoctrineAccess.objects.exists())
        self.assertFalse(AuditLog.objects.exists())

    def test_the_custom_name_setting_is_saved_and_audited(self):
        self.post(custom_mode="groups", custom_groups=[self.fcs])

        row = DoctrineSettings.objects.get()
        self.assertTrue(row.custom_name_restricted)
        self.assertEqual(list(row.custom_name_groups.all()), [self.fcs])
        entry = AuditLog.objects.get(action="configuration.update", object_type="DoctrineSettings")
        self.assertEqual(entry.old_value["custom_name_restricted"], False)
        self.assertEqual(entry.new_value["custom_name_restricted"], True)
        self.assertEqual(entry.new_value["group_names"], ["Example FC"])
        self.assertFalse(doctrine_access.can_use_custom_name(self.fc))

    def test_switching_the_custom_name_back_to_everybody_keeps_the_groups(self):
        self.post(custom_mode="groups", custom_groups=[self.fcs])

        self.post(custom_mode="everybody", custom_groups=[self.fcs])

        row = DoctrineSettings.objects.get()
        self.assertFalse(row.custom_name_restricted)
        self.assertEqual(list(row.custom_name_groups.all()), [self.fcs])
        self.assertTrue(doctrine_access.can_use_custom_name(self.fc))

    def test_a_limit_without_a_group_warns_that_only_administrators_can_type(self):
        response = self.post(custom_mode="groups")

        self.assertTrue(DoctrineSettings.objects.get().custom_name_restricted)
        self.assertIn("No group is chosen: only administrators can use a custom doctrine name.", flashed(response))

    def test_an_unknown_custom_mode_is_refused(self):
        response = self.post(custom_mode="nobody")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DoctrineSettings.objects.exists())

    def test_the_page_needs_the_configuration_permission(self):
        member = f.create_user("plain", perms=f.FC_PERMS)
        self.client.force_login(member)

        self.assertEqual(self.client.get(self.url).status_code, 403)
        response = self.post(rows=[(f"{SOURCE}|12", [self.caplead])])
        self.assertEqual(response.status_code, 403)
        self.assertFalse(DoctrineAccess.objects.exists())

    def test_a_post_needs_the_csrf_token(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.admin)

        response = client.post(self.url, {"key_0": f"{SOURCE}|12", "groups_0": [str(self.caplead.pk)]})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(DoctrineAccess.objects.exists())

    def test_a_change_is_in_effect_for_the_entry_points_at_once(self):
        self.post(rows=[(f"{SOURCE}|12", [self.caplead])])

        form = StartFleetForm(user=self.fc)

        self.assertNotIn(FLAGSHIP_VALUE, [v for v, _l in form.fields["doctrine_choice"].choices])

    def test_the_old_ship_order_links_are_on_the_page(self):
        page = self.client.get(self.url).content.decode()

        self.assertIn(reverse("fleetops:doctrine_order_edit", args=[12]), page)

    def test_the_index_card_points_to_the_page(self):
        page = self.client.get(reverse("fleetops:configuration_index")).content.decode()

        self.assertIn("Doctrine Settings", page)
        self.assertIn(self.url, page)

    def test_the_django_admin_is_only_a_fallback(self):
        from django.contrib import admin

        self.assertIn(DoctrineAccess, admin.site._registry)
        self.assertIn(DoctrineSettings, admin.site._registry)


class ServerSideBlankTests(DoctrineAccessBase):
    """None / Custom without a name is checked on the server, not only by the forms."""

    def refusal(self, user, source="none"):
        with mock.patch("fleetops.providers.doctrines.get_doctrines", return_value=list(BOTH)):
            return doctrine_access.refusal(user, {"doctrine_source": source, "doctrine_name": ""})

    def test_a_request_naming_no_doctrine_is_refused_without_the_right(self):
        restrict_custom(self.caplead)

        for source in ("none", ""):
            self.assertEqual(self.refusal(self.fc, source), "Choose a doctrine from the list.")

    def test_it_is_allowed_with_the_right_or_without_a_restriction(self):
        self.assertEqual(self.refusal(self.fc), "")
        restrict_custom(self.caplead)
        self.assertEqual(self.refusal(self.join(self.fc, self.caplead)), "")

    def test_it_is_allowed_when_there_is_no_doctrine_to_choose(self):
        restrict_custom(self.caplead)
        with mock.patch("fleetops.providers.doctrines.get_doctrines", return_value=[]):
            self.assertEqual(doctrine_access.refusal(self.fc, {"doctrine_source": "none"}), "")

    def test_the_start_service_refuses_it(self):
        restrict_custom(self.caplead)
        data = {"doctrine_source": "none", "doctrine_name": ""}

        with mock.patch("fleetops.providers.doctrines.get_doctrines", return_value=list(BOTH)):
            self.assertEqual(start_refusal(self.fc, data, full_mode=False), "Choose a doctrine from the list.")


class RunningFleetEditTests(DoctrineAccessBase):
    def setUp(self):
        super().setUp()
        self.operation = f.create_operation(self.fc, doctrine_name="Subcap Doctrine", doctrine_source=SOURCE, doctrine_external_id="13")
        self.client.force_login(self.fc)

    def edit(self, name):
        started = self.operation.started_at
        data = {
            "fleet_type": self.operation.fleet_type_id,
            "doctrine_name": name,
            "formup": self.operation.formup,
            "additional_message": "",
            "started_at": started.strftime("%Y-%m-%dT%H:%M"),
        }
        return self.client.post(reverse("fleetops:edit_operation", args=[self.operation.uuid]), data)

    def test_a_typed_name_is_refused_without_the_right_and_the_record_is_unchanged(self):
        restrict_custom(self.caplead)

        response = self.edit("Typed Doctrine")

        self.assertContains(response, CUSTOM_NOT_ALLOWED)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.doctrine_name, "Subcap Doctrine")
        self.assertEqual(self.operation.doctrine_source, SOURCE)

    def test_a_typed_name_is_saved_with_the_right(self):
        restrict_custom(self.fcs)
        self.join(self.fc, self.fcs)

        self.edit("Typed Doctrine")

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.doctrine_name, "Typed Doctrine")
        self.assertEqual(self.operation.doctrine_source, "custom")

    def test_saving_the_unchanged_name_is_not_refused(self):
        restrict_custom(self.caplead)

        self.edit("Subcap Doctrine")

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.doctrine_source, SOURCE)


class DoctrineSettingsSingletonTests(DoctrineAccessBase):
    def test_a_row_made_after_a_delete_is_still_the_one_the_rules_read(self):
        DoctrineSettings.objects.all().delete()

        row = DoctrineSettings(custom_name_restricted=True)
        row.save()

        self.assertEqual(row.pk, 1)
        self.assertFalse(doctrine_access.can_use_custom_name(self.fc))

    def test_the_admin_does_not_delete_the_row(self):
        from django.contrib import admin

        model_admin = admin.site._registry[DoctrineSettings]
        request = mock.Mock()
        self.assertFalse(model_admin.has_delete_permission(request))


class ScheduledEditUnavailableDoctrineTests(ScheduledFleetTests):
    def test_without_the_custom_name_right_a_gone_doctrine_leaves_the_placeholder_not_the_first_doctrine(self):
        restrict_custom(self.caplead)
        operation = self.scheduled(doctrine_source=SOURCE, doctrine_external_id="99", doctrine_name="Old Doctrine")
        self.client.force_login(self.fc)

        page = self.client.get(reverse("fleetops:edit_scheduled_fleet", args=[operation.uuid])).content.decode()

        self.assertRegex(page, r'<option value="" (selected disabled|disabled selected)>Choose a doctrine</option>')
        self.assertNotIn("Old Doctrine", page)
