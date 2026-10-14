# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Every source file carries the copyright and licence lines."""

from pathlib import Path

from django.test import SimpleTestCase

import fleetops

ROOT = Path(fleetops.__file__).resolve().parent.parent
HOLDER = "2026 yilifaer and AA FleetOps contributors"
LICENSE_ID = "GPL-3.0-only"

HEADERS = {
    ".py": [f"# SPDX-FileCopyrightText: {HOLDER}", f"# SPDX-License-Identifier: {LICENSE_ID}"],
    ".js": [f"// SPDX-FileCopyrightText: {HOLDER}", f"// SPDX-License-Identifier: {LICENSE_ID}"],
    ".css": [f"/* SPDX-FileCopyrightText: {HOLDER} */", f"/* SPDX-License-Identifier: {LICENSE_ID} */"],
}


def source_files():
    """Python files of the package (migrations aside), tests, test project and tools, the two scripts, static files."""
    found = []
    for path in (ROOT / "fleetops").rglob("*.py"):
        if "migrations" not in path.relative_to(ROOT / "fleetops").parts:
            found.append(path)
    for folder in ("tests", "testauth", "tools"):
        if (ROOT / folder).is_dir():
            found.extend((ROOT / folder).rglob("*.py"))
    found.extend(ROOT / name for name in ("manage.py", "runtests.py"))
    found.extend(
        path for path in (ROOT / "fleetops" / "static").rglob("*") if path.suffix in {".js", ".css"}
    )
    return sorted(path for path in found if "__pycache__" not in path.parts and path.read_text(encoding="utf-8").strip())


class LicenseHeaderTests(SimpleTestCase):
    def test_every_source_file_has_the_header(self):
        files = source_files()
        self.assertGreater(len(files), 200)

        wrong = []
        for path in files:
            lines = path.read_text(encoding="utf-8").splitlines()
            start = 1 if lines[0].startswith("#!") else 0
            if lines[start : start + 2] != HEADERS[path.suffix]:
                wrong.append(str(path.relative_to(ROOT)))
        self.assertEqual(wrong, [], "Files without the SPDX header")

    def test_the_headers_are_checked_for_each_kind_of_file(self):
        suffixes = {path.suffix for path in source_files()}

        self.assertEqual(suffixes, {".py", ".js", ".css"})

    def test_scripts_keep_their_first_line(self):
        for name in ("manage.py", "runtests.py"):
            with self.subTest(name=name):
                lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
                self.assertTrue(lines[0].startswith("#!"))
                self.assertEqual(lines[1:3], HEADERS[".py"])

    def test_migrations_and_templates_have_none(self):
        for path in (ROOT / "fleetops" / "migrations").glob("*.py"):
            self.assertNotIn("SPDX-", path.read_text(encoding="utf-8"), path.name)
        for path in (ROOT / "fleetops" / "templates").rglob("*.html"):
            self.assertNotIn("SPDX-", path.read_text(encoding="utf-8"), path.name)
