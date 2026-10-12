from django.core.management.base import BaseCommand

from fleetops.models import FleetMemberState
from fleetops.services import sde

# Ids per SDE query, well below the bound-parameter limits of SQLite and MariaDB.
BATCH_SIZE = 500


class Command(BaseCommand):
    help = (
        "Read the ship and solar system names of fleet member records again from the SDE, in English. "
        "Fixes names stored in another language; fleet member events keep the names they were recorded with."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show how many records would change without changing anything.",
        )

    def handle(self, *args, **options):
        if not sde.sde_installed():
            self.stdout.write(self.style.WARNING("The eve_sde app is not installed; there are no names to read."))
            return
        dry_run = options["dry_run"]
        ships = self._refresh("ship_type_id", "ship_type_name", sde.item_type_names, dry_run)
        systems = self._refresh("solar_system_id", "solar_system_name", sde.solar_system_names, dry_run)
        prefix = "Would update" if dry_run else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}: {ships} ship names and {systems} solar system names on fleet member records."
            )
        )

    def _refresh(self, id_field, name_field, lookup, dry_run) -> int:
        ids = sorted(
            FleetMemberState.objects.exclude(**{id_field: None}).order_by().values_list(id_field, flat=True).distinct()
        )
        changed = 0
        for start in range(0, len(ids), BATCH_SIZE):
            for object_id, name in lookup(ids[start : start + BATCH_SIZE]).items():
                if not name:
                    continue
                rows = FleetMemberState.objects.filter(**{id_field: object_id}).exclude(**{name_field: name})
                changed += rows.count() if dry_run else rows.update(**{name_field: name})
        return changed
