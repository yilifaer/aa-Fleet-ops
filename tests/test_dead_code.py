# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Guard against code that nothing uses: unused imports and the helpers removed in 0.3.0b1."""

import ast
import importlib
from pathlib import Path

from django.test import SimpleTestCase

import fleetops

PACKAGE_DIR = Path(fleetops.__file__).resolve().parent

# Names that were defined and never called, removed on purpose; they must not come back unnoticed.
REMOVED = {
    "fleetops.srp.display": ("MONTHLY_KIND_LABELS",),
    "fleetops.srp.reports": ("table_cell_text",),
    "fleetops.srp.types": ("ZKILL_STATUSES",),
    "fleetops.services.messages": ("plain_motd",),
    "fleetops.services.fleet_controls": ("fc_can_kick",),
    "fleetops.services.fleet_rules": ("RULE_KINDS",),
}


def unused_imports(path):
    source = path.read_text()
    tree = ast.parse(source)
    lines = source.splitlines()
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported[(alias.asname or alias.name).split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            for alias in node.names:
                imported[alias.asname or alias.name] = node.lineno
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    for node in ast.walk(tree):  # a name listed in __all__ is a deliberate re-export
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "__all__" for t in node.targets):
            used.update(element.value for element in ast.walk(node.value) if isinstance(element, ast.Constant))
    return sorted(
        f"{path.relative_to(PACKAGE_DIR)}:{lineno} {name}"
        for name, lineno in imported.items()
        if name not in used and name != "*" and "noqa" not in lines[lineno - 1]
    )


class DeadCodeTests(SimpleTestCase):
    def test_no_module_imports_a_name_it_does_not_use(self):
        found = []
        for path in sorted(PACKAGE_DIR.rglob("*.py")):
            if "migrations" in path.parts or path.name == "__init__.py":
                continue
            found.extend(unused_imports(path))
        self.assertEqual(found, [], "unused imports (remove them, or mark a deliberate re-export with noqa)")

    def test_removed_helpers_stay_removed(self):
        for module_name, names in REMOVED.items():
            module = importlib.import_module(module_name)
            for name in names:
                self.assertFalse(hasattr(module, name), f"{module_name}.{name}")
