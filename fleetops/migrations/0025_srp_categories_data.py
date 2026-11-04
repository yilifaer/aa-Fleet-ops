# AA FleetOps 0.3.0b1: SRP categories, data. Creates the built-in "fleet" category, puts the claims made through a
# fleet link into it and switches the SRP settings that were never configured to the zKillboard total value.
# Going back refuses to run while a payout rule belongs to a category (see ``refuse_rollback``).
#
# Migrations must not import FleetOps code: the constants below are copies.

from django.db import migrations
from django.db.models import Q
from django.db.migrations.exceptions import IrreversibleError

SYSTEM_KEY = "fleet"
SYSTEM_NAME = "Fleet"
IN_FLEET = "in_fleet"
ZKILL_FITTED = "zkill_fitted"
ZKILL_TOTAL = "zkill_total"
SETTINGS_OBJECT_TYPE = "SrpSettings"
MAX_LISTED = 50


def _has_category(Q):
    """A rule with a category, or one left with a category key by a write that skipped ``save()``."""
    return Q(category__isnull=False) | ~Q(category_key=0)


def create_fleet_category(apps, schema_editor):
    SrpCategory = apps.get_model("fleetops", "SrpCategory")
    SrpCategory.objects.get_or_create(
        key=SYSTEM_KEY,
        defaults={
            "name": SYSTEM_NAME,
            "is_system": True,
            "member_selectable": False,
            "sort_order": 0,
        },
    )


def delete_fleet_category(apps, schema_editor):
    # Runs after the claims and the rules were cleared (reverse order of the operations below).
    SrpCategory = apps.get_model("fleetops", "SrpCategory")
    SrpCategory.objects.filter(key=SYSTEM_KEY, is_system=True).delete()


def backfill_claims(apps, schema_editor):
    SrpCategory = apps.get_model("fleetops", "SrpCategory")
    SrpClaim = apps.get_model("fleetops", "SrpClaim")
    fleet = SrpCategory.objects.filter(key=SYSTEM_KEY).first()
    if fleet is None:
        return
    SrpClaim.objects.filter(scope=IN_FLEET, category__isnull=True).update(category=fleet)


def clear_categories(apps, schema_editor):
    # Claims and rules point to categories with PROTECT; the category column goes with the schema migration.
    SrpClaim = apps.get_model("fleetops", "SrpClaim")
    SrpPayoutRule = apps.get_model("fleetops", "SrpPayoutRule")
    SrpClaim.objects.exclude(category__isnull=True).update(category=None)
    SrpPayoutRule.objects.filter(_has_category(Q)).update(category=None, category_key=0)


def switch_untouched_value_source(apps, schema_editor):
    """Sites that never saved their SRP settings and have no claim get the new default value source.

    A site that saved the settings once (an audit entry exists) or has claims keeps what it has.
    """
    SrpSettings = apps.get_model("fleetops", "SrpSettings")
    SrpClaim = apps.get_model("fleetops", "SrpClaim")
    AuditLog = apps.get_model("fleetops", "AuditLog")
    if AuditLog.objects.filter(object_type=SETTINGS_OBJECT_TYPE).exists() or SrpClaim.objects.exists():
        return
    SrpSettings.objects.filter(value_source=ZKILL_FITTED).update(value_source=ZKILL_TOTAL)


def refuse_rollback(apps, schema_editor):
    """Refuse to go back while a payout rule belongs to a category.

    Without the category column, a rule made for one category would apply to every claim and could pay too much.
    Only the rule IDs are listed. Delete the rules or make them general first.
    """
    SrpPayoutRule = apps.get_model("fleetops", "SrpPayoutRule")
    rules = SrpPayoutRule.objects.filter(_has_category(Q))
    count = rules.count()
    if count:
        ids = list(rules.order_by("pk").values_list("pk", flat=True)[:MAX_LISTED])
        listed = ", ".join(str(pk) for pk in ids)
        if count > MAX_LISTED:
            listed += f" and {count - MAX_LISTED} more, {count} rules in total"
        raise IrreversibleError(
            f"Payout rules that belong to an SRP category (IDs: {listed}) would apply to every claim without the "
            "category column. Delete them or make them general (all categories) first."
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0024_srp_categories"),
    ]

    # Going back runs the operations in reverse order: the guard first, the category last.
    operations = [
        migrations.RunPython(create_fleet_category, delete_fleet_category),
        migrations.RunPython(backfill_claims, clear_categories),
        migrations.RunPython(switch_untouched_value_source, noop),
        migrations.RunPython(noop, refuse_rollback),
    ]
