# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The interface checks: the grep rules of tools/check_ui.py and the pure parts of the browser check.

Each rule R1-R9 must catch a sample (so the checker cannot silently pass) and a pragma with a reason must
silence it. The browser check itself needs Chromium and is run by hand (tools/ui_check/README.md); here only
its page list, lane assignment and asset helpers are tested.
"""

import contextlib
import importlib.util
import io
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

import fleetops

ROOT = Path(fleetops.__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
UI_CHECK = TOOLS / "ui_check"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_ui = load("fleetops_tool_check_ui", TOOLS / "check_ui.py")


def found(label, text, rules=check_ui.ALL_RULES):
    """``[(rule, line)]`` of the findings in ``text`` checked as the file ``label``."""
    findings, _allowed = check_ui.check_text(label, text, rules=rules)
    return [(finding.rule, finding.line) for finding in findings]


def allowed(label, text):
    _findings, ok = check_ui.check_text(label, text)
    return [(finding.rule, finding.line, finding.reason) for finding in ok]


class RuleTestCase(SimpleTestCase):
    def assertCaught(self, label, text, rule, line=1):
        self.assertIn((rule, line), found(label, text), f"{rule} not reported for {text!r}")

    def assertClean(self, label, text, rule=None):
        rules = (rule,) if rule else check_ui.ALL_RULES
        self.assertEqual(found(label, text, rules), [], f"unexpected finding for {text!r}")


class ForbiddenClassTests(RuleTestCase):
    def test_template_classes_are_found_as_whole_tokens(self):
        for token in ("text-dark", "bg-light", "text-bg-warning", "table-light", "bg-secondary", "bg-danger", "badge",
                      "link-body-emphasis", "btn-success", "btn-secondary", "btn-outline-info", "btn-close-white",
                      "border", "border-top", "border-primary", "bg-body-tertiary", "text-body-emphasis",
                      "text-white", "text-black", "bg-white", "display-6", "lead", "fw-light", "fw-lighter"):
            with self.subTest(token=token):
                self.assertCaught("page.html", f'<span class="mb-1 {token} ms-2">x</span>', "R1")

    def test_allowed_classes_and_partial_tokens_are_not_reported(self):
        for markup in (
            '<div class="border-0 border-top-0 btn btn-primary btn-outline-secondary btn-outline-danger btn-danger">',
            '<span class="badge-like fw-semibold text-muted lead-in">',
            '<span class="bg-{{ tone }} border-{{ side }}">',
            '<div class="fo-pill fo-pill-ok">',
        ):
            with self.subTest(markup=markup):
                self.assertClean("page.html", markup, "R1")

    def test_a_class_inside_a_template_condition_is_still_found(self):
        self.assertCaught("page.html", '<span class="{% if hot %}text-white{% endif %} x">', "R1")

    def test_the_line_of_a_multi_line_tag_is_the_line_of_the_attribute(self):
        text = '<div\n  id="a"\n  class="badge">x</div>'
        self.assertCaught("page.html", text, "R1", line=3)

    def test_script_class_lists_are_found(self):
        samples = (
            "el.className = 'btn btn-secondary';",
            "el.className += ' text-white';",
            "row.classList.add('fw-semibold', 'border-top');",
            "const chip = element('span', 'badge text-bg-primary me-1');",
            "box.innerHTML = '<span class=\"badge bg-info\">x</span>';",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertCaught("page.js", sample, "R1")
        self.assertClean("page.js", "el.className = 'btn btn-primary fo-pill';", "R1")
        self.assertClean("page.js", "label.textContent = 'The lead badge border';", "R1")

    def test_python_class_strings_are_found(self):
        self.assertCaught("tags.py", 'CLASSES = {"pending": "text-bg-warning"}', "R1")
        self.assertCaught("tags.py", "html = format_html('<span class=\"badge {}\">{}</span>', tone, text)", "R1")
        self.assertClean("tags.py", 'label = "lead"\nname = "badge"', "R1")
        self.assertClean("tags.py", 'TONES = {"ok": "fo-pill-ok"}', "R1")

    def test_a_pragma_with_a_reason_silences_the_rule(self):
        self.assertEqual(found("page.html", '<span class="badge">x</span> {# ui-allow: the theme badge #}'), [])
        self.assertEqual(found("page.html", '{# ui-allow: the theme badge #}\n<span class="badge">x</span>'), [])
        self.assertEqual(found("page.js", "el.className = 'badge'; // ui-allow: demo"), [])
        self.assertEqual(found("tags.py", 'C = "text-bg-warning"  # ui-allow: legacy mapping'), [])
        self.assertEqual(allowed("page.html", '<span class="badge">x</span> {# ui-allow: the theme badge #}'),
                         [("R1", 1, "the theme badge")])

    def test_a_pragma_without_a_reason_or_two_lines_away_does_not_count(self):
        self.assertCaught("page.html", '<span class="badge">x</span> {# ui-allow: #}', "R1")
        self.assertCaught("page.html", '{# ui-allow: reason #}\n\n<span class="badge">x</span>', "R1", line=3)
        self.assertCaught("page.html", '{# ui-allow: reason #} <i></i>\n<span class="badge">x</span>', "R1", line=2)


class ThemeVariableTests(RuleTestCase):
    def test_css_variables_that_do_not_follow_the_page_are_found(self):
        for name in ("--bs-tertiary-bg", "--bs-secondary-bg", "--bs-border-color", "--bs-primary-bg-subtle",
                     "--bs-warning-border-subtle", "--bs-emphasis-color", "--bs-light", "--bs-dark-rgb", "--bs-gray-200",
                     "--bs-white", "--bs-black", "--bs-highlight-bg", "--bs-secondary-color", "--bs-tertiary-color"):
            with self.subTest(name=name):
                self.assertCaught("a.css", f".x {{ background: var({name}); }}", "R2")

    def test_the_allowed_variables_are_not_reported(self):
        for name in ("--bs-body-color", "--bs-body-bg-rgb", "--bs-primary", "--bs-border-color-translucent",
                     "--bs-border-width", "--bs-link-color", "--bs-teal", "--fo-border"):
            with self.subTest(name=name):
                self.assertClean("a.css", f".x {{ color: var({name}); }}", "R2")

    def test_scripts_and_inline_styles_are_checked_too(self):
        self.assertCaught("a.js", "const c = getComputedStyle(el).getPropertyValue('--bs-secondary-color');", "R2")
        self.assertCaught("a.html", '<div style="color: var(--bs-light)">x</div>', "R2")

    def test_pragma(self):
        self.assertEqual(found("a.css", ".x { color: var(--bs-light); } /* ui-allow: demo */"), [])


class ColourTests(RuleTestCase):
    def test_colours_written_out_are_found(self):
        for value in ("#fff", "#0d6efd", "#0d6efdcc", "rgb(1, 2, 3)", "rgba(10, 20, 30, .5)", "hsl(10 20% 30%)", "red"):
            with self.subTest(value=value):
                self.assertCaught("a.css", f".x {{ color: {value}; }}", "R3")
        self.assertCaught("a.css", ".x { border: 1px solid #ccc; }", "R3")
        self.assertCaught("a.css", ".x { --fo-token: #abcdef; }", "R3")
        self.assertCaught("a.html", '<span style="background: #fafafa">x</span>', "R3")
        self.assertCaught("a.js", "el.style.color = '#abc';", "R3")
        self.assertCaught("a.js", "el.style.setProperty('background-color', 'rgb(1,2,3)');", "R3")

    def test_the_kept_colours_and_variables_are_not_reported(self):
        for text in (
            ".fo-render { background: #111; }",
            ".crown { color: #d4a017; }",
            ".x { color: var(--fo-text-1); background: rgba(var(--bs-body-color-rgb), .04); }",
            ":root { --fo-shadow: 0 1px 2px rgba(0, 0, 0, .08); }",
            ".x { background: url(#sprite) no-repeat; border: 1px solid transparent; outline-color: currentColor; }",
            ".x { content: ''; font-family: Tan, serif; }",
            "#add-row, #bad { margin: 0; }",
        ):
            with self.subTest(text=text):
                self.assertClean("a.css", text, "R3")
        self.assertClean("a.html", '<span style="background: {{ colour }}">x</span>', "R3")
        self.assertClean("a.js", "document.querySelector('#add-row').click();", "R3")

    def test_pragma(self):
        self.assertEqual(found("a.css", ".x { color: #fff; } /* ui-allow: contrast on a photo */"), [])
        self.assertEqual(found("a.css", "/* ui-allow: contrast on a photo */\n.x { color: #fff; }"), [])


class SchemeTests(RuleTestCase):
    def test_prefers_color_scheme_is_found_in_css_and_scripts(self):
        self.assertCaught("a.css", "@media (prefers-color-scheme: dark) { .x { margin: 0; } }", "R4")
        self.assertCaught("a.js", "window.matchMedia('(prefers-color-scheme: dark)');", "R4")
        self.assertCaught("a.html", "<style>\n@media (prefers-color-scheme: dark) { .x { margin: 0; } }\n</style>", "R4", line=2)
        self.assertClean("a.js", "// the theme decides, not prefers-color-scheme\nlet a = 1;", "R4")

    def test_pragma(self):
        self.assertEqual(found("a.css", "@media (prefers-color-scheme: dark) { .x { margin: 0; } } /* ui-allow: print */"), [])


class DisabledButtonAndHandlerTests(RuleTestCase):
    def test_a_disabled_button_made_of_a_span_or_div_is_found(self):
        self.assertCaught("a.html", '<span class="btn btn-sm disabled">Soon</span>', "R5")
        self.assertCaught("a.html", '<div class="disabled btn">Soon</div>', "R5")
        self.assertClean("a.html", '<button class="btn btn-sm" disabled>Soon</button><a class="btn disabled" href="#">x</a>', "R5")
        self.assertClean("a.html", '<span class="badge-x disabled">x</span>', "R5")

    def test_inline_handlers_are_found(self):
        self.assertCaught("a.html", '<button onclick="go()">x</button>', "R6")
        self.assertCaught("a.html", '<form onsubmit="return confirm(\'Sure?\')">', "R6")
        self.assertClean("a.html", '<form onsubmit="save()"><button data-fo-confirm="Sure?">x</button></form>', "R6")

    def test_pragma(self):
        self.assertEqual(found("a.html", '<button onclick="go()">x</button> {# ui-allow: legacy widget #}'), [])
        self.assertEqual(found("a.html", '<span class="btn disabled">x</span> {# ui-allow: placeholder #}'), [])


class TypeTests(RuleTestCase):
    def test_light_weights_are_found(self):
        for value in ("100", "200", "300", "lighter"):
            with self.subTest(value=value):
                self.assertCaught("a.css", f".x {{ font-weight: {value}; }}", "R7")
        for value in ("400", "500", "600", "bold", "bolder", "normal"):
            with self.subTest(value=value):
                self.assertClean("a.css", f".x {{ font-weight: {value}; }}", "R7")

    def test_small_sizes_are_found(self):
        for value in ("11px", "9px", ".7em", "0.7em", "0.7rem", "0.74rem", "x-small", "xx-small", "60%"):
            with self.subTest(value=value):
                self.assertCaught("a.css", f".x {{ font-size: {value}; }}", "R7")
        for value in ("12px", ".75em", "0.75rem", "0.8125rem", "14px", "1rem", "smaller", "var(--fs)", "calc(.5rem + 4px)", "75%"):
            with self.subTest(value=value):
                self.assertClean("a.css", f".x {{ font-size: {value}; }}", "R7")
        self.assertCaught("a.css", ".x { font: 600 10px/1.2 sans-serif; }", "R7")

    def test_inline_styles_are_checked(self):
        self.assertCaught("a.html", '<small style="font-size: .6rem; font-weight: 300">x</small>', "R7")
        self.assertClean("a.html", '<small style="font-size: .8rem">x</small>', "R7")

    def test_pragma(self):
        self.assertEqual(found("a.css", ".x { font-size: 10px; } /* ui-allow: map legend */"), [])


class DecorationTests(RuleTestCase):
    def test_decorations_are_found(self):
        for declaration in (
            "text-shadow: 0 1px 1px black",
            "filter: drop-shadow(0 1px 2px var(--fo-border))",
            "backdrop-filter: blur(4px)",
            "background: radial-gradient(circle, var(--fo-accent), transparent)",
            "background-image: conic-gradient(var(--fo-ok), var(--fo-bad))",
            "background: repeating-linear-gradient(45deg, var(--fo-ok), var(--fo-bad) 4px)",
            "background-image: linear-gradient(var(--fo-accent-bg), var(--fo-accent-bg))",
            "box-shadow: 0 2px 4px var(--fo-border)",
            "box-shadow: inset 0 0 0 1px var(--fo-border)",
        ):
            with self.subTest(declaration=declaration):
                self.assertCaught("a.css", f".x {{ {declaration}; }}", "R8")

    def test_plain_things_are_not_reported(self):
        for declaration in (
            "text-shadow: none", "box-shadow: none", "box-shadow: var(--fo-shadow)", "box-shadow: var(--fo-shadow-raised)",
            "filter: grayscale(1)", "background: var(--fo-surface-1)",
        ):
            with self.subTest(declaration=declaration):
                self.assertClean("a.css", f".x {{ {declaration}; }}", "R8")

    def test_a_linear_gradient_with_a_reason_is_allowed(self):
        text = ".fo-table-pin { background-image: linear-gradient(var(--fo-accent-bg), var(--fo-accent-bg)); } /* ui-allow: flat row tint */"
        self.assertEqual(found("a.css", text), [])
        self.assertEqual(allowed("a.css", text)[0][:2], ("R8", 1))

    def test_inline_styles_are_checked(self):
        self.assertCaught("a.html", '<div style="text-shadow: 0 0 2px #000">x</div>', "R8")


class ImageTests(RuleTestCase):
    def test_an_image_needs_the_fo_render_class(self):
        self.assertCaught("a.html", '<img src="{{ url }}" alt="">', "R9")
        self.assertCaught("a.html", '<img class="rounded" src="{{ url }}" alt="">', "R9")
        self.assertClean("a.html", '<img class="fo-render" src="{{ url }}" alt="">', "R9")
        self.assertClean("a.html", '<img src="{{ url }}" class="{{ extra }} fo-render ms-1" alt="">', "R9")
        self.assertClean("a.html", '<i class="fa-solid fa-image"></i><picture-frame></picture-frame>', "R9")

    def test_script_images_need_it_too(self):
        self.assertCaught("a.js", "const img = document.createElement('img');\nimg.src = url;", "R9")
        self.assertClean("a.js", "const img = document.createElement('img');\nimg.className = 'fo-render';\nimg.src = url;", "R9")
        self.assertClean("a.js", "const img = document.createElement('img');\nimg.classList.add('fo-render');", "R9")
        self.assertCaught("a.js", "const img = element('img', 'rounded');", "R9")
        self.assertClean("a.js", "const img = element('img', 'fo-render ms-1');", "R9")
        self.assertCaught("a.js", "box.innerHTML = '<img src=\"' + url + '\">';", "R9")
        self.assertClean("a.js", "box.innerHTML = '<img class=\"fo-render\" src=\"' + url + '\">';", "R9")

    def test_pragma(self):
        self.assertEqual(found("a.html", '<img src="x.png" alt=""> {# ui-allow: a favicon preview #}'), [])


class CommandLineTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def run_tool(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = check_ui.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def write(self, name, text):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_default_run_only_reports(self):
        bad = self.write("fleetops/templates/fleetops/x.html", '<span class="badge">x</span>\n')
        code, out, _err = self.run_tool("--paths", str(bad), "--root", str(self.folder))

        self.assertEqual(code, 0)
        self.assertIn("fleetops/templates/fleetops/x.html:1: R1", out)
        self.assertIn("report mode", out)

    def test_strict_exits_with_one_when_a_rule_is_broken(self):
        bad = self.write("a.css", ".x { color: #fff; }\n")
        good = self.write("b.css", ".x { color: var(--fo-text-1); }\n")

        self.assertEqual(self.run_tool("--paths", str(bad), "--strict")[0], 1)
        code, out, _err = self.run_tool("--paths", str(good), "--strict")
        self.assertEqual(code, 0)
        self.assertIn("0 problems", out)

    def test_the_exceptions_are_listed_with_their_reason(self):
        path = self.write("a.css", ".x { color: #fff; } /* ui-allow: a photo overlay */\n")
        code, out, _err = self.run_tool("--paths", str(path), "--strict")

        self.assertEqual(code, 0)
        self.assertIn("1 allowed exceptions", out)
        self.assertIn("a photo overlay", out)

    def test_a_folder_is_searched_and_the_rules_can_be_chosen(self):
        self.write("static/a.css", ".x { font-weight: 300; color: #fff; }\n")
        self.write("templates/b.html", '<span class="badge">x</span>\n')
        self.write("static/readme.txt", "badge #fff\n")
        code, out, _err = self.run_tool("--paths", str(self.folder), "--rules", "R7", "--strict")

        self.assertEqual(code, 1)
        self.assertIn("R7", out)
        self.assertNotIn("R1", out.split("checked")[0])
        self.assertIn("checked 2 files", out)

    def test_an_unknown_rule_is_refused(self):
        code, _out, err = self.run_tool("--rules", "R99")

        self.assertEqual(code, 2)
        self.assertIn("R99", err)

    def test_the_default_scope_covers_the_interface_sources(self):
        files = {path.relative_to(ROOT).as_posix() for path in check_ui.default_files(ROOT)}

        self.assertIn("fleetops/static/fleetops/fleetops.css", files)
        self.assertIn("fleetops/static/fleetops/fleet_actions.js", files)
        self.assertIn("fleetops/templates/fleetops/dashboard.html", files)
        self.assertIn("fleetops/templatetags/fleetops_srp.py", files)
        self.assertIn("fleetops/views.py", files)
        self.assertNotIn("fleetops/templates/fleetops/comms_join.html", files)
        self.assertFalse([f for f in files if "/migrations/" in f or f.startswith("tests/")])

    def test_the_whole_default_scope_can_be_scanned(self):
        findings, _allowed = check_ui.scan(check_ui.default_files(ROOT), ROOT)

        # Every finding belongs to a known rule, and the repository has none: the interface rules hold everywhere.
        self.assertTrue(all(finding.rule in check_ui.RULE_TITLES for finding in findings))
        self.assertEqual([str(finding) for finding in findings], [])

    def test_the_class_and_variable_lists_are_valid_for_both_languages(self):
        import json

        rules = json.loads((UI_CHECK / "rules.json").read_text(encoding="utf-8"))

        self.assertGreater(len(rules["forbidden_class_patterns"]), 15)
        self.assertGreater(len(rules["forbidden_variable_patterns"]), 10)
        for pattern in rules["forbidden_class_patterns"] + rules["forbidden_variable_patterns"]:
            with self.subTest(pattern=pattern):
                re.compile(pattern)
                # Written in the common subset of Python and JavaScript regular expressions.
                self.assertNotIn("(?P", pattern)
                self.assertNotIn("(?i", pattern)


seed = load("fleetops_ui_check_seed", UI_CHECK / "seed.py")
fetch_assets = load("fleetops_ui_check_fetch_assets", UI_CHECK / "fetch_assets.py")

# The pages design 3.8 (G21) and PU 9.2 name; each must be in the list from the start.
REQUIRED_PAGES = {
    "dashboard", "my-stats", "my-history", "fleet-active-member", "fleet-closed", "srp-home", "srp-link", "srp-claim-paid",
    "pap-link-register", "leaderboard", "dashboard-fc", "start", "presets", "fleet-active-manager", "fleet-nowrite",
    "scheduled", "edit-scheduled", "operations", "manual-fleet", "pap-fleet-start", "roster-import", "edit-operation",
    "approvals", "fc-stats", "fc-stats-detail", "incentives", "fleet-pending", "fleet-rejected", "corp-stats",
    "corp-members", "corp-inactive", "member-detail", "all-corps", "alliance-members", "history-alliance", "srp-review",
    "srp-payments", "srp-summary", "srp-claim-pending", "config", "config-fleet-types", "config-ship-functions",
    "config-srp-rules", "config-settings", "config-srp-settings", "audit", "start-error", "config-error",
    "config-groups", "config-group-edit", "config-channel-edit", "start-fleetleader", "start-fcleader",
    "send-ping-pingleader", "send-ping-fleetleader", "send-ping-fcleader", "send-ping-fc", "fleet-group-manager",
    "fleet-group-readonly", "operations-fleetleader", "denied-start", "denied-send-ping", "notfound-fleet",
    "notfound-preping", "notfound-corp",
}
ROLES = {
    "member", "fc", "fc_nowrite", "lead", "corp", "stats", "srp", "admin", "pingleader", "fleetleader", "fleetleader2",
    "fcleader",
}


class PageListTests(SimpleTestCase):
    pages = seed.build_pages()

    def test_every_page_of_the_design_is_in_the_list(self):
        ids = [page.id for page in self.pages]

        self.assertEqual(sorted(REQUIRED_PAGES - set(ids)), [])
        self.assertEqual(len(ids), len(set(ids)), "page ids must be unique")

    def test_roles_and_statuses_are_known(self):
        for page in self.pages:
            with self.subTest(page=page.id):
                self.assertIn(page.role, ROLES)
                self.assertIn(page.status, (200, 403, 404))
                self.assertTrue(callable(page.url))

    def test_refused_and_missing_pages_expect_their_status(self):
        by_id = {page.id: page for page in self.pages}

        self.assertEqual(by_id["denied-start"].status, 403)
        self.assertEqual(by_id["denied-send-ping"].status, 403)
        for name in ("notfound-fleet", "notfound-preping", "notfound-corp"):
            self.assertEqual(by_id[name].status, 404)
        self.assertEqual(by_id["dashboard"].status, 200)

    def test_lanes_follow_the_ownership_rule_of_the_design(self):
        lane = seed.lane_of
        expected = {
            "start": "A", "start-error": "A", "start-fleetleader": "A", "presets": "A", "scheduled": "A", "edit-scheduled": "A",
            "fleet-active-manager": "B", "fleet-group-readonly": "B", "pap-link-register": "B", "pap-fleet-start": "B",
            "fleet-pending": "C", "fleet-rejected": "C", "operations": "C", "operations-fleetleader": "C",
            "manual-fleet": "C", "roster-import": "C", "edit-operation": "C", "approvals": "C", "incentives": "C",
            "dashboard": "D", "dashboard-fc": "D", "my-stats": "D", "leaderboard": "D", "corp-members": "D",
            "all-corps": "D", "alliance-members": "D", "history-alliance": "D", "fc-stats-detail": "D", "member-detail": "D",
            "srp-home": "E", "srp-claim-pending": "E", "config": "E", "config-error": "E", "config-channel-edit": "E",
            "audit": "E", "send-ping-fc": "F", "send-ping-pingleader": "F", "config-groups": "F", "config-group-edit": "F",
            "denied-start": "S0", "notfound-fleet": "S0",
        }
        for page_id, letter in expected.items():
            with self.subTest(page=page_id):
                self.assertEqual(lane(page_id), letter)

    def test_every_page_belongs_to_a_known_lane(self):
        self.assertTrue({page.lane for page in self.pages} <= set(seed.LANES))

    def test_the_tree_has_everything_the_pages_need(self):
        for page in self.pages:
            with self.subTest(page=page.id):
                self.assertEqual(seed.missing_requirement(page, None), "")

    def test_the_group_pages_and_the_stub_ping_page_are_expected_to_answer(self):
        by_id = {page.id: page for page in self.pages}

        for name in ("config-groups", "config-group-edit", "config-channel-edit", "start-fleetleader", "start-fcleader",
                     "send-ping-fleetleader", "send-ping-fcleader", "send-ping-pingleader", "send-ping-fc",
                     "fleet-group-manager", "fleet-group-readonly", "operations-fleetleader", "dashboard-fleetleader"):
            with self.subTest(page=name):
                self.assertEqual(by_id[name].requires, ())
                self.assertEqual(by_id[name].status, 200)

    def test_the_two_error_states_are_scripted_pages(self):
        by_id = {page.id: page for page in self.pages}

        self.assertEqual(by_id["start-error"].states, ("start-error",))
        self.assertEqual(by_id["config-error"].states, ("config-error",))
        self.assertEqual(by_id["start-error"].role, "fc")
        self.assertEqual(by_id["config-error"].role, "admin")

    def test_the_requirements_of_a_page_are_checked_against_the_tree(self):
        def reason(*requires):
            page = seed.Page("x", "member", lambda ctx: "/", requires=requires)
            return seed.missing_requirement(page, None)

        self.assertEqual(reason(), "")
        self.assertEqual(reason("url:dashboard", "model:FleetOperation", "section:fleet-types"), "")
        self.assertIn("nothing_here", reason("url:nothing_here"))
        self.assertIn("NoSuchModel", reason("model:NoSuchModel"))
        self.assertIn("no-such-section", reason("section:no-such-section"))
        self.assertEqual(reason("field:FleetOperation.status"), "")
        self.assertIn("FleetOperation.nothing", reason("field:FleetOperation.nothing"))

    def test_the_themes_are_the_five_modes_of_the_design(self):
        self.assertEqual(seed.THEME_KEYS, ("bootstrap", "bootstrap-dark", "flatly", "darkly", "materia"))


class AssetCacheTests(SimpleTestCase):
    def test_a_hash_matches_only_its_own_data(self):
        data = b"body { color: red }"
        integrity = fetch_assets.sri(data)

        self.assertTrue(integrity.startswith("sha512-"))
        self.assertTrue(fetch_assets.matches(data, integrity))
        self.assertTrue(fetch_assets.matches(data, "sha256-nonsense " + integrity))
        self.assertFalse(fetch_assets.matches(data + b" ", integrity))
        self.assertFalse(fetch_assets.matches(data, "sha512-AAAA"))

    def test_the_urls_and_hashes_come_from_the_installed_alliance_auth(self):
        pinned = fetch_assets.pinned_urls()

        for theme in ("flatly", "darkly", "materia"):
            self.assertTrue(any(f"/{theme}/bootstrap.min.css" in url for url in pinned), theme)
        self.assertTrue(any("font-awesome" in url for url in pinned))
        self.assertTrue(any("/jquery/" in url for url in pinned))
        self.assertTrue(all(value.startswith("sha") for value in pinned.values()))

    def test_a_cdn_library_url_is_split_into_package_parts(self):
        url = "https://cdnjs.cloudflare.com/ajax/libs/bootswatch/5.3.3/flatly/bootstrap.min.css"

        self.assertEqual(fetch_assets.library_of(url), ("bootswatch", "5.3.3", "flatly/bootstrap.min.css"))
        self.assertIsNone(fetch_assets.library_of("https://example.invalid/x.css"))

    def test_a_cdn_file_that_does_not_match_its_hash_stops_the_fetch(self):
        url = "https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js"
        with mock.patch.object(fetch_assets, "fetch", return_value=b"tampered"):
            with self.assertRaises(fetch_assets.HashMismatch):
                fetch_assets.obtain(url, fetch_assets.sri(b"the real file"))

    def test_an_unreachable_cdn_falls_back_to_the_package_and_still_checks_the_hash(self):
        url = "https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js"
        real = b"the real file"
        with mock.patch.object(fetch_assets, "fetch", side_effect=OSError("no network")), \
                mock.patch.object(fetch_assets, "from_npm", return_value=real):
            data, source, verified = fetch_assets.obtain(url, fetch_assets.sri(real))
            self.assertEqual((data, source, verified), (real, "npm", "sri"))
            with self.assertRaises(fetch_assets.AssetError):
                fetch_assets.obtain(url, fetch_assets.sri(b"another file"))


class ScriptSyntaxTests(SimpleTestCase):
    def test_the_scripts_parse(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is not installed")
        for path in sorted(UI_CHECK.glob("*.js")):
            with self.subTest(script=path.name):
                result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_script_and_python_file_has_its_licence_header(self):
        for path in sorted(UI_CHECK.glob("*.js")):
            with self.subTest(script=path.name):
                lines = path.read_text(encoding="utf-8").splitlines()
                start = 1 if lines[0].startswith("#!") else 0
                self.assertEqual(lines[start], "// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors")
                self.assertEqual(lines[start + 1], "// SPDX-License-Identifier: GPL-3.0-only")


class RepositoryFilesTests(SimpleTestCase):
    def test_the_run_folders_of_the_browser_check_are_not_committed(self):
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

        for folder in ("tools/ui_check/.work/", "tools/ui_check/.cache/", "tools/ui_check/out/"):
            with self.subTest(folder=folder):
                self.assertIn(folder, lines)
        self.assertFalse((UI_CHECK / ".gitignore").exists())

    def test_the_browser_check_is_not_in_the_source_distribution(self):
        lines = (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()

        self.assertIn("recursive-include tools *.py", lines)
        self.assertIn("prune tools/ui_check", lines)
        self.assertLess(lines.index("recursive-include tools *.py"), lines.index("prune tools/ui_check"))
