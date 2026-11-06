# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from django.core.management.base import BaseCommand, CommandError

from fleetops.services.history import prune_history, refusal_message


class Command(BaseCommand):
    help = "Prune FleetOps history according to retention and current-alliance membership settings."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show how many rows would be removed without deleting anything.",
        )

        parser.add_argument(
            "--force",
            action="store_true",
            help="Prune the PAP of departed members although the safety check refuses it (see the pruning share on the FleetOps settings page).",
        )

    def handle(self, *args, **options):
        result = prune_history(dry_run=options["dry_run"], force=options["force"])
        refused = bool(result.get("refused"))
        prefix = "Would remove" if options["dry_run"] else "Removed"
        self.stdout.write(
            (self.style.WARNING if refused else self.style.SUCCESS)(
                f"{prefix}: {result['old_attendance']} old PAP records, "
                f"{result['left_alliance']} departed-member PAP records, "
                f"{result['old_events']} old member events, "
                f"{result['old_pings']} old pings without a fleet."
            )
        )
        if refused:
            # The retention part has run; the exit status tells a cron job that the departed members were not pruned.
            if options["dry_run"]:
                self.stderr.write(refusal_message(result))
            else:
                raise CommandError(refusal_message(result))
