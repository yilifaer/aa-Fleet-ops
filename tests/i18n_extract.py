# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Find the messages that FleetOps code asks the catalog for, without gettext's own tools.

The tests use this to check that the catalogs hold every message of the code. ``makemessages`` stays the
tool that updates them; this reads the same call forms: ``gettext``, ``ngettext``, ``pgettext`` and
``npgettext`` (also the lazy and ``noop`` forms) in Python, the translation tags in templates and the
``gettext`` family of the browser in scripts.
"""

import ast
import re
from pathlib import Path

from django.utils.translation.template import templatize

from . import i18n_utils as po

PACKAGE = po.PACKAGE_DIR
SEPARATOR = po.CONTEXT_SEPARATOR

# call name -> (context argument?, plural argument?)
CALLS = {
    "gettext": (False, False),
    "gettext_lazy": (False, False),
    "gettext_noop": (False, False),
    "_": (False, False),
    "ngettext": (False, True),
    "ngettext_lazy": (False, True),
    "pgettext": (True, False),
    "pgettext_lazy": (True, False),
    "npgettext": (True, True),
    "npgettext_lazy": (True, True),
}
SKIPPED_DIRECTORIES = {"migrations", "locale", "__pycache__"}


def _key(context, msgid):
    return msgid if context is None else f"{context}{SEPARATOR}{msgid}"


def _call_name(node):
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def python_messages(path):
    """``{key: path}`` of the literal messages in a Python file."""
    found = {}
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name not in CALLS:
            continue
        has_context, has_plural = CALLS[name]
        strings = [arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None for arg in node.args]
        needed = 1 + has_context
        if len(strings) < needed or any(value is None for value in strings[:needed]):
            continue
        context = strings[0] if has_context else None
        found[_key(context, strings[has_context])] = str(path)
    return found


def python_files():
    for path in sorted(PACKAGE.rglob("*.py")):
        if not SKIPPED_DIRECTORIES.intersection(path.relative_to(PACKAGE).parts):
            yield path


_STRING = r"""u?(?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")"""
_TEMPLATE_CALL = re.compile(
    rf"(?<![\w.])(?P<name>_|gettext|ngettext|pgettext|npgettext)\(\s*(?P<a>{_STRING})"
    rf"(?:\s*,\s*(?P<b>{_STRING}))?(?:\s*,\s*(?P<c>{_STRING}))?"
)


def template_messages(path):
    """``{key: path}`` of the messages in a template, read from the code Django makes of it for xgettext."""
    found = {}
    source = templatize(Path(path).read_text(encoding="utf-8"), origin=str(path))
    for match in _TEMPLATE_CALL.finditer(source):
        values = [ast.literal_eval(match.group(group)) if match.group(group) else None for group in "abc"]
        name = match.group("name")
        if name in {"_", "gettext", "ngettext"}:
            found[_key(None, values[0])] = str(path)
        elif values[1] is not None:
            found[_key(values[0], values[1])] = str(path)
    return found


def template_files():
    return sorted((PACKAGE / "templates").rglob("*.html"))


_JS_CALL = re.compile(
    r"""\b(?P<name>gettext|ngettext|pgettext|npgettext)\(\s*
        (?P<first>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
        (?:\s*,\s*(?P<second>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"))?
        (?:\s*,\s*(?P<third>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"))?""",
    re.VERBOSE,
)
_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "'": "'", '"': '"', "\\": "\\"}


def _js_string(literal):
    body = literal[1:-1]
    return re.sub(r"\\(.)", lambda found: _JS_ESCAPES.get(found.group(1), found.group(1)), body)


def script_messages(path):
    """``{key: path}`` of the messages a script asks for with ``gettext`` and its siblings."""
    found = {}
    source = Path(path).read_text(encoding="utf-8")
    for match in _JS_CALL.finditer(source):
        name = match.group("name")
        first, second = match.group("first"), match.group("second")
        if name == "gettext":
            found[_key(None, _js_string(first))] = str(path)
        elif name == "ngettext" and second:
            found[_key(None, _js_string(first))] = str(path)
        elif name == "pgettext" and second:
            found[_key(_js_string(first), _js_string(second))] = str(path)
        elif name == "npgettext" and second and match.group("third"):
            found[_key(_js_string(first), _js_string(second))] = str(path)
    return found


def script_files():
    return sorted((PACKAGE / "static" / "fleetops").glob("*.js"))


def catalog_keys(domain, language="zh_Hans"):
    entries = po.read_po(po.LOCALE_DIR / language / "LC_MESSAGES" / f"{domain}.po")
    return {entry.key for entry in entries if not entry.is_header and not entry.obsolete}
