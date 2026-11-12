# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fixes found by the re-test of 0.3.0b2: the ping cooldown wording, the Chinese wording of the catalog and the hint of
the MOTD template page. (The MOTD escaping, the settings dead end and the opening character have tests next to their
code: tests/test_messages.py, tests/test_srp_config.py and tests/test_srp_ingame.py.)"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone, translation

from fleetops import texts
from fleetops.constants import PING_COOLDOWN_SECONDS
from fleetops.models import StandalonePing
from fleetops.services.operations import ping_cooldown_problem

from . import factories as f


class PingCooldownWordingTests(TestCase):
    def test_one_second_is_singular_and_the_rest_plural(self):
        self.assertEqual(
            texts.PING_COOLDOWN(name="Ops", seconds=1, wait=59),
            "Ops received a ping 1 second ago. Wait 59 seconds.",
        )
        self.assertEqual(
            texts.PING_COOLDOWN(name="Ops", seconds=59, wait=1),
            "Ops received a ping 59 seconds ago. Wait 1 second.",
        )

    def test_the_message_is_translated_whatever_the_numbers(self):
        for seconds, wait in ((1, 59), (30, 30), (59, 1)):
            with self.subTest(seconds=seconds), translation.override("zh-hans"):
                shown = texts.translate_stored(texts.PING_COOLDOWN(name="Ops", seconds=seconds, wait=wait))
                self.assertEqual(shown, f"Ops 在 {seconds} 秒前刚收到一条 Ping。请再等 {wait} 秒。")

    def test_a_ping_that_just_went_out_never_reads_as_zero_seconds_ago(self):
        fc = f.create_user("cool_word_fc", perms=f.FC_PERMS)
        channel = f.ping_channel("Cool")
        now = timezone.now()
        ping = f.standalone_ping(sender=fc, channel=channel, status=StandalonePing.Status.SENT)
        StandalonePing.objects.filter(pk=ping.pk).update(created_at=now)

        for elapsed in (0, 0.2, 1.0, 30.4, PING_COOLDOWN_SECONDS - 0.5):
            with self.subTest(elapsed=elapsed):
                text = ping_cooldown_problem(channel, now=now + timedelta(seconds=elapsed))
                self.assertNotIn(" 0 second", text)
                self.assertRegex(text, r"ping [1-9]\d* seconds? ago\. Wait [1-9]\d* seconds?\.$")
        self.assertIn("1 second ago. Wait 60 seconds.", ping_cooldown_problem(channel, now=now))

    def test_the_time_since_the_last_ping_is_rounded_to_the_nearest_second(self):
        fc = f.create_user("cool_round_fc", perms=f.FC_PERMS)
        channel = f.ping_channel("CoolRound")
        now = timezone.now()
        ping = f.standalone_ping(sender=fc, channel=channel, status=StandalonePing.Status.SENT)
        StandalonePing.objects.filter(pk=ping.pk).update(created_at=now)

        late = ping_cooldown_problem(channel, now=now + timedelta(seconds=PING_COOLDOWN_SECONDS - 0.6))
        self.assertIn("59 seconds ago. Wait 1 second.", late)
        early = ping_cooldown_problem(channel, now=now + timedelta(seconds=0.2))
        self.assertIn("1 second ago. Wait 60 seconds.", early)


class MotdTemplateHintTests(TestCase):
    def test_the_template_edit_page_says_which_variables_make_the_fc_and_the_system_clickable(self):
        from django.urls import reverse

        from fleetops.models import MessageTemplate

        admin = f.create_user("hint_admin", perms=f.MEMBER_PERMS + ["fleetops.manage_configuration"])
        self.client.force_login(admin)
        template = MessageTemplate.objects.create(
            name="Own MOTD", template_type=MessageTemplate.TemplateType.MOTD, content="{{ fc }}"
        )

        page = self.client.get(reverse("fleetops:configuration_edit", args=["templates", template.pk]))

        self.assertContains(page, "{{ fc_link }} and {{ formup_link }}")
        self.assertContains(page, "{{ fc }} and {{ formup }} are plain text")
        from fleetops.forms_config import MessageTemplateForm

        with translation.override("zh-hans"):
            self.assertIn("纯文字", str(MessageTemplateForm().fields["content"].help_text))


class ChineseWordingTests(TestCase):
    """One word each for doctrine, paid and open in game (see docs/translations.md)."""

    def test_the_catalog_uses_the_agreed_words(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent / "fleetops" / "locale" / "zh_Hans" / "LC_MESSAGES"
        for name in ("django.po", "djangojs.po"):
            text = (root / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertNotIn("教义", text)
                self.assertNotIn("在游戏中打开", text)
                self.assertNotIn("游戏内打开", text)
                # "Paid" is never shortened to 已付 (the word 已付款 is the one).
                self.assertIsNone(re.search(r"已付(?!款)", text))


class MotdApostropheTests(TestCase):
    def _render(self, content, **values):
        from fleetops.models import MessageTemplate
        from fleetops.services.messages import _render_motd

        record = MessageTemplate(name="Own MOTD", template_type=MessageTemplate.TemplateType.MOTD, content=content)
        return _render_motd(record, "", values, [])

    def test_an_apostrophe_cannot_leave_a_single_quoted_attribute(self):
        motd = self._render("<font color='{{ formup }}'>x</font>", formup="x' onclick='y")
        self.assertNotIn("onclick='y'", motd)
        self.assertNotIn("color='x'", motd)
        self.assertIn("&#x27;", motd)

    def test_an_apostrophe_in_the_text_is_shown_as_typed(self):
        motd = self._render("<b>{{ formup }}</b> {{ doctrine }}", formup="O'Brien's", doctrine="a'b")
        self.assertEqual(motd, "<b>O'Brien's</b> a'b")
        self.assertEqual(self._render("{{ formup }} <br>", formup="it's"), "it's <br>")
