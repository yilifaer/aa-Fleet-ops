# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The release tools in tools/: changelog sections, distribution checks, link checks and translation checks."""

import contextlib
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

import fleetops

from .test_docs_sync import load_tool

ROOT = Path(fleetops.__file__).resolve().parent.parent
VERSION = "0.2.0b1"

CHANGELOG = """# Changelog

## [0.3.0.dev0] - Unreleased

- Next thing.

## [0.2.0b1] - 2026-11-01

### Added

- Simplified Chinese translation.

```text
## not a heading
```

### Upgrade notes

- Run `migrate`.

## 0.1.0a6 (unreleased)

- Old section.

## 0.1.0a1 - 2026-08-23

- First.

## [0.1.0a0] - 2026-01-01
"""


def run(tool, *args):
    """``(exit code, stdout, stderr)`` of ``tool.main(args)``."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = tool.main(list(args))
    return code, out.getvalue(), err.getvalue()


class TemporaryTree(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, name, text="", binary=False):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if binary:
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        return path


class ChangelogSectionTests(TemporaryTree):
    tool = load_tool("changelog_section")

    def section(self, version, *extra):
        path = self.write("CHANGELOG.md", CHANGELOG)
        return run(self.tool, version, "--changelog", str(path), *extra)

    def test_a_dated_section_is_printed_without_its_heading(self):
        code, out, _err = self.section("0.2.0b1")

        self.assertEqual(code, 0)
        self.assertIn("- Simplified Chinese translation.", out)
        self.assertIn("### Upgrade notes", out)
        self.assertIn("## not a heading", out)
        self.assertNotIn("0.2.0b1", out)
        self.assertNotIn("Next thing", out)
        self.assertNotIn("Old section", out)

    def test_a_leading_v_is_ignored(self):
        self.assertEqual(self.section("v0.2.0b1")[0], 0)

    def test_older_heading_styles_are_found(self):
        self.assertEqual(self.section("0.1.0a1")[1].strip(), "- First.")
        self.assertEqual(self.section("0.1.0a6", "--allow-unreleased")[1].strip(), "- Old section.")

    def test_an_unreleased_section_is_refused_unless_allowed(self):
        code, out, err = self.section("0.3.0.dev0")

        self.assertEqual((code, out), (1, ""))
        self.assertIn("unreleased", err)
        code, out, _err = self.section("0.3.0.dev0", "--allow-unreleased")
        self.assertEqual((code, out.strip()), (0, "- Next thing."))

    def test_a_missing_or_empty_section_fails(self):
        code, out, err = self.section("9.9.9")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no section", err)

        code, out, err = self.section("0.1.0a0")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("empty", err)

    def test_equal_versions_are_equal_in_other_spellings(self):
        self.assertTrue(self.tool.same_version("0.2.0b1", "v0.2.0b1"))
        self.assertFalse(self.tool.same_version("0.2.0b1", "0.2.0b2"))
        try:
            import packaging  # noqa: F401
        except ImportError:
            return
        self.assertTrue(self.tool.same_version("0.2.0b1", "0.2.0.beta1"))


class CheckDocsTests(TemporaryTree):
    tool = load_tool("check_docs")

    def problems(self):
        return self.tool.check(self.root)

    def test_working_links_pass(self):
        self.write("README.md", "# Title\n\nSee [guide](docs/guide.md#first-steps), [top](#title), [dir](docs/).\n")
        self.write("docs/guide.md", "# Guide\n\n## First steps\n\nBack to [readme](../README.md).\n")

        self.assertEqual(self.problems(), [])

    def test_broken_links_and_anchors_are_reported_with_their_line(self):
        self.write(
            "README.md",
            "# Title\n\n[gone](docs/gone.md)\n[anchor](docs/guide.md#nope)\n[mine](#missing)\n",
        )
        self.write("docs/guide.md", "# Guide\n")

        found = self.problems()

        self.assertEqual(len(found), 3)
        self.assertTrue(found[0].startswith("README.md:3: broken link"))
        self.assertTrue(found[1].startswith("README.md:4: no heading"))
        self.assertTrue(found[2].startswith("README.md:5: no heading"))

    def test_code_other_sites_and_mail_links_are_not_checked(self):
        self.write(
            "README.md",
            "# Title\n\n`[x](missing.md)`\n\n```text\n[y](missing.md)\n```\n\n"
            "[site](https://example.com/missing.md) [mail](mailto:a@example.com)\n",
        )

        self.assertEqual(self.problems(), [])

    def test_absolute_links_into_the_repository_are_checked_against_the_tree(self):
        base = "https://github.com/yilifaer/aa-fleetops"
        self.write("README.md", f"# Title\n\n[ok]({base}/blob/dev/docs/a.md#part) [bad]({base}/blob/dev/docs/b.md)\n")
        self.write("docs/a.md", "# A\n\n## Part\n")

        found = self.problems()

        self.assertEqual(len(found), 1)
        self.assertIn("docs/b.md", found[0])

    def test_pages_of_the_repository_that_are_not_files_are_left_alone(self):
        base = "https://github.com/yilifaer/aa-fleetops"
        self.write("README.md", f"# Title\n\n[i]({base}/issues) [d]({base}/discussions) [r]({base}/releases/latest)\n")

        self.assertEqual(self.problems(), [])

    def test_headings_become_anchors_the_way_github_makes_them(self):
        slug = self.tool.slugify
        self.assertEqual(slug("Statistics rules"), "statistics-rules")
        self.assertEqual(slug("Logi / Boost Channels"), "logi--boost-channels")
        self.assertEqual(slug("`fleetops_seed` and **more**"), "fleetops_seed-and-more")
        self.assertEqual(slug("配置 reference"), "配置-reference")
        text = "# Same\n\n## Same\n\n## Same\n"
        self.assertEqual(self.tool.anchors(text), {"same", "same-1", "same-2"})

    def test_the_main_function_reports_success_and_failure(self):
        self.write("README.md", "# Title\n")
        self.assertEqual(run(self.tool, "--root", str(self.root))[0], 0)

        self.write("README.md", "# Title\n\n[x](nowhere.md)\n")
        code, _out, err = run(self.tool, "--root", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("nowhere.md", err)


class CheckDistTests(TemporaryTree):
    tool = load_tool("check_dist")

    METADATA = (
        "Metadata-Version: 2.4\nName: aa-fleetops\nVersion: {version}\nAuthor: yilifaer and AA FleetOps contributors\n"
        "License-Expression: GPL-3.0-only\nLicense-File: LICENSE\nClassifier: Framework :: Django\n"
    )

    def setUp(self):
        super().setUp()
        self.write("fleetops/__init__.py", f'__version__ = "{VERSION}"\n')
        self.write("fleetops/models.py", "x = 1\n")
        self.write("fleetops/templates/fleetops/base.html", "<html></html>")
        self.write("fleetops/static/fleetops/fleetops.css", "a {}")
        self.write("fleetops/locale/zh_Hans/LC_MESSAGES/django.mo", b"\x00", binary=True)
        self.write("fleetops/locale/zh_Hans/LC_MESSAGES/.gitkeep", "")
        self.wanted = {
            "fleetops/__init__.py",
            "fleetops/models.py",
            "fleetops/templates/fleetops/base.html",
            "fleetops/static/fleetops/fleetops.css",
            "fleetops/locale/zh_Hans/LC_MESSAGES/django.mo",
        }

    def wheel(self, *, drop=(), extra=(), metadata=None, name=f"aa_fleetops-{VERSION}-py3-none-any.whl", license=True):
        path = self.root / "dist" / name
        path.parent.mkdir(exist_ok=True)
        info = f"aa_fleetops-{VERSION}.dist-info"
        with zipfile.ZipFile(path, "w") as archive:
            for member in sorted(self.wanted - set(drop)) + list(extra):
                archive.writestr(member, "x")
            archive.writestr(f"{info}/METADATA", metadata or self.METADATA.format(version=VERSION))
            if license:
                archive.writestr(f"{info}/licenses/LICENSE", "text")
        return path

    def sdist(self, *, drop=(), extra=()):
        path = self.root / "dist" / f"aa_fleetops-{VERSION}.tar.gz"
        path.parent.mkdir(exist_ok=True)
        prefix = f"aa_fleetops-{VERSION}"
        members = set(self.wanted) | set(self.tool.SDIST_FILES) | {"PKG-INFO", "docs/architecture.md"} | set(extra)
        with tarfile.open(path, "w:gz") as archive:
            for member in sorted(members - set(drop)):
                data = b"x"
                info = tarfile.TarInfo(f"{prefix}/{member}")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return path

    def test_the_files_of_the_package_are_found_without_git(self):
        self.assertEqual(self.tool.repository_files(self.root), self.wanted)

    def test_the_version_is_read_from_the_source(self):
        self.assertEqual(self.tool.source_version(self.root), VERSION)

    def test_a_good_wheel_passes(self):
        self.assertEqual(self.tool.check_wheel(self.wheel(), self.wanted, VERSION), [])

    def test_missing_files_are_reported(self):
        found = self.tool.check_wheel(
            self.wheel(drop=["fleetops/locale/zh_Hans/LC_MESSAGES/django.mo"]), self.wanted, VERSION
        )

        self.assertEqual(len(found), 1)
        self.assertIn("django.mo", found[0])

    def test_test_code_does_not_belong_in_a_wheel(self):
        for member in ("tests/test_x.py", "testauth/settings.py"):
            with self.subTest(member=member):
                found = self.tool.check_wheel(self.wheel(extra=[member]), self.wanted, VERSION)
                self.assertTrue(any(member in line for line in found))

    def test_the_metadata_has_to_match(self):
        cases = {
            "Name: aa-fleetops": "Name: other",
            "License-Expression: GPL-3.0-only": "License-Expression: MIT",
            "License-File: LICENSE\n": "",
            "Classifier: Framework :: Django": "Classifier: License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
            "Author: yilifaer and AA FleetOps contributors": "Author: Someone <someone@example.com>",
        }
        for old, new in cases.items():
            with self.subTest(old=old):
                metadata = self.METADATA.format(version=VERSION).replace(old, new)
                self.assertTrue(self.tool.check_wheel(self.wheel(metadata=metadata), self.wanted, VERSION))
        found = self.tool.check_wheel(self.wheel(metadata=self.METADATA.format(version="0.2.0b2")), self.wanted, VERSION)
        self.assertTrue(any("Version" in line for line in found))

    def test_the_licence_file_has_to_be_in_the_wheel(self):
        found = self.tool.check_wheel(self.wheel(license=False), self.wanted, VERSION)

        self.assertTrue(any("LICENSE" in line for line in found))

    def test_an_email_header_is_refused(self):
        metadata = self.METADATA.format(version=VERSION) + "Author-email: someone@example.com\n"

        self.assertTrue(self.tool.check_wheel(self.wheel(metadata=metadata), self.wanted, VERSION))

    def test_a_good_source_distribution_passes(self):
        self.assertEqual(self.tool.check_sdist(self.sdist(), self.wanted, VERSION), [])

    def test_a_source_distribution_needs_the_documents_and_the_package_data(self):
        for member in ("README.zh-CN.md", "LICENSE", "fleetops/locale/zh_Hans/LC_MESSAGES/django.mo", "docs/architecture.md"):
            with self.subTest(member=member):
                found = self.tool.check_sdist(self.sdist(drop=[member]), self.wanted, VERSION)
                self.assertTrue(found)

    def test_test_modules_do_not_belong_in_a_source_distribution(self):
        found = self.tool.check_sdist(self.sdist(extra=["tests/test_names.py", "tests/factories.py"]), self.wanted, VERSION)

        self.assertEqual(len(found), 1)
        self.assertIn("2 test file(s) in the sdist", found[0])
        self.assertIn("tests/factories.py", found[0])

    def test_the_directory_check_and_the_command(self):
        self.wheel()
        self.sdist()
        code, out, _err = run(self.tool, str(self.root / "dist"), "--root", str(self.root))

        self.assertEqual(code, 0)
        self.assertIn("ok: aa_fleetops-0.2.0b1-py3-none-any.whl", out)

        (self.root / "dist" / f"aa_fleetops-{VERSION}.tar.gz").unlink()
        code, _out, err = run(self.tool, str(self.root / "dist"), "--root", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("No source distribution", err)

    def test_the_command_prints_the_version(self):
        code, out, _err = run(self.tool, "--print-version", "--root", str(self.root))

        self.assertEqual((code, out.strip()), (0, VERSION))

    def test_the_version_of_this_checkout_is_read(self):
        self.assertEqual(self.tool.source_version(ROOT), fleetops.__version__)


class CheckTranslationsTests(TemporaryTree):
    tool = load_tool("check_translations")

    HEADER = (
        'msgid ""\nmsgstr ""\n"Project-Id-Version: AA FleetOps\\n"\n"Language: zh_Hans\\n"\n'
        '"Content-Type: text/plain; charset=UTF-8\\n"\n"Plural-Forms: nplurals=1; plural=0;\\n"\n'
    )

    def catalog(self, body):
        self.write("fleetops/locale/zh_Hans/LC_MESSAGES/django.po", self.HEADER + "\n" + body)

    def compile(self, name="django"):
        """The compiled file of a catalog of the temporary tree, made the way compilemessages makes it."""
        folder = self.root / "fleetops/locale/zh_Hans/LC_MESSAGES"
        subprocess.run(["msgfmt", "-o", str(folder / f"{name}.mo"), str(folder / f"{name}.po")], check=True)

    def test_entries_are_read_from_msgattrib_output(self):
        found = self.tool.entries(
            self.HEADER
            + '\n#: templates/a.html\nmsgid "Save"\nmsgstr ""\n'
            + '\nmsgctxt "fleet status"\nmsgid "Active"\nmsgstr ""\n'
            + '\nmsgid ""\n"A long "\n"message"\nmsgid_plural "Many"\nmsgstr[0] ""\n'
        )

        self.assertEqual(
            found,
            [
                ("templates/a.html", "", "Save"),
                ("", "fleet status", "Active"),
                ("", "", "A long message"),
            ],
        )

    def test_annotations_escape_line_breaks_and_percent_signs(self):
        self.assertEqual(
            self.tool.annotation("error", "a.po", "Title", "50%\nnext"),
            "::error file=a.po,title=Title::50%25%0Anext",
        )

    def test_catalogs_are_found_per_language(self):
        self.catalog('msgid "Save"\nmsgstr "保存"\n')
        self.write("fleetops/locale/de/LC_MESSAGES/djangojs.po", self.HEADER)

        self.assertEqual(self.tool.locales(self.root), ["de", "zh_Hans"])
        self.assertEqual([p.name for p in self.tool.catalogs(self.root)], ["djangojs.po", "django.po"])

    def test_gaps_in_a_catalog_are_counted(self):
        if shutil.which("msgattrib") is None:
            self.skipTest("the GNU gettext tools are not installed")
        self.catalog(
            'msgid "Save"\nmsgstr "保存"\n\nmsgid "Cancel"\nmsgstr ""\n\n#, fuzzy\nmsgid "Delete"\nmsgstr "删"\n'
        )
        lines = []

        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
            total = self.tool.check(self.root, report=lines.append)

        self.assertEqual(total, 2)
        self.assertTrue(any("Untranslated message: Cancel" in line for line in lines))
        self.assertTrue(any("Fuzzy translation: Delete" in line for line in lines))

    def test_gaps_are_annotations_on_github(self):
        if shutil.which("msgattrib") is None:
            self.skipTest("the GNU gettext tools are not installed")
        self.catalog('msgid "Cancel"\nmsgstr ""\n')
        lines = []

        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            self.tool.check(self.root, report=lines.append)

        self.assertEqual(
            lines,
            ["::error file=fleetops/locale/zh_Hans/LC_MESSAGES/django.po,title=Untranslated message::Cancel"],
        )

    def test_the_command_succeeds_on_a_complete_catalog_and_fails_on_a_gap(self):
        if shutil.which("msgattrib") is None:
            self.skipTest("the GNU gettext tools are not installed")
        self.catalog('msgid "Save"\nmsgstr "保存"\n')
        self.compile()
        code, out, _err = run(self.tool, "--no-update", "--root", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("ok: 1 catalog", out)

        self.catalog('msgid "Save"\nmsgstr ""\n')
        code, _out, err = run(self.tool, "--no-update", "--root", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("1 message(s) still need a translation", err)

    def test_a_compiled_catalog_that_is_missing_or_out_of_date_is_found(self):
        if shutil.which("msgfmt") is None:
            self.skipTest("the GNU gettext tools are not installed")
        self.catalog('msgid "Save"\nmsgstr "保存"\n')
        lines = []
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
            self.assertEqual(self.tool.check_compiled(self.root, report=lines.append), 1)
            self.assertIn("django.mo is missing", lines[0])

            self.compile()
            lines.clear()
            self.assertEqual(self.tool.check_compiled(self.root, report=lines.append), 0)

            # A translation was changed in the .po file and nobody compiled it.
            self.catalog('msgid "Save"\nmsgstr "存储"\n')
            self.assertEqual(self.tool.check_compiled(self.root, report=lines.append), 1)
            self.assertIn("django.mo differs from django.po", lines[0])

            self.compile()
            lines.clear()
            self.assertEqual(self.tool.check_compiled(self.root, report=lines.append), 0)
            self.assertEqual(lines, [])

    def test_a_new_message_that_was_not_compiled_is_found(self):
        if shutil.which("msgfmt") is None:
            self.skipTest("the GNU gettext tools are not installed")
        self.catalog('msgid "Save"\nmsgstr "保存"\n')
        self.compile()
        self.catalog('msgid "Save"\nmsgstr "保存"\n\nmsgid "Cancel"\nmsgstr "取消"\n')

        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
            self.assertEqual(self.tool.check_compiled(self.root, report=lambda line: None), 1)

    def test_only_the_header_dates_do_not_count_as_a_difference(self):
        if shutil.which("msgfmt") is None:
            self.skipTest("the GNU gettext tools are not installed")
        self.catalog('msgid "Save"\nmsgstr "保存"\n')
        self.compile()
        # makemessages writes a creation date into the header and moves the locations.
        self.write(
            "fleetops/locale/zh_Hans/LC_MESSAGES/django.po",
            self.HEADER.replace('"Language', '"POT-Creation-Date: 2026-10-14 10:00+0000\\n"\n"Language')
            + '\n#: templates/a.html\nmsgid "Save"\nmsgstr "保存"\n',
        )

        self.assertEqual(self.tool.check_compiled(self.root, report=lambda line: None), 0)

    def test_a_compiled_catalog_without_a_source_is_found(self):
        self.write("fleetops/locale/de/LC_MESSAGES/django.mo", b"\x00", binary=True)
        self.catalog('msgid "Save"\nmsgstr "保存"\n')
        lines = []

        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            if shutil.which("msgfmt") is None:
                self.skipTest("the GNU gettext tools are not installed")
            self.compile()
            self.assertEqual(self.tool.check_compiled(self.root, report=lines.append), 1)

        self.assertEqual(
            lines,
            [
                "::error file=fleetops/locale/de/LC_MESSAGES/django.mo,title=Compiled catalog"
                "::no .po file belongs to this compiled catalog"
            ],
        )

    def test_the_command_fails_on_a_stale_compiled_catalog(self):
        if shutil.which("msgfmt") is None or shutil.which("msgattrib") is None:
            self.skipTest("the GNU gettext tools are not installed")
        self.catalog('msgid "Save"\nmsgstr "保存"\n')
        self.compile()
        self.catalog('msgid "Save"\nmsgstr "存储"\n')

        code, _out, err = run(self.tool, "--no-update", "--root", str(self.root))

        self.assertEqual(code, 1)
        self.assertIn("1 compiled catalog(s) do not match their source", err)

    def test_the_command_needs_the_gettext_tools_and_a_catalog(self):
        self.catalog('msgid "Save"\nmsgstr "保存"\n')
        with mock.patch.object(self.tool.shutil, "which", return_value=None):
            self.assertEqual(run(self.tool, "--no-update", "--root", str(self.root))[0], 2)

        empty = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        (empty / "fleetops").mkdir()
        if shutil.which("msgattrib"):
            self.assertEqual(run(self.tool, "--no-update", "--root", str(empty))[0], 2)
