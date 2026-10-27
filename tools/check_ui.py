#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Check the interface sources of FleetOps against the interface rules.

Usage: python tools/check_ui.py [--paths PATH ...] [--strict] [--rules R1,R7] [--root PATH]

Without ``--paths`` the checked files are ``fleetops/static/fleetops/*.css`` and ``*.js``, the templates in
``fleetops/templates/fleetops`` (except ``comms_join.html``), ``fleetops/templatetags/*.py``,
``fleetops/views*.py`` and ``fleetops/ui.py``. A folder in ``--paths`` is searched for files of those kinds.

Rules (the class and variable lists are in ``tools/ui_check/rules.json``, which the browser check reads too):

  R1  classes that bring a colour or look of their own (``text-dark``, ``badge``, ``btn-secondary``,
      ``border`` ...) and the type classes ``display-*``, ``lead``, ``fw-light``, ``fw-lighter``;
  R2  theme variables that do not follow the page colours (``--bs-secondary-bg``, ``--bs-gray-*`` ...);
  R3  colours written out (hex, ``rgb()``, ``hsl()``, colour names) instead of the interface variables;
  R4  ``prefers-color-scheme`` (the theme decides, not the system setting);
  R5  a ``span`` or ``div`` made to look like a disabled button;
  R6  inline ``onclick`` and ``onsubmit="return confirm(`` handlers (use ``data-fo-confirm``);
  R7  type: ``font-weight`` below 400, ``font-size`` below 12px (0.75rem) in CSS and inline styles;
  R8  decoration: text shadows, blur, radial/conic/repeating gradients, box shadows other than
      ``var(--fo-shadow...)``, and ``linear-gradient`` without a reason;
  R9  every ``img`` has the class ``fo-render``.

An exception is written next to the line with a reason: ``/* ui-allow: reason */`` in CSS,
``// ui-allow: reason`` in scripts, ``{# ui-allow: reason #}`` in templates and ``# ui-allow: reason`` in Python,
on the same line or alone on the line above. A pragma without a reason does not count.

The default run only reports and exits 0; ``--strict`` exits 1 when a rule is broken.
"""

import argparse
import ast
import json
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RULES_FILE = Path(__file__).resolve().parent / "ui_check" / "rules.json"
PACKAGE = Path("fleetops")

RULE_TITLES = {
    "R1": "class with its own colour or look",
    "R2": "theme variable that does not follow the page colours",
    "R3": "colour written out",
    "R4": "prefers-color-scheme",
    "R5": "disabled button made of a span or div",
    "R6": "inline event handler",
    "R7": "type size or weight",
    "R8": "decoration",
    "R9": "image without fo-render",
}
ALL_RULES = tuple(RULE_TITLES)

CLASS_HINTS = {
    "badge": "use {% pill %} / .fo-pill",
    "border": "use .fo-border or the component's own border",
    "text-white": "use the interface colours (--fo-text-1)",
    "display-": "use the type scale of the interface (h2, .fo-metric-value)",
    "lead": "use the type scale of the interface",
    "fw-light": "the lightest weight is 400",
}

# Colours that stay: the ship backdrop of the fleet tree and the crown of the fleet boss.
ALLOWED_COLOURS = {"#111", "#111111", "#d4a017"}
COLOUR_PROPERTIES = (
    "color", "background", "background-color", "border", "border-color", "outline", "outline-color", "fill",
    "stroke", "box-shadow", "text-shadow", "caret-color", "accent-color", "text-decoration-color",
    "column-rule-color", "scrollbar-color",
)
COLOUR_NAMES = frozenset(
    "aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown burlywood "
    "cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan darkgoldenrod "
    "darkgray darkgreen darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon "
    "darkseagreen darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue dimgray "
    "dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray green "
    "greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender lavenderblush lawngreen lemonchiffon "
    "lightblue lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon "
    "lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime limegreen linen "
    "magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue "
    "mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin navajowhite navy "
    "oldlace olive olivedrab orange orangered orchid palegoldenrod palegreen paleturquoise palevioletred papayawhip "
    "peachpuff peru pink plum powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown "
    "seagreen seashell sienna silver skyblue slateblue slategray slategrey snow springgreen steelblue tan teal "
    "thistle tomato turquoise violet wheat white whitesmoke yellow yellowgreen".split()
)
HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b")
FUNCTION_COLOUR = re.compile(r"\b(?:rgba?\(\s*\d|hsla?\(|hwb\(|lab\(|lch\(|oklab\(|oklch\()[^)]*\)?", re.IGNORECASE)
BLACK_SHADOW = re.compile(r"rgba\(\s*0\s*,\s*0\s*,\s*0\s*,", re.IGNORECASE)
SIZE_VALUE = re.compile(r"(?<![\w.-])(\d*\.?\d+)(px|rem|em)\b")

PRAGMAS = {
    ".css": re.compile(r"/\*\s*ui-allow:\s*(.*?)\s*\*/"),
    ".js": re.compile(r"(?://|/\*)\s*ui-allow:\s*(.*?)\s*(?:\*/)?\s*$"),
    ".html": re.compile(r"(?:\{#|<!--)\s*ui-allow:\s*(.*?)\s*(?:#\}|-->)"),
    ".py": re.compile(r"#\s*ui-allow:\s*(.*?)\s*$"),
}
# Pragma comments alone on a line, which cover the line below.
STANDALONE_PRAGMA = {
    ".css": re.compile(r"^\s*/\*\s*ui-allow:.*\*/\s*$"),
    ".js": re.compile(r"^\s*(?://|/\*)\s*ui-allow:.*$"),
    ".html": re.compile(r"^\s*(?:\{#|<!--)\s*ui-allow:.*(?:#\}|-->)\s*$"),
    ".py": re.compile(r"^\s*#\s*ui-allow:.*$"),
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    message: str
    reason: str = ""  # set for an allowed exception

    def __str__(self):
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


def load_rules(rules_file=RULES_FILE):
    data = json.loads(Path(rules_file).read_text(encoding="utf-8"))
    class_tokens = re.compile(r"^(?:" + "|".join(data["forbidden_class_patterns"]) + r")$")
    variables = [re.compile(pattern) for pattern in data["forbidden_variable_patterns"]]
    return class_tokens, variables


CLASS_TOKENS, FORBIDDEN_VARIABLES = load_rules()


def line_starts(text):
    starts = [0]
    for match in re.finditer("\n", text):
        starts.append(match.end())
    return starts


def line_of(starts, offset):
    low, high = 0, len(starts) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if starts[middle] <= offset:
            low = middle
        else:
            high = middle - 1
    return low + 1


def blank(text, start, end):
    """``text`` with ``start:end`` replaced by spaces; line breaks stay so line numbers do not move."""
    chunk = re.sub(r"[^\n]", " ", text[start:end])
    return text[:start] + chunk + text[end:]


def strip_css_comments(text):
    return re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)


def split_declarations(block):
    """``(offset, declaration)`` pairs of a CSS block body, split at semicolons outside brackets and quotes."""
    parts, depth, quote, start = [], 0, "", 0
    for index, char in enumerate(block):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == ";" and depth == 0:
            parts.append((start, block[start:index]))
            start = index + 1
    parts.append((start, block[start:]))
    return [(offset, part) for offset, part in parts if part.strip()]


def size_in_px(number, unit):
    value = float(number)
    return value if unit == "px" else value * 16


class Source:
    """One file under check: collects findings and allowed exceptions."""

    def __init__(self, label, text, suffix, rules):
        self.label = label
        self.suffix = suffix
        self.text = text
        self.rules = rules
        self.lines = text.split("\n")
        self.findings = []
        self.allowed = []
        self.seen = set()

    def reason_for(self, line):
        """The reason of the pragma that covers ``line`` (1-based), or None."""
        pragma = PRAGMAS.get(self.suffix)
        if pragma is None:
            return None
        for number in (line, line - 1):
            if not 1 <= number <= len(self.lines):
                continue
            text = self.lines[number - 1]
            if number == line - 1 and not STANDALONE_PRAGMA[self.suffix].match(text):
                continue
            match = pragma.search(text)
            if match and match.group(1).strip():
                return match.group(1).strip()
        return None

    def report(self, rule, line, message):
        if rule not in self.rules:
            return
        key = (rule, line, message)
        if key in self.seen:
            return
        self.seen.add(key)
        reason = self.reason_for(line)
        if reason:
            self.allowed.append(Finding(self.label, line, rule, message, reason))
        else:
            self.findings.append(Finding(self.label, line, rule, message))


# --------------------------------------------------------------------------------------------------------
# Declarations (CSS files, <style> blocks, style="" attributes)
# --------------------------------------------------------------------------------------------------------


def check_declaration(source, line, prop, value):
    prop = prop.strip().lower()
    raw = value.strip()
    if not prop:
        return
    whole = f"{prop}: {raw}"
    for pattern in FORBIDDEN_VARIABLES:
        match = pattern.search(whole)
        if match:
            source.report("R2", line, f"theme variable {match.group(0)} does not follow the page colours")
    check_colours(source, line, prop, raw)
    # R7 type
    if prop == "font-weight":
        word = raw.lower().replace("!important", "").strip()
        if word in {"lighter", "100", "200", "300"}:
            source.report("R7", line, f"font-weight {word} is lighter than 400")
    if prop in {"font-size", "font"}:
        text = re.sub(r"var\([^)]*\)|calc\([^)]*\)", " ", raw.lower())
        if prop == "font-size" and text.strip().split("!")[0].strip() in {"x-small", "xx-small", "xxx-small"}:
            source.report("R7", line, f"font-size {text.strip()} is below 12px")
        found = SIZE_VALUE.search(text)
        if found:
            number, unit = found.groups()
            minimum = 12 if unit == "px" else 0.75
            if float(number) < minimum:
                source.report("R7", line, f"font-size {number}{unit} is below {'12px' if unit == 'px' else '.75' + unit}")
        percent = re.match(r"^\s*(\d*\.?\d+)%", text) if prop == "font-size" else None
        if percent and float(percent.group(1)) < 75:
            source.report("R7", line, f"font-size {percent.group(1)}% is below 75%")
    # R8 decoration
    if prop == "text-shadow" and raw.lower().split("!")[0].strip() != "none":
        source.report("R8", line, "text-shadow")
    if prop.endswith("filter") and re.search(r"(?:drop-shadow|blur)\(", raw, re.IGNORECASE):
        source.report("R8", line, f"{prop} with drop-shadow or blur")
    for gradient in re.findall(r"(radial-gradient|conic-gradient|repeating-[a-z]+-gradient)\(", raw, re.IGNORECASE):
        source.report("R8", line, f"{gradient.lower()}")
    if re.search(r"(?<![\w-])linear-gradient\(", raw, re.IGNORECASE):
        source.report("R8", line, "linear-gradient needs a reason")
    if prop.endswith("box-shadow"):
        shadow = raw.lower().replace("!important", "").strip()
        if shadow != "none" and not re.fullmatch(r"var\(\s*--fo-shadow[\w-]*\s*(?:,[^)]*)?\)", shadow):
            source.report("R8", line, "box-shadow other than none or var(--fo-shadow...)")


def check_colours(source, line, prop, raw):
    shadow_variable = prop.startswith("--fo-shadow") or prop.endswith("box-shadow")
    text = re.sub(r"url\([^)]*\)", " ", raw)
    # Template values such as {{ color }} are data, not a literal.
    text = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", text)
    for match in HEX_COLOUR.finditer(text):
        if match.group(0).lower() in ALLOWED_COLOURS:
            continue
        source.report("R3", line, f"colour {match.group(0)} written out; use the interface variables")
    for match in FUNCTION_COLOUR.finditer(text):
        if shadow_variable and BLACK_SHADOW.match(text[match.start():]):
            continue
        source.report("R3", line, f"colour {match.group(0).strip()} written out; use the interface variables")
    if prop in COLOUR_PROPERTIES:
        stripped = re.sub(r"var\([^)]*\)|[a-z-]+\(", " ", text.lower())
        for word in re.findall(r"[a-z]+", stripped):
            if word in COLOUR_NAMES:
                source.report("R3", line, f"colour name {word} written out; use the interface variables")


def check_css_text(source, text, base_line=1):
    """R2, R3, R4, R7, R8 in CSS text; ``base_line`` is the line of its first character."""
    code = strip_css_comments(text)
    starts = line_starts(code)
    for match in re.finditer(r"prefers-color-scheme", code):
        source.report("R4", base_line + line_of(starts, match.start()) - 1, "prefers-color-scheme: the theme decides")
    for block in re.finditer(r"\{([^{}]*)\}", code):
        body = block.group(1)
        offset = block.start(1)
        for position, declaration in split_declarations(body):
            if ":" not in declaration:
                continue
            prop, _, value = declaration.partition(":")
            absolute = offset + position + len(declaration) - len(declaration.lstrip())
            line = base_line + line_of(starts, absolute) - 1
            check_declaration(source, line, prop, value)


def check_style_attribute(source, line, value):
    """A ``style="..."`` attribute: its declarations."""
    for _position, declaration in split_declarations(value):
        if ":" in declaration:
            prop, _, rest = declaration.partition(":")
            check_declaration(source, line, prop, rest)


# --------------------------------------------------------------------------------------------------------
# Class tokens
# --------------------------------------------------------------------------------------------------------


def forbidden_tokens(value):
    """Forbidden whole class tokens of a class attribute value; template variables make a token unknown."""
    value = re.sub(r"\{%.*?%\}", " ", value, flags=re.S)
    value = re.sub(r"\{\{.*?\}\}", "\x01", value, flags=re.S)
    return [token for token in value.split() if "\x01" not in token and CLASS_TOKENS.match(token)]


def class_hint(token):
    for key, hint in CLASS_HINTS.items():
        if token == key or (key.endswith("-") and token.startswith(key)):
            return f"; {hint}"
    return ""


def report_classes(source, line, value):
    for token in forbidden_tokens(value):
        source.report("R1", line, f'class "{token}" is not used in FleetOps{class_hint(token)}')


# --------------------------------------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------------------------------------

TAG = re.compile(r"<([a-zA-Z][\w-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>", re.S)
ATTRIBUTE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.S)


def mask_template_tags(text):
    """Template tags and comments replaced by spaces of the same length (line breaks stay)."""
    def mask(match):
        return re.sub(r"[^\n]", " ", match.group(0))

    return re.sub(r"\{#.*?#\}|\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", mask, text, flags=re.S)


def check_html_text(source, text):
    comments_masked = mask_template_tags(text)
    # {{ }} and {% %} stay inside attribute values (they decide whether a token is known), but they must not
    # end a tag early, so tags are found on a copy where their text is replaced by a neutral character.
    neutral = re.sub(
        r"\{\{.*?\}\}|\{%.*?%\}", lambda m: re.sub(r"[^\n]", "\x02", m.group(0)), comments_masked, flags=re.S
    )
    starts = line_starts(text)
    for match in TAG.finditer(neutral):
        name = match.group(1).lower()
        line = line_of(starts, match.start())
        shape = neutral[match.start():match.end()]
        original = comments_masked[match.start():match.end()]
        attributes = {}
        for attribute in ATTRIBUTE.finditer(shape):
            group = 2 if attribute.group(2) is not None else 3
            key = attribute.group(1).lower()
            value = original[attribute.start(group):attribute.end(group)]
            attributes[key] = value
            attr_line = line + shape[:attribute.start()].count("\n")
            if key == "class":
                report_classes(source, attr_line, value)
            elif key == "style":
                check_style_attribute(source, attr_line, value)
            elif key == "onclick":
                source.report("R6", attr_line, "inline onclick handler; use data-fo-confirm or a script")
            elif key == "onsubmit" and re.match(r"\s*return\s+confirm\(", value):
                source.report("R6", attr_line, 'onsubmit="return confirm(": use data-fo-confirm')
        classes = attributes.get("class", "")
        if name in {"span", "div"} and re.search(r"(?:^|\s)btn(?:\s|$)", classes) and re.search(
            r"(?:^|\s)disabled(?:\s|$)", classes
        ):
            source.report("R5", line, f"a {name} made to look like a disabled button; use a real disabled button")
        if name == "img":
            tokens = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", classes).split()
            if "fo-render" not in tokens:
                source.report("R9", line, "img without the class fo-render")
    for match in re.finditer(r"<style\b[^>]*>(.*?)</style>", text, flags=re.S | re.I):
        base = line_of(starts, match.start(1))
        check_css_text(source, mask_template_tags(match.group(1)), base)
    for match in re.finditer(r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, flags=re.S | re.I):
        base = line_of(starts, match.start(1))
        check_js_text(source, match.group(1), base)


# --------------------------------------------------------------------------------------------------------
# Scripts
# --------------------------------------------------------------------------------------------------------


def js_strings(text):
    """``(start, end, comment_free_text)`` of the string literals of a script, and the script without comments.

    A small scanner is enough here: it follows quotes, template literals, line and block comments and keeps
    regular expressions out of the picture by treating a slash after an operator as a division.
    """
    strings, out = [], []
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if text.startswith("//", index):
            end = text.find("\n", index)
            end = length if end == -1 else end
            out.append(" " * (end - index))
            index = end
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = length if end == -1 else end + 2
            out.append(re.sub(r"[^\n]", " ", text[index:end]))
            index = end
        elif char in "\"'`":
            start = index
            index += 1
            while index < length and text[index] != char:
                if text[index] == "\\":
                    index += 1
                elif char != "`" and text[index] == "\n":
                    break
                index += 1
            index = min(index + 1, length)
            strings.append((start, index, text[start + 1:index - 1]))
            out.append(text[start:index])
        else:
            out.append(char)
            index += 1
    return strings, "".join(out)


JS_CLASS_CONTEXT = re.compile(
    r"""(?:\bclassName\s*\+?=\s*|\bclass\s*=\s*\\?|\bclassList\.(?:add|remove|toggle|replace|contains)\(\s*|"""
    r"""\b(?:element|el|h|create)\(\s*['"][a-z][a-z0-9]*['"]\s*,\s*)$"""
)
COLOUR_WORDS = re.compile(r"color|background|border|outline|fill", re.IGNORECASE)


def check_js_text(source, text, base_line=1):
    strings, code = js_strings(text)
    starts = line_starts(text)

    def line_at(offset):
        return base_line + line_of(starts, offset) - 1

    for match in re.finditer(r"prefers-color-scheme", code):
        source.report("R4", line_at(match.start()), "prefers-color-scheme: the theme decides")
    for start, end, body in strings:
        line = line_at(start)
        before = code[max(0, start - 60):start]
        before_line = before.split("\n")[-1]
        # Class lists: className = '...', classList.add('...', '...'), element('span', '...'), HTML in strings.
        if JS_CLASS_CONTEXT.search(before_line) or re.search(r"classList\.\w+\([^)]*$", before_line):
            report_classes(source, line, body)
        for attribute in re.finditer(r"""\bclass\s*=\s*\\?["']([^"'\\]*)""", body):
            report_classes(source, line, attribute.group(1))
        for pattern in FORBIDDEN_VARIABLES:
            found = pattern.search(body)
            if found:
                source.report("R2", line, f"theme variable {found.group(0)} does not follow the page colours")
        if COLOUR_WORDS.search(body) or COLOUR_WORDS.search(before_line):
            check_colours(source, line, "color", body)
        for tag in re.finditer(r"<img\b", body, flags=re.I):
            # The tag may be built from several strings; the class has to be in this one or right after it.
            if "fo-render" not in body[tag.start():] + code[end:end + 200]:
                source.report("R9", line, "img without the class fo-render")
    for match in re.finditer(r"""(?:element|el|h|create)\(\s*(['"])img\1\s*,\s*(['"])([^'"]*)\2""", code):
        if "fo-render" not in match.group(3).split():
            source.report("R9", line_at(match.start()), "img without the class fo-render")
    for match in re.finditer(r"""createElement\(\s*(['"])img\1\s*\)""", code):
        line = line_at(match.start())
        window = "\n".join(text.split("\n")[line - base_line:line - base_line + 8])
        window = window.split("\n\n")[0]
        if "fo-render" not in window:
            source.report("R9", line, "createElement('img') without the class fo-render")


# --------------------------------------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------------------------------------


def check_python_text(source, text):
    """R1 (class strings) and R3 (colours in style strings) in the string literals of a Python file."""
    try:
        tokens = list(tokenize.generate_tokens(iter(text.splitlines(keepends=True)).__next__))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return
    fstring_middle = getattr(tokenize, "FSTRING_MIDDLE", None)
    for token in tokens:
        if token.type == tokenize.STRING:
            try:
                value = ast.literal_eval(token.string)
            except (ValueError, SyntaxError):
                value = token.string
            if isinstance(value, bytes):
                continue
        elif fstring_middle is not None and token.type == fstring_middle:
            value = token.string
        else:
            continue
        line = token.start[0]
        for attribute in re.finditer(r"""\bclass\s*=\s*["']([^"']*)["']""", value):
            report_classes(source, line, attribute.group(1))
        # A class dictionary entry: several class names or a hyphenated forbidden one on their own.
        if re.fullmatch(r"[a-z][a-z0-9_-]*(?:\s+[a-z][a-z0-9_-]*)*", value or "") and "-" in value:
            report_classes(source, line, value)
        for style in re.finditer(r"""\bstyle\s*=\s*["']([^"']*)["']""", value):
            check_style_attribute(source, line, style.group(1))


# --------------------------------------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------------------------------------


def check_text(label, text, suffix=None, rules=ALL_RULES):
    """Check ``text`` as if it were the file ``label``; returns ``(findings, allowed)``."""
    suffix = suffix or Path(label).suffix
    source = Source(label, text, suffix, set(rules))
    if suffix == ".css":
        check_css_text(source, text)
    elif suffix == ".js":
        check_js_text(source, text)
    elif suffix == ".html":
        check_html_text(source, text)
    elif suffix == ".py":
        check_python_text(source, text)
    return source.findings, source.allowed


def default_files(root):
    package = root / PACKAGE
    files = []
    static = package / "static" / "fleetops"
    files += sorted(static.glob("*.css")) + sorted(static.glob("*.js"))
    templates = package / "templates" / "fleetops"
    files += sorted(path for path in templates.rglob("*.html") if path.name != "comms_join.html")
    files += sorted((package / "templatetags").glob("*.py"))
    files += sorted(package.glob("views*.py"))
    files += [path for path in (package / "ui.py",) if path.exists()]
    return files


def expand(paths, root):
    files = []
    for entry in paths:
        path = Path(entry)
        if not path.is_absolute():
            path = Path.cwd() / path
        if path.is_dir():
            for suffix in (".css", ".js", ".html", ".py"):
                files += sorted(p for p in path.rglob(f"*{suffix}") if "__pycache__" not in p.parts
                                and "migrations" not in p.parts and p.name != "comms_join.html")
        elif path.exists():
            files.append(path)
        else:
            print(f"warning: {entry} does not exist", file=sys.stderr)
    return files


def label_for(path, root):
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def scan(files, root=ROOT, rules=ALL_RULES):
    findings, allowed = [], []
    for path in files:
        if path.suffix not in {".css", ".js", ".html", ".py"}:
            continue
        found, ok = check_text(label_for(path, root), path.read_text(encoding="utf-8"), path.suffix, rules)
        findings += found
        allowed += ok
    return findings, allowed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--paths", nargs="+", help="files or folders to check instead of the default scope")
    parser.add_argument("--strict", action="store_true", help="exit 1 when a rule is broken")
    parser.add_argument("--rules", help="comma separated rules to check, for example R1,R7 (default: all)")
    parser.add_argument("--root", help="the root of the checkout (default: the one this tool is in)")
    parser.add_argument("--summary", action="store_true", help="print only the counts")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else ROOT
    rules = tuple(rule.strip().upper() for rule in args.rules.split(",")) if args.rules else ALL_RULES
    unknown = [rule for rule in rules if rule not in RULE_TITLES]
    if unknown:
        print(f"error: unknown rule {', '.join(unknown)}", file=sys.stderr)
        return 2
    files = expand(args.paths, root) if args.paths else default_files(root)
    findings, allowed = scan(files, root, rules)

    if not args.summary:
        for finding in sorted(findings, key=lambda f: (f.path, f.line, f.rule)):
            print(finding)
    counts = {rule: sum(1 for f in findings if f.rule == rule) for rule in rules}
    print(f"\nchecked {len(files)} files, {len(findings)} problems"
          + (": " + ", ".join(f"{rule} {count}" for rule, count in counts.items() if count) if findings else ""))
    if allowed:
        print(f"{len(allowed)} allowed exceptions:")
        for finding in sorted(allowed, key=lambda f: (f.path, f.line)):
            print(f"  {finding.path}:{finding.line}: {finding.rule} ({finding.reason})")
    if findings and not args.strict:
        print("report mode: use --strict to fail on these problems")
    return 1 if findings and args.strict else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # The reader (for example "head") closed the pipe; nothing left to say.
        sys.stderr.close()
        sys.exit(0)
