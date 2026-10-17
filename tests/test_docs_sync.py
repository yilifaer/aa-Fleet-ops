# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The documents match the code: permissions, template variables, defaults, scheduled tasks, links and layout."""

import importlib.util
import re
from decimal import Decimal
from pathlib import Path

from django.apps import apps
from django.test import SimpleTestCase, TestCase

import fleetops
from fleetops import tasks
from fleetops.constants import (
    CLICK_SCOPES,
    DEFAULT_MOTD_TEMPLATE,
    DEFAULT_PING_TEMPLATE,
    DEFAULT_STANDALONE_PING_TEMPLATE,
    FLEET_SCOPES,
)
from fleetops.models import FleetOpsSettings, SrpSettings
from fleetops.services.messages import build_context

from . import factories as f
from . import i18n_utils as po

PACKAGE = Path(fleetops.__file__).resolve().parent
ROOT = PACKAGE.parent
DOCS = ROOT / "docs"
ZH = DOCS / "zh-CN"
CJK = re.compile("[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")
# The documents that exist in Simplified Chinese as well: the administrator documents only.
TRANSLATED = ("configuration.md", "permissions.md", "message-templates.md", "integrations.md")
LEGACY_FILES = (
    "FEATURES.md",
    "TESTING.md",
    "docs/IMPLEMENTATION_STATUS.md",
    "docs/NEW-IN-A4.md",
    "docs/NEW-IN-A5.md",
    "docs/TEST_REPORT_0.1.0a4.md",
    "docs/LOCAL_MACOS_TEST.md",
    "docs/STAGING_TEST_PLAN.md",
    "docs/GITHUB_RELEASE.md",
    "docs/ARCHITECTURE.md",
    "docs/PERMISSIONS.md",
    "docs/SRP_PROVIDER.md",
)
# Tasks that run from the beat; they need an entry in CELERYBEAT_SCHEDULE, which the READMEs show.
BEAT_TASKS = ("schedule_active_fleet_tracking", "prune_attendance_history")
# Tasks that nothing queues any more but that must stay importable: messages of earlier versions may still
# be waiting in the broker.
RETIRED_TASKS = ("kick_members",)


def read(path):
    return Path(path).read_text(encoding="utf-8")


def load_tool(name):
    spec = importlib.util.spec_from_file_location(f"fleetops_tool_{name}", ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def permissions():
    """``{codename: label}`` of every permission of the app."""
    found = {}
    for model in apps.get_app_config("fleetops").get_models():
        for codename, label in model._meta.permissions:
            found[codename] = label
    return found


def documents(*folders):
    """Markdown files of the repository that talk about FleetOps (not the changelog)."""
    files = [ROOT / "README.md", ROOT / "README.zh-CN.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md"]
    for folder in folders or (DOCS, ZH):
        files += sorted(folder.glob("*.md"))
    return [path for path in files if path.is_file()]


class LayoutTests(SimpleTestCase):
    def test_the_legacy_files_are_gone(self):
        for name in LEGACY_FILES:
            folder = ROOT / Path(name).parent
            names = {path.name for path in folder.iterdir()}
            with self.subTest(name=name):
                self.assertNotIn(Path(name).name, names)

    def test_the_documents_exist(self):
        for name in (
            "configuration.md",
            "message-templates.md",
            "permissions.md",
            "integrations.md",
            "translations.md",
            "manual-testing.md",
            "releasing.md",
            "architecture.md",
            "srp-providers.md",
        ):
            with self.subTest(name=name):
                self.assertTrue((DOCS / name).is_file())
        for name in ("README.md", "README.zh-CN.md", "CONTRIBUTING.md", "SECURITY.md", "LICENSE", "CHANGELOG.md"):
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).is_file())

    def test_the_administrator_documents_have_a_chinese_version_and_nothing_else_does(self):
        self.assertEqual(sorted(path.name for path in ZH.glob("*.md")), sorted(TRANSLATED))
        for name in TRANSLATED:
            with self.subTest(name=name):
                self.assertTrue((DOCS / name).is_file())

    def test_the_chinese_documents_are_chinese_and_point_to_the_english_ones(self):
        self.assertIn("英文版为准", read(ROOT / "README.zh-CN.md"))
        for name in TRANSLATED:
            text = read(ZH / name)
            with self.subTest(name=name):
                self.assertTrue(CJK.search(text))
                self.assertIn(f"../{name}", text)

    def test_the_english_documents_have_no_chinese_text(self):
        # The language link of the README and the glossary of the translation guide are the exceptions.
        files = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md"]
        files += [path for path in sorted(DOCS.glob("*.md")) if path.name != "translations.md"]
        for path in files:
            text = read(path).replace("简体中文", "")
            with self.subTest(path=path.name):
                self.assertIsNone(CJK.search(text), f"Chinese text in {path.name}")

    def test_the_readme_works_on_pypi(self):
        text = read(ROOT / "README.md")

        self.assertIn("https://github.com/yilifaer/aa-fleetops/blob/dev/README.zh-CN.md", text)
        relative = [
            target
            for target in re.findall(r"\]\(([^)\s]+)\)", text)
            if not target.startswith(("http://", "https://", "#", "mailto:"))
        ]
        self.assertEqual(relative, [], "PyPI does not resolve relative links")

    def test_every_language_is_in_the_translation_guide(self):
        guide = read(DOCS / "translations.md")
        folders = [path.name for path in (PACKAGE / "locale").iterdir() if path.is_dir()]

        self.assertTrue(folders)
        for folder in folders:
            with self.subTest(folder=folder):
                self.assertIn(f"`{folder}`", guide)

    def test_the_source_distribution_lists_the_documents(self):
        manifest = read(ROOT / "MANIFEST.in")

        for name in ("CHANGELOG.md", "README.zh-CN.md", "SECURITY.md", "CONTRIBUTING.md"):
            self.assertIn(name, manifest)
        self.assertIn("recursive-include docs *.md", manifest)

    def test_the_links_in_the_documents_resolve(self):
        problems = load_tool("check_docs").check(ROOT)

        self.assertEqual(problems, [])


class ChineseLabelTests(SimpleTestCase):
    """The controls and messages that the Chinese documents quote read the same on the Chinese pages."""

    QUOTED = {
        "configuration.md": (
            "不发 Discord 消息（仅复制）",
            "支付高于政策金额",
            "超出月度限额通过",
            "zKillboard 装配价值",
            "ESI 市场估价",
            "舰船类别需要 django-eveonline-sde。",
            "可搭桥质量需要 django-eveonline-sde。",
            "舰队内和舰队外",
            "关于",
        ),
        "permissions.md": ("已保存（已隐藏）", "处理人", "付款人"),
    }

    def test_quoted_labels_are_in_the_catalog_and_in_the_document(self):
        shown = {text for text in po.mo_messages(po.LOCALE_DIR / "zh_Hans" / "LC_MESSAGES" / "django.mo").values()}
        for name, labels in self.QUOTED.items():
            text = read(ZH / name)
            for label in labels:
                with self.subTest(document=name, label=label):
                    self.assertIn(label, shown)
                    self.assertIn(label, text)


class PermissionDocsTests(SimpleTestCase):
    def test_every_permission_is_documented_with_its_label(self):
        text = read(DOCS / "permissions.md")
        for codename, label in permissions().items():
            with self.subTest(codename=codename):
                rows = [line for line in text.splitlines() if line.startswith(f"| `fleetops.{codename}` |")]
                self.assertEqual(len(rows), 1)
                self.assertIn(label, rows[0])

    def test_every_permission_is_in_the_chinese_document(self):
        text = read(ZH / "permissions.md")
        for codename in permissions():
            with self.subTest(codename=codename):
                self.assertRegex(text, rf"`(?:fleetops\.)?{codename}`")

    def test_documents_name_only_permissions_that_exist(self):
        known = set(permissions())
        for path in documents():
            names = set(re.findall(r"`fleetops\.([a-z_]+)`", read(path)))
            # fleetops.tasks and the like are modules, not permissions.
            unknown = {
                name
                for name in names - known
                if not (PACKAGE / f"{name}.py").exists() and not (PACKAGE / name).is_dir()
            }
            with self.subTest(path=path.name):
                self.assertEqual(unknown, set())

    def test_the_readmes_list_the_groups_with_valid_permissions(self):
        for name in ("README.md", "README.zh-CN.md"):
            text = read(ROOT / name)
            with self.subTest(name=name):
                for codename in ("basic_access", "request_srp", "view_corp_stats", "manage_srp", "manage_configuration"):
                    self.assertIn(codename, text)
                    self.assertIn(codename, permissions())


class MessageDocsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = f.create_user(perms=f.FC_PERMS)
        cls.operation = f.create_operation(cls.user)

    def test_every_template_variable_is_documented(self):
        variables = build_context(self.operation)
        self.assertGreater(len(variables), 25)
        for path in (DOCS / "message-templates.md", ZH / "message-templates.md"):
            text = read(path)
            for name in variables:
                with self.subTest(path=path.parent.name, variable=name):
                    self.assertIn(f"`{name}`", text)

    def test_documents_name_only_variables_that_exist(self):
        variables = set(build_context(self.operation))
        for path in (DOCS / "message-templates.md", ZH / "message-templates.md"):
            table = [line for line in read(path).splitlines() if re.match(r"\| `[a-z_]+` \|", line)]
            documented = {re.match(r"\| `([a-z_]+)` \|", line).group(1) for line in table}
            with self.subTest(path=path.parent.name):
                self.assertEqual(documented - variables, set())
                self.assertEqual(variables - documented, set())

    def test_the_built_in_templates_are_shown_exactly(self):
        for path in (DOCS / "message-templates.md", ZH / "message-templates.md"):
            text = read(path)
            with self.subTest(path=path.parent.name):
                self.assertIn(DEFAULT_PING_TEMPLATE, text)
                self.assertIn(DEFAULT_MOTD_TEMPLATE, text)
                self.assertIn(DEFAULT_STANDALONE_PING_TEMPLATE, text)

    def test_the_documents_say_that_pings_and_motds_are_always_english(self):
        for path, phrase in ((DOCS / "message-templates.md", "always English"), (ZH / "message-templates.md", "始终是英文")):
            text = read(path)
            with self.subTest(path=path.parent.name):
                self.assertIn(phrase, text)
                self.assertNotIn("site language", text)
                self.assertNotIn("站点语言", text)
        for path, phrase in (
            (DOCS / "configuration.md", "pings and MOTDs are always English"),
            (ZH / "configuration.md", "Ping 和 MOTD 始终是英文"),
        ):
            with self.subTest(path=path.parent.name, document="configuration"):
                self.assertIn(phrase, read(path))

    def test_the_documents_name_the_links_of_the_default_motd(self):
        for path in (DOCS / "message-templates.md", ZH / "message-templates.md"):
            text = read(path)
            with self.subTest(path=path.parent.name):
                self.assertIn("FC: {{ fc_link }}<br>Form Up: {{ formup_link }}", text)
                self.assertNotIn("FC: {{ fc }}<br>", text)


class ScheduledTaskDocsTests(SimpleTestCase):
    @staticmethod
    def celery_tasks():
        return {name: obj for name, obj in vars(tasks).items() if hasattr(obj, "delay") and hasattr(obj, "name")}

    def test_the_readmes_show_every_beat_entry(self):
        for name in ("README.md", "README.zh-CN.md"):
            text = read(ROOT / name)
            for task in BEAT_TASKS:
                with self.subTest(name=name, task=task):
                    self.assertIn(f'"task": "fleetops.tasks.{task}"', text)

    def test_the_beat_tasks_exist(self):
        for task in BEAT_TASKS:
            with self.subTest(task=task):
                self.assertIn(task, self.celery_tasks())
                self.assertEqual(self.celery_tasks()[task].name, f"fleetops.tasks.{task}")

    def test_documents_name_only_tasks_that_exist(self):
        for path in documents():
            names = set(re.findall(r"fleetops\.tasks\.([A-Za-z_]+)", read(path)))
            with self.subTest(path=path.name):
                self.assertEqual({name for name in names if name not in self.celery_tasks()}, set())

    def test_a_task_that_nothing_queues_is_a_beat_task_the_readmes_show(self):
        sources = "\n".join(read(path) for path in PACKAGE.rglob("*.py") if "migrations" not in path.parts)
        for name in self.celery_tasks():
            queued = re.search(rf"\b{name}\.(?:delay|apply_async|s|si|signature|retry)\(", sources)
            with self.subTest(task=name):
                if not queued:
                    self.assertIn(name, BEAT_TASKS + RETIRED_TASKS, f"{name} is never queued: add a beat entry?")


class InstallDocsTests(SimpleTestCase):
    def test_the_readmes_name_the_scopes(self):
        for name in ("README.md", "README.zh-CN.md"):
            text = read(ROOT / name)
            for scope in FLEET_SCOPES + CLICK_SCOPES:
                with self.subTest(name=name, scope=scope):
                    self.assertIn(scope, text)

    def test_the_readmes_name_the_optional_extras(self):
        extras = read(ROOT / "pyproject.toml")

        self.assertIn("[project.optional-dependencies]", extras)
        for name in ("README.md", "README.zh-CN.md"):
            text = read(ROOT / name)
            for extra in ("fittings", "sde"):
                with self.subTest(name=name, extra=extra):
                    self.assertRegex(extras, rf"(?m)^{extra} = \[")
                    self.assertIn(f"aa-fleetops[{extra}]", text)

    def test_the_readme_and_the_package_agree_on_the_license(self):
        text = " ".join(read(ROOT / "README.md").split())

        self.assertIn("GNU General Public License version 3", text)
        self.assertIn("yilifaer and AA FleetOps contributors", text)
        self.assertEqual(fleetops.__url__, "https://github.com/yilifaer/aa-fleetops")


class DefaultsInDocsTests(SimpleTestCase):
    """The defaults in the configuration tables are the defaults of the models."""

    SETTINGS = {
        "Tracking interval": "tracking_interval",
        "Stale threshold": "stale_threshold",
        "Auto end enabled": "auto_end_enabled",
        "Auto end missing count": "auto_end_missing_count",
        "Incentive enabled": "incentive_enabled",
        "Incentive minimum fleets": "incentive_minimum_fleets",
        "Data retention days": "data_retention_days",
        "SRP auto create": "srp_auto_create",
        "PAP minimum time": "pap_minimum_minutes",
        "Minimum fleet duration": "minimum_fleet_minutes",
        "Leaderboard places": "leaderboard_size",
        "PAP link validity": "pap_link_minutes",
        "Live fleet refresh": "fleet_live_refresh_seconds",
        "MOTD warning length": "motd_warning_length",
    }
    SRP = {
        "Enabled": "enabled",
        "Out-of-fleet losses": "out_of_fleet_enabled",
        "Claim deadline (days)": "claim_deadline_days",
        "Use zKillboard": "zkillboard_enabled",
        "Payout percentage": "percentage",
        "Capsules need a payout rule": "capsules_need_rule",
        "Notify members": "notify_members",
        "SRP managers may decide on their own claims": "allow_self_review",
    }

    @staticmethod
    def shown(model, field, words):
        default = model._meta.get_field(field).default
        if isinstance(default, bool):
            return words[default]
        if isinstance(default, Decimal):
            return str(int(default)) if default == default.to_integral() else str(default)
        return str(default)

    def row(self, text, label):
        """The cells of the table row whose first cell is ``label`` or holds it in brackets."""
        for line in text.splitlines():
            if line.startswith("|"):
                cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                first = cells[0]
                if first == label or first.endswith(f"（{label}）") or first.endswith(f"({label})"):
                    return cells
        return None

    def check(self, path, words):
        text = read(path)
        for model, labels in ((FleetOpsSettings, self.SETTINGS), (SrpSettings, self.SRP)):
            for label, field in labels.items():
                cells = self.row(text, label)
                with self.subTest(path=path.parent.name, setting=label):
                    self.assertIsNotNone(cells, f"{label} is not in a table of {path.name}")
                    self.assertEqual(cells[1], self.shown(model, field, words))

    def test_english_defaults(self):
        self.check(DOCS / "configuration.md", {True: "on", False: "off"})

    def test_chinese_defaults(self):
        self.check(ZH / "configuration.md", {True: "开", False: "关"})
