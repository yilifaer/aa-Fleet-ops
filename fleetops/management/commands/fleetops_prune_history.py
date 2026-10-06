from django.core.management.base import BaseCommand

from fleetops.services.history import prune_history


class Command(BaseCommand):
    help = "Prune FleetOps history according to retention and current-alliance membership settings."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show how many rows would be removed without deleting anything.",
        )

    def handle(self, *args, **options):
        result = prune_history(dry_run=options["dry_run"])
        prefix = "Would remove" if options["dry_run"] else "Removed"
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}: {result['old_attendance']} old attendance, "
                f"{result['left_alliance']} departed-member attendance, "
                f"{result['old_events']} old member events."
            )
        )
