# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Release acceptance of the SRP rework, chapter 9.5 of the requirements: nothing secret in the logs, the audit log, the
pages, the files and the command output of the new SRP functions, and which pages a member and a staff user may open.

A member with refreshable tokens (their text is distinctive), a webhook channel, a claim, a standing link, a ban, a
payment batch and an import are put through the scan, the pages, the exports and the commands; every output is then
searched for the token texts, for ``Bearer``, for the webhook address and for the names and values of ESI response
headers. The failing reads carry those texts in their message and on the error, so a log line that wrote the message,
the arguments or the response would be caught. Ownership of single claims and batches is tested in
``test_srp_loss_auth`` and ``test_srp_payments_batches``.
"""

import io
import logging
import zipfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.constants import KILLMAIL_READ_SCOPE
from fleetops.models import AuditLog, DiscordWebhook, SrpBan, SrpClaim, SrpClaimEvent, SrpLoss
from fleetops.providers.esi import FleetESIError
from fleetops.srp import discovery

from . import factories as f
from .aasrp_fakes import FakeAaSrp
from .test_srp_discovery import FakeEsi

WEBHOOK_TOKEN = "Zq9-webhook-token-0451"
WEBHOOK_URL = f"https://discord.com/api/webhooks/123/{WEBHOOK_TOKEN}"
ESI_HEADERS = {
    "X-Esi-Error-Limit-Remain": "17",
    "X-Esi-Error-Limit-Reset": "43",
    "X-Ratelimit-Remaining": "1999",
    "ETag": "W/sweep-etag-0451",
    "Authorization": "Bearer sweep-header-secret",
}
FORBIDDEN = (
    "Bearer",
    "Authorization",
    "access_token",
    "refresh_token",
    "discord.com/api/webhooks",
    WEBHOOK_TOKEN,
    "X-Esi-Error-Limit",
    "X-Ratelimit",
    "sweep-etag-0451",
    "sweep-header-secret",
)


def esi_failure(token, code="ESI_FAILED", status=500):
    """An error whose message, arguments and response all carry secrets."""
    exc = FleetESIError(code, f"failed access_token={token.access_token} refresh_token={token.refresh_token}", status)
    exc.headers = dict(ESI_HEADERS)
    exc.response = mock.Mock(headers=dict(ESI_HEADERS), status_code=status, text=f"Bearer {token.access_token}")
    return exc


class SecretSweepTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.webhook = DiscordWebhook.objects.create(name="Ops Pings", webhook_url=WEBHOOK_URL)
        cls.member = f.create_user("sweep-member", perms=f.SRP_MEMBER_PERMS)
        cls.main = f.main_of(cls.member)
        cls.alt = f.add_alt(cls.member, "Sweep Alt")
        cls.tokens = [f.add_token(cls.member, c, scopes=[KILLMAIL_READ_SCOPE]) for c in (cls.main, cls.alt)]
        cls.staff = f.create_user(
            "sweep-staff", perms=f.SRP_MANAGER_PERMS + ["fleetops.manage_configuration", "fleetops.view_audit_log"]
        )
        cls.tokens.append(f.add_token(cls.staff, f.main_of(cls.staff), scopes=[KILLMAIL_READ_SCOPE]))
        cls.settings = f.srp_settings(enabled=True, loss_discovery_enabled=True)
        cls.category = f.srp_category("Roaming", key="roaming")
        cls.standing = f.standing_link(category=cls.category)
        cls.claim = f.srp_claim(
            cls.member, link=cls.standing, category=cls.category, amount=50_000_000, status=SrpClaim.Status.APPROVED
        )
        SrpBan.objects.create(user=f.create_user("sweep-banned", perms=f.SRP_MEMBER_PERMS), reason="Test")

    def secrets(self):
        values = []
        for token in self.tokens:
            values += [token.access_token, token.refresh_token]
        return values + list(FORBIDDEN)

    def assertClean(self, text, where):
        for word in self.secrets():
            self.assertNotIn(word, text, f"{word!r} found in {where}")

    def test_the_scan_the_pages_the_files_and_the_commands_leak_nothing(self):
        now = timezone.now()
        esi = FakeEsi(now)
        esi.listed(self.main, esi.killmail(self.main, minutes_ago=30))
        output = []
        codes = {}
        with (
            self.assertLogs(level=logging.DEBUG) as logs,
            mock.patch.object(discovery, "get_recent_killmails", side_effect=esi.list_page),
            mock.patch.object(discovery, "get_killmail", side_effect=esi.detail),
            # L-5: the scan asks for the names of all losses of a character at once.
            mock.patch.object(
                discovery.killmails,
                "names_of",
                side_effect=lambda ships, systems, budget=None: (dict.fromkeys(ships, "Rifter"), dict.fromkeys(systems, "Amamake")),
            ),
        ):
            logging.getLogger("fleetops").debug("sweep start")
            discovery.run_scan(now)
            # Reads that fail with a message, headers and a response that carry secrets must not reach the log.
            esi.list_error = esi_failure(self.tokens[0])
            discovery.mark_due([self.main.character_id, self.alt.character_id], now)
            discovery.run_scan(now + timedelta(hours=3))
            esi.list_error = esi_failure(self.tokens[1], "LOSS_DENIED", 403)
            discovery.mark_due([self.main.character_id, self.alt.character_id], now)
            discovery.run_scan(now + timedelta(hours=6))
            esi.list_error = RuntimeError(f"boom {self.tokens[0].refresh_token} {ESI_HEADERS['ETag']}")
            discovery.mark_due([self.main.character_id, self.alt.character_id], now)
            discovery.run_scan(now + timedelta(hours=9))

            for user in (self.member, self.staff):
                self.client.force_login(user)
                urls = [
                    reverse("fleetops:srp_home"),
                    reverse("fleetops:srp_claim", args=[self.claim.uuid]),
                    reverse("fleetops:srp_summary"),
                    reverse("fleetops:srp_reports"),
                    reverse("fleetops:srp_review"),
                    reverse("fleetops:srp_payments"),
                    reverse("fleetops:srp_links"),
                    reverse("fleetops:srp_bans"),
                    reverse("fleetops:audit_log"),
                    reverse("fleetops:configuration_list", args=["srp-categories"]),
                    reverse("fleetops:configuration_list", args=["srp"]),
                ]
                for url in urls:
                    response = self.client.get(url)
                    codes[(user.username, url)] = response.status_code
                    if response.status_code == 200:
                        output.append((url, response.content.decode()))
                for dataset in ("srp-claims", "srp-categories", "srp-payable", "srp-limits", "srp-trend"):
                    for form in ("csv", "xlsx"):
                        response = self.client.get(
                            reverse("fleetops:export", args=[dataset]), {"format": form, "preset": "last_12"}
                        )
                        codes[(user.username, dataset, form)] = response.status_code
                        if response.status_code == 200:
                            output.append((f"{dataset}.{form}", self._file_text(response, form)))

            output.append(("rollback_check", self._run("fleetops_srp_rollback_check")))
            output.append(("backfill dry-run", self._run("fleetops_srp_backfill_killmails", "--dry-run")))
            srp = FakeAaSrp("5.x")
            srp.install(self)
            srp.add_request(srp.add_link("Roaming fleet"), self.main, creator=self.member, decided_by=self.staff)
            output.append(("import dry run", self._run("fleetops_import_aasrp", "--dry-run")))
            output.append(("import", self._run("fleetops_import_aasrp")))
            output.append(("undo", self._run("fleetops_import_aasrp", "--undo")))

        self.assertGreater(len(output), 25, codes)
        # The member opens their own pages only; every staff page and every file is refused. The staff open all.
        member_pages = {reverse("fleetops:srp_home"), reverse("fleetops:srp_claim", args=[self.claim.uuid])}
        for key, code in codes.items():
            username, what = key[0], key[1:]
            expected = 200 if username == "sweep-staff" or what[0] in member_pages else 403
            self.assertEqual(code, expected, key)
        for where, text in output:
            self.assertClean(text, where)
        self.assertClean("\n".join(logs.output), "the logs")
        self.assertClean(
            str(list(AuditLog.objects.values("action", "object_type", "object_id", "old_value", "new_value", "reason"))),
            "the audit log",
        )
        self.assertClean(
            str(list(SrpClaimEvent.objects.values("action", "data", "reason"))), "the claim events"
        )
        # The sweep did reach the pages and the scan: a page was read and losses were stored.
        self.assertTrue(any(where.endswith("/srp/") for where, _ in output))
        self.assertTrue(SrpLoss.objects.exists())

    @staticmethod
    def _file_text(response, form):
        """The text of a download; an Excel file is unpacked first (its parts are compressed)."""
        data = b"".join(response.streaming_content) if response.streaming else response.content
        if form != "xlsx":
            return data.decode("utf-8", "replace")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return "\n".join(archive.read(name).decode("utf-8", "replace") for name in archive.namelist())

    def _run(self, name, *args):
        out = StringIO()
        try:
            call_command(name, *args, stdout=out, stderr=out)
        except Exception as exc:  # a command that stops with an error is judged by its message too
            out.write(str(exc))
        return out.getvalue()


class ReleaseTextTests(TestCase):
    """The release texts say what the code and the CI do (checklist: README and documents match the code)."""

    ROOT = Path(__file__).resolve().parent.parent

    def text(self, name):
        path = self.ROOT / name
        if not path.exists():  # a test run from an installed package has no repository files
            self.skipTest(f"{name} is not available")
        return path.read_text(encoding="utf-8")

    def test_the_upgrade_notes_name_collectstatic_and_the_old_behaviour_list_is_complete(self):
        notes = self.text("CHANGELOG.md").split("### Upgrade notes", 1)[1].split("### Rollback notes", 1)[0]
        self.assertIn("collectstatic --noinput", notes)
        rollback = self.text("CHANGELOG.md").split("**To behave like 0.2.x without rolling back:**", 1)[1][:900]
        self.assertIn("Show the insurance estimate", rollback)
        self.assertIn("Value source", rollback)
        self.assertNotIn("give `manage_srp`", rollback)

    def test_postgresql_is_a_test_database_and_a_ci_job(self):
        self.assertIn("django.db.backends.postgresql", self.text("testauth/settings.py"))
        ci = self.text(".github/workflows/ci.yml")
        self.assertIn("image: postgres:", ci)
        self.assertIn("tests.test_migration_srp", ci)

    def test_the_readmes_and_the_documents_list_what_the_release_built(self):
        for name in ("README.md", "README.zh-CN.md"):
            self.assertIn("fleetops_import_aasrp", self.text(name), name)
        for name, row in (
            ("docs/configuration.md", "Name template of links, Close links after (hours) | on for the six categories above; `{category} {date}`"),
            ("docs/zh-CN/configuration.md", "链接名称模板、链接关闭时限（小时） | 上面六个类别都开；`{category} {date}`"),
        ):
            self.assertIn(row, self.text(name), name)
        self.assertNotIn("built in phases", self.text("docs/releasing.md"))
