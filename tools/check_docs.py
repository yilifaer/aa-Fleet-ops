#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Check that the links in the Markdown files of the repository resolve.

Usage: python tools/check_docs.py [--root PATH]

Checked: the Markdown files in the repository root, ``docs/`` and ``.github/``. Relative links must point to a
file or folder that exists, and ``#anchor`` parts must match a heading of the target file. Absolute links into
this repository (``https://github.com/yilifaer/aa-fleetops/blob/dev/...``, ``.../tree/dev/...``) are checked
the same way against the working tree. Links to other sites are not followed.
"""

import argparse
import posixpath
import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = "https://github.com/yilifaer/aa-fleetops"
SKIPPED_DIRECTORIES = {".git", ".venv", "venv", "node_modules", "build", "dist"}

INLINE_LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
REFERENCE_LINK = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$")
HTML_LINK = re.compile(r"""\b(?:href|src)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
CODE_SPAN = re.compile(r"(`+)(.+?)\1")
HTML_COMMENT = re.compile(r"<!--.*?-->")
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
HTML_ANCHOR = re.compile(r"""<a\s+[^>]*\b(?:id|name)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def markdown_files(root):
    found = sorted(root.glob("*.md"))
    for folder in ("docs", ".github"):
        if (root / folder).is_dir():
            found += sorted(
                p for p in (root / folder).rglob("*.md") if not SKIPPED_DIRECTORIES & set(p.relative_to(root).parts)
            )
    return found


def prose_lines(text):
    """``(line number, line)`` of the text outside fenced code blocks."""
    fence = None
    for number, line in enumerate(text.splitlines(), start=1):
        marker = line.lstrip()[:3]
        if marker in ("```", "~~~"):
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is None:
            yield number, line


def slugify(heading):
    """The anchor GitHub gives to a heading."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text)
    text = CODE_SPAN.sub(r"\2", text).replace("*", "").replace("~", "")
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(text):
    """The anchors of a Markdown file: its headings (repeats get -1, -2, ...) and explicit HTML anchors."""
    found = set()
    seen = {}
    for _number, line in prose_lines(text):
        match = HEADING.match(line)
        if match:
            slug = slugify(match.group(2))
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            found.add(slug if count == 0 else f"{slug}-{count}")
        found.update(HTML_ANCHOR.findall(line))
    return found


def targets(line):
    line = HTML_COMMENT.sub("", line)
    line = CODE_SPAN.sub("", line)
    found = INLINE_LINK.findall(line)
    reference = REFERENCE_LINK.match(line)
    if reference:
        found.append(reference.group(1))
    found += HTML_LINK.findall(line)
    return found


REPOSITORY_LINK = re.compile(
    rf"^{re.escape(REPOSITORY)}/(?:blob|tree|raw)/[^/#?]+/(?P<path>[^#?]*)(?:\?[^#]*)?(?:#(?P<fragment>.*))?$"
)


def repository_target(target):
    """``(path, fragment)`` for an absolute link into this repository, ``None`` for any other link."""
    match = REPOSITORY_LINK.match(target)
    if match is None:
        return None
    return unquote(match.group("path")).rstrip("/"), match.group("fragment") or ""


class Checker:
    def __init__(self, root):
        self.root = root
        self._anchors = {}
        self.problems = []

    def anchors_of(self, path):
        if path not in self._anchors:
            self._anchors[path] = anchors(path.read_text(encoding="utf-8"))
        return self._anchors[path]

    def check_file(self, path):
        text = path.read_text(encoding="utf-8")
        for number, line in prose_lines(text):
            for target in targets(line):
                problem = self.check_target(path, target)
                if problem:
                    self.problems.append(f"{path.relative_to(self.root).as_posix()}:{number}: {problem}")

    def check_target(self, source, target):
        target = target.strip()
        if not target or target.startswith("mailto:"):
            return None
        if target.startswith(("http://", "https://")):
            inside = repository_target(target)
            if inside is None:
                return None
            path_part, fragment = inside
            destination = (self.root / path_part).resolve()
        elif SCHEME.match(target):
            return None
        else:
            path_part, _, fragment = target.partition("#")
            path_part = unquote(path_part.partition("?")[0])
            if not path_part:
                destination = source
            elif path_part.startswith("/"):
                destination = (self.root / path_part.lstrip("/")).resolve()
            else:
                destination = (source.parent / posixpath.normpath(path_part)).resolve()
        try:
            destination.relative_to(self.root.resolve())
        except ValueError:
            return f"link leaves the repository: {target}"
        if not destination.exists():
            return f"broken link: {target}"
        if fragment and destination.is_file() and destination.suffix == ".md":
            if unquote(fragment).lower() not in self.anchors_of(destination):
                return f"no heading for the anchor in link: {target}"
        return None


def check(root):
    """Problems found in the Markdown files below ``root``."""
    checker = Checker(root)
    for path in markdown_files(root):
        checker.check_file(path)
    return checker.problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=str(ROOT), help="repository checkout (default: this one)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    problems = check(root)
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"ok: {len(markdown_files(root))} Markdown files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
