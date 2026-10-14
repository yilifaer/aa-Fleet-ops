import glob
import os
import re
from pathlib import Path

from django.test import SimpleTestCase

import fleetops

PACKAGE_DIR = Path(fleetops.__file__).resolve().parent
PYPROJECT = PACKAGE_DIR.parent / "pyproject.toml"


def package_data_globs():
    """The fleetops globs of [tool.setuptools.package-data], read without tomllib (Python 3.10)."""
    text = PYPROJECT.read_text(encoding="utf-8")
    block = re.search(r"^\[tool\.setuptools\.package-data\]\s*$(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    if block is None:
        return []
    entry = re.search(r"^fleetops\s*=\s*\[(.*?)\]", block.group(1), re.MULTILINE | re.DOTALL)
    return re.findall(r"""["']([^"']+)["']""", entry.group(1)) if entry else []


def packaged_files(patterns):
    # setuptools expands package-data globs relative to the package with recursive "**".
    found = set()
    for pattern in patterns:
        for path in glob.glob(os.path.join(PACKAGE_DIR, pattern), recursive=True):
            if os.path.isfile(path):
                found.add(Path(path).resolve())
    return found


class PackageDataTests(SimpleTestCase):
    def test_globs_are_read_from_pyproject(self):
        patterns = package_data_globs()

        self.assertTrue(patterns)
        self.assertTrue(any(pattern.startswith("templates/") for pattern in patterns))
        self.assertTrue(any(pattern.startswith("static/") for pattern in patterns))

    def test_every_template_and_static_file_is_packaged(self):
        packaged = packaged_files(package_data_globs())
        files = [
            path.resolve()
            for folder in ("templates", "static")
            for path in (PACKAGE_DIR / folder).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        ]

        self.assertTrue(any(path.suffix == ".html" for path in files))
        self.assertTrue(any(path.name == "form.html" and path.parent.name == "configuration" for path in files))
        missing = sorted(str(path.relative_to(PACKAGE_DIR)) for path in files if path not in packaged)
        self.assertEqual(missing, [], "Files not covered by [tool.setuptools.package-data]")

    def test_statistics_templates_are_packaged(self):
        packaged = packaged_files(package_data_globs())
        folder = PACKAGE_DIR / "templates" / "fleetops" / "statistics"
        templates = {path.resolve() for path in folder.glob("*.html")}

        self.assertTrue({"_tabs.html", "_status_badge.html", "_export_buttons.html"} <= {p.name for p in templates})
        self.assertEqual(templates - packaged, set())

    def test_srp_templates_are_packaged(self):
        packaged = packaged_files(package_data_globs())
        folder = PACKAGE_DIR / "templates" / "fleetops" / "srp"
        templates = {path.resolve() for path in folder.glob("*.html")}

        names = {path.name for path in templates}
        self.assertTrue({"home.html", "link.html", "claim.html", "review.html", "payments.html", "summary.html"} <= names)
        self.assertEqual(templates - packaged, set())
        self.assertIn((PACKAGE_DIR / "templates" / "fleetops" / "operation" / "_srp.html").resolve(), packaged)
