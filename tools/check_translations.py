#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Check that the translation catalogs are complete and compiled.

Usage: python tools/check_translations.py [--no-update] [--root PATH]

First the catalogs are brought up to date with the source (``makemessages`` for ``django`` and ``djangojs``,
run inside the ``fleetops`` folder without a settings module so that it writes next to the app). Then every
``.po`` file is checked with ``msgattrib``: no untranslated and no fuzzy entry may be left. Last, every
committed ``.mo`` file is compared with what ``msgfmt`` makes of its ``.po`` file, so a change that was not
compiled (``compilemessages``) is found before it is released. Messages are printed as GitHub annotations when
the command runs in GitHub Actions. With ``--no-update`` the catalogs are checked as they are.

Needs the GNU gettext tools; the update step also needs Django.
"""

import argparse
import gettext
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "fleetops"
MAKEMESSAGES_OPTIONS = ["--no-obsolete", "--no-wrap", "--add-location", "file"]


def locales(root):
    """Names of the language folders in ``fleetops/locale``."""
    folder = root / PACKAGE / "locale"
    return sorted(p.name for p in folder.iterdir() if (p / "LC_MESSAGES").is_dir()) if folder.is_dir() else []


def catalogs(root):
    return sorted((root / PACKAGE / "locale").glob("*/LC_MESSAGES/*.po"))


def update_catalogs(root):
    """Run makemessages for both domains; returns True when both worked."""
    environment = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    languages = [argument for name in locales(root) for argument in ("-l", name)]
    commands = (
        [*languages, *MAKEMESSAGES_OPTIONS, "-i", "migrations"],
        [*languages, "-d", "djangojs", *MAKEMESSAGES_OPTIONS],
    )
    for options in commands:
        command = [sys.executable, "-m", "django", "makemessages", *options]
        result = subprocess.run(command, cwd=root / PACKAGE, env=environment, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"error: makemessages failed ({' '.join(options)})\n{result.stderr.strip()}", file=sys.stderr)
            return False
    return True


def string_value(lines):
    """The text of a PO string spread over quoted lines."""
    text = "".join(re.match(r'\s*"(.*)"\s*$', line).group(1) for line in lines)
    return text.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")


def entries(po_text):
    """``(location, context, msgid)`` of every entry in msgattrib output, without the header."""
    found = []
    for block in re.split(r"\n\s*\n", po_text):
        location = " ".join(line[3:].strip() for line in block.splitlines() if line.startswith("#:"))
        fields = {"msgctxt": [], "msgid": []}
        current = None
        for line in block.splitlines():
            keyword = re.match(r"(msgctxt|msgid_plural|msgid|msgstr(?:\[\d+\])?)\s+(\".*\")\s*$", line)
            if keyword:
                current = keyword.group(1) if keyword.group(1) in fields else None
                if current:
                    fields[current] = [keyword.group(2)]
            elif current and line.lstrip().startswith('"'):
                fields[current].append(line)
            elif not line.startswith("#"):
                current = None
        msgid = string_value(fields["msgid"]) if fields["msgid"] else ""
        if msgid:
            found.append((location, string_value(fields["msgctxt"]) if fields["msgctxt"] else "", msgid))
    return found


def incomplete(po_file, problem):
    """Entries of one catalog that are untranslated or fuzzy; ``problem`` is a msgattrib option."""
    result = subprocess.run(
        ["msgattrib", problem, "--no-obsolete", "--no-wrap", str(po_file)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"msgattrib failed for {po_file}: {result.stderr.strip()}")
    return entries(result.stdout)


def annotation(kind, file, title, message):
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::{kind} file={file},title={title}::{escaped}"


def check(root, report=print):
    """Number of incomplete entries found in the catalogs below ``root``."""
    on_github = os.environ.get("GITHUB_ACTIONS") == "true"
    total = 0
    for po_file in catalogs(root):
        shown = po_file.relative_to(root).as_posix()
        for option, title in (("--untranslated", "Untranslated message"), ("--only-fuzzy", "Fuzzy translation")):
            for location, context, msgid in incomplete(po_file, option):
                total += 1
                text = f"[{context}] {msgid}" if context else msgid
                if location:
                    text = f"{text} ({location})"
                report(annotation("error", shown, title, text) if on_github else f"{shown}: {title}: {text}")
    return total


def messages(mo_file):
    """What a compiled catalog translates: ``{msgid: msgstr}`` without the header, and its plural rule."""
    with open(mo_file, "rb") as handle:
        catalog = gettext.GNUTranslations(handle)
    found = {key: value for key, value in catalog._catalog.items() if key != ""}
    return found, catalog.info().get("plural-forms", "")


def check_compiled(root, report=print):
    """Number of compiled catalogs below ``root`` that differ from their ``.po`` file, or are missing or alone."""
    on_github = os.environ.get("GITHUB_ACTIONS") == "true"
    problems = []
    for po_file in catalogs(root):
        mo_file = po_file.with_suffix(".mo")
        shown = po_file.relative_to(root).as_posix()
        if not mo_file.is_file():
            problems.append((shown, f"{mo_file.name} is missing: run compilemessages"))
            continue
        with tempfile.TemporaryDirectory() as folder:
            fresh = Path(folder) / mo_file.name
            result = subprocess.run(["msgfmt", "-o", str(fresh), str(po_file)], capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"msgfmt failed for {po_file}: {result.stderr.strip()}")
            if messages(fresh) != messages(mo_file):
                problems.append((shown, f"{mo_file.name} differs from {po_file.name}: run compilemessages"))
    for mo_file in sorted((root / PACKAGE / "locale").glob("*/LC_MESSAGES/*.mo")):
        if not mo_file.with_suffix(".po").is_file():
            problems.append((mo_file.relative_to(root).as_posix(), "no .po file belongs to this compiled catalog"))
    for shown, message in problems:
        report(annotation("error", shown, "Compiled catalog", message) if on_github else f"{shown}: {message}")
    return len(problems)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-update", action="store_true", help="check the catalogs without running makemessages")
    parser.add_argument("--root", default=str(ROOT), help="repository checkout (default: this one)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    for tool in ("msgattrib", "msgfmt"):
        if shutil.which(tool) is None:
            print(f"error: the GNU gettext tools are not installed ({tool} not found)", file=sys.stderr)
            return 2
    if not catalogs(root):
        print(f"error: no .po files below {root / PACKAGE / 'locale'}", file=sys.stderr)
        return 2
    if not args.no_update and not update_catalogs(root):
        return 2
    total = check(root)
    if total:
        print(f"{total} message(s) still need a translation.", file=sys.stderr)
    stale = check_compiled(root)
    if stale:
        print(f"{stale} compiled catalog(s) do not match their source; run compilemessages.", file=sys.stderr)
    if total or stale:
        return 1
    print(f"ok: {len(catalogs(root))} catalog(s) complete and compiled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
