# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from django.core.management.base import BaseCommand, CommandError

from fleetops.srp import rollback

CHECKS = (
    ("Payout rules without a category", rollback.RULES),
    ("SRP links that can be removed without losing claims or imported data", rollback.LINKS),
    ("No claims imported from aa-srp", rollback.CLAIMS),
)


class Command(BaseCommand):
    help = (
        "Check whether 'migrate fleetops 0023' (going back to FleetOps 0.2.x) would be stopped by the rollback guard. "
        "Reads only and changes nothing. Exits with an error when something is in the way."
    )

    def handle(self, *args, **options):
        found = {problem.code: problem for problem in rollback.check_all()}
        for label, code in CHECKS:
            if code in found:
                self.stdout.write(self.style.ERROR(f"FAILED  {label}"))
                self.stdout.write(f"        {rollback.describe(found[code])}")
            else:
                self.stdout.write(self.style.SUCCESS(f"OK      {label}"))
        if found:
            raise CommandError("Going back is not safe yet. Fix the points above and run this check again.")
        self.stdout.write(self.style.SUCCESS("All checks passed: 'migrate fleetops 0023' is not stopped by the guard."))
