# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Stored messages: declaring, recognising and translating them, and the label tables."""

import ast
import random
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.template import Context, Template
from django.test import SimpleTestCase, override_settings
from django.utils import translation
from django.utils.functional import lazy

import fleetops
from fleetops import texts
from fleetops.services import fleet_controls, fleet_jobs, pap_links
from fleetops.templatetags import fleetops_i18n
from fleetops.texts import StoredText

PACKAGE_DIR = Path(fleetops.__file__).resolve().parent

CATALOG = {
    "zh-hans": {
        "Discord ping": "Discord 通知",
        "Could not read fleet members: %(detail)s": "无法读取舰队成员：%(detail)s",
        "ESI returned HTTP %(status)s (%(reason)s).": "ESI 返回 HTTP %(status)s（%(reason)s）。",
        "Ended manually": "手动结束",
        "%(step)s failed unexpectedly (%(error_type)s).": "%(step)s 意外失败（%(error_type)s）。",
    }
}


def fake_gettext(message):
    return CATALOG.get(translation.get_language(), {}).get(message, message)


fake_gettext_lazy = lazy(fake_gettext, str)


class IsolatedRegistryMixin:
    """Declare messages without leaving them in the registry of the application."""

    def setUp(self):
        super().setUp()
        for name in ("_ENTRIES", "_PATTERNS"):
            patcher = mock.patch.object(texts, name, list(getattr(texts, name)))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(texts, "_CONSTANTS", dict(texts._CONSTANTS))
        patcher.start()
        self.addCleanup(patcher.stop)
        texts.clear_cache()
        self.addCleanup(texts.clear_cache)


class FakeCatalogMixin:
    """Translate through ``CATALOG`` instead of the compiled catalogs."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(texts, "gettext", fake_gettext)
        patcher.start()
        self.addCleanup(patcher.stop)
        texts.clear_cache()
        self.addCleanup(texts.clear_cache)


class DeclarationTests(IsolatedRegistryMixin, SimpleTestCase):
    def test_a_constant_message_is_its_own_text(self):
        message = StoredText("Nothing to do.")

        self.assertEqual(message(), "Nothing to do.")
        self.assertEqual(message.params, ())
        self.assertEqual(str(message), "Nothing to do.")

    def test_parameters_are_filled_in_by_name(self):
        message = StoredText("Kicked %(count)s of %(total)s pilots.")

        self.assertEqual(message(count=2, total=5), "Kicked 2 of 5 pilots.")
        self.assertEqual(message.params, ("count", "total"))

    def test_a_percent_sign_is_written_twice(self):
        message = StoredText("Keeps %(share)s%% of the fleet.")

        self.assertEqual(message(share=40), "Keeps 40% of the fleet.")
        self.assertEqual(texts.find("Keeps 40% of the fleet.")[1], {"share": "40"})
        self.assertEqual(StoredText("Fully 100%% done.")(), "Fully 100% done.")

    def test_values_are_inserted_as_they_are(self):
        message = StoredText("Pilot %(name)s left.")

        self.assertEqual(message(name="100%s %(x)s {y}"), "Pilot 100%s %(x)s {y} left.")

    def test_wrong_parameters_are_refused(self):
        message = StoredText("Kicked %(name)s: %(reason)s")

        with self.assertRaises(TypeError):
            message(name="Bob")
        with self.assertRaises(TypeError):
            message(name="Bob", reason="gone", extra="x")

    def test_other_format_styles_are_refused(self):
        for msgid in ("Kicked %s pilots.", "Kicked %d pilots.", "Kicked 50% pilots.", "Kicked %(n)d pilots."):
            with self.subTest(msgid=msgid), self.assertRaises(ValueError):
                StoredText(msgid)

    def test_a_message_needs_words_of_its_own(self):
        for msgid in ("%(detail)s", "%(a)s%(b)s"):
            with self.subTest(msgid=msgid), self.assertRaises(ValueError):
                StoredText(msgid)

    def test_nested_names_have_to_be_placeholders(self):
        with self.assertRaises(ValueError):
            StoredText("Could not read: %(detail)s", nested=("reason",))

    def test_a_constant_message_with_a_percent_sign_reads_the_same_translated(self):
        StoredText("Fully 100%% done.")
        CATALOG["zh-hans"]["Fully 100%% done."] = "已完成 100%%。"
        self.addCleanup(CATALOG["zh-hans"].pop, "Fully 100%% done.")

        with mock.patch.object(texts, "gettext", fake_gettext), translation.override("zh-hans"):
            self.assertEqual(texts.translate_stored("Fully 100% done."), "已完成 100%。")

    def test_a_message_is_declared_once(self):
        StoredText("Declared once.")

        with self.assertRaises(ValueError):
            StoredText("Declared once.")

    def test_number_names_have_to_be_placeholders(self):
        with self.assertRaises(ValueError):
            StoredText("Moved %(count)s pilots.", numbers=("total",))

    def test_a_placeholder_is_a_number_or_a_nested_message(self):
        with self.assertRaises(ValueError):
            StoredText("Moved %(count)s pilots: %(detail)s", nested=("count",), numbers=("count",))

    def test_a_number_takes_a_whole_number_only(self):
        message = StoredText("Moved %(done)s of %(total)s pilots.", numbers=("done", "total"))

        self.assertEqual(message(done=2, total=5), "Moved 2 of 5 pilots.")
        self.assertEqual(message(done="2", total="5"), "Moved 2 of 5 pilots.")
        for bad in ("two", "", "2.5", 2.5, None, True, "-1"):
            with self.subTest(value=bad), self.assertRaises(TypeError):
                message(done=bad, total=5)

    def test_a_number_matches_digits_only(self):
        message = StoredText("Moved %(done)s of %(total)s pilots.", numbers=("done", "total"))

        self.assertIs(texts.find("Moved 2 of 5 pilots.")[0], message)
        self.assertEqual(texts.find("Moved 12 of 105 pilots.")[1], {"done": "12", "total": "105"})
        for text in ("Moved two of 5 pilots.", "Moved 2 of  pilots.", "Moved 2 of 5x pilots.", "Moved  of 5 pilots."):
            with self.subTest(text=text):
                self.assertIsNone(texts.find(text))

    def test_priority_needs_a_number(self):
        with self.assertRaises(ValueError):
            StoredText("Moved %(count)s pilots.", priority=True)
        with self.assertRaises(ValueError):
            StoredText("Moved %(count)s pilots.", nested=(), numbers=(), priority=True)

    def test_a_message_with_priority_is_tried_before_heavier_messages(self):
        heavy = StoredText("%(label)s: a very long and specific ending: %(done)s of %(total)s finished.")
        light = StoredText("%(label)s: %(done)s of %(total)s finished.", numbers=("done", "total"), priority=True)
        text = "Step: a very long and specific ending: 1 of 2 finished."

        self.assertGreater(heavy.weight, light.weight)
        self.assertIs(texts.find(text)[0], light)
        # Without digits the light message does not fit, so the heavy one is found as before.
        self.assertIs(texts.find("Step: a very long and specific ending: x of 2 finished.")[0], heavy)

    def test_a_plural_passes_numbers_and_priority_to_both_wordings(self):
        plural = texts.StoredPlural(
            "Moved its pilot of %(total)s.",
            "Moved %(count)s pilots of %(total)s.",
            numbers=("count", "total"),
            priority=True,
        )

        self.assertEqual(plural.plural.numbers, ("count", "total"))
        self.assertEqual(plural.singular.numbers, ("total",))
        self.assertTrue(plural.singular.priority and plural.plural.priority)
        self.assertEqual(plural(count=1, total=4), "Moved its pilot of 4.")
        self.assertEqual(plural(count=3, total=4), "Moved 3 pilots of 4.")
        with self.assertRaises(TypeError):
            plural(count="many", total=4)


class RecognitionTests(IsolatedRegistryMixin, SimpleTestCase):
    def test_the_whole_text_has_to_match(self):
        StoredText("Could not read the roster: %(detail)s")

        self.assertIsNotNone(texts.find("Could not read the roster: timeout"))
        self.assertIsNone(texts.find("Retry. Could not read the roster: timeout"))
        self.assertIsNone(texts.find("Could not read the roster"))
        self.assertIsNone(texts.find("could not read the roster: timeout"))

    def test_the_most_specific_message_wins(self):
        general = StoredText("Failed: %(detail)s")
        specific = StoredText("Failed: fleet %(fleet)s no longer exists.")

        self.assertIs(texts.find("Failed: fleet 12 no longer exists.")[0], specific)
        self.assertIs(texts.find("Failed: timeout")[0], general)

    def test_messages_of_equal_weight_keep_their_declaration_order(self):
        first = StoredText("Moved %(name)s to Alpha.")
        StoredText("Moved %(who)s to Bravo.")

        self.assertIs(texts.find("Moved Bob to Alpha.")[0], first)
        self.assertEqual(texts.find("Moved Bob to Bravo.")[1], {"who": "Bob"})

    def test_a_constant_beats_a_pattern(self):
        constant = StoredText("Failed: fleet gone")
        StoredText("Failed: %(detail)s")

        self.assertIs(texts.find("Failed: fleet gone")[0], constant)

    def test_a_repeated_placeholder_has_to_repeat_its_value(self):
        StoredText("%(name)s is %(name)s.")

        self.assertEqual(texts.find("Bob is Bob.")[1], {"name": "Bob"})
        self.assertIsNone(texts.find("Bob is Alice."))

    def test_values_can_be_empty_or_span_lines(self):
        StoredText("Result: %(detail)s|")

        self.assertEqual(texts.find("Result: |")[1], {"detail": ""})
        self.assertEqual(texts.find("Result: one\ntwo|")[1], {"detail": "one\ntwo"})

    def test_registered_checks_nested_messages_too(self):
        StoredText("Step %(step)s failed.", nested=("step",))
        StoredText("Known step")

        self.assertTrue(texts.registered("Step Known step failed."))
        self.assertFalse(texts.registered("Step Other step failed."))
        self.assertTrue(texts.registered("Step Other step failed.", deep=False))
        self.assertFalse(texts.registered("Something else"))
        self.assertFalse(texts.registered(""))
        self.assertFalse(texts.registered(None))

    def test_values_that_look_like_other_messages_keep_the_text_whole(self):
        StoredText("Kicked %(name)s.")
        StoredText("Kicked %(name)s from the fleet.")

        for name in ("Bob", "Bob. Kicked Eve", "a: b (c)", "100%", "Bob from the fleet"):
            for message in (texts.entries()[-2], texts.entries()[-1]):
                text = message(name=name)
                with self.subTest(text=text):
                    self.assertEqual(texts.translate_stored(text), text)


class TranslationTests(FakeCatalogMixin, SimpleTestCase):
    def test_a_constant_message_is_translated_in_the_viewer_language(self):
        with translation.override("zh-hans"):
            self.assertEqual(texts.translate_stored(texts.END_MANUAL()), "手动结束")
        with translation.override("en"):
            self.assertEqual(texts.translate_stored(texts.END_MANUAL()), "Ended manually")

    def test_parameters_survive_the_translation(self):
        text = texts.READ_MEMBERS_FAILED(detail="ESI did not respond in time.")

        with translation.override("zh-hans"):
            self.assertEqual(texts.translate_stored(text), "无法读取舰队成员：ESI did not respond in time.")

    def test_nested_messages_are_translated_on_their_own(self):
        detail = texts.ESI_HTTP(status=503, reason="Service Unavailable")
        text = texts.READ_MEMBERS_FAILED(detail=detail)

        with translation.override("zh-hans"):
            self.assertEqual(
                texts.translate_stored(text),
                "无法读取舰队成员：ESI 返回 HTTP 503（Service Unavailable）。",
            )

    def test_a_step_name_inside_a_failure_is_translated(self):
        text = texts.STEP_FAILED(step=texts.STEP_NAMES["Discord ping"](), error_type="ConnectionError")

        with translation.override("zh-hans"):
            self.assertEqual(texts.translate_stored(text), "Discord 通知 意外失败（ConnectionError）。")
        self.assertEqual(texts.translate_stored(text), text)

    def test_unknown_text_is_returned_unchanged(self):
        for text in ("Something a person typed", "ended manually", "Ended manually.", "Kicked Bob."):
            with self.subTest(text=text), translation.override("zh-hans"):
                self.assertEqual(texts.translate_stored(text), text)

    def test_text_that_only_looks_like_a_catalog_message_stays_English(self):
        # "Discord ping" is a message here, but a text is only translated when the registry knows it.
        CATALOG["zh-hans"]["Unregistered text"] = "未登记"
        self.addCleanup(CATALOG["zh-hans"].pop, "Unregistered text")

        with translation.override("zh-hans"):
            self.assertEqual(texts.translate_stored("Unregistered text"), "Unregistered text")

    def test_empty_and_non_text_values(self):
        with translation.override("zh-hans"):
            self.assertEqual(texts.translate_stored(None), "")
            self.assertEqual(texts.translate_stored(""), "")
            self.assertEqual(texts.translate_stored(42), "42")

    def test_the_result_follows_the_active_language(self):
        text = texts.END_MANUAL()

        with translation.override("zh-hans"):
            first = texts.translate_stored(text)
        with translation.override("en"):
            second = texts.translate_stored(text)
        with translation.override("zh-hans"):
            third = texts.translate_stored(text)

        self.assertEqual((first, second, third), ("手动结束", "Ended manually", "手动结束"))

    def test_without_an_active_language_the_text_is_unchanged(self):
        with translation.override(None):
            self.assertEqual(texts.translate_stored(texts.END_MANUAL()), "Ended manually")

    def test_a_translation_with_wrong_placeholders_falls_back_to_English(self):
        CATALOG["zh-hans"]["Ping channel %(name)s is disabled."] = "频道 %(wrong)s 已停用。"
        self.addCleanup(CATALOG["zh-hans"].pop, "Ping channel %(name)s is disabled.")
        text = texts.PING_CHANNEL_DISABLED(name="Main")

        with translation.override("zh-hans"):
            self.assertEqual(texts.translate_stored(text), text)

    def test_changing_the_catalogs_clears_the_cache(self):
        with translation.override("zh-hans"):
            self.assertEqual(texts.translate_stored(texts.END_MANUAL()), "手动结束")
            CATALOG["zh-hans"]["Ended manually"] = "已手动结束"
            self.addCleanup(CATALOG["zh-hans"].__setitem__, "Ended manually", "手动结束")
            self.assertEqual(texts.translate_stored(texts.END_MANUAL()), "手动结束")
            with override_settings(LOCALE_PATHS=[]):
                # Django forgets the active language together with its catalogs.
                translation.activate("zh-hans")
                self.assertEqual(texts.translate_stored(texts.END_MANUAL()), "已手动结束")


class DeclaredMessageTests(SimpleTestCase):
    """The messages declared in ``fleetops.texts`` itself."""

    def sample(self, entry, awkward=False):
        values = {}
        for name in entry.params:
            if name in entry.nested:
                values[name] = texts.STEP_NAMES["Discord ping"]()
            elif name in entry.numbers:
                values[name] = 7
            else:
                values[name] = "Bob: a. b (c) 100%" if awkward else f"sample {name}"
        return entry(**values)

    def test_there_are_messages(self):
        self.assertGreater(len(texts.entries()), 50)
        self.assertEqual(len({entry.msgid for entry in texts.entries()}), len(texts.entries()))

    def test_every_message_is_recognised_as_itself(self):
        for entry in texts.entries():
            text = self.sample(entry)
            with self.subTest(msgid=entry.msgid):
                found = texts.find(text)
                self.assertIsNotNone(found, text)
                self.assertIs(found[0], entry)
                self.assertTrue(texts.registered(text))
                if entry.pattern is not None:
                    same_weight = [
                        other for other in texts.entries() if other is not entry and other.weight == entry.weight
                    ]
                    self.assertEqual([other for other in same_weight if other.match(text) is not None], [])

    def test_a_message_reads_the_same_in_English_whatever_its_values(self):
        for entry in texts.entries():
            for awkward in (False, True):
                text = self.sample(entry, awkward)
                with self.subTest(msgid=entry.msgid, awkward=awkward), translation.override("en"):
                    self.assertEqual(texts.translate_stored(text), text)

    def test_a_character_without_a_name_reads_by_the_wording_of_the_language(self):
        text = texts.NOT_FLEET_BOSS(character="Bob", boss="character 3000123")

        with translation.override("en"):
            self.assertEqual(texts.translate_stored(text), text)
        with translation.override("zh-hans"):
            shown = texts.translate_stored(text)
            self.assertIn("角色 3000123", shown)
            self.assertNotIn("character", shown)
            # Only a label that is exactly that is changed; a name that has the words in it is not.
            self.assertEqual(texts.shown_name("Bob"), "Bob")
            self.assertEqual(texts.shown_name("character 3000123 Jr"), "character 3000123 Jr")
            self.assertEqual(texts.shown_name(""), "")
            self.assertIsNone(texts.shown_name(None))

    def test_step_names_are_messages_of_their_own_name(self):
        for name, entry in texts.STEP_NAMES.items():
            with self.subTest(name=name):
                self.assertEqual(entry(), name)

    def test_the_end_reasons_match_the_texts_shown_for_a_fleet(self):
        from fleetops.models import FleetOperation, end_reason_text

        reasons = FleetOperation.EndReason
        self.assertEqual(texts.END_MANUAL(), end_reason_text(reasons.MANUAL))
        self.assertEqual(texts.END_FLEET_GONE(), end_reason_text(reasons.FLEET_GONE))
        self.assertEqual(texts.END_BOSS_HANDOVER(name="Bob"), end_reason_text(reasons.BOSS_HANDOVER, "Bob"))
        self.assertTrue(texts.registered(end_reason_text(reasons.BOSS_HANDOVER)))

    def test_existing_texts_are_recognised(self):
        from fleetops.services import operations

        self.assertTrue(texts.registered(operations.NOT_RENDERED))
        self.assertTrue(texts.registered(operations.NO_PING_CHANNEL))
        self.assertTrue(texts.registered(operations.PING_NOT_REQUESTED))
        self.assertTrue(texts.registered(operations.EMPTY_PING))
        self.assertTrue(texts.registered(operations.START_ABANDONED_MESSAGE))
        self.assertTrue(texts.registered(operations.NOT_OWNED_MESSAGE))
        self.assertTrue(texts.registered(operations.not_boss_message("Bob", None)))
        for step in ("Discord ping", "MOTD update", "SRP creation"):
            for fleet in (SimpleNamespace(is_manual=True), SimpleNamespace(is_manual=False)):
                self.assertTrue(texts.registered(operations._skipped_message(fleet, step)))


class StepAndRoleLabelTests(SimpleTestCase):
    def step_codes(self):
        """Every code written to ``OperationAction.action`` in the application."""
        codes = set()
        for path in sorted(PACKAGE_DIR.rglob("*.py")):
            if "migrations" in path.parts or "__pycache__" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                    if name == "set_action" and len(node.args) > 1:
                        argument = node.args[1]
                        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                            codes.add(argument.value)
                    if name == "_send_ping":
                        codes.update(
                            keyword.value.value
                            for keyword in node.keywords
                            if keyword.arg == "action" and isinstance(keyword.value, ast.Constant)
                        )
                elif isinstance(node, ast.FunctionDef) and node.name == "_send_ping":
                    codes.update(
                        default.value for default in node.args.kw_defaults if isinstance(default, ast.Constant)
                    )
        # Codes set from variables.
        codes.update(action for action, _audit, _label in fleet_controls.PURPOSES.values())
        codes.update(action for action, _step in pap_links.LINK_ONLY_SKIPPED)
        codes.update(f"fleet_{kind}" for kind in fleet_jobs.RUNNERS)
        return codes

    def test_every_step_code_has_a_label(self):
        codes = self.step_codes()

        self.assertIn("discord_ping", codes)
        self.assertIn("kick_capsules", codes)
        self.assertIn("fleet_move", codes)
        self.assertEqual(sorted(codes - set(texts.STEP_LABELS)), [])

    def test_labels_are_readable_text(self):
        for code, label in texts.STEP_LABELS.items():
            with self.subTest(code=code):
                self.assertEqual(str(label), str(label).strip())
                self.assertNotIn("_", str(label))
                self.assertTrue(str(label)[0].isupper())

    def test_the_four_fleet_positions_have_labels(self):
        self.assertEqual(
            set(texts.FLEET_ROLE_LABELS), {"fleet_commander", "wing_commander", "squad_commander", "squad_member"}
        )
        from fleetops.services.fleet_rules import ROLE_LABELS

        self.assertEqual({code: str(label) for code, label in texts.FLEET_ROLE_LABELS.items()}, ROLE_LABELS)


class TemplateFilterTests(FakeCatalogMixin, SimpleTestCase):
    def render(self, source, **context):
        return Template("{% load fleetops_i18n %}" + source).render(Context(context))

    def test_stored_text_translates_and_escapes(self):
        with translation.override("zh-hans"):
            self.assertEqual(self.render("{{ value|stored_text }}", value=texts.END_MANUAL()), "手动结束")
        self.assertEqual(self.render("{{ value|stored_text }}", value="<b>x</b> & y"), "&lt;b&gt;x&lt;/b&gt; &amp; y")
        self.assertEqual(self.render("{{ value|stored_text }}", value=None), "")

    def test_step_label(self):
        self.assertEqual(self.render("{{ code|step_label }}", code="discord_ping"), "Discord ping")
        self.assertEqual(self.render("{{ code|step_label }}", code="some_new_step"), "Some new step")
        self.assertEqual(fleetops_i18n.step_label(None), "")

    def test_step_label_follows_the_language(self):
        with mock.patch.dict(texts.STEP_LABELS, {"discord_ping": fake_gettext_lazy("Discord ping")}):
            with translation.override("zh-hans"):
                self.assertEqual(fleetops_i18n.step_label("discord_ping"), "Discord 通知")
            with translation.override("en"):
                self.assertEqual(fleetops_i18n.step_label("discord_ping"), "Discord ping")

    def test_fleet_role_label(self):
        self.assertEqual(self.render("{{ code|fleet_role_label }}", code="squad_commander"), "Squad Commander")
        self.assertEqual(self.render("{{ code|fleet_role_label }}", code="unknown"), "unknown")
        self.assertEqual(fleetops_i18n.fleet_role_label(None), "")

    def test_fleetops_version_tag(self):
        self.assertEqual(self.render("{% fleetops_version %}"), fleetops.__version__)


class JobSummaryTranslationTests(SimpleTestCase):
    """The summary of a job reads as a summary in Chinese, whatever label stands in front of it (FB06.2)."""

    LABELS = {
        "MOVE_JOB": lambda count: texts.MOVE_JOB(count=count),
        "MOVE_JOB_TO": lambda count: texts.MOVE_JOB_TO(count=count, wing="FAX", squad="111"),
        "INVITE_JOB": lambda count: texts.INVITE_JOB(count=count),
        "INVITE_JOB_INTO": lambda count: texts.INVITE_JOB_INTO(count=count, wing="FAX", squad="111"),
        "KICK_JOB_CAPSULES": lambda count: texts.KICK_JOB_CAPSULES(count=count),
        "KICK_JOB_OUTSIDE": lambda count: texts.KICK_JOB_OUTSIDE(count=count, system="Jita"),
        "TEMPLATE_JOB": lambda count: texts.TEMPLATE_JOB(name="Standard"),
    }

    def shown(self, text):
        with translation.override("zh-hans"):
            return texts.translate_stored(text)

    def test_every_label_with_every_count_has_a_summary_in_chinese(self):
        for name, label in self.LABELS.items():
            for count in (1, 2):
                text = texts.JOB_SUMMARY(label=label(count), done=count, total=count)
                shown = self.shown(text)
                with self.subTest(label=name, count=count):
                    self.assertIn(f"已完成 {count} / {count}", shown)
                    self.assertNotIn(" of ", shown)
                    self.assertNotIn(" done", shown)
                    self.assertIs(texts.find(text)[0], texts.JOB_SUMMARY)
                    self.assertEqual(texts.find(text)[1]["label"], label(count))

    def test_every_label_with_failures_has_a_summary_in_chinese(self):
        for name, label in self.LABELS.items():
            for count in (1, 2):
                text = texts.JOB_SUMMARY_FAILED(label=label(count), done=count - 1, total=count, failed=1)
                shown = self.shown(text)
                with self.subTest(label=name, count=count):
                    self.assertIn(f"已完成 {count - 1} / {count}，失败 1", shown)
                    self.assertNotIn(" of ", shown)
                    self.assertNotIn(" done", shown)
                    self.assertNotIn("failed", shown)
                    self.assertIs(texts.find(text)[0], texts.JOB_SUMMARY_FAILED)

    def test_the_label_is_translated_inside_the_summary(self):
        text = texts.JOB_SUMMARY(label=texts.MOVE_JOB_TO(count=2, wing="FAX", squad="111"), done=2, total=2)

        self.assertEqual(self.shown(text), "把 2 名成员移到 FAX / 111：已完成 2 / 2。")

    def test_a_label_alone_still_translates(self):
        self.assertEqual(self.shown(texts.MOVE_JOB_TO(count=2, wing="FAX", squad="111")), "把 2 名成员移到 FAX / 111")

    def test_an_end_with_a_boss_handover_keeps_the_name(self):
        reason = texts.END_BOSS_HANDOVER(name="Pilot A")
        text = texts.FLEET_END_PAP(reason=reason, granted=2, multiplier=1)

        found = texts.find(text)
        self.assertIs(found[0], texts.FLEET_END_PAP)
        self.assertEqual(found[1]["reason"], reason)
        self.assertEqual(self.shown(text), "自动结束：舰队 Boss 已移交给 Pilot A。PAP：已发放 2，倍率 1 倍。")

    def test_an_end_with_the_other_reasons_translates(self):
        for reason in (texts.END_MANUAL(), texts.END_FLEET_GONE(), texts.END_BOSS_HANDOVER_UNKNOWN()):
            text = texts.FLEET_END_PAP(reason=reason, granted=3, multiplier=2)
            with self.subTest(reason=reason):
                shown = self.shown(text)
                self.assertTrue(shown.endswith("PAP：已发放 3，倍率 2 倍。"), shown)
                self.assertNotIn("PAP:", shown)

    def test_two_summaries_in_a_row_are_still_two_messages(self):
        first = texts.JOB_SUMMARY(label=texts.MOVE_JOB(count=2), done=2, total=2)
        second = texts.JOB_SUMMARY(label=texts.INVITE_JOB(count=1), done=1, total=1)

        self.assertEqual(self.shown(f"{first} {second}"), f"{self.shown(first)} {self.shown(second)}")

    def test_a_summary_followed_by_a_stop_is_still_two_messages(self):
        text = (
            texts.JOB_SUMMARY(label=texts.MOVE_JOB(count=2), done=1, total=2)
            + " "
            + texts.STOPPED(error=texts.JOB_WORKER_DOWN())
        )

        shown = self.shown(text)
        self.assertIn("已完成 1 / 2", shown)
        self.assertNotIn("done", shown)
        self.assertNotIn("Stopped", shown)


class LanguageHelperTests(SimpleTestCase):
    def test_outgoing_text_is_english(self):
        with translation.override("zh-hans"):
            with texts.outgoing_language():
                self.assertEqual(translation.get_language(), "en")
            self.assertEqual(translation.get_language(), "zh-hans")

    @override_settings(LANGUAGE_CODE="zh-hans")
    def test_outgoing_text_ignores_the_site_language(self):
        for viewer in ("en", "zh-hans"):
            with translation.override(viewer):
                with texts.outgoing_language():
                    self.assertEqual(translation.get_language(), "en")
                self.assertEqual(translation.get_language(), viewer)

    def test_user_language(self):
        user = SimpleNamespace(profile=SimpleNamespace(language="zh-hans"))

        self.assertEqual(texts.user_language(user), "zh-hans")
        self.assertEqual(texts.user_language(SimpleNamespace(profile=SimpleNamespace(language=""))), "en-us")
        self.assertEqual(texts.user_language(SimpleNamespace()), "en-us")
        self.assertEqual(texts.user_language(None), "en-us")

    @override_settings(LANGUAGE_CODE="de")
    def test_user_language_without_a_preference_is_the_site_language(self):
        self.assertEqual(texts.user_language(SimpleNamespace(profile=SimpleNamespace(language=""))), "de")


class StoredLanguageTests(SimpleTestCase):
    def test_stored_text_is_written_in_English(self):
        with translation.override("zh-hans"):
            with texts.stored_language():
                self.assertEqual(translation.get_language(), "en")
            self.assertEqual(translation.get_language(), "zh-hans")

    @override_settings(LANGUAGE_CODE="zh-hans")
    def test_the_site_language_does_not_change_stored_text(self):
        with texts.stored_language():
            self.assertEqual(translation.get_language(), "en")

    def test_a_label_helper_called_inside_it_reads_English(self):
        from fleetops.services import fleet_rules

        with translation.override("zh-hans"), texts.stored_language():
            self.assertEqual(str(fleet_rules.ROLE_LABELS["squad_commander"]), "Squad Commander")


class GenericMessageTests(IsolatedRegistryMixin, SimpleTestCase):
    def test_a_message_without_words_needs_a_nested_parameter(self):
        for msgid in ("%(who)s -> %(error)s", "%(a)s, %(b)s", "(%(a)s) - [%(b)s]"):
            with self.subTest(msgid=msgid), self.assertRaises(ValueError):
                StoredText(msgid)

        self.assertTrue(StoredText("%(who)s -> %(error)s", nested=("error",)).generic)

    def test_a_message_with_words_is_not_generic(self):
        self.assertFalse(StoredText("Removed %(name)s.").generic)
        self.assertFalse(StoredText("Fleet 7").generic)
        self.assertFalse(StoredText("Room for %(count)s").generic)

    def test_it_only_matches_when_the_nested_text_is_a_message(self):
        StoredText("Known failure")
        entry = StoredText("%(who)s -> %(error)s", nested=("error",))

        self.assertIs(texts.find("Bob -> Known failure")[0], entry)
        self.assertEqual(texts.find("Bob -> Known failure")[1], {"who": "Bob", "error": "Known failure"})
        self.assertIsNone(texts.find("Bob -> something a person typed"))
        self.assertTrue(texts.registered("Bob -> Known failure"))
        self.assertFalse(texts.registered("Bob -> something a person typed"))
        self.assertFalse(texts.registered("12 -> 30"))

    def test_ordinary_text_with_a_colon_is_left_alone(self):
        StoredText("Known failure")
        StoredText("%(who)s -> %(error)s", nested=("error",))

        for text in ("Note: remember the form-up", "Bob -> ", "-> Known failure x", "a -> b -> c"):
            with self.subTest(text=text), translation.override("zh-hans"):
                self.assertEqual(texts.translate_stored(text), text)

    def test_a_specific_message_still_beats_a_generic_one(self):
        StoredText("Known failure")
        generic = StoredText("%(who)s -> %(error)s", nested=("error",))
        specific = StoredText("Pilot %(name)s -> Known failure")

        self.assertIs(texts.find("Pilot Bob -> Known failure")[0], specific)
        self.assertIs(texts.find("Bob -> Known failure")[0], generic)


class SeriesTests(IsolatedRegistryMixin, FakeCatalogMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.created = StoredText("Fleet created.")
        self.sent = StoredText("Ping sent.")
        self.removed = StoredText("Removed %(name)s.")
        self.gone = StoredText("%(name)s is already gone")
        patcher = mock.patch.dict(
            CATALOG["zh-hans"],
            {
                "Fleet created.": "舰队已创建。",
                "Ping sent.": "通知已发送。",
                "Removed %(name)s.": "已移除 %(name)s。",
                "%(name)s is already gone": "%(name)s 已离开",
                "Result: %(detail)s": "结果：%(detail)s",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_messages_in_a_row_are_declared_messages(self):
        text = f"{self.created()} {self.sent()}"

        self.assertIsNone(texts.find(text))
        self.assertTrue(texts.registered(text))
        self.assertEqual(texts.split_series(text), ("Fleet created.", " ", "Ping sent."))

    def test_a_list_joined_by_semicolons_is_a_series(self):
        text = "; ".join([self.gone(name="Bob"), self.gone(name="Eve"), self.gone(name="Mia")])

        self.assertIsNotNone(texts.split_series(text))
        self.assertTrue(texts.registered(text))

    def test_a_series_can_mix_both_separators(self):
        text = f"{self.created()} {self.gone(name='Bob')}; {self.sent()}"

        self.assertEqual(
            texts.split_series(text), ("Fleet created.", " ", "Bob is already gone", "; ", "Ping sent.")
        )

    def test_one_part_that_is_not_a_message_makes_it_plain_text(self):
        for text in (
            "Fleet created. Something a person typed.",
            "Something a person typed. Ping sent.",
            "Fleet created.  Ping sent.",
            "Fleet created.Ping sent.",
            "Fleet created. Ping sent. Extra",
        ):
            with self.subTest(text=text):
                self.assertIsNone(texts.split_series(text))
                self.assertFalse(texts.registered(text))

    def test_text_without_a_separator_or_too_long_is_not_searched(self):
        self.assertIsNone(texts.split_series(""))
        self.assertIsNone(texts.split_series(None))
        self.assertIsNone(texts.split_series("Fleet created."))
        long_text = " ".join(["Fleet created."] * (texts.MAX_TEXT_LENGTH // 10))
        self.assertGreater(len(long_text), texts.MAX_TEXT_LENGTH)
        self.assertIsNone(texts.split_series(long_text))
        self.assertTrue(texts.registered(" ".join(["Fleet created."] * 20)))

    def test_a_name_with_a_full_stop_and_space_stays_one_message(self):
        text = self.removed(name="St. John")

        self.assertIsNone(texts.split_series(text))
        self.assertEqual(texts.find(text)[1], {"name": "St. John"})
        with translation.override("zh-hans"):
            self.assertEqual(texts.translate_stored(text), "已移除 St. John。")

    def test_each_part_is_translated_and_the_separators_stay(self):
        text = f"{self.created()} {self.sent()}"
        listed = "; ".join([self.gone(name="Bob"), self.gone(name="Eve")])

        with translation.override("zh-hans"):
            self.assertEqual(texts.translate_stored(text), "舰队已创建。 通知已发送。")
            self.assertEqual(texts.translate_stored(listed), "Bob 已离开; Eve 已离开")
        with translation.override("en"):
            self.assertEqual(texts.translate_stored(text), text)
            self.assertEqual(texts.translate_stored(listed), listed)

    def test_a_series_of_declared_messages_with_parameters(self):
        text = f"{self.removed(name='Bob')} {self.removed(name='Eve')} {self.created()}"

        with translation.override("zh-hans"):
            self.assertEqual(texts.translate_stored(text), "已移除 Bob。 已移除 Eve。 舰队已创建。")

    def test_a_series_inside_a_nested_parameter_is_translated(self):
        outer = StoredText("Result: %(detail)s", nested=("detail",))
        text = outer(detail=f"{self.created()} {self.sent()}")

        self.assertTrue(texts.registered(text))
        with translation.override("zh-hans"):
            self.assertEqual(texts.translate_stored(text), "结果：舰队已创建。 通知已发送。")

    def test_a_message_declared_later_counts_from_then_on(self):
        text = "Fleet created. Ping sent. Stopped early."

        self.assertIsNone(texts.split_series(text))
        StoredText("Stopped early.")

        self.assertEqual(len(texts.split_series(text)), 5)


class FixedWordsTests(SimpleTestCase):
    """Looking for a message's fixed words first must not change which texts match."""

    def test_the_check_of_the_fixed_words_agrees_with_the_expression(self):
        generator = random.Random(4)
        fillers = ["", "x", "a b", ": ", " of ", ". ", "; ", "\n"]
        for entry in texts.entries():
            if entry.pattern is None:
                continue
            for _ in range(25):
                pieces = [entry.literals[0]]
                for literal in entry.literals[1:]:
                    if generator.random() < 0.2:
                        pieces.append(generator.choice(entry.literals))
                    pieces.append(generator.choice(fillers))
                    pieces.append(literal)
                text = "".join(pieces)
                if generator.random() < 0.3:
                    text = text[: generator.randint(0, len(text))]
                with self.subTest(msgid=entry.msgid, text=text):
                    self.assertEqual(entry.match(text) is not None, entry.pattern.fullmatch(text) is not None)


class TextFromPeopleTests(SimpleTestCase):
    """Text that a person wrote is shown as written, and showing it stays quick, whatever it looks like."""

    TIME_LIMIT = 0.5

    def assertQuick(self, text):
        """Every way of asking about ``text`` finishes in good time; returns what the viewer would read."""
        shown = {}
        for language in ("en", "zh-hans"):
            texts.clear_cache()
            started = time.perf_counter()
            with translation.override(language):
                shown[language] = texts.translate_stored(text)
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, self.TIME_LIMIT, f"{elapsed:.2f} s for {text[:60]!r}")
        started = time.perf_counter()
        texts.registered(text)
        texts.split_series(text)
        texts.find(text)
        self.assertLess(time.perf_counter() - started, self.TIME_LIMIT)
        return shown

    def assertShownAsWritten(self, text):
        for language, shown in self.assertQuick(text).items():
            self.assertEqual(shown, text, language)
        self.assertFalse(texts.registered(text))
        self.assertIsNone(texts.split_series(text))

    def test_a_long_run_of_colons_does_not_run_out_of_stack(self):
        self.assertShownAsWritten(": " * 499 + "x")
        self.assertShownAsWritten(": " * 3000)

    def test_the_same_words_again_and_again_do_not_slow_the_search_down(self):
        self.assertShownAsWritten("x / y: " * 140)
        self.assertShownAsWritten("a: b of c done, " * 62)
        self.assertShownAsWritten(": , of done, " * 76)
        self.assertShownAsWritten("Skipped: a; " * 80)

    def test_the_fixed_words_of_every_message_repeated_stay_quick(self):
        for entry in texts.entries():
            if not entry.nested and not entry.generic:
                continue
            words = "".join(piece for piece in entry.literals if piece)
            for joiner in ("", " ", "; "):
                text = ((words + joiner) * 40)[:1000]
                with self.subTest(msgid=entry.msgid, joiner=joiner):
                    self.assertQuick(text)

    def test_messages_inside_messages_without_end_stay_quick_and_keep_their_end(self):
        text = "Could not read fleet members: " * 400 + "x"

        for language, shown in self.assertQuick(text).items():
            with self.subTest(language=language):
                self.assertTrue(shown.endswith("x"))

    def test_a_text_longer_than_any_message_is_not_searched(self):
        text = texts.READ_MEMBERS_FAILED(detail="x" * (texts.MAX_TEXT_LENGTH + 1))

        self.assertGreater(len(text), texts.MAX_TEXT_LENGTH)
        with translation.override("zh-hans"):
            self.assertEqual(texts.translate_stored(text), text)
        self.assertIsNone(texts.find(text))
        self.assertFalse(texts.registered(text))

    def test_messages_that_nest_as_deep_as_the_ones_in_use_are_translated(self):
        text = texts.JOB_SUMMARY(
            label=texts.STEP_NAMES["Discord ping"](), done=2, total=3
        )
        outer = texts.NAMED_ERROR(
            name="Wing 1", error=texts.READ_MEMBERS_FAILED(detail=texts.ESI_HTTP(status=503, reason="Unavailable"))
        )

        for sample in (text, outer):
            with self.subTest(sample=sample):
                self.assertTrue(texts.registered(sample))
                with translation.override("zh-hans"):
                    shown = texts.translate_stored(sample)
                self.assertNotEqual(shown, sample)
                self.assertNotIn("Could not", shown)

    def test_a_search_that_takes_too_long_gives_up_and_shows_the_text_as_written(self):
        text = " ".join(sorted(message for message in texts._CONSTANTS if message.endswith("."))[:8])

        self.assertTrue(texts.registered(text))
        with mock.patch.object(texts, "MAX_LOOKUPS", 2):
            texts.clear_cache()
            self.assertFalse(texts.registered(text))
            self.assertIsNone(texts.split_series(text))
            with translation.override("zh-hans"):
                self.assertEqual(texts.translate_stored(text), text)
        texts.clear_cache()
        with translation.override("zh-hans"):
            self.assertNotEqual(texts.translate_stored(text), text)


class PluralTests(IsolatedRegistryMixin, FakeCatalogMixin, SimpleTestCase):
    def test_a_plural_declares_both_wordings(self):
        before = len(texts.entries())
        plural = texts.StoredPlural("Moved %(count)s pilot.", "Moved %(count)s pilots.")

        self.assertEqual(len(texts.entries()), before + 2)
        self.assertEqual(plural.singular.msgid, "Moved %(count)s pilot.")
        self.assertEqual(plural.plural.msgid, "Moved %(count)s pilots.")
        self.assertIs(texts.find("Moved 1 pilot.")[0], plural.singular)
        self.assertIs(texts.find("Moved 5 pilots.")[0], plural.plural)

    def test_exactly_one_takes_the_singular_everything_else_the_plural(self):
        plural = texts.StoredPlural("Moved %(count)s pilot.", "Moved %(count)s pilots.")

        self.assertEqual(plural(count=1), "Moved 1 pilot.")
        self.assertEqual(plural(count="1"), "Moved 1 pilot.")
        self.assertEqual(plural(count=0), "Moved 0 pilots.")
        self.assertEqual(plural(count=2), "Moved 2 pilots.")
        self.assertEqual(plural(count=1001), "Moved 1001 pilots.")

    def test_the_singular_may_leave_the_number_out(self):
        plural = texts.StoredPlural("Move its member first.", "Move its %(count)s members first.")

        self.assertEqual(plural(count=1), "Move its member first.")
        self.assertEqual(plural(count=4), "Move its 4 members first.")
        self.assertIs(texts.find("Move its member first.")[0], plural.singular)

    def test_other_parameters_go_to_the_wording_that_uses_them(self):
        plural = texts.StoredPlural("Moved %(count)s pilot to %(unit)s.", "Moved %(count)s pilots to %(unit)s.")

        self.assertEqual(plural(count=1, unit="Alpha"), "Moved 1 pilot to Alpha.")
        self.assertEqual(plural(count=3, unit="Alpha"), "Moved 3 pilots to Alpha.")
        with self.assertRaises(TypeError):
            plural(count=3)
        with self.assertRaises(TypeError):
            plural(count=3, unit="Alpha", extra="x")

    def test_the_plural_wording_needs_the_number(self):
        with self.assertRaises(ValueError):
            texts.StoredPlural("Moved a pilot.", "Moved some pilots.")

    def test_another_name_can_hold_the_number(self):
        plural = texts.StoredPlural("%(total)s pilot left.", "%(total)s pilots left.", count="total")

        self.assertEqual(plural(total=1), "1 pilot left.")
        self.assertEqual(plural(total=2), "2 pilots left.")

    def test_nested_parameters_are_checked_in_both_wordings(self):
        StoredText("Known failure")
        plural = texts.StoredPlural(
            "%(count)s pilot failed: %(error)s", "%(count)s pilots failed: %(error)s", nested=("error",)
        )

        self.assertTrue(texts.registered(plural(count=1, error="Known failure")))
        self.assertTrue(texts.registered(plural(count=3, error="Known failure")))
        self.assertFalse(texts.registered(plural(count=3, error="Something typed")))

    def test_each_wording_is_translated_on_its_own(self):
        texts.StoredPlural("Moved %(count)s pilot.", "Moved %(count)s pilots.")
        patcher = mock.patch.dict(
            CATALOG["zh-hans"],
            {
                "Moved %(count)s pilot.": "已移动 %(count)s 名成员。",
                "Moved %(count)s pilots.": "已移动 %(count)s 名成员（多人）。",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        with translation.override("zh-hans"):
            self.assertEqual(texts.translate_stored("Moved 1 pilot."), "已移动 1 名成员。")
            self.assertEqual(texts.translate_stored("Moved 2 pilots."), "已移动 2 名成员（多人）。")
        with translation.override("en"):
            self.assertEqual(texts.translate_stored("Moved 2 pilots."), "Moved 2 pilots.")


class DeclaredPluralTests(SimpleTestCase):
    """The pairs declared in ``fleetops.texts`` itself."""

    def plurals(self):
        return {name: value for name, value in vars(texts).items() if isinstance(value, texts.StoredPlural)}

    def text(self, plural, count):
        params = {name: f"sample {name}" for name in plural.plural.params}
        for name in plural.plural.nested:
            params[name] = texts.STEP_NAMES["Discord ping"]()
        params[plural.count] = count
        return plural(**params)

    def test_there_are_pairs(self):
        self.assertGreater(len(self.plurals()), 5)

    def test_every_pair_picks_its_wording_by_the_count(self):
        for name, plural in self.plurals().items():
            for count, entry in ((1, plural.singular), (0, plural.plural), (2, plural.plural), (12, plural.plural)):
                text = self.text(plural, count)
                with self.subTest(name=name, count=count):
                    self.assertIs(texts.find(text)[0], entry, text)
                    self.assertTrue(texts.registered(text))

    def test_the_wordings_are_two_different_messages(self):
        for name, plural in self.plurals().items():
            with self.subTest(name=name):
                self.assertNotEqual(plural.singular.msgid, plural.plural.msgid)
                self.assertIn(f"%({plural.count})s", plural.plural.msgid)

    def test_both_wordings_read_the_same_in_English(self):
        for name, plural in self.plurals().items():
            for count in (1, 3):
                text = self.text(plural, count)
                with self.subTest(name=name, count=count), translation.override("en"):
                    self.assertEqual(texts.translate_stored(text), text)
