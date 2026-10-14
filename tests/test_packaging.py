# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

import glob
import hashlib
import os
import re
import unittest
from pathlib import Path

from django.test import SimpleTestCase

import fleetops

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None

PACKAGE_DIR = Path(fleetops.__file__).resolve().parent
ROOT = PACKAGE_DIR.parent
PYPROJECT = ROOT / "pyproject.toml"
REPOSITORY = "https://github.com/yilifaer/aa-fleetops"
AUTHOR = "yilifaer and AA FleetOps contributors"
# SHA-256 of the GNU General Public License version 3 as the Free Software Foundation publishes it.
GPL3_SHA256 = "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"


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
        files += [path.resolve() for path in (PACKAGE_DIR / "locale").glob("*/LC_MESSAGES/*") if path.is_file()]

        self.assertTrue(any(path.suffix == ".html" for path in files))
        self.assertTrue(any(path.name == "form.html" and path.parent.name == "configuration" for path in files))
        self.assertTrue(any(path.suffix == ".mo" for path in files))
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


class TranslationFilesTests(SimpleTestCase):
    def test_catalogs_are_packaged_as_sources_and_compiled(self):
        patterns = package_data_globs()
        packaged = packaged_files(patterns)

        for domain in ("django", "djangojs"):
            for suffix in (".po", ".mo"):
                path = PACKAGE_DIR / "locale" / "zh_Hans" / "LC_MESSAGES" / f"{domain}{suffix}"
                with self.subTest(path=path.name):
                    self.assertTrue(path.is_file())
                    self.assertIn(path.resolve(), packaged)

    def test_only_catalogs_live_in_the_locale_folder(self):
        extra = [
            str(path.relative_to(PACKAGE_DIR))
            for path in (PACKAGE_DIR / "locale").rglob("*")
            if path.is_file() and path.suffix not in {".po", ".mo"}
        ]

        self.assertEqual(extra, [])

    def test_gettext_attributes_keep_catalogs_as_they_are(self):
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8").split("\n")

        self.assertIn("*.mo binary", attributes)
        self.assertIn("*.po text eol=lf", attributes)
        self.assertIn("LICENSE text eol=lf", attributes)


class SourceDistributionTests(SimpleTestCase):
    def test_manifest_adds_the_documents_package_data_does_not_cover(self):
        lines = (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()

        self.assertIn("include CHANGELOG.md README.zh-CN.md SECURITY.md CONTRIBUTING.md", lines)
        self.assertIn("recursive-include docs *.md", lines)

    def test_manifest_adds_the_release_tools_and_leaves_the_tests_out(self):
        lines = (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()

        self.assertIn("recursive-include tools *.py", lines)
        # setuptools adds tests/test*.py by itself; without the test project and the helpers they cannot run.
        self.assertIn("prune tests", lines)

    def test_everything_the_manifest_names_exists(self):
        text = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        for words in (line.split() for line in text.splitlines() if line.strip() and not line.startswith("#")):
            if words[0] == "include":
                for name in words[1:]:
                    with self.subTest(name=name):
                        self.assertTrue((ROOT / name).is_file())
            elif words[0] == "recursive-include":
                with self.subTest(folder=words[1]):
                    self.assertTrue((ROOT / words[1]).is_dir())


class LicenseFileTests(SimpleTestCase):
    def test_license_is_the_full_gpl_3_text(self):
        data = (ROOT / "LICENSE").read_bytes()

        self.assertEqual(hashlib.sha256(data).hexdigest(), GPL3_SHA256)
        self.assertIn(b"GNU GENERAL PUBLIC LICENSE\n                       Version 3, 29 June 2007", data)

    def test_package_modules_carry_the_public_metadata(self):
        self.assertEqual(fleetops.__url__, REPOSITORY)
        self.assertEqual(fleetops.__author__, AUTHOR)
        self.assertEqual(fleetops.__title__, "AA FleetOps")
        self.assertRegex(fleetops.__version__, r"^\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$")
        self.assertNotIn("@", fleetops.__author__)


@unittest.skipIf(tomllib is None, "tomllib needs Python 3.11")
class ProjectMetadataTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(PYPROJECT, "rb") as handle:
            cls.config = tomllib.load(handle)
        cls.project = cls.config["project"]

    def test_the_version_is_read_from_the_package(self):
        self.assertEqual(self.project["dynamic"], ["version"])
        self.assertNotIn("version", self.project)
        self.assertEqual(self.config["tool"]["setuptools"]["dynamic"]["version"], {"attr": "fleetops.__version__"})

    def test_licence_is_declared_as_an_spdx_expression_with_its_file(self):
        self.assertEqual(self.project["license"], "GPL-3.0-only")
        self.assertEqual(self.project["license-files"], ["LICENSE"])
        self.assertEqual([c for c in self.project["classifiers"] if c.startswith("License ::")], [])

    def test_authors_have_a_name_and_no_address(self):
        self.assertEqual(self.project["authors"], [{"name": AUTHOR}])

    def test_all_links_point_to_the_repository(self):
        urls = self.project["urls"]

        self.assertEqual(urls["Homepage"], REPOSITORY)
        self.assertEqual(urls["Source"], REPOSITORY)
        self.assertEqual(urls["Issues"], f"{REPOSITORY}/issues")
        self.assertTrue(urls["Changelog"].startswith(f"{REPOSITORY}/blob/dev/"))
        self.assertTrue(urls["Documentation"].startswith(f"{REPOSITORY}/tree/dev/"))
        self.assertEqual(urls["Homepage"], fleetops.__url__)

    def test_the_package_name_and_languages(self):
        self.assertEqual(self.project["name"], "aa-fleetops")
        self.assertIn("Natural Language :: English", self.project["classifiers"])
        self.assertIn("Natural Language :: Chinese (Simplified)", self.project["classifiers"])

    def test_package_data_is_recursive_and_includes_the_catalogs(self):
        patterns = self.config["tool"]["setuptools"]["package-data"]["fleetops"]

        self.assertIn("templates/**/*.html", patterns)
        self.assertIn("static/**/*", patterns)
        self.assertIn("locale/*/LC_MESSAGES/*.po", patterns)
        self.assertIn("locale/*/LC_MESSAGES/*.mo", patterns)

    def test_the_regular_expression_reader_finds_the_same_globs(self):
        self.assertEqual(package_data_globs(), self.config["tool"]["setuptools"]["package-data"]["fleetops"])
