# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Migration 0018 only records translatable labels: it runs no SQL and the models match it."""

import importlib
from io import StringIO

from django.core.management import call_command
from django.db import migrations
from django.test import TestCase, TransactionTestCase

MIGRATION = "0018_translatable_labels"
# What a migration prints around its statements; nothing else may be in it.
HARMLESS = {"BEGIN;", "COMMIT;"}


class NoSqlTests(TransactionTestCase):
    """The schema editor of SQLite cannot work inside the transaction of a TestCase."""

    # The test flushes the database when it is done; the tests after it expect the data of the migrations.
    serialized_rollback = True

    def test_it_runs_no_sql(self):
        out = StringIO()
        call_command("sqlmigrate", "fleetops", MIGRATION, stdout=out)
        lines = (line.strip() for line in out.getvalue().splitlines())
        statements = [line for line in lines if line and not line.startswith("--") and line not in HARMLESS]
        self.assertEqual(statements, [])


class TranslatableLabelsMigrationTests(TestCase):
    def test_it_only_changes_labels(self):
        module = importlib.import_module(f"fleetops.migrations.{MIGRATION}")
        operations = module.Migration.operations
        self.assertTrue(operations)
        for operation in operations:
            self.assertIsInstance(operation, (migrations.AlterField, migrations.AlterModelOptions), operation)

    def test_it_follows_the_last_migration_of_the_previous_phase(self):
        module = importlib.import_module(f"fleetops.migrations.{MIGRATION}")
        self.assertIn(("fleetops", "0017_srp_data"), module.Migration.dependencies)

    def test_the_models_need_no_further_migration(self):
        call_command("makemigrations", "fleetops", check=True, dry_run=True, stdout=StringIO())
