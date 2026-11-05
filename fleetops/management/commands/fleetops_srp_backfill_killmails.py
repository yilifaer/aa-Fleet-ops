# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

import time

from django.core.management.base import BaseCommand
from django.db.models import Q

from fleetops.models import SrpKillmail
from fleetops.providers.esi import FleetESIError
from fleetops.providers.esi_killmails import get_killmail
from fleetops.srp import attackers as attacker_info
from fleetops.srp import killmails
from fleetops.srp.types import SrpError

# Pause between two reads of ESI, so a long run stays well below the rate limits.
DELAY_SECONDS = 0.25


class Command(BaseCommand):
    help = (
        "Fill in what the review page shows for SRP killmails stored by an older version: the security status of the "
        "system (--security) and the attackers (--attackers). Without an option both are filled in. Only killmails that "
        "have a claim are read, again with the stored hash, one read at a time with a pause. Safe to run again."
    )

    def add_arguments(self, parser):
        parser.add_argument("--security", action="store_true", help="Look up the security status and region again.")
        parser.add_argument("--attackers", action="store_true", help="Read the attackers and the damage again.")
        parser.add_argument("--limit", type=int, default=0, help="Stop after this many killmails (0 = no limit).")
        parser.add_argument(
            "--dry-run", action="store_true", help="Show how many killmails would be read without reading any."
        )

    def handle(self, *args, **options):
        security = options["security"] or not options["attackers"]
        attackers = options["attackers"] or not options["security"]
        wanted = Q(pk__in=[])
        if security:
            wanted |= Q(solar_system_security__isnull=True)
        if attackers:
            wanted |= Q(damage_taken__isnull=True)
        rows = SrpKillmail.objects.filter(claim__isnull=False, origin=SrpKillmail.Origin.ESI).filter(wanted)
        rows = rows.order_by("killmail_time", "pk")
        limit = max(0, options["limit"])
        if limit:
            rows = rows[:limit]
        pks = list(rows.values_list("pk", flat=True))
        if options["dry_run"]:
            self.stdout.write(self.style.SUCCESS(f"Would read {len(pks)} killmails."))
            return
        done = failed = 0
        for index, pk in enumerate(pks):
            if index:
                time.sleep(DELAY_SECONDS)
            km = SrpKillmail.objects.filter(pk=pk).first()
            if km is None:
                continue
            try:
                self._fill(km, security, attackers)
            except FleetESIError as exc:
                failed += 1
                # Only the kind of failure is written, never the text of an ESI answer.
                self.stderr.write(f"Killmail {pk}: not read ({exc.code}).")
                if exc.code == "ESI_LIMITED":
                    self.stderr.write("ESI asked to slow down; run the command again later.")
                    break
            except SrpError as exc:
                failed += 1
                self.stderr.write(f"Killmail {pk}: not usable ({exc.code}).")
            else:
                done += 1
        self.stdout.write(self.style.SUCCESS(f"Filled in {done} killmails; {failed} could not be read."))

    def _fill(self, km, security, attackers):
        if security:
            fields = killmails.location_fields(km.ship_type_id, km.solar_system_id)
            if fields["solar_system_security"] is not None:
                SrpKillmail.objects.filter(pk=km.pk).update(**fields)
        if attackers:
            parsed = killmails.killmail_fields(get_killmail(km.pk, km.killmail_hash))
            SrpKillmail.objects.filter(pk=km.pk).update(attackers=parsed["attackers"], damage_taken=parsed["damage_taken"])
            km.refresh_from_db()
            attacker_info.queue_name_warming(km)
