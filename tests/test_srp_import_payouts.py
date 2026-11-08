# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""CAT-11: the payout table CSV import command."""

import csv
import os
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase

from fleetops.forms_config import ShipLookup
from fleetops.models import AuditLog, SrpPayoutRule

from . import factories as f

COLUMN = "Loose stack"


class PayoutImportTests(TestCase):
    def setUp(self):
        self.category = f.srp_category("Loose stack", key="loose-stack")
        patcher = mock.patch(
            "fleetops.management.commands.fleetops_srp_import_payouts.lookup_ship",
            side_effect=lambda text: ShipLookup(int(text), f"Looked up {text}"),
        )
        self.lookup = patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, rows, header=("type_id", "name", "Strategic", COLUMN), bom=True):
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8-sig" if bom else "utf-8", newline="")
        self.addCleanup(os.unlink, handle.name)
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
        handle.close()
        return handle.name

    def run_command(self, path, *args, expect_error=False):
        out, err = StringIO(), StringIO()
        arguments = [path, "--category", "loose-stack", "--column", COLUMN, *args]
        if expect_error:
            with self.assertRaises(CommandError) as caught:
                call_command("fleetops_srp_import_payouts", *arguments, stdout=out, stderr=err)
            return str(caught.exception)
        call_command("fleetops_srp_import_payouts", *arguments, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def rows(self, count):
        return [(str(1000 + index), f"Ship {index}", "", str(1_000_000 * (index + 1))) for index in range(count)]

    def test_a_dry_run_counts_and_writes_nothing(self):
        path = self.write(self.rows(5) + [("2000", "Free", "", ""), ("2001", "Zero", "", "0")])

        out, err = self.run_command(path)

        self.assertIn("5 rules would be created, 0 would be updated, 0 unchanged, 2 without an amount, 0 rows with a problem.", out)
        self.assertIn("Dry run", out)
        self.assertEqual((SrpPayoutRule.objects.count(), AuditLog.objects.count()), (0, 0))
        # Changed for the dry run lookup: every row here has a name in the file, so nobody is asked (a dry run used to
        # never look anything up; see the two tests below for rows without a name).
        self.lookup.assert_not_called()

    def test_the_help_does_not_promise_a_ship_check_the_lookup_does_not_make(self):
        from fleetops.management.commands.fleetops_srp_import_payouts import Command

        self.assertNotIn("no ship", Command.help)
        self.assertIn("no EVE item type", Command.help)

    def test_a_dry_run_looks_up_ships_without_a_name_and_reports_what_is_no_ship(self):
        from django.core.exceptions import ValidationError

        def lookup(text):
            if text == "1001":
                raise ValidationError("Not a ship.")
            if text == "1002":
                return ShipLookup(1002, "", "The name could not be looked up.")
            return ShipLookup(int(text), f"Looked up {text}")

        self.lookup.side_effect = lookup
        path = self.write([("1000", "", "", "5000000"), ("1001", "", "", "5000000"), ("1002", "", "", "5000000")])

        out, err = self.run_command(path)

        self.assertEqual(self.lookup.call_count, 3)
        self.assertIn("2 rules would be created", out)
        self.assertIn("1 rows with a problem", out)
        self.assertIn("Line 3: skipped (Not a ship.)", err)
        self.assertIn("Ship names that could not be looked up (1;", err)
        self.assertIn("1002", err)
        self.assertIn("would be made without a name", err)
        self.assertEqual((SrpPayoutRule.objects.count(), AuditLog.objects.count()), (0, 0))

        # The writing run finds the same problems.
        _out, err = self.run_command(path, "--apply")
        self.assertIn("Line 3: skipped (Not a ship.)", err)
        self.assertIn("were made without a name", err)
        self.assertEqual(sorted(SrpPayoutRule.objects.values_list("ship_type_id", flat=True)), [1000, 1002])

    def test_apply_creates_the_rules_and_one_audit_entry_each(self):
        path = self.write(self.rows(4))

        out, _err = self.run_command(path, "--apply")

        self.assertIn("4 rules created", out)
        rule = SrpPayoutRule.objects.get(ship_type_id=1002)
        self.assertEqual(
            (rule.ship_type_name, rule.amount, rule.scope, rule.category, rule.category_key, rule.doctrine_key, rule.is_active),
            ("Ship 2", 3_000_000, "any", self.category, self.category.pk, "", True),
        )
        entries = AuditLog.objects.filter(action="configuration.create")
        self.assertEqual(entries.count(), 4)
        entry = entries.first()
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.reason, f"Imported from {Path(path).name}")
        self.assertNotIn(os.path.dirname(path), entry.reason)
        self.assertIsNone(entry.old_value)

    def test_a_second_run_changes_nothing(self):
        path = self.write(self.rows(3))
        self.run_command(path, "--apply")
        audits = AuditLog.objects.count()

        out, _err = self.run_command(path, "--apply")

        self.assertIn("0 rules created, 0 updated, 3 unchanged", out)
        self.assertEqual(AuditLog.objects.count(), audits)

    def test_a_changed_amount_updates_the_rule_and_audits_old_and_new(self):
        self.run_command(self.write(self.rows(1)), "--apply")
        path = self.write([("1000", "Ship 0", "", "9000000")])

        out, _err = self.run_command(path, "--apply")

        self.assertIn("1 updated", out)
        self.assertEqual(SrpPayoutRule.objects.get().amount, 9_000_000)
        entry = AuditLog.objects.get(action="configuration.update")
        self.assertEqual((entry.old_value["amount"], entry.new_value["amount"]), (1_000_000, 9_000_000))

    def test_rules_of_other_categories_scopes_and_general_rules_stay(self):
        other = f.srp_category("Other", key="other")
        general = f.payout_rule(1000, 5)
        elsewhere = f.payout_rule(1000, 6, category=other)

        self.run_command(self.write(self.rows(1)), "--apply", "--scope", "in_fleet")

        self.assertEqual(SrpPayoutRule.objects.count(), 3)
        self.assertEqual(SrpPayoutRule.objects.get(pk=general.pk).amount, 5)
        self.assertEqual(SrpPayoutRule.objects.get(pk=elsewhere.pk).amount, 6)
        self.assertEqual(SrpPayoutRule.objects.get(category=self.category).scope, "in_fleet")

    def test_bad_rows_are_reported_with_their_line_and_skipped(self):
        path = self.write(
            [
                ("1000", "A", "", "5000000"),
                ("abc", "B", "", "5000000"),
                ("1002", "C", "", "lots"),
                ("1003", "D", "", "12.5"),
                ("1004", "E", "", "99999999999999999"),
                ("1000", "A again", "", "6000000"),
                ("1006", "F", "", "7000000"),
            ]
        )

        out, err = self.run_command(path, "--apply")

        self.assertIn("2 rules created", out)
        self.assertIn("5 rows with a problem", out)
        for line in (3, 4, 5, 6, 7):
            self.assertIn(f"Line {line}: skipped", err)
        self.assertEqual(sorted(SrpPayoutRule.objects.values_list("ship_type_id", flat=True)), [1000, 1006])
        self.assertEqual(SrpPayoutRule.objects.get(ship_type_id=1000).amount, 5_000_000)

    def test_without_a_name_column_the_ship_is_looked_up(self):
        path = self.write([("1000", "5000000")], header=("type_id", COLUMN))

        self.run_command(path, "--apply")

        self.assertEqual(SrpPayoutRule.objects.get().ship_type_name, "Looked up 1000")
        self.lookup.assert_called_once_with("1000")

    def test_another_name_column_can_be_named(self):
        path = self.write([("1000", "Hull", "5000000")], header=("type_id", "ship", COLUMN))

        self.run_command(path, "--apply", "--name-column", "ship")

        self.assertEqual(SrpPayoutRule.objects.get().ship_type_name, "Hull")

    def test_a_lookup_that_fails_is_a_bad_row(self):
        from django.core.exceptions import ValidationError

        self.lookup.side_effect = ValidationError("Not a ship.")
        path = self.write([("1000", "", "", "5000000")])

        out, err = self.run_command(path, "--apply")

        self.assertIn("Line 2: skipped (Not a ship.)", err)
        self.assertEqual(SrpPayoutRule.objects.count(), 0)

    def test_errors_in_the_options_and_the_file(self):
        path = self.write(self.rows(1))
        self.assertIn("no category", self.run_command(path, "--category", "nope", expect_error=True))
        self.assertIn("no column called", self.run_command(path, "--column", "missing", expect_error=True))
        self.assertIn("no type_id column", self.run_command(self.write([("a",)], header=("x",)), expect_error=True))
        self.assertIn("Cannot read missing.csv", self.run_command("/nonexistent/missing.csv", expect_error=True))
        self.assertEqual(SrpPayoutRule.objects.count(), 0)

    def test_the_payout_table_of_the_observed_alliance_gives_222_rules(self):
        """CAT-11 acceptance with the shape of the observed table: 252 ships, 222 with an amount in the column."""
        rows = [(str(5000 + index), f"Hull {index}", "", "" if index < 30 else "10000000") for index in range(252)]
        path = self.write(rows)

        out, _err = self.run_command(path)
        self.assertIn("222 rules would be created", out)
        self.run_command(path, "--apply")
        self.assertEqual(AuditLog.objects.filter(action="configuration.create").count(), 222)
        out, _err = self.run_command(path, "--apply")
        self.assertIn("0 rules created, 0 updated", out)
