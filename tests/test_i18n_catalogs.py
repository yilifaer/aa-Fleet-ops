# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The catalogs in fleetops/locale are complete, consistent and compiled."""

import gettext
import re
from pathlib import Path

from django.apps import apps
from django.test import SimpleTestCase, tag

from . import i18n_extract as extract
from . import i18n_utils as po

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")

HEADER = (
    'msgid ""\n'
    'msgstr ""\n'
    '"Project-Id-Version: AA FleetOps\\n"\n'
    '"Language: zh_Hans\\n"\n'
    '"Content-Type: text/plain; charset=UTF-8\\n"\n'
    '"Plural-Forms: nplurals=1; plural=0;\\n"\n'
)


def problems(body, header=HEADER):
    return po.catalog_problems(po.parse_po(header + "\n" + body))


class ParserTests(SimpleTestCase):
    def test_entries_with_contexts_plurals_flags_and_continuations(self):
        entries = po.parse_po(
            HEADER
            + '\n#: fleetops/a.py\n#, python-format\nmsgctxt "fleet status"\nmsgid "Active %(n)s"\nmsgstr ""\n"进行中 "\n"%(n)s"\n'
            + '\nmsgid "One"\nmsgid_plural "Many"\nmsgstr[0] "多"\n'
            + '\n#, fuzzy\nmsgid "Guess"\nmsgstr "猜"\n'
            + '\n#~ msgid "Old"\n#~ msgstr "旧"\n'
        )

        self.assertEqual(len(entries), 5)
        header, status, plural, fuzzy, old = entries
        self.assertTrue(header.is_header)
        self.assertEqual((status.msgctxt, status.msgid, status.msgstr), ("fleet status", "Active %(n)s", {0: "进行中 %(n)s"}))
        self.assertEqual(status.flags, ("python-format",))
        self.assertEqual(status.key, "fleet status\x04Active %(n)s")
        self.assertEqual((plural.msgid, plural.msgid_plural, plural.msgstr), ("One", "Many", {0: "多"}))
        self.assertTrue(fuzzy.fuzzy)
        self.assertTrue(old.obsolete)
        self.assertEqual(old.msgstr, {0: "旧"})

    def test_escapes_are_read(self):
        entries = po.parse_po(HEADER + '\nmsgid "a\\"b\\n"\nmsgstr "c\\\\d\\t"\n')

        self.assertEqual((entries[1].msgid, entries[1].msgstr[0]), ('a"b\n', "c\\d\t"))

    def test_unreadable_lines_are_reported(self):
        for body in ('msgid "a"\nmsgstr b\n', 'msgid "a"\nmsgstrr "b"\n', '"stray"\n', 'msgid "a\\q"\nmsgstr "b"\n'):
            with self.subTest(body=body), self.assertRaises(po.PoError):
                po.parse_po(body)

    def test_placeholders(self):
        self.assertEqual(
            po.placeholders("%(a)s of %(b)d, 100%% and %s {name} {}"),
            {"%(a)s": 1, "%(b)d": 1, "%s": 1, "{name}": 1, "{}": 1},
        )
        self.assertEqual(po.placeholders("%(a)s %(a)s")["%(a)s"], 2)
        self.assertEqual(po.placeholders("no marks"), {})

    def test_html_tags(self):
        self.assertEqual(
            po.html_tags('See <a  href="%(url)s">the <b>page</b></a>.'),
            {'<a href="%(url)s">': 1, "<b>": 1, "</b>": 1, "</a>": 1},
        )
        self.assertEqual(po.html_tags("1 < 2 and 3 > 2"), {})


class CheckerTests(SimpleTestCase):
    def test_a_clean_catalog_has_no_problems(self):
        body = (
            'msgid "Save"\nmsgstr "保存"\n\n'
            'msgid "Kicked %(count)s <b>pilots</b>"\nmsgstr "已踢出 %(count)s 名<b>飞行员</b>"\n\n'
            'msgid "One pilot"\nmsgid_plural "%(count)s pilots"\nmsgstr[0] "%(count)s 名飞行员"\n'
        )

        self.assertEqual(problems(body), [])

    def test_untranslated_and_fuzzy_entries_are_problems(self):
        found = problems('msgid "Save"\nmsgstr ""\n\n#, fuzzy\nmsgid "Cancel"\nmsgstr "取消"\n')

        self.assertEqual(len(found), 2)
        self.assertTrue(any("Save" in line and "empty translation" in line for line in found))
        self.assertTrue(any("Cancel" in line and "fuzzy" in line for line in found))

    def test_placeholders_have_to_match(self):
        for msgid, msgstr in (
            ("Kicked %(count)s", "已踢出"),
            ("Kicked %(count)s", "已踢出 %(number)s"),
            ("Kicked %(count)s", "已踢出 %(count)s %(count)s"),
            ("Kicked %s", "已踢出 %d"),
            ("Kicked {name}", "已踢出"),
        ):
            with self.subTest(msgid=msgid, msgstr=msgstr):
                found = problems(f'msgid "{msgid}"\nmsgstr "{msgstr}"\n')
                self.assertEqual(len(found), 1)
                self.assertIn("placeholders", found[0])

    def test_html_tags_have_to_match(self):
        for msgstr in ("<b>已踢出", "已踢出</b>", "<i>已踢出</i>", "已踢出"):
            with self.subTest(msgstr=msgstr):
                found = problems(f'msgid "<b>Kicked</b>"\nmsgstr "{msgstr}"\n')
                self.assertEqual(len(found), 1)
                self.assertIn("HTML tags", found[0])

    def test_links_keep_their_address(self):
        found = problems('msgid "<a href=\\"%(url)s\\">Open</a>"\nmsgstr "<a href=\\"/x/\\">打开</a>"\n')

        self.assertTrue(found)

    def test_a_language_with_one_plural_form_uses_the_plural_text(self):
        self.assertEqual(
            problems('msgid "One pilot"\nmsgid_plural "%(count)s pilots"\nmsgstr[0] "%(count)s 名"\n'), []
        )
        self.assertTrue(problems('msgid "One pilot"\nmsgid_plural "%(count)s pilots"\nmsgstr[0] "一名"\n'))
        self.assertTrue(
            problems('msgid "One pilot"\nmsgid_plural "%(count)s pilots"\nmsgstr[0] "%(count)s 名"\nmsgstr[1] "x"\n')
        )

    def test_a_language_with_two_forms_checks_each_form(self):
        header = HEADER.replace("nplurals=1; plural=0;", "nplurals=2; plural=(n != 1);")
        entry = 'msgid "One pilot"\nmsgid_plural "%(count)s pilots"\nmsgstr[0] "Ein Pilot"\nmsgstr[1] "%(count)s Piloten"\n'

        self.assertEqual(problems(entry, header), [])
        self.assertTrue(problems(entry.replace("%(count)s Piloten", "Piloten"), header))
        self.assertTrue(problems(entry.replace('msgstr[1] "%(count)s Piloten"\n', ""), header))

    def test_the_same_message_twice_is_a_problem(self):
        found = problems('msgid "Save"\nmsgstr "保存"\n\nmsgid "Save"\nmsgstr "存"\n')

        self.assertEqual(len(found), 1)
        self.assertIn("twice", found[0])

    def test_the_same_text_in_two_contexts_is_fine(self):
        body = 'msgctxt "a"\nmsgid "Open"\nmsgstr "打开"\n\nmsgctxt "b"\nmsgid "Open"\nmsgstr "开放"\n'

        self.assertEqual(problems(body), [])

    def test_the_header_is_required_and_has_to_be_complete(self):
        self.assertEqual(po.catalog_problems(po.parse_po('msgid "a"\nmsgstr "b"\n')), ["has no header"])
        found = po.catalog_problems(po.parse_po('msgid ""\nmsgstr ""\n"Language: zh_Hans\\n"\n'))
        self.assertEqual(len(found), 4)
        self.assertEqual(len(problems("", HEADER.replace("UTF-8", "ISO-8859-1"))), 1)

    def test_a_fuzzy_header_is_a_problem(self):
        self.assertEqual(po.catalog_problems(po.parse_po("#, fuzzy\n" + HEADER)), ["header is marked fuzzy"])

    def test_obsolete_entries_are_ignored(self):
        self.assertEqual(problems('#~ msgid "Old"\n#~ msgstr ""\n'), [])

    def test_compiled_messages_have_the_shape_of_the_source(self):
        entries = po.parse_po(
            HEADER
            + '\nmsgid "Save"\nmsgstr "保存"\n'
            + '\nmsgctxt "c"\nmsgid "Open"\nmsgstr "打开"\n'
            + '\nmsgid "One"\nmsgid_plural "Many"\nmsgstr[0] "多"\n'
            + '\n#, fuzzy\nmsgid "Guess"\nmsgstr "猜"\n'
            + '\nmsgid "Later"\nmsgstr ""\n'
        )

        self.assertEqual(po.po_messages(entries), {"Save": "保存", "c\x04Open": "打开", ("One", 0): "多"})


class ShippedCatalogTests(SimpleTestCase):
    def test_the_catalogs_are_found(self):
        pairs = po.catalog_pairs()

        self.assertTrue(pairs)
        self.assertIn("zh_Hans", {path.parents[1].name for path, _ in pairs})
        for language in {path.parents[1] for path, _ in pairs}:
            self.assertEqual(
                sorted(path.name for path in (language / "LC_MESSAGES").glob("*.po")), ["django.po", "djangojs.po"]
            )

    def test_every_catalog_is_complete_and_consistent(self):
        for path, _mo in po.catalog_pairs():
            with self.subTest(catalog=str(path.relative_to(po.LOCALE_DIR))):
                self.assertEqual(po.catalog_problems(po.read_po(path)), [])

    def test_the_header_names_the_project_and_the_language(self):
        for path, _mo in po.catalog_pairs():
            with self.subTest(catalog=str(path.relative_to(po.LOCALE_DIR))):
                fields = po.header_fields(po.read_po(path))
                self.assertEqual(fields["Project-Id-Version"], "AA FleetOps")
                self.assertEqual(fields["Language"], path.parents[1].name)

    def test_zh_hans_has_one_plural_form(self):
        for domain in ("django", "djangojs"):
            with self.subTest(domain=domain):
                fields = po.header_fields(po.read_po(po.LOCALE_DIR / "zh_Hans" / "LC_MESSAGES" / f"{domain}.po"))
                self.assertEqual(fields["Plural-Forms"], "nplurals=1; plural=0;")

    def test_no_personal_names_or_addresses_remain(self):
        for path, _mo in po.catalog_pairs():
            text = path.read_text(encoding="utf-8")
            with self.subTest(catalog=str(path.relative_to(po.LOCALE_DIR))):
                for marker in ("FIRST AUTHOR", "FULL NAME", "EMAIL@ADDRESS", "Last-Translator", "Language-Team", "YEAR"):
                    self.assertNotIn(marker, text)
                self.assertIsNone(EMAIL.search(text), "an e-mail address in the catalog")

    def test_every_catalog_is_compiled_and_up_to_date(self):
        for path, mo in po.catalog_pairs():
            with self.subTest(catalog=str(path.relative_to(po.LOCALE_DIR))):
                self.assertTrue(mo.is_file(), f"{mo.name} is missing: run compilemessages")
                self.assertEqual(
                    po.mo_messages(mo),
                    po.po_messages(po.read_po(path)),
                    f"{mo.name} differs from {path.name}: run compilemessages",
                )

    def test_there_is_no_compiled_catalog_without_a_source(self):
        sources = {path for path, _mo in po.catalog_pairs()}

        for mo in po.LOCALE_DIR.glob("*/LC_MESSAGES/*.mo"):
            self.assertIn(mo.with_suffix(".po"), sources)


@tag("catalog")
class CatalogMatchesCodeTests(SimpleTestCase):
    """The catalog holds the messages of the code and nothing else: a forgotten ``makemessages`` fails here."""

    @classmethod
    def code_messages(cls):
        found = {}
        for path in extract.python_files():
            found.update(extract.python_messages(path))
        for path in extract.template_files():
            found.update(extract.template_messages(path))
        return found

    def test_the_extraction_finds_the_messages_of_the_code(self):
        found = self.code_messages()

        self.assertGreater(len(found), 2000)
        self.assertIn("Dashboard", {key.split(po.CONTEXT_SEPARATOR)[-1] for key in found})
        self.assertTrue(any(po.CONTEXT_SEPARATOR in key for key in found), "messages with a context")

    def test_every_message_of_the_code_is_in_the_catalog(self):
        missing = sorted(set(self.code_messages()) - extract.catalog_keys("django"))

        self.assertEqual(missing, [], "run makemessages and translate the new messages")

    def test_the_catalog_has_no_message_the_code_does_not_use(self):
        stale = sorted(extract.catalog_keys("django") - set(self.code_messages()))

        self.assertEqual(stale, [], "run makemessages to drop the messages the code no longer uses")

    def test_common_words_keep_the_wording_of_fleetops(self):
        # Other apps translate these words too, and their catalog could win; the context keeps these apart.
        entries = {entry.key: entry for entry in po.read_po(po.LOCALE_DIR / "zh_Hans" / "LC_MESSAGES" / "django.po")}

        for word, wording in (("Corporation", "军团"), ("Alliance", "联盟"), ("Dashboard", "总览"), ("Name", "名称")):
            with self.subTest(word=word):
                self.assertEqual(entries[f"FleetOps{po.CONTEXT_SEPARATOR}{word}"].msgstr[0], wording)

    def test_the_catalogs_of_other_apps_do_not_change_the_wording_of_fleetops(self):
        # Django merges the catalogs of the installed apps and the app listed first wins. FleetOps comes last,
        # so a message that another app translates differently would show that app's wording on FleetOps pages.
        # Common words such as "Name" or "Corporation" carry the context "FleetOps" to keep them apart.
        ours = {}
        for entry in po.read_po(po.LOCALE_DIR / "zh_Hans" / "LC_MESSAGES" / "django.po"):
            if not entry.is_header and entry.msgctxt is None:
                ours[entry.msgid] = entry.msgstr.get(0, "")
        shadowed = {}
        for config in apps.get_app_configs():
            if config.name == "fleetops":
                break
            path = Path(config.path) / "locale" / "zh_Hans" / "LC_MESSAGES" / "django.mo"
            if not path.is_file():
                continue
            with path.open("rb") as handle:
                other = gettext.GNUTranslations(handle)._catalog
            for msgid, wording in ours.items():
                if msgid in other and other[msgid] != wording:
                    shadowed.setdefault(msgid, config.name)

        self.assertEqual(
            shadowed, {}, "give these messages the context \"FleetOps\" (pgettext) so that no other app replaces them"
        )
