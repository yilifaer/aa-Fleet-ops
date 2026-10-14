# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Guards for the text of templates and scripts: nothing a person reads is left untranslated.

The tests read the source files. A text node or a text attribute of a template that holds an English word
outside the translation tags is a violation, and so is a script that puts a string literal into the page
without ``gettext``.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

from django.test import SimpleTestCase

import fleetops

PACKAGE = Path(fleetops.__file__).resolve().parent
TEMPLATE_DIR = PACKAGE / "templates"
STATIC_DIR = PACKAGE / "static" / "fleetops"

WORD = re.compile(r"[A-Za-z][A-Za-z']{2,}")
TEXT_ATTRIBUTES = {"title", "placeholder", "aria-label", "alt", "data-confirm"}

# Names and abbreviations that are the same in every language.
KEPT_WORDS = {
    "FleetOps",
    "PAP",
    "FC",
    "SRP",
    "MOTD",
    "ESI",
    "EVE",
    "ISK",
    "UTC",
    "Discord",
    "zKillboard",
    "EveWho",
    "Mumble",
    "SDE",
    "KM",
    "CSV",
    "Excel",
    "JavaScript",
}

_TEMPLATE_CODE = re.compile(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", re.S)
_TRANSLATED_BLOCK = re.compile(
    r"\{%\s*(?:blocktranslate|blocktrans)\b.*?%\}.*?\{%\s*(?:endblocktranslate|endblocktrans)\s*%\}", re.S
)
_COMMENT_BLOCK = re.compile(r"\{%\s*comment\b.*?%\}.*?\{%\s*endcomment\s*%\}", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_EMBEDDED = re.compile(r"<(script|style)\b.*?</\1\s*>", re.S | re.I)
_I18N_TAG = re.compile(r"\{%\s*(?:translate|trans|blocktranslate|blocktrans|get_current_language)\b")
_I18N_LOAD = re.compile(r"\{%\s*load\b[^%]*\bi18n\b")


def template_files():
    return sorted(TEMPLATE_DIR.rglob("*.html"))


def markup_without_template_code(source):
    """The HTML of a template once its tags, translated blocks, comments and scripts are removed."""
    source = _COMMENT_BLOCK.sub(" ", source)
    source = _TRANSLATED_BLOCK.sub(" ", source)
    source = _TEMPLATE_CODE.sub(" ", source)
    source = _HTML_COMMENT.sub(" ", source)
    return _EMBEDDED.sub(" ", source)


class TextCollector(HTMLParser):
    """Text nodes and the text attributes of an HTML fragment."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.texts = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        for name, value in attrs:
            if value and (name in TEXT_ATTRIBUTES or name.startswith("data-label-")):
                self.texts.append(value)
            elif value and name == "value" and tag == "input" and attributes.get("type") in {"submit", "button"}:
                self.texts.append(value)

    def handle_data(self, data):
        if data.strip():
            self.texts.append(data)


def untranslated_words(source):
    collector = TextCollector()
    collector.feed(markup_without_template_code(source))
    found = []
    for text in collector.texts:
        words = [word for word in WORD.findall(text) if word not in KEPT_WORDS]
        if words:
            found.append(" ".join(text.split())[:120])
    return found


class TemplateGuardTests(SimpleTestCase):
    def test_the_guard_finds_what_it_is_looking_for(self):
        source = (
            '{% load i18n %}<p title="Plain title">Plain text</p><p>{% translate "Fine" %}</p>'
            '<p>{% blocktranslate %}Also fine{% endblocktranslate %}</p><script>var x = "English";</script>'
            "{# a comment #}<!-- another --><input type=\"submit\" value=\"Send\">"
        )

        self.assertEqual(untranslated_words(source), ["Plain title", "Plain text", "Send"])

    def test_the_names_that_stay_are_not_flagged(self):
        self.assertEqual(untranslated_words("<p>FleetOps PAP FC SRP MOTD ESI EVE ISK Discord zKillboard</p>"), [])

    def test_templates_use_the_translation_tags_for_all_text(self):
        files = template_files()

        self.assertGreater(len(files), 60)
        for path in files:
            with self.subTest(template=str(path.relative_to(TEMPLATE_DIR))):
                self.assertEqual(untranslated_words(path.read_text(encoding="utf-8")), [])

    def test_templates_load_the_translation_tags_they_use(self):
        for path in template_files():
            source = path.read_text(encoding="utf-8")
            if _I18N_TAG.search(source):
                with self.subTest(template=str(path.relative_to(TEMPLATE_DIR))):
                    self.assertRegex(source, _I18N_LOAD)

    def test_counts_are_not_pluralised_with_a_filter(self):
        for path in template_files():
            with self.subTest(template=str(path.relative_to(TEMPLATE_DIR))):
                self.assertNotRegex(path.read_text(encoding="utf-8"), r"\|\s*pluralize")


_ASSIGNED_TEXT = re.compile(
    r"""\.(?P<target>textContent|innerText|title|placeholder|alt|ariaLabel)\s*=\s*
        (?P<value>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`[^`]*`)""",
    re.VERBOSE,
)
_DIALOG = re.compile(r"""\b(?P<target>alert|confirm|prompt)\(\s*(?P<value>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`[^`]*`)""")
_ATTRIBUTE_CALL = re.compile(
    r"""\.setAttribute\(\s*['"](?:title|placeholder|alt|aria-label)['"]\s*,\s*
        (?P<value>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`[^`]*`)""",
    re.VERBOSE,
)


def literal_texts(source):
    """The string literals that a script gives to the page as text, outside ``gettext``."""
    found = []
    for pattern in (_ASSIGNED_TEXT, _DIALOG, _ATTRIBUTE_CALL):
        for match in pattern.finditer(source):
            value = match.group("value")[1:-1]
            if [word for word in WORD.findall(re.sub(r"\$\{[^}]*\}", " ", value)) if word not in KEPT_WORDS]:
                found.append(value[:100])
    return found


class ScriptGuardTests(SimpleTestCase):
    def test_the_guard_finds_what_it_is_looking_for(self):
        source = (
            "el.textContent = 'Saved'; el.title = gettext('Fine'); alert('Oops'); confirm(gettext('Sure'));"
            " el.setAttribute('title', 'Hello there'); el.textContent = `${n}`; el.placeholder = '';"
        )

        self.assertEqual(literal_texts(source), ["Saved", "Oops", "Hello there"])

    def test_scripts_take_their_texts_from_the_catalog(self):
        files = sorted(STATIC_DIR.glob("*.js"))

        self.assertGreaterEqual(len(files), 8)
        for path in files:
            with self.subTest(script=path.name):
                self.assertEqual(literal_texts(path.read_text(encoding="utf-8")), [])
