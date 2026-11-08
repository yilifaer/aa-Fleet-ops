# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Plain text out of the rich text the game puts on the clipboard.

A fitting or a link copied in the game is HTML-like: ``<font ...>`` around ``<a href="fitting:...">name</a>``.
Pasted into a text field it is far too long, and cut off in the middle it leaves an open tag that would swallow
every line after it in a MOTD. :func:`plain_text` keeps only what a person reads; :func:`fitting_link` recognises
a complete fitting link.

Only the tags the game itself writes are removed (an allow-list of names); other text in angle brackets, such as an
alliance ticker ``<ABCD>``, a Discord timestamp ``<t:1700000000:R>`` or a comparison ``x<y and z>w``, stays as typed.
All patterns here match in linear time: a tag is ``<`` and a known name, then characters that are neither ``<`` nor
``>``, so there is nothing to backtrack into.
"""

import re

_NAMES = (
    "font|b|i|u|s|br|a|loc|color|size|url|span|center|left|right|p|strong|em|small|big|mark|strike|sub|sup|hr"
    "|ul|ol|li|div|img|h1|h2|h3|h4|h5|h6|showinfo|localized"
)
# A complete tag: <font color="#ffffff">, </font>, <br/>, <a href="...">, <loc>. The name must end there (a space, a
# "/" or the ">"), so "<a:x:1>" and "<t:1:R>" of Discord are text, and so is any name that is not on the list.
_TAG = re.compile(rf"</?(?:{_NAMES})(?=[\s/>])[^<>]*>", re.IGNORECASE)
# The rest of a tag that was cut off before its ">" (a text cut at 255 characters): up to the next "<" or the end.
_OPEN_TAG = re.compile(rf"</?(?:{_NAMES})(?=[\s/<]|$)[^<>]*", re.IGNORECASE)
_LINE_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_SPACES = re.compile(r"[^\S\n]+")
_FITTING = re.compile(r'<a\s+href\s*=\s*["\']fitting:([^"\'<>]*)["\'][^<>]*>([^<>]*)</a\s*>', re.IGNORECASE)


def plain_text(text, *, keep_lines: bool = False) -> str:
    """``text`` without the game's rich text tags, whitespace merged and trimmed.

    A link keeps its name (``<a href="...">name</a>`` gives ``name``), ``<br>`` is a space, or a line break with
    ``keep_lines`` (for a message of several lines, where only the spaces within a line are merged). A tag cut
    off before its ``>`` is dropped as well. Text that holds no tag comes back unchanged apart from the spaces.
    """
    text = str(text or "")
    if "<" in text:
        text = _LINE_BREAK.sub("\n", text)
        text = _TAG.sub("", text)
        text = _OPEN_TAG.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if keep_lines:
        lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return " ".join(text.split())


def fitting_link(text):
    """``(name, href)`` of the first complete ``<a href="fitting:...">name</a>`` in ``text``, else ``None``.

    ``href`` is the whole address (``fitting:...``); a link that is cut off, or has no name, is not recognised.
    """
    found = _FITTING.search(str(text or ""))
    if found is None:
        return None
    name = plain_text(found.group(2))
    if not name:
        return None
    return name, f"fitting:{found.group(1)}"
