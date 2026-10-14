# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Read character names from a roster pasted out of the game client.

Three kinds of text are understood, line by line:

* the fleet composition window (Chinese or English client): seven tab separated columns,
  name / location / ship type / ship group / fleet role / fleet skills / position;
* a chat channel member list: one name per line;
* chat log lines such as ``[12:18:36] Name > text``, which give the speaker.

Parsing is pure text work. Looking the names up in Alliance Auth or ESI is left to the
caller.
"""

import re
from dataclasses import dataclass

from django.utils.translation import gettext, ngettext

MAX_NAME_LENGTH = 37
DEFAULT_MAX_LINES = 1000

SOURCE_FLEET = "fleet"
SOURCE_NAME_LIST = "name_list"
SOURCE_CHAT_LOG = "chat_log"

FLEET_COLUMNS = ("name", "location", "ship_type", "ship_group", "role", "skills", "position")

_DOCKED_RE = re.compile(r"\s*[(（]\s*(?:已停靠|docked)\s*[)）]\s*$", re.IGNORECASE)
# "[12:18:36] Name > text" as copied from a chat window, or "[ 2026.10.12 12:18:36 ] ..."
# as written to the chat log files.
_CHAT_LOG_RE = re.compile(r"^\[\s*\d[\d.:/\-\s]*\]\s*(?P<name>.+?)\s+>(?:\s|$)")
_HEADER_NAMES = frozenset({"name", "名字", "名称", "角色"})
_HEADER_SHIP_TYPES = frozenset({"ship type", "舰船类型"})
_SYSTEM_SPEAKERS = frozenset({"eve system", "eve系统"})
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿"))
_NAME_PUNCTUATION = frozenset(" -'._")


@dataclass(slots=True)
class RosterLine:
    name: str
    location: str = ""
    ship_type: str = ""
    ship_group: str = ""
    role: str = ""
    position: str = ""
    line_no: int = 0
    skills: str = ""
    docked: bool = False
    source: str = SOURCE_NAME_LIST
    error: str = ""

    @property
    def key(self) -> str:
        return name_key(self.name)

    @property
    def is_valid(self) -> bool:
        return not self.error


def clean_name(value: str) -> str:
    """Drop invisible characters and collapse the whitespace copied along with a name."""
    return " ".join((value or "").translate(_INVISIBLE).split())


def name_key(name: str) -> str:
    """Key under which two spellings of the same character name compare equal."""
    return clean_name(name).casefold()


def name_error(name: str) -> str:
    """Why ``name`` cannot be an EVE character name, or an empty string when it can."""
    if len(name) > MAX_NAME_LENGTH:
        return gettext("Character names are at most %(max)s characters long.") % {"max": MAX_NAME_LENGTH}
    if any(not (char.isalnum() or char in _NAME_PUNCTUATION) for char in name):
        return gettext("Contains characters that cannot appear in a character name.")
    return ""


def _fleet_row(line: str, line_no: int) -> RosterLine | None:
    cells = [cell.strip() for cell in line.split("\t")]
    cells += [""] * (len(FLEET_COLUMNS) - len(cells))
    values = dict(zip(FLEET_COLUMNS, cells))
    if values["name"].casefold() in _HEADER_NAMES or values["ship_type"].casefold() in _HEADER_SHIP_TYPES:
        return None
    location = values["location"]
    docked = _DOCKED_RE.search(location)
    if docked:
        location = location[: docked.start()].strip()
    return RosterLine(
        name=values["name"],
        location=location,
        ship_type=values["ship_type"],
        ship_group=values["ship_group"],
        role=values["role"],
        position=values["position"],
        line_no=line_no,
        skills=values["skills"],
        docked=bool(docked),
        source=SOURCE_FLEET,
    )


def _read_lines(text: str, warnings: list[str]) -> list[RosterLine]:
    rows = []
    for line_no, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.translate(_INVISIBLE)
        stripped = line.strip()
        if not stripped:
            continue
        match = _CHAT_LOG_RE.match(stripped)
        if match:
            rows.append(RosterLine(name=match["name"], line_no=line_no, source=SOURCE_CHAT_LOG))
        elif "\t" in line:
            row = _fleet_row(line, line_no)
            if row is None:
                continue
            if not clean_name(row.name):
                warnings.append(
                    gettext("Line %(line)s: no character name before the first tab, the line was skipped.")
                    % {"line": line_no}
                )
                continue
            rows.append(row)
        else:
            rows.append(RosterLine(name=stripped, line_no=line_no))
    return rows


def parse_roster(text: str, *, max_lines: int = DEFAULT_MAX_LINES) -> tuple[list[RosterLine], list[str]]:
    """Split a pasted roster into one ``RosterLine`` per character, plus warnings.

    Blank lines and header rows are skipped and names are deduplicated case-insensitively
    (the first occurrence wins). Names that cannot be EVE character names are kept with
    ``error`` set, so they can be listed as invalid. When the text holds chat log lines,
    the other plain lines are log headers or stray text and are ignored. At most
    ``max_lines`` rows are returned.
    """
    warnings = []
    candidates = _read_lines(text, warnings)
    has_chat_log = any(row.source == SOURCE_CHAT_LOG for row in candidates)

    lines = []
    seen = set()
    overflow = set()
    ignored = duplicates = 0
    for row in candidates:
        row.name = clean_name(row.name)
        if not any(char.isalnum() for char in row.name):
            # Separator lines such as "-----"
            continue
        if has_chat_log and row.source == SOURCE_NAME_LIST:
            ignored += 1
            continue
        key = row.name.casefold()
        if row.source == SOURCE_CHAT_LOG and key in _SYSTEM_SPEAKERS:
            continue
        if key in seen or key in overflow:
            # A speaker naturally shows up on many chat lines; only repeats elsewhere are worth a note.
            if row.source != SOURCE_CHAT_LOG:
                duplicates += 1
            continue
        if len(lines) >= max_lines:
            overflow.add(key)
            continue
        seen.add(key)
        row.error = name_error(row.name)
        lines.append(row)

    if ignored:
        warnings.append(
            ngettext(
                "Ignored %(count)s line that is not a chat message.",
                "Ignored %(count)s lines that are not chat messages.",
                ignored,
            )
            % {"count": ignored}
        )
    if duplicates:
        warnings.append(
            ngettext("Skipped %(count)s duplicate name.", "Skipped %(count)s duplicate names.", duplicates)
            % {"count": duplicates}
        )
    if overflow:
        warnings.append(
            ngettext(
                "Only the first %(max)s names are used, %(count)s more was ignored.",
                "Only the first %(max)s names are used, %(count)s more were ignored.",
                len(overflow),
            )
            % {"max": max_lines, "count": len(overflow)}
        )
    return lines, warnings
