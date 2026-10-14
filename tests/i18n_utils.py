# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""A small reader and checker for gettext catalogs, shared by the translation tests."""

import gettext
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import fleetops

PACKAGE_DIR = Path(fleetops.__file__).resolve().parent
LOCALE_DIR = PACKAGE_DIR / "locale"

CONTEXT_SEPARATOR = "\x04"
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
_NAMED = re.compile(r"%\(\w+\)[#0\- +]*\d*(?:\.\d+)?[sdifrx]")
_POSITIONAL = re.compile(r"%[#0\- +]*\d*(?:\.\d+)?[sdifrx]")
_BRACES = re.compile(r"\{\w*\}")
_TAG = re.compile(r"</?[A-Za-z][^<>]*>")


class PoError(ValueError):
    """A catalog line that cannot be read."""


@dataclass
class PoEntry:
    line: int
    msgctxt: str | None = None
    msgid: str = ""
    msgid_plural: str | None = None
    msgstr: dict = field(default_factory=dict)  # plural index -> text
    flags: tuple = ()
    obsolete: bool = False

    @property
    def is_header(self):
        return self.msgid == "" and self.msgctxt is None

    @property
    def fuzzy(self):
        return "fuzzy" in self.flags

    @property
    def key(self):
        """The key the compiled catalog uses for this message."""
        if self.msgctxt is None:
            return self.msgid
        return f"{self.msgctxt}{CONTEXT_SEPARATOR}{self.msgid}"

    @property
    def translated(self):
        return bool(self.msgstr) and all(text != "" for text in self.msgstr.values())

    def label(self):
        return f"line {self.line}: {self.msgid[:60]!r}"


def _unquote(text, line):
    text = text.strip()
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        raise PoError(f"line {line}: expected a quoted string, got {text[:40]!r}")
    body = text[1:-1]
    out, index = [], 0
    while index < len(body):
        char = body[index]
        if char == "\\":
            index += 1
            if index >= len(body) or body[index] not in _ESCAPES:
                raise PoError(f"line {line}: unknown escape in {text[:40]!r}")
            char = _ESCAPES[body[index]]
        elif char == '"':
            raise PoError(f"line {line}: unescaped quote in {text[:40]!r}")
        out.append(char)
        index += 1
    return "".join(out)


def parse_po(text):
    """The entries of a catalog, header first. Obsolete entries (``#~``) are returned with ``obsolete`` set."""
    entries = []
    state = {"current": None, "target": None, "flags": (), "first": None}

    def finish():
        if state["current"] is not None:
            entries.append(state["current"])
        state.update(current=None, target=None, flags=(), first=None)

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        obsolete = line.startswith("#~")
        if obsolete:
            line = line[2:].strip()
        if not line:
            finish()
            continue
        current = state["current"]
        if line.startswith("#"):
            if current is not None and current.msgstr:
                finish()
            if state["current"] is None:
                state["first"] = state["first"] or number
                if line.startswith("#,"):
                    state["flags"] += tuple(flag.strip() for flag in line[2:].split(",") if flag.strip())
            continue
        if line.startswith('"'):
            if current is None or state["target"] is None:
                raise PoError(f"line {number}: a string without a keyword before it")
            attribute, index = state["target"]
            addition = _unquote(line, number)
            if attribute == "msgstr":
                current.msgstr[index] += addition
            else:
                setattr(current, attribute, getattr(current, attribute) + addition)
            continue
        keyword, _, rest = line.partition(" ")
        index = 0
        found = re.fullmatch(r"msgstr\[(\d+)\]", keyword)
        if found:
            keyword, index = "msgstr", int(found.group(1))
        if keyword not in {"msgctxt", "msgid", "msgid_plural", "msgstr"}:
            raise PoError(f"line {number}: unknown keyword {keyword!r}")
        value = _unquote(rest, number)
        if current is not None and current.msgstr and keyword in {"msgctxt", "msgid"}:
            finish()
            current = None
        if current is None:
            current = state["current"] = PoEntry(
                line=state["first"] or number, flags=state["flags"], obsolete=obsolete
            )
        if keyword == "msgstr":
            current.msgstr[index] = value
        else:
            setattr(current, keyword, value)
        state["target"] = (keyword, index)
    finish()
    return entries


def read_po(path):
    return parse_po(Path(path).read_text(encoding="utf-8"))


def header_fields(entries):
    """The header of a catalog as a dict (empty when there is none)."""
    for entry in entries:
        if entry.is_header:
            fields = {}
            for line in entry.msgstr.get(0, "").splitlines():
                name, _, value = line.partition(":")
                if value:
                    fields[name.strip()] = value.strip()
            return fields
    return {}


def placeholders(text):
    """Every format placeholder of ``text`` as a counter; ``%%`` is not a placeholder."""
    text = text.replace("%%", "")
    found = Counter(_NAMED.findall(text))
    text = _NAMED.sub("", text)
    found.update(_POSITIONAL.findall(text))
    found.update(_BRACES.findall(text))
    return found


def html_tags(text):
    """The HTML tags of ``text`` as a counter, with white space inside a tag collapsed."""
    return Counter(re.sub(r"\s+", " ", tag) for tag in _TAG.findall(text))


def entry_problems(entry, nplurals=None):
    """What is wrong with one entry, as a list of sentences."""
    problems = []
    if entry.fuzzy:
        problems.append("is marked fuzzy")
    if not entry.translated:
        problems.append("has an empty translation")
        return problems
    if entry.msgid_plural is not None and nplurals is not None and len(entry.msgstr) != nplurals:
        problems.append(f"has {len(entry.msgstr)} plural forms, the header says {nplurals}")
    for index, text in sorted(entry.msgstr.items()):
        # A language with one form uses msgstr[0] for every count, so it has to fit the plural text.
        use_plural = entry.msgid_plural is not None and (index > 0 or nplurals == 1)
        source = entry.msgid_plural if use_plural else entry.msgid
        if placeholders(text) != placeholders(source):
            problems.append(
                f"msgstr[{index}] has the placeholders {dict(placeholders(text))}, the source has {dict(placeholders(source))}"
            )
        if html_tags(text) != html_tags(source):
            problems.append(
                f"msgstr[{index}] has the HTML tags {dict(html_tags(text))}, the source has {dict(html_tags(source))}"
            )
    return problems


def catalog_problems(entries):
    """Everything wrong with a parsed catalog; an empty list means it can ship."""
    problems = []
    fields = header_fields(entries)
    if not fields:
        return ["has no header"]
    if any(entry.is_header and entry.fuzzy for entry in entries):
        problems.append("header is marked fuzzy")
    for name in ("Project-Id-Version", "Language", "Content-Type", "Plural-Forms"):
        if not fields.get(name):
            problems.append(f"header lacks {name}")
    if "charset=UTF-8" not in fields.get("Content-Type", ""):
        problems.append("header Content-Type is not UTF-8")
    nplurals = None
    found = re.search(r"nplurals\s*=\s*(\d+)", fields.get("Plural-Forms", ""))
    if found:
        nplurals = int(found.group(1))
    elif fields.get("Plural-Forms"):
        problems.append("Plural-Forms has no nplurals")
    seen = set()
    for entry in entries:
        if entry.is_header or entry.obsolete:
            continue
        if entry.key in seen:
            problems.append(f"{entry.label()} appears twice")
        seen.add(entry.key)
        problems.extend(f"{entry.label()} {problem}" for problem in entry_problems(entry, nplurals))
    return problems


def po_messages(entries):
    """The messages ``msgfmt`` compiles from ``entries``: key -> text, plural forms as (key, index) -> text."""
    messages = {}
    for entry in entries:
        if entry.is_header or entry.obsolete or entry.fuzzy or not entry.translated:
            continue
        if entry.msgid_plural is None:
            messages[entry.key] = entry.msgstr[0]
        else:
            for index, text in entry.msgstr.items():
                messages[(entry.key, index)] = text
    return messages


def mo_messages(path):
    """The messages of a compiled catalog in the same shape as :func:`po_messages` (header left out)."""
    with open(path, "rb") as handle:
        catalog = gettext.GNUTranslations(handle)._catalog
    return {key: value for key, value in catalog.items() if key != ""}


def catalog_pairs():
    """``(po path, mo path)`` of every catalog that ships in ``fleetops/locale``."""
    return [(po, po.with_suffix(".mo")) for po in sorted(LOCALE_DIR.glob("*/LC_MESSAGES/*.po"))]
