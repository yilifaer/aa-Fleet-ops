# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from datetime import datetime, time
from datetime import timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from fleetops.srp import importer, legacy_import
from fleetops.srp.types import SrpError


def _date(value: str, option: str) -> datetime:
    try:
        return datetime.combine(datetime.strptime(value, "%Y-%m-%d").date(), time.min, tzinfo=dt_timezone.utc)
    except ValueError:
        raise CommandError(f"{option} must be a date like 2026-09-01 (EVE time).") from None


class Command(BaseCommand):
    help = (
        "Import the SRP history of aa-srp (read only on its side): approved requests become paid claims, rejected ones "
        "rejected claims, pending requests are not imported. Safe to run again: claims are matched by their aa-srp "
        "request number, and claims that were not imported are never touched. Use --dry-run first, and run the real "
        "import in a maintenance window (every batch holds the SRP lock). --undo removes everything the import made; "
        "--enrich fills in the real killmails of the imported losses (slow, one lookup every two seconds)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Only print the report; write nothing.")
        parser.add_argument("--since", help="Only requests posted on or after this date (YYYY-MM-DD, EVE time).")
        parser.add_argument("--until", help="Only requests posted before this date (YYYY-MM-DD, EVE time).")
        parser.add_argument(
            "--category-map",
            metavar="PATH",
            help='JSON list [{"pattern": "(?i)solo", "category": "<key>"}]: links whose name matches become standing links of the category.',
        )
        parser.add_argument("--actor", metavar="USERNAME", help="The user the audit entries are written for.")
        parser.add_argument("--batch-size", type=int, default=legacy_import.BATCH_SIZE, help="Claims per batch (500).")
        parser.add_argument("--undo", action="store_true", help="Delete everything the import made (combine with --dry-run to count).")
        parser.add_argument("--enrich", action="store_true", help="Look up the real killmails of the imported losses.")
        parser.add_argument("--rate", type=float, default=importer.DEFAULT_ENRICH_RATE, help="With --enrich: lookups per second (0.5).")
        parser.add_argument("--limit", type=int, default=0, help="With --enrich: stop after this many lookups (0 = no limit).")

    # --- helpers -----------------------------------------------------------------------------------------

    def _actor(self, username):
        if not username:
            return None
        user = get_user_model().objects.filter(username=username).first()
        if user is None:
            raise CommandError("--actor: there is no user with that name.")
        return user

    def _line(self, text=""):
        self.stdout.write(text)

    def _warn_pending(self, report):
        if report.pending:
            self.stderr.write(
                self.style.WARNING(
                    f"WARNING: {report.pending} requests are still pending in aa-srp ({report.pending_amount:,} ISK). "
                    "They are not imported. Decide on them in aa-srp before the switch."
                )
            )

    def _print_report(self, report):
        self._line(f"aa-srp links read: {report.links_seen}; requests read: {report.requests_seen}.")
        if report.value_source:
            self._line(f"Loss value source in aa-srp: {report.value_source}.")
        self._line("Requests by month (month of the link) and status, count and ISK:")
        for month in sorted(report.months):
            parts = ", ".join(
                f"{status} {count} / {amount:,}" for status, (count, amount) in sorted(report.months[month].items())
            )
            self._line(f"  {month}: {parts}")
        verb = "Would create" if report.dry_run else "Created"
        self._line(
            f"{verb} {report.create} claims; {'would update' if report.dry_run else 'updated'} {report.update}; "
            f"unchanged {report.unchanged}."
        )
        self._line(
            f"Links used: {report.links_used} ({report.links_created} new, {report.links_updated} changed); "
            f"standing links by category map: {report.category_links}."
        )
        if report.skipped:
            self._line("Skipped:")
            for reason, count in report.skipped.most_common():
                self._line(f"  {importer.SKIP_LABELS.get(reason, reason)}: {count}")
        if report.native_claims:
            self._line("Already claimed in FleetOps (not touched): " + ", ".join(report.native_claims))
        if report.pending:
            self._line(f"Pending in aa-srp (not imported): {report.pending}, {report.pending_amount:,} ISK.")
        if report.unmatched_links:
            self._line("Links no category rule matched (name: requests seen):")
            for name, count in report.unmatched_links.most_common(30):
                self._line(f"  {name}: {count}")
        self._line(f"Kill times estimated from the link time: {report.estimated_times}.")
        if report.without_user:
            self._line(f"Claims without an Auth user (owner not found): {report.without_user}.")
        self._line(f"Time: {report.seconds:.1f} s.")

    # --- handle ------------------------------------------------------------------------------------------

    def handle(self, *args, **options):
        actor = self._actor(options["actor"])
        if options["undo"] and options["enrich"]:
            raise CommandError("--undo and --enrich cannot be combined.")
        filtered = options["since"] or options["until"] or options["category_map"] or options["batch_size"] != legacy_import.BATCH_SIZE
        if options["undo"] and filtered:
            raise CommandError("--undo always removes the whole import; it takes no filters (--since, --until, --category-map, --batch-size).")
        # Undoing and completing the stand-in killmails only need FleetOps, so they still work once aa-srp is gone.
        if options["undo"]:
            counts = importer.undo(actor, dry_run=options["dry_run"])
            verb = "Would delete" if options["dry_run"] else "Deleted"
            self._line(
                f"{verb} {counts['claims']} imported claims, {counts['killmails']} imported killmails and "
                f"{counts['links']} imported links."
            )
            return
        if options["enrich"]:
            self._enrich(options, actor)
            return
        if not legacy_import.available():
            raise CommandError("aa-srp (aasrp) is not installed.")
        if options["batch_size"] < 1:
            raise CommandError("--batch-size must be at least 1.")
        category_map = ()
        if options["category_map"]:
            try:
                category_map = importer.load_category_map(options["category_map"])
            except importer.ImportConfigError as exc:
                raise CommandError(str(exc)) from exc
        since = _date(options["since"], "--since") if options["since"] else None
        until = _date(options["until"], "--until") if options["until"] else None
        run = importer.Options(
            dry_run=options["dry_run"],
            since=since,
            until=until,
            category_map=category_map,
            actor=actor,
            batch_size=options["batch_size"],
        )
        try:
            report = importer.run_import(run, on_collected=None if run.dry_run else self._warn_pending)
        except legacy_import.AaSrpMissing as exc:
            raise CommandError(str(exc)) from exc
        self._print_report(report)
        if run.dry_run:
            self._warn_pending(report)
            self._line("Dry run: nothing was written.")

    def _enrich(self, options, actor):
        if options["dry_run"]:
            self._line(f"Would look up {importer.placeholders().count()} killmails.")
            return
        try:
            result = importer.enrich(limit=max(0, options["limit"]), rate=options["rate"], actor=actor)
        except SrpError as exc:
            raise CommandError(exc.message) from exc
        if result.stopped:
            self.stderr.write(result.stopped)
        self._line(f"Completed {result.done} killmails; {result.failed} could not be read; {result.remaining} still wait.")
