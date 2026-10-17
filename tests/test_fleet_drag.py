# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Dragging members in the fleet tree: a label as the drag image, and no height change while a drag lasts (FB03)."""

import re
from pathlib import Path

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from fleetops.services.fleet_view import fleet_panel

from .test_fleet_view import ALPHA, BRAVO, MAIN, FleetViewTestCase

STATIC = Path(__file__).resolve().parent.parent / "fleetops" / "static" / "fleetops"


def source(name):
    return (STATIC / name).read_text(encoding="utf-8")


def function_body(script, name):
    """The text of ``function name(...) { ... }`` of a script (the braces of its body are balanced)."""
    start = script.index(f"function {name}(")
    depth, index = 0, script.index("{", start)
    for position in range(index, len(script)):
        depth += {"{": 1, "}": -1}.get(script[position], 0)
        if depth == 0:
            return script[index : position + 1]
    raise AssertionError(name)


class DragScriptTests(SimpleTestCase):
    def setUp(self):
        self.script = source("fleet_actions.js")

    def test_the_drag_image_is_a_label_in_the_page(self):
        self.assertIn("setDragImage(makeDragLabel(row, ids)", self.script)
        body = function_body(self.script, "makeDragLabel")
        self.assertIn("fleet-drag-label", body)
        self.assertIn("document.body.appendChild(", body)
        # The label shows the member's name, or how many members are dragged.
        self.assertIn("row.dataset.characterName", body)
        self.assertIn("%(count)s selected", body)
        # Names come from ESI: they are set as text.
        self.assertIn("textContent", body)
        self.assertNotIn("innerHTML", self.script)

    def test_the_label_is_removed_when_the_drag_ends(self):
        self.assertIn("removeDragLabel();", function_body(self.script, "endDrag"))
        # And once the browser has taken its picture, even when no dragend arrives.
        start = self.script.index("// After the browser took the drag image")
        self.assertIn("removeDragLabel();", self.script[start : start + 400])
        self.assertIn("dragLabel.remove()", function_body(self.script, "removeDragLabel"))

    def test_nothing_changes_height_when_a_drag_starts(self):
        # The empty squads are sized by the server (class fleet-members-empty); the drag only adds an outline.
        body = function_body(self.script, "markDropTargets")
        self.assertIn("fleet-drop-empty", body)
        for forbidden in ("style.minHeight", "style.outline", "6c757d", "--bs-secondary-color", "fleetDropSized"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.script)

    def test_the_drop_marks_are_cleared_with_the_other_ones(self):
        body = function_body(self.script, "clearDropMarks")
        self.assertIn("fleet-drop-empty", body)

    def test_the_stylesheet_gives_the_label_and_the_empty_squads_their_looks(self):
        css = source("fleet.css")

        label = css.split(".fleet-drag-label {", 1)[1].split("}", 1)[0]
        self.assertIn("position: fixed", label)
        self.assertIn("pointer-events: none", label)
        self.assertIn("var(--fo-surface-solid)", label)
        empty = css.split(".fleet-members-empty {", 1)[1].split("}", 1)[0]
        self.assertIn("min-height: 1.75rem", empty)
        outline = css.split(".fleet-drop-empty:not(.fleet-drop-over) {", 1)[1].split("}", 1)[0]
        self.assertIn("outline: 1px dashed var(--fo-border)", outline)
        self.assertNotIn("height", outline)


class DragMarkupTests(FleetViewTestCase):
    def html(self):
        self.operation.refresh_from_db()
        return render_to_string("fleetops/operation/_fleet_tree.html", fleet_panel(self.operation, self.fc))

    def test_a_member_row_names_the_member_for_the_label(self):
        self.pilot("Pilot A", squad_id=ALPHA)

        html = self.html()

        self.assertRegex(html, r'<li class="fleet-member"[^>]* data-character-name="Pilot A"')

    def test_the_name_in_the_label_is_escaped(self):
        self.pilot('Pilot "<b>"', squad_id=ALPHA)

        html = self.html()

        self.assertIn('data-character-name="Pilot &quot;&lt;b&gt;&quot;"', html)
        self.assertNotIn("<b>", html.split("fleet-members", 1)[1].split("Pilot", 1)[0])

    def test_a_squad_without_members_keeps_its_height_all_the_time(self):
        self.pilot("Pilot A", squad_id=ALPHA)

        html = self.html()

        lists = re.findall(
            r'<ul class="list-unstyled fleet-members([^"]*)" data-drop-role="squad_member" '
            r'data-wing-id="(\d+)" data-squad-id="(\d+)"',
            html,
        )
        by_squad = {int(squad): classes for classes, _wing, squad in lists}
        self.assertNotIn("fleet-members-empty", by_squad[ALPHA])
        self.assertIn("fleet-members-empty", by_squad[BRAVO])

    def test_the_wing_lists_are_not_marked(self):
        html = self.html()

        self.assertNotIn("fleet-members-empty fleet-command", html)
        self.assertEqual(html.count("fleet-members-empty"), 3)
        self.assertIn(f'data-wing-id="{MAIN}"', html)

    def test_a_member_in_the_squad_is_a_drag_source_with_a_selection_box(self):
        self.pilot("Pilot A", squad_id=ALPHA)

        html = self.html()

        self.assertIn('draggable="true"', html)
        self.assertIn(
            '<label class="fo-check-cell fleet-select-cell"><input class="form-check-input fleet-select"', html
        )
