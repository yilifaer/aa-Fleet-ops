# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Text that is sent out follows the site language; text for the person at the screen follows theirs.

Pings, MOTDs and cancellation notices are read by a whole fleet, so whoever clicks does not change them. The
warnings next to the preview are for the person who looks at it. The tests replace the catalogs with small
fake ones, so they do not depend on the translations that ship.
"""

from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.utils import translation
from django.utils.functional import lazy

from fleetops.models import FleetOperation, MessageTemplate
from fleetops.services import messages as messages_module
from fleetops.services.mentions import EVERYONE, HERE, ROLES, MentionSelection, RoleRef, mention_text, target_label
from fleetops.services.messages import render_cancellation_notice
from fleetops.services.motd import regenerate_and_push_motd
from fleetops.services.operations import build_unsaved_operation, end_fleet
from fleetops.services.scheduling import cancel_scheduled_fleet
from fleetops.texts import registered

from . import factories as f
from .test_messages import FLEET_TIME, SENT, MessageTestBase, make_template
from .test_operations_tracking import StartFleetTestBase, frozen_now
from .test_scheduling import SchedulingTestBase

Mode = FleetOperation.MentionMode
SITE_LANGUAGES = ("en-us", "zh-hans")
VIEWER_LANGUAGES = ("en", "zh-hans")
CANCELLED = (
    "**Cancelled:** %(fleet_type)s at %(fleet_time)s (%(formup)s)\n"
    "-# FleetOps notice from %(sender)s at %(sent_at)s EVE"
)
MISSING_MENTION = "The ping template does not contain {{ ping_mention }}; nobody will be notified."
PING_TOO_LONG = (
    "The ping is %(length)s characters; Discord accepts at most %(limit)s. Shorten the additional information."
)
MOTD_TOO_LONG = "The MOTD is %(length)s characters; the game may cut a MOTD longer than %(limit)s."

# Words in a ping: "discord broadcast" is the context of the phrases the site language decides.
BROADCAST = {
    "zh-hans": {
        "all": "所有人",
        "online members": "在线成员",
        "this channel": "本频道",
        "role %(id)s": "身份组 %(id)s",
        "Now": "现在",
        "None / Custom": "无 / 自定义",
        CANCELLED: (
            "**已取消：** %(fleet_type)s，%(fleet_time)s（%(formup)s）\n"
            "-# 来自 %(sender)s 的 FleetOps 通知，%(sent_at)s EVE"
        ),
    }
}
# Words in the warnings next to the preview: the language of the viewer decides.
WARNINGS = {
    "zh-hans": {
        MISSING_MENTION: "通知模板不含 {{ ping_mention }}，不会提醒任何人。",
        PING_TOO_LONG: "通知有 %(length)s 个字符，Discord 最多接受 %(limit)s 个。请缩短附加信息。",
        MOTD_TOO_LONG: "MOTD 有 %(length)s 个字符，游戏可能会截断超过 %(limit)s 个字符的 MOTD。",
    }
}


def fake_pgettext(context, message):
    if context != "discord broadcast":
        return message
    return BROADCAST.get(translation.get_language(), {}).get(message, message)


def fake_gettext(message):
    return WARNINGS.get(translation.get_language(), {}).get(message, message)


class FakeCatalogs:
    """Translate the phrases of this module through the tables above."""

    def setUp(self):
        super().setUp()
        patches = [
            mock.patch("fleetops.services.messages.pgettext", fake_pgettext),
            mock.patch("fleetops.services.mentions.pgettext", fake_pgettext),
            mock.patch("fleetops.services.messages.gettext", fake_gettext),
            mock.patch.object(messages_module, "MISSING_MENTION_WARNING", lazy(fake_gettext, str)(MISSING_MENTION)),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)


def in_languages(site, viewer):
    """The site language is set for the process, the viewer language for the request."""
    return _Both(site, viewer)


class _Both:
    def __init__(self, site, viewer):
        self.settings = override_settings(LANGUAGE_CODE=site)
        self.viewer = translation.override(viewer)

    def __enter__(self):
        self.settings.enable()
        self.viewer.__enter__()

    def __exit__(self, *exc_info):
        self.viewer.__exit__(*exc_info)
        self.settings.disable()


class BuiltInPhraseTests(FakeCatalogs, SimpleTestCase):
    SELECTIONS = (
        (MentionSelection(EVERYONE), "all", "所有人"),
        (MentionSelection(HERE), "online members", "在线成员"),
        (MentionSelection(), "this channel", "本频道"),
        (MentionSelection(ROLES, (RoleRef("111", ""),)), "role 111", "身份组 111"),
        (MentionSelection(ROLES, (RoleRef("111", "Sig A"), RoleRef("222", "Sig B"))), "Sig A, Sig B", "Sig A, Sig B"),
    )

    def test_who_the_ping_went_to_follows_the_site_language(self):
        for selection, english, chinese in self.SELECTIONS:
            for site, viewer in ((s, v) for s in SITE_LANGUAGES for v in VIEWER_LANGUAGES):
                with self.subTest(mode=selection.mode, site=site, viewer=viewer), in_languages(site, viewer):
                    self.assertEqual(target_label(selection), chinese if site == "zh-hans" else english)

    def test_a_role_without_a_name_is_called_by_its_id_in_the_site_language(self):
        selection = MentionSelection(ROLES, (RoleRef("111", ""), RoleRef("222", "Sig B")))

        with in_languages("en-us", "zh-hans"):
            self.assertEqual(mention_text(selection, display=True), "@role 111 @Sig B")
        with in_languages("zh-hans", "en"):
            self.assertEqual(mention_text(selection, display=True), "@身份组 111 @Sig B")
        # What Discord receives does not hold words at all.
        with in_languages("zh-hans", "zh-hans"):
            self.assertEqual(mention_text(selection), "<@&111> <@&222>")

    def test_the_site_language_does_not_leak_out_of_the_helpers(self):
        with in_languages("en-us", "zh-hans"):
            target_label(MentionSelection(EVERYONE))
            self.assertEqual(translation.get_language(), "zh-hans")


class MessageTextTests(FakeCatalogs, MessageTestBase):
    def render_in(self, site, viewer, operation, **kwargs):
        with in_languages(site, viewer):
            return self.render(operation, **kwargs)

    def test_the_phrases_of_a_ping_follow_the_site_language(self):
        template = make_template("{{ fleet_time }} | {{ doctrine }} | {{ ping_target_label }}")
        operation = self.operation(ping_template=template, ping_mention_mode=Mode.EVERYONE)
        expected = {"en-us": "Now | None / Custom | all", "zh-hans": "现在 | 无 / 自定义 | 所有人"}

        for site in SITE_LANGUAGES:
            for viewer in VIEWER_LANGUAGES:
                with self.subTest(site=site, viewer=viewer):
                    self.assertEqual(self.render_in(site, viewer, operation).ping, expected[site])

    def test_the_templates_are_rendered_in_the_site_language(self):
        # A template with translation tags of its own reads the language it is rendered in.
        source = "{% load i18n %}{% get_current_language as LANG %}{{ LANG }}"
        operation = self.operation(
            ping_template=make_template(source),
            motd_template=make_template(source, MessageTemplate.TemplateType.MOTD),
            ping_mention_mode=Mode.EVERYONE,
        )

        for site in SITE_LANGUAGES:
            for viewer in VIEWER_LANGUAGES:
                with self.subTest(site=site, viewer=viewer):
                    rendered = self.render_in(site, viewer, operation)
                    self.assertEqual((rendered.ping, rendered.motd), (site, site))

    def test_a_scheduled_fleet_names_its_time_whatever_the_language(self):
        template = make_template("{{ fleet_time }} | {{ fleet_time_eve }}")
        operation = self.operation(ping_template=template, scheduled_at=FLEET_TIME, ping_mention_mode=Mode.EVERYONE)

        for site in SITE_LANGUAGES:
            with self.subTest(site=site):
                self.assertEqual(
                    self.render_in(site, "zh-hans", operation).ping, "2026-10-12 19:00 EVE | 2026-10-12 19:00 EVE"
                )

    def test_the_built_in_ping_ends_with_who_it_went_to(self):
        operation = self.full_operation(ping_mention_mode=Mode.EVERYONE)

        english = self.render_in("en-us", "zh-hans", operation).ping
        chinese = self.render_in("zh-hans", "en", operation).ping

        self.assertTrue(english.endswith("from Boss Main to all at 2026-10-12 18:30 EVE"), english)
        self.assertTrue(chinese.endswith("from Boss Main to 所有人 at 2026-10-12 18:30 EVE"), chinese)

    def test_a_role_without_a_name_is_shown_in_the_site_language(self):
        operation = self.full_operation(ping_mention_mode=Mode.ROLES, ping_roles=[{"id": "111", "name": ""}])

        english = self.render_in("en-us", "zh-hans", operation)
        chinese = self.render_in("zh-hans", "en", operation)

        self.assertIn("@role 111", english.ping_display)
        self.assertIn("@身份组 111", chinese.ping_display)
        self.assertTrue(english.ping.startswith("<@&111>"))
        self.assertTrue(chinese.ping.startswith("<@&111>"))

    def test_what_is_sent_does_not_depend_on_the_viewer(self):
        for mode, roles in (
            (Mode.NONE, None),
            (Mode.EVERYONE, None),
            (Mode.HERE, None),
            (Mode.ROLES, [{"id": "111", "name": "Sig A"}, {"id": "222", "name": ""}]),
        ):
            operation = self.full_operation(ping_mention_mode=mode, ping_roles=roles)
            for site in SITE_LANGUAGES:
                results = [self.render_in(site, viewer, operation) for viewer in VIEWER_LANGUAGES]
                with self.subTest(mode=mode, site=site):
                    first, second = results
                    self.assertEqual(first.ping, second.ping)
                    self.assertEqual(first.motd, second.motd)
                    self.assertEqual(first.ping_display, second.ping_display)
                    self.assertEqual(first.errors, second.errors)
                    self.assertEqual((first.ping_length, first.motd_length), (second.ping_length, second.motd_length))

    def test_warnings_follow_the_viewer(self):
        operation = self.full_operation(additional_message="x" * 2100, ping_mention_mode=Mode.EVERYONE)

        for site in SITE_LANGUAGES:
            english = self.render_in(site, "en", operation)
            chinese = self.render_in(site, "zh-hans", operation)
            with self.subTest(site=site):
                self.assertEqual(
                    english.warnings[0],
                    f"The ping is {english.ping_length} characters; Discord accepts at most 2000. "
                    "Shorten the additional information.",
                )
                self.assertEqual(
                    chinese.warnings[0], f"通知有 {chinese.ping_length} 个字符，Discord 最多接受 2000 个。请缩短附加信息。"
                )
                self.assertIn("个字符，游戏可能会截断超过 1000 个字符的 MOTD。", chinese.warnings[1])
                self.assertTrue(english.warnings[1].startswith("The MOTD is "))
                self.assertEqual(len(english.warnings), len(chinese.warnings))

    def test_a_ping_that_is_posted_blocks_in_the_language_of_the_viewer(self):
        channel = f.ping_channel("STR", allow_everyone=True)
        operation = self.full_operation(
            additional_message="x" * 2100, ping_mention_mode=Mode.EVERYONE, ping_channel=channel
        )

        english = self.render_in("zh-hans", "en", operation)
        chinese = self.render_in("zh-hans", "zh-hans", operation)

        self.assertTrue(english.blocking[0].startswith("The ping is "))
        self.assertTrue(chinese.blocking[0].startswith("通知有 "))
        self.assertEqual(english.ping, chinese.ping)

    def test_the_missing_mention_warning_follows_the_viewer(self):
        template = make_template("Form up {{ formup }}")
        operation = self.operation(ping_template=template, ping_mention_mode=Mode.EVERYONE)

        english = self.render_in("zh-hans", "en", operation)
        chinese = self.render_in("en-us", "zh-hans", operation)

        self.assertEqual(english.warnings, [MISSING_MENTION])
        self.assertEqual(chinese.warnings, ["通知模板不含 {{ ping_mention }}，不会提醒任何人。"])

    def test_notes_about_a_broken_template_are_stored_text(self):
        broken = make_template("{% nope %}")
        operation = self.operation(ping_template=broken, ping_mention_mode=Mode.EVERYONE)

        english = self.render_in("zh-hans", "en", operation)
        chinese = self.render_in("zh-hans", "zh-hans", operation)

        self.assertEqual(english.errors, chinese.errors)
        self.assertEqual(len(english.errors), 1)
        self.assertIn("TemplateSyntaxError", english.errors[0])
        self.assertTrue(registered(english.errors[0]), english.errors[0])

    def test_the_cancellation_notice_follows_the_site_language(self):
        lead = f.create_user(perms=f.FC_LEAD_PERMS, main_name="Lead Main")
        operation = f.create_scheduled_operation(
            self.fc, scheduled_at=FLEET_TIME, type_obj=self.stratop, formup="Jita IV-4", ping_mention_mode=Mode.EVERYONE
        )
        english = (
            "**Cancelled:** StratOp at 2026-10-12 19:00 EVE (Jita IV-4)\n"
            "-# FleetOps notice from Lead Main at 2026-10-12 18:30 EVE"
        )
        chinese = (
            "**已取消：** StratOp，2026-10-12 19:00 EVE（Jita IV-4）\n"
            "-# 来自 Lead Main 的 FleetOps 通知，2026-10-12 18:30 EVE"
        )

        for site in SITE_LANGUAGES:
            for viewer in VIEWER_LANGUAGES:
                with self.subTest(site=site, viewer=viewer), in_languages(site, viewer):
                    text = render_cancellation_notice(operation, actor=lead, now=SENT)
                    self.assertEqual(text, chinese if site == "zh-hans" else english)

    def test_the_cancellation_notice_still_mentions_nobody(self):
        operation = f.create_scheduled_operation(
            self.fc, scheduled_at=FLEET_TIME, type_obj=self.stratop, formup="@everyone @here"
        )

        with in_languages("zh-hans", "zh-hans"):
            text = render_cancellation_notice(operation, now=SENT)

        self.assertNotIn("@everyone", text)
        self.assertNotIn("@here", text)


class CancellationPostTests(FakeCatalogs, SchedulingTestBase):
    def posted_in(self, site, viewer):
        operation = self.scheduled(scheduled_at=datetime(2099, 10, 12, 19, 0, tzinfo=dt_timezone.utc))
        self.discord_post.reset_mock()
        with in_languages(site, viewer):
            cancel_scheduled_fleet(operation, actor=self.fc, notify=True)
        [payload] = self.discord_payloads()
        return payload

    def test_the_posted_notice_follows_the_site_language_and_mentions_nobody(self):
        english = self.posted_in("en-us", "zh-hans")
        chinese = self.posted_in("zh-hans", "en")

        self.assertTrue(english["content"].startswith("**Cancelled:** StratOp at 2099-10-12 19:00 EVE (Amarr)"))
        self.assertTrue(chinese["content"].startswith("**已取消：** StratOp，2099-10-12 19:00 EVE（Amarr）"))
        for payload in (english, chinese):
            self.assertEqual(payload["allowed_mentions"], {"parse": []})
            self.assertEqual(payload["flags"], 4)


class SentTextTests(FakeCatalogs, StartFleetTestBase):
    NOW = datetime(2026, 10, 12, 18, 30, tzinfo=dt_timezone.utc)

    def start_and_collect(self, viewer):
        """Start a fleet as a request in ``viewer``, then end it; what was sent and stored."""
        self.discord_post.reset_mock()
        self.esi.put_calls.clear()
        with translation.override(viewer), frozen_now(self.NOW):
            operation = self.start()
        operation.refresh_from_db()
        collected = {
            "ping": operation.ping_text,
            "motd": operation.motd_text,
            "posted": [call.kwargs["json"] for call in self.discord_post.call_args_list],
            "game motd": [call["body"]["motd"] for call in self.esi.put_calls],
        }
        end_fleet(operation, actor=self.fc)
        return operation, collected

    def test_a_fleet_started_in_either_language_sends_the_same_text(self):
        for site in SITE_LANGUAGES:
            with override_settings(LANGUAGE_CODE=site):
                _op, english = self.start_and_collect("en")
                _op, chinese = self.start_and_collect("zh-hans")
            with self.subTest(site=site):
                self.assertEqual(english, chinese)
                self.assertTrue(english["posted"])
                self.assertIn("所有人" if site == "zh-hans" else " to all at ", english["ping"])

    def test_the_preview_shows_what_is_sent(self):
        from fleetops.services.messages import render_messages

        for site in SITE_LANGUAGES:
            with override_settings(LANGUAGE_CODE=site):
                previews = []
                for viewer in VIEWER_LANGUAGES:
                    with translation.override(viewer):
                        draft = build_unsaved_operation(self.fc, self.cleaned_data())
                        previews.append(render_messages(draft, actor=self.fc, now=self.NOW))
                _op, sent = self.start_and_collect("zh-hans")
            with self.subTest(site=site):
                self.assertEqual(previews[0].ping, previews[1].ping)
                self.assertEqual(previews[0].motd, previews[1].motd)
                self.assertEqual(previews[0].ping, sent["ping"])
                self.assertEqual(sent["posted"][0]["content"], sent["ping"])

    def test_a_motd_pushed_again_does_not_depend_on_the_viewer(self):
        with frozen_now(self.NOW):
            operation = self.start()
        for site in SITE_LANGUAGES:
            motds = []
            for viewer in VIEWER_LANGUAGES:
                self.esi.put_calls.clear()
                with in_languages(site, viewer), frozen_now(self.NOW):
                    regenerate_and_push_motd(operation, actor=self.fc)
                operation.refresh_from_db()
                motds.append((operation.motd_text, [call["body"]["motd"] for call in self.esi.put_calls]))
            with self.subTest(site=site):
                self.assertEqual(motds[0], motds[1])
                self.assertEqual(len(motds[0][1]), 1)
