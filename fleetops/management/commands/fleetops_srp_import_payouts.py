# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

import csv
import os
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from fleetops.forms_config import lookup_ship
from fleetops.models import SrpCategory, SrpPayoutRule
from fleetops.services.audit import audit, config_snapshot

SCOPES = tuple(value for value, _label in SrpPayoutRule.Scope.choices)


class Command(BaseCommand):
    help = (
        "Import payout rules (fixed amounts per ship) of one category from a CSV file. The file needs a type_id column "
        "and one column with the amounts. Without --apply nothing is written: the command only says how many rules "
        "would be made or changed. An empty or zero amount skips the ship. A rule for the same ship, scope and category "
        "is updated; every change is audited as a configuration change."
    )

    def add_arguments(self, parser):
        parser.add_argument("path", metavar="PATH", help="The CSV file (UTF-8, with or without a byte order mark).")
        parser.add_argument("--category", required=True, metavar="KEY", help="The key of the category the rules belong to.")
        parser.add_argument("--column", default="amount", help="The column that holds the amounts (amount).")
        parser.add_argument("--name-column", default="name", help="The column that holds the English ship name (name).")
        parser.add_argument("--scope", default="any", choices=SCOPES, help="Where the rules apply (any).")
        parser.add_argument("--apply", action="store_true", help="Write the rules; without it this is a dry run.")

    # --- rows --------------------------------------------------------------------------------------------

    @staticmethod
    def _amount(text):
        text = (text or "").replace(",", "").replace(" ", "").strip()
        if not text:
            return None
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise ValueError("the amount is not a number") from None
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError("the amount is not a whole number")
        return int(number)

    @staticmethod
    def _type_id(text):
        text = (text or "").strip()
        if not text.isascii() or not text.isdigit() or int(text) <= 0:
            raise ValueError("type_id is not a ship type ID")
        return int(text)

    def _name(self, row, name_column, type_id):
        name = (row.get(name_column) or "").strip()
        if name:
            return name[:255]
        return lookup_ship(str(type_id)).name[:255]

    # --- handle ------------------------------------------------------------------------------------------

    def handle(self, *args, **options):
        category = SrpCategory.objects.filter(key=options["category"]).first()
        if category is None:
            raise CommandError(f"There is no category with the key \"{options['category']}\".")
        path = options["path"]
        column, name_column, scope = options["column"], options["name_column"], options["scope"]
        # The file name only, never the whole path, goes into the audit.
        source = os.path.basename(path)
        try:
            handle = open(path, newline="", encoding="utf-8-sig")
        except OSError as exc:
            raise CommandError(f"Cannot read {source} ({type(exc).__name__}).") from exc
        created = updated = unchanged = skipped = 0
        bad = []
        seen = set()
        with handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            if "type_id" not in fields:
                raise CommandError("The file has no type_id column.")
            if column not in fields:
                raise CommandError(f"The file has no column called \"{column}\".")
            field = SrpPayoutRule._meta.get_field("amount")
            for row in reader:
                line = reader.line_num
                try:
                    amount = self._amount(row.get(column))
                    if not amount:
                        skipped += 1
                        continue
                    field.run_validators(amount)
                    type_id = self._type_id(row.get("type_id"))
                    if type_id in seen:
                        raise ValueError("the ship appears twice")
                    seen.add(type_id)
                    rule = SrpPayoutRule.objects.filter(
                        ship_type_id=type_id, doctrine_key="", scope=scope, category_key=category.pk
                    ).first()
                    if rule is not None and rule.amount == amount:
                        unchanged += 1
                        continue
                    # A dry run asks nobody for a name; the writing run reads it from the file or looks the ship up.
                    name = ""
                    if options["apply"]:
                        name = rule.ship_type_name if rule is not None and rule.ship_type_name else self._name(row, name_column, type_id)
                except (ValueError, ValidationError) as exc:
                    message = "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
                    bad.append((line, message))
                    continue
                if rule is None:
                    created += 1
                else:
                    updated += 1
                if not options["apply"]:
                    continue
                before = config_snapshot(rule) if rule is not None else None
                if rule is None:
                    rule = SrpPayoutRule(
                        ship_type_id=type_id, ship_type_name=name, scope=scope, category=category, amount=amount
                    )
                else:
                    rule.amount = amount
                    rule.ship_type_name = name
                rule.save()
                audit(
                    None,
                    "configuration.create" if before is None else "configuration.update",
                    rule,
                    before,
                    config_snapshot(rule),
                    f"Imported from {source}",
                )
        for line, message in bad:
            self.stderr.write(f"Line {line}: skipped ({message}).")
        verb = "" if options["apply"] else "would be "
        self.stdout.write(
            f"{created} rules {verb}created, {updated} {verb}updated, {unchanged} unchanged, {skipped} without an amount, "
            f"{len(bad)} rows with a problem."
        )
        if not options["apply"]:
            self.stdout.write("Dry run: nothing was written. Add --apply to write the rules.")
