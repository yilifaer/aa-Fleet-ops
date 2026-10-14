#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Print the CHANGELOG section of one version, for the notes of a GitHub Release.

Usage: python tools/changelog_section.py VERSION [--changelog PATH] [--allow-unreleased]

The section starts at a ``## [VERSION]`` or ``## VERSION`` heading and ends at the next ``##`` heading.
The command fails when the section is missing or empty, and when its heading still says "Unreleased":
the date is filled in when the release is made.
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADING = re.compile(r"^##\s+\[?(?P<version>[^\]\s]+)\]?(?P<rest>.*)$")


def same_version(left, right):
    left, right = left.lstrip("vV"), right.lstrip("vV")
    try:
        from packaging.version import Version
    except ImportError:
        return left == right
    try:
        return Version(left) == Version(right)
    except Exception:
        return left == right


def find_section(text, version):
    """``(heading, body)`` of the section of ``version``, or ``None``."""
    lines = text.splitlines()
    in_fence = False
    start = None
    heading = ""
    for index, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        if in_fence:
            continue
        match = HEADING.match(line)
        if not match or line.startswith("###"):
            continue
        if start is not None:
            return heading, "\n".join(lines[start:index]).strip()
        if same_version(match.group("version"), version):
            start = index + 1
            heading = line.strip()
    if start is not None:
        return heading, "\n".join(lines[start:]).strip()
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("version", help="version such as 0.2.0b1 (a leading v is ignored)")
    parser.add_argument("--changelog", default=str(ROOT / "CHANGELOG.md"))
    parser.add_argument("--allow-unreleased", action="store_true", help="accept a heading that says Unreleased")
    args = parser.parse_args(argv)

    section = find_section(Path(args.changelog).read_text(encoding="utf-8"), args.version)
    if section is None:
        print(f"error: no section for version {args.version} in {args.changelog}", file=sys.stderr)
        return 1
    heading, body = section
    if not body:
        print(f"error: the section '{heading}' is empty", file=sys.stderr)
        return 1
    if "unreleased" in heading.lower() and not args.allow_unreleased:
        print(f"error: the section '{heading}' is still unreleased; enter the release date", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
