# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""STAT-3 acceptance: an import shaped like the aa-srp history observed in production (1664 requests since 2026-09-01,
seven groups of links) gives the category table of the observation, apart from the pending requests the import skips.

The counts per group are the observed ones. The amounts are invented but follow the observed pattern: most requests are
paid in full, a few are cut at 250m by the administrators, a few are cut to another amount. No names of real
people, corporations or alliances are used.
"""

import json
import tempfile
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from io import StringIO

from allianceauth.eveonline.models import EveCharacter
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from fleetops.models import SrpClaim, SrpFleetLink
from fleetops.srp import importer, summaries

from . import factories as f
from .aasrp_fakes import FakeAaSrp

START = datetime(2026, 9, 1, tzinfo=dt_timezone.utc)
END = datetime(2026, 10, 1, tzinfo=dt_timezone.utc)

# name -> (category key or None for the plain fleet links, link names, requests, approved, rejected, pending, members,
#          approved paid in full, approved cut at 250m, approved cut to another amount)
GROUPS = {
    "Solo PVP": ("solo-pvp", ["HD/SOLO/PVP 250M", "Solo PVP Fleet", "Solo PVP Fleet"], 1220, 1204, 15, 1, 141, 1062, 134, 8),
    "Fleet links": (None, ["Fleet of FC one", "Fleet of FC two", "Fleet of FC three"], 148, 140, 2, 6, 90, 139, 0, 1),
    "Alliance fleets": ("alliance-fleets", ["Alliance Fleets", "Alliance Fleets"], 22, 21, 0, 1, 11, 20, 0, 1),
    "Home defense": ("home-defense", ["Home defense Fleet", "Home defense Fleet"], 58, 54, 0, 4, 29, 48, 6, 0),
    "Home defense ESS": ("home-defense-ess", ["Home defense ESS Fleets", "Home defense ESS Fleets"], 82, 82, 0, 0, 34, 67, 15, 0),
    "Corp roaming": ("corp-roaming", ["Corp Roaming Fleets", "Corp Roaming Fleets"], 108, 103, 1, 4, 32, 98, 5, 0),
    "Corp roaming ESS": ("corp-roaming-ess", ["Corp Roaming ESS Fleets", "Corp Roaming ESS Fleets"], 26, 23, 2, 1, 11, 22, 1, 0),
}
FULL, CUT_LOSS, CUT_TO, OTHER_LOSS, OTHER_TO = 100_000_000, 400_000_000, 250_000_000, 300_000_000, 200_000_000


class ProductionShapeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.srp = FakeAaSrp("5.x")
        km = 100_000_000
        pk = 9_000_000
        rows = []
        for name, (key, links, total, approved, rejected, pending, members, full, cut, other) in GROUPS.items():
            assert full + cut + other == approved and approved + rejected + pending == total
            users = User.objects.bulk_create([User(username=f"{key or 'fleet'}-member-{i}") for i in range(members)])
            characters = EveCharacter.objects.bulk_create(
                [
                    EveCharacter(character_id=pk + i, character_name=f"Test pilot {pk + i}", corporation_id=2_000_001,
                                 corporation_name="Test Corp", corporation_ticker="TEST")
                    for i in range(members)
                ]
            )
            pk += members
            link_rows = [cls.srp.add_link(link_name, fleet_time=START + timedelta(days=2 + number)) for number, link_name in enumerate(links)]
            plan = ["Approved"] * full + ["Approved-cut"] * cut + ["Approved-other"] * other + ["Rejected"] * rejected + ["Pending"] * pending
            for number, kind in enumerate(plan):
                km += 1
                # Imported requests go round the members; the pending ones are made by members who also have others.
                member = number % members
                link = link_rows[number % len(link_rows)]
                if kind == "Approved":
                    loss, payout, status = FULL, FULL, "Approved"
                elif kind == "Approved-cut":
                    loss, payout, status = CUT_LOSS, CUT_TO, "Approved"
                elif kind == "Approved-other":
                    loss, payout, status = OTHER_LOSS, OTHER_TO, "Approved"
                elif kind == "Rejected":
                    loss, payout, status = 500_000_000, 0, "Rejected"
                else:
                    loss, payout, status = FULL, 0, "Pending"
                cls.srp.add_request(
                    link, characters[member], status=status, loss=loss, payout=payout, killmail_id=km,
                    creator=users[member], decided_by=users[0], reject_info="Not covered" if status == "Rejected" else "",
                )

    def setUp(self):
        self.srp.install(self)
        for key, name in (
            ("solo-pvp", "Solo PVP"), ("alliance-fleets", "Alliance fleets"), ("home-defense", "Home defense"), ("home-defense-ess", "Home defense ESS"),
            ("corp-roaming", "Corp roaming"), ("corp-roaming-ess", "Corp roaming ESS"),
        ):
            f.srp_category(name, key=key)
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        self.addCleanup(__import__("os").unlink, handle.name)
        json.dump(
            [
                {"pattern": r"(?i)solo", "category": "solo-pvp"},
                {"pattern": r"^Alliance Fleets", "category": "alliance-fleets"},
                {"pattern": r"^Home defense ESS", "category": "home-defense-ess"},
                {"pattern": r"^Home defense", "category": "home-defense"},
                {"pattern": r"^Corp Roaming ESS", "category": "corp-roaming-ess"},
                {"pattern": r"^Corp Roaming", "category": "corp-roaming"},
            ],
            handle,
        )
        handle.close()
        self.map_path = handle.name

    def test_the_category_table_matches_the_observation(self):
        out = StringIO()
        call_command("fleetops_import_aasrp", "--dry-run", "--category-map", self.map_path, stdout=out, stderr=StringIO())
        dry = out.getvalue()
        self.assertIn("requests read: 1664", dry)
        self.assertIn("pending in aa-srp: 17", dry)
        self.assertEqual(SrpClaim.objects.count(), 0)

        report = importer.run_import(importer.Options(category_map=importer.load_category_map(self.map_path)))

        self.assertEqual((report.pending, report.create), (17, 1647))
        rows = {row.category_key or "": row for row in summaries.by_category(START, END)}
        expected = {
            "solo-pvp": GROUPS["Solo PVP"],
            "": GROUPS["Fleet links"],
            "alliance-fleets": GROUPS["Alliance fleets"],
            "home-defense": GROUPS["Home defense"],
            "home-defense-ess": GROUPS["Home defense ESS"],
            "corp-roaming": GROUPS["Corp roaming"],
            "corp-roaming-ess": GROUPS["Corp roaming ESS"],
        }
        # The plain fleet links are in the built-in "fleet" category.
        rows[""] = rows.pop("fleet")
        total_claims = total_approved = 0
        for key, (_k, _links, requests, approved, rejected, pending, members, full, cut, other) in expected.items():
            row = rows[key]
            with self.subTest(category=key or "fleet"):
                imported = requests - pending
                self.assertEqual(row.totals.claims, imported)
                self.assertEqual((row.totals.approved_count, row.totals.paid_count), (approved, approved))
                self.assertEqual(row.totals.rejected, rejected)
                self.assertEqual(row.members, members)
                self.assertEqual(row.totals.approved, full * FULL + cut * CUT_TO + other * OTHER_TO)
                # The administrators' cuts: at 250m and to another amount.
                self.assertEqual(row.capped, cut + other)
            total_claims += row.totals.claims
            total_approved += row.totals.approved
        # The categories add up to the total.
        totals = summaries.period_totals(START, END)
        self.assertEqual((totals.claims, totals.approved), (total_claims, total_approved))
        self.assertEqual(totals.claims, 1647)
        # Every approved request is paid; 20 of the imported ones were rejected.
        self.assertEqual((totals.paid_count, totals.rejected), (1647 - 20, 20))

    def test_every_claim_of_a_standing_category_is_out_of_fleet_and_the_others_in_fleet(self):
        importer.run_import(importer.Options(category_map=importer.load_category_map(self.map_path)))

        self.assertEqual(SrpClaim.objects.filter(category__key="fleet", scope="out_of_fleet").count(), 0)
        self.assertEqual(SrpClaim.objects.exclude(category__key="fleet").filter(scope="in_fleet").count(), 0)
        self.assertEqual(SrpFleetLink.objects.filter(kind="standing", origin="aasrp").count(), 2 + 3 + 2 + 2 + 2 + 2)
        self.assertEqual(SrpFleetLink.objects.filter(kind="fleet", origin="aasrp").count(), 3)
