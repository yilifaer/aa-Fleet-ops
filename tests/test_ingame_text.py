# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Game rich text in custom doctrine names and in the message fields: plain text on save, plain text in messages."""

import time
import uuid
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from fleetops.forms import ManualFleetForm, OperationEditForm, StartFleetForm
from fleetops.forms_common import DOCTRINE_NAME_MAX_LENGTH, DOCTRINE_PASTE_MAX_LENGTH, DoctrineNameField
from fleetops.forms_config import SrpPayoutRuleForm
from fleetops.forms_groups import SendPingForm
from fleetops.forms_links import LinkOnlyFleetForm
from fleetops.models import CommsPreset, FleetOperation, StandalonePing
from fleetops.services.ingame_text import fitting_link, plain_text
from fleetops.services.messages import render_messages, render_standalone_ping

from . import factories as f
from .link_fakes import setUpModule, tearDownModule  # noqa: F401 - unittest runs them around the module

FITTING = (
    '<font size="14" color="#ffffe400">Ship: </font><font size="14" color="#ff6868e1">'
    '<a href="fitting:11987:2048;1:1952;1:2977;4:8419;1:{pad}">Example Fleet Issue</a></font>'
)
# What the browser's maxlength=255 left of a pasted link: an open <font> and an open <a href="fitting:...
CUT_OFF = FITTING.format(pad="9" * 400)[:255]


def pasted(pad="9" * 400):
    return FITTING.format(pad=pad)


class PlainTextTests(TestCase):
    def test_a_complete_link_keeps_its_name(self):
        text = pasted()
        self.assertGreater(len(text), 400)
        self.assertEqual(plain_text(text), "Ship: Example Fleet Issue")

    def test_nested_font_tags_and_line_breaks(self):
        text = '<font color="#fff"><b>Armor <i>Fleet</i></b><br>Logi<br/></font>'
        self.assertEqual(plain_text(text), "Armor Fleet Logi")
        self.assertEqual(plain_text(text, keep_lines=True), "Armor Fleet\nLogi")

    def test_a_cut_off_link_is_dropped(self):
        self.assertIn('<a href="fitting:', CUT_OFF)
        self.assertNotIn("</a>", CUT_OFF)
        self.assertEqual(plain_text(CUT_OFF), "Ship:")
        self.assertEqual(plain_text("Ferox <font color"), "Ferox")

    def test_text_without_tags_is_only_tidied(self):
        self.assertEqual(plain_text("  Ferox   Navy \t Fleet "), "Ferox Navy Fleet")
        self.assertEqual(plain_text("Shield <3 and a < b > c"), "Shield <3 and a < b > c")
        self.assertEqual(plain_text("<@123456> hi"), "<@123456> hi")

    def test_other_angle_bracket_text_stays_as_typed(self):
        for text in (
            "see <t:1700000000:R> now",
            "x<y and z>w",
            "<a:x:1> emoji",
            "Primary: <ABCD> dreads, stay on grid",
            "Ferox <ABCD> fit",
            "a <bc d",
        ):
            self.assertEqual(plain_text(text), text)
        self.assertEqual(plain_text("a <b c"), "a")
        self.assertEqual(plain_text("Go <B>now</B> <FONT size=3>x</FONT>"), "Go now x")

    def test_no_pattern_needs_python_3_11(self):
        # Possessive quantifiers (*+, ++, ?+) only compile from Python 3.11; the plugin supports 3.10.
        import re

        from fleetops.services import ingame_text

        for name in ("_TAG", "_OPEN_TAG", "_LINE_BREAK", "_SPACES", "_FITTING"):
            self.assertNotRegex(getattr(ingame_text, name).pattern, r"[*+?}]\+")

    def test_empty_values(self):
        for value in ("", None, "   ", "<font></font>"):
            self.assertEqual(plain_text(value), "")

    def test_lines_are_kept_on_request(self):
        self.assertEqual(plain_text("a  b\r\n\r\n\r\n\r\n c", keep_lines=True), "a b\n\nc")

    def test_long_malformed_input_is_fast(self):
        started = time.monotonic()
        for bad in ("<" * 100000, "<a " * 50000, '<a href="fitting:' * 20000, "<a" + " " * 100000, "<font " + "<" * 50000):
            plain_text(bad)
            plain_text(bad, keep_lines=True)
            fitting_link(bad)
        self.assertLess(time.monotonic() - started, 2)


class FittingLinkTests(TestCase):
    def test_a_complete_link(self):
        self.assertEqual(
            fitting_link(pasted(pad="1")),
            ("Example Fleet Issue", "fitting:11987:2048;1:1952;1:2977;4:8419;1:1"),
        )

    def test_a_cut_off_link_is_none(self):
        self.assertIsNone(fitting_link(CUT_OFF))
        self.assertIsNone(fitting_link('<a href="fitting:1:2">Name'))

    def test_other_links_and_empty_values_are_none(self):
        self.assertIsNone(fitting_link('<a href="showinfo:5//30000142">Jita</a>'))
        self.assertIsNone(fitting_link('<a href="fitting:1:2"></a>'))
        self.assertIsNone(fitting_link(""))
        self.assertIsNone(fitting_link(None))


class DoctrineFieldTests(TestCase):
    def field(self):
        return DoctrineNameField(label="Doctrine")

    def test_a_pasted_link_becomes_its_plain_name(self):
        self.assertEqual(self.field().clean(pasted()), "Ship: Example Fleet Issue")

    def test_a_long_plain_name_is_an_error_not_a_cut(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError) as caught:
            self.field().clean("x" * (DOCTRINE_NAME_MAX_LENGTH + 1))
        self.assertIn("256 characters", caught.exception.messages[0])
        self.assertIn("limit is 255", caught.exception.messages[0])
        self.assertEqual(self.field().clean("x" * DOCTRINE_NAME_MAX_LENGTH), "x" * DOCTRINE_NAME_MAX_LENGTH)

    def test_a_huge_paste_is_refused(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            self.field().clean("<b>" * DOCTRINE_PASTE_MAX_LENGTH)

    def test_the_input_takes_a_pasted_link_whole(self):
        html = str(self.field().widget.render("doctrine", ""))
        self.assertIn(f'maxlength="{DOCTRINE_PASTE_MAX_LENGTH}"', html)
        self.assertGreater(DOCTRINE_PASTE_MAX_LENGTH, len(pasted()))


class StartFormTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Form FC")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.fleet_type = f.fleet_type("StratOp")

    def data(self, **extra):
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fc_character_id": str(self.fc_char.character_id),
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Jita",
        }
        data.update(extra)
        return data

    def test_the_start_form_stores_the_plain_name(self):
        form = StartFleetForm(self.data(custom_doctrine=pasted()), user=self.fc)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["doctrine_name"], "Ship: Example Fleet Issue")
        self.assertEqual(form.cleaned_data["doctrine_source"], "custom")
        self.assertEqual(form.cleaned_data["doctrine_external_id"], "")

    def test_the_scheduled_fleet_form_stores_the_plain_name(self):
        data = self.data(custom_doctrine=pasted(), operation_mode="scheduled")
        form = StartFleetForm(data, user=self.fc)
        form.is_valid()
        self.assertNotIn("custom_doctrine", form.errors)
        self.assertEqual(form.cleaned_data["doctrine_name"], "Ship: Example Fleet Issue")

    def test_the_start_form_refuses_a_name_that_is_too_long(self):
        form = StartFleetForm(self.data(custom_doctrine="y" * 300), user=self.fc)
        self.assertFalse(form.is_valid())
        self.assertIn("custom_doctrine", form.errors)
        self.assertIn("limit is 255", form.errors["custom_doctrine"][0])

    def test_the_link_form_stores_the_plain_name(self):
        form = LinkOnlyFleetForm(
            {
                "request_id": str(uuid.uuid4()),
                "fc_character_id": str(self.fc_char.character_id),
                "fleet_type": str(self.fleet_type.pk),
                "formup": "Jita",
                "custom_doctrine": pasted(),
                "link_minutes": "30",
            },
            user=self.fc,
        )
        form.is_valid()
        self.assertNotIn("custom_doctrine", form.errors)
        self.assertEqual(form.cleaned_data["doctrine_name"], "Ship: Example Fleet Issue")
        too_long = LinkOnlyFleetForm({"custom_doctrine": "z" * 256}, user=self.fc)
        self.assertFalse(too_long.is_valid())
        self.assertIn("custom_doctrine", too_long.errors)

    def test_the_ping_form_and_the_rule_form_use_the_same_field(self):
        self.assertIsInstance(SendPingForm(user=self.fc).fields["custom_doctrine"], DoctrineNameField)
        with mock.patch("fleetops.forms_config.get_doctrines", return_value=[]):
            self.assertIsInstance(SrpPayoutRuleForm().fields["doctrine_custom"], DoctrineNameField)
        self.assertIsInstance(StartFleetForm(user=self.fc).fields["custom_doctrine"], DoctrineNameField)

    def test_the_rule_form_keeps_the_plain_name_and_refuses_a_long_one(self):
        base = {"ship": "", "doctrine_choice": "custom_doctrine", "scope": "any", "amount": "50m", "is_active": "on"}
        with mock.patch("fleetops.forms_config.get_doctrines", return_value=[]):
            form = SrpPayoutRuleForm({**base, "doctrine_custom": pasted()})
            form.is_valid()
            self.assertEqual(form.cleaned_data["doctrine_custom"], "Ship: Example Fleet Issue")
            long = SrpPayoutRuleForm({**base, "doctrine_custom": "w" * 300})
            self.assertFalse(long.is_valid())
            self.assertIn("doctrine_custom", long.errors)

    def test_the_manual_fleet_form(self):
        data = {
            "fleet_type": self.fleet_type.pk,
            "formup": "Amamake",
            "started_at": "2026-10-11T19:00",
            "ended_at": "2026-10-11T21:00",
            "attendance_multiplier": "1",
        }
        ok = ManualFleetForm(data={**data, "doctrine_name": pasted()})
        ok.is_valid()
        self.assertEqual(ok.cleaned_data["doctrine_name"], "Ship: Example Fleet Issue")
        bad = ManualFleetForm(data={**data, "doctrine_name": "v" * 256})
        self.assertFalse(bad.is_valid())
        self.assertIn("doctrine_name", bad.errors)

    def test_the_edit_form_cleans_and_never_cuts(self):
        operation = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED)
        data = {
            "fleet_type": str(operation.fleet_type_id),
            "formup": "Jita",
            "started_at": "2026-10-11T19:00",
            "ended_at": "2026-10-11T21:00",
        }
        ok = OperationEditForm({**data, "doctrine_name": pasted()}, instance=operation)
        ok.is_valid()
        self.assertNotIn("doctrine_name", ok.errors)
        self.assertEqual(ok.cleaned_data["doctrine_name"], "Ship: Example Fleet Issue")
        bad = OperationEditForm({**data, "doctrine_name": "u" * 256}, instance=operation)
        self.assertIn("doctrine_name", bad.errors)


class StoredNameTests(TestCase):
    """Saving a record whose name still holds markup, without touching the name, keeps its doctrine key."""

    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Keep FC")

    def test_editing_another_field_of_a_rule_keeps_the_key(self):
        from fleetops.models import SrpPayoutRule

        with mock.patch("fleetops.forms_config.get_doctrines", return_value=[]):
            rule = SrpPayoutRule(doctrine_source="custom", doctrine_name=CUT_OFF, amount=50_000_000, label="r")
            rule.save()
            key = rule.doctrine_key
            self.assertTrue(key.startswith("name:<font"))
            form = SrpPayoutRuleForm(
                {
                    "label": "r",
                    "ship": "",
                    "doctrine_choice": "custom",
                    "doctrine_custom": CUT_OFF,
                    "scope": rule.scope,
                    "amount": "60m",
                    "is_active": "on",
                },
                instance=rule,
            )
            self.assertTrue(form.is_valid(), form.errors)
            saved = form.save()
        self.assertEqual(saved.doctrine_name, CUT_OFF)
        self.assertEqual(saved.doctrine_key, key)

    def test_editing_another_field_of_a_fleet_keeps_its_name(self):
        operation = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED, doctrine_name=CUT_OFF)
        form = OperationEditForm(
            {
                "fleet_type": str(operation.fleet_type_id),
                "doctrine_name": CUT_OFF,
                "formup": "Jita",
                "started_at": "2026-10-11T19:00",
                "ended_at": "2026-10-11T21:00",
            },
            instance=operation,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["doctrine_name"], CUT_OFF)
        changed = OperationEditForm(
            {**form.data, "doctrine_name": "x " + CUT_OFF},
            instance=operation,
        )
        changed.is_valid()
        self.assertEqual(changed.cleaned_data["doctrine_name"], "x Ship:")


class StartPageErrorTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Page FC")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.fleet_type = f.fleet_type("StratOp")
        self.client.force_login(self.fc)

    def data(self, **extra):
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fc_character_id": str(self.fc_char.character_id),
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Jita",
            "custom_doctrine": "y" * 300,
        }
        data.update(extra)
        return data

    def test_the_start_page_shows_the_length_error(self):
        response = self.client.post(reverse("fleetops:start_fleet"), self.data())
        self.assertContains(response, "the limit is 255")

    def test_the_edit_scheduled_page_shows_the_length_error(self):
        operation = f.create_scheduled_operation(self.fc)
        response = self.client.post(
            reverse("fleetops:edit_scheduled_fleet", args=[operation.uuid]),
            self.data(operation_mode="scheduled", scheduled_at="2030-10-11T19:00"),
        )
        self.assertContains(response, "the limit is 255")


class MessageTests(TestCase):
    """A value stored before the cleaning existed (cut off by the browser) must not spoil the lines after it."""

    def setUp(self):
        f.builtin_default_templates()
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.comms = CommsPreset.objects.create(name="Main Comms", channel_name="Fleet 1")

    def operation(self, **extra):
        values = dict(
            formup="Jita IV-4",
            doctrine_name=CUT_OFF,
            doctrine_source="custom",
            comms=self.comms,
            send_ping=True,
            srp_url="https://srp.example.org/request/1/",
        )
        values.update(extra)
        return f.create_operation(self.fc, **values)

    def test_the_motd_lines_after_a_cut_off_doctrine_are_intact(self):
        rendered = render_messages(self.operation())
        self.assertIn("<br>Doctrine: Ship:<br>Comms: Main Comms", rendered.motd)
        self.assertIn('<br>SRP: <a href="https://srp.example.org/request/1/">Request SRP</a>', rendered.motd)
        self.assertNotIn("fitting:", rendered.motd)
        self.assertEqual(rendered.motd.count("<font"), 1)
        self.assertTrue(rendered.motd.endswith("</font>"))

    def test_the_ping_has_no_markup(self):
        rendered = render_messages(self.operation())
        self.assertIn("**Doctrine:** Ship:\n", rendered.ping)
        for text in (rendered.ping, rendered.ping_display):
            self.assertNotIn("<font", text)
            self.assertNotIn("<a ", text)
            self.assertNotIn("fitting:", text)

    def test_a_complete_pasted_link_reads_as_its_name(self):
        operation = self.operation()
        # Rows saved before the form cleaned the text can be longer than the column; set it in memory only.
        operation.doctrine_name = pasted()
        rendered = render_messages(operation)
        self.assertIn("<br>Doctrine: Ship: Example Fleet Issue<br>Comms:", rendered.motd)
        self.assertIn("**Doctrine:** Ship: Example Fleet Issue\n", rendered.ping)

    def test_form_up_and_the_additional_message_cannot_swallow_lines(self):
        operation = self.operation(
            formup='<font color="#ff0000"><a href="showinfo:5//1">Jita',
            additional_message='Bring <b>drones</b>\n<font size="30">Undock\nat 19:00 <a href="x',
        )
        rendered = render_messages(operation)
        self.assertIn("Jita</a></loc><br>Doctrine:", rendered.motd)
        self.assertIn("<br><br>Bring drones<br>Undock<br>at 19:00</font>", rendered.motd)
        self.assertEqual(rendered.motd.count("<font"), 1)
        self.assertNotIn("<a href=\"x", rendered.motd)
        self.assertIn("Bring drones\nUndock\nat 19:00\n\n", rendered.ping)
        self.assertNotIn("<", rendered.ping.replace("<t:", "").replace("<@", ""))

    def test_discord_timestamps_and_tickers_in_the_additional_message_survive(self):
        operation = self.operation(additional_message="Undock <t:1700000000:R>, primary <ABCD> dreads")
        rendered = render_messages(operation)
        self.assertIn("<t:1700000000:R>", rendered.ping)
        self.assertIn("<ABCD>", rendered.ping)
        # Changed in 0.3.0b3: the MOTD is HTML-like text, so the angle brackets of a ticker are written as entities
        # (the game shows them as typed; a bare "<ABCD>" would be eaten as a tag).
        self.assertIn("primary &lt;ABCD&gt; dreads", rendered.motd)

    def test_text_that_holds_no_tags_is_left_alone(self):
        operation = self.operation(doctrine_name="Ferox & Co", formup="Jita IV", additional_message="Bring drones")
        rendered = render_messages(operation)
        # Changed in 0.3.0b3: the "&" is an entity in the MOTD.
        self.assertIn("Doctrine: Ferox &amp; Co<br>", rendered.motd)
        self.assertIn("<br><br>Bring drones</font>", rendered.motd)

    def test_mass_mentions_in_user_text_stay_neutralised(self):
        operation = self.operation(doctrine_name="<b>@everyone</b> @here", additional_message="<i>@everyone</i>")
        rendered = render_messages(operation)
        self.assertNotIn("@everyone", rendered.ping)
        self.assertNotIn("@here", rendered.ping)
        self.assertNotIn("@everyone", rendered.ping_display)

    def test_a_ping_without_a_fleet_is_plain_text_too(self):
        ping = StandalonePing(
            sender_name="Boss Main",
            doctrine_name=CUT_OFF,
            doctrine_source="custom",
            formup="<font color=x>Jita",
            additional_message="Go <b>now</b> <font size=",
        )
        rendered = render_standalone_ping(ping, actor=self.fc)
        self.assertIn("Go now", rendered.ping)
        self.assertIn("**Formup Location:** Jita", rendered.ping)
        self.assertIn("**Doctrine:** Ship:", rendered.ping)
        self.assertNotIn("<", rendered.ping)
