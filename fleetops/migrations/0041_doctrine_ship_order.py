# AA FleetOps 0.3.0b2: the order of the ships of a doctrine, schema. Creates the table DoctrineShipOrder, one row
# per doctrine with the ship names in order and the mark between every two ("greater", "same", "much greater").
# New and upgraded sites start with no row, so every doctrine keeps its automatic order; no data is changed or read.
#
# This is now the last migration of the chain, so going back undoes it first. Its operations are undone in reverse
# order: the rollback guard below runs before the table is dropped, so a rollback below 0035 is still refused first
# and a refused rollback leaves the running version with the table it can read. The guard is the one of 0035 (the
# same checks, the same message); 0035 runs it a second time when the rollback goes on, which changes nothing. Going
# forward the guard does nothing. Dropping the table only loses the stored orders, which are easy to set again.
#
# Migrations must not import FleetOps code: the guard and its constants below are copies of 0035, kept equal by
# tests/test_migration_srp.py. The names and the table layout are literals.

from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError
from django.db.models import Count, Q

AASRP = "aasrp"
MAX_LISTED = 50

RULES = "rules"
LINKS = "links"
CLAIMS = "claims"


def _listed(values, total):
    text = ", ".join(str(value) for value in values)
    if total > len(values):
        text += f" and {total - len(values)} more, {total} in total"
    return text


def check_all(apps):
    """The reasons going back is not safe: ``[(code, detail)]``, empty when it is. Reads only.

    ``detail`` is ``{"count": n, "listed": [...]}`` with rule IDs for ``rules``, link codes for ``links`` and nothing
    listed for ``claims``.
    """
    SrpPayoutRule = apps.get_model("fleetops", "SrpPayoutRule")
    SrpFleetLink = apps.get_model("fleetops", "SrpFleetLink")
    SrpClaim = apps.get_model("fleetops", "SrpClaim")
    problems = []

    # A rule with a category, or one left with a category key by a write that skipped save().
    rules = SrpPayoutRule.objects.filter(Q(category__isnull=False) | ~Q(category_key=0))
    count = rules.count()
    if count:
        listed = list(rules.order_by("pk").values_list("pk", flat=True)[:MAX_LISTED])
        problems.append((RULES, {"count": count, "listed": listed}))

    links = (
        SrpFleetLink.objects.annotate(claim_count=Count("claims"))
        .filter(Q(operation__isnull=True, claim_count__gt=0) | Q(origin=AASRP))
    )
    count = links.count()
    if count:
        listed = list(links.order_by("pk").values_list("code", flat=True)[:MAX_LISTED])
        problems.append((LINKS, {"count": count, "listed": listed}))

    count = SrpClaim.objects.filter(source=AASRP).count()
    if count:
        problems.append((CLAIMS, {"count": count, "listed": []}))
    return problems


def describe(problems):
    """The lines of the refusal, one per problem. Only rule IDs, link codes and counts: no names, no amounts."""
    lines = []
    for code, detail in problems:
        if code == RULES:
            lines.append(
                f"Payout rules that belong to a category (IDs: {_listed(detail['listed'], detail['count'])}): delete "
                "them or change them into general rules without a category."
            )
        elif code == LINKS:
            lines.append(
                "SRP links without a fleet that have claims, or that were imported from aa-srp (codes: "
                f"{_listed(detail['listed'], detail['count'])}): for imported links run fleetops_import_aasrp --undo; "
                "delete the other links (their claims stay, without a link) or restore the database from before the upgrade."
            )
        elif code == CLAIMS:
            lines.append(
                f"{detail['count']} claims were imported from aa-srp. Run fleetops_import_aasrp --undo first."
            )
    return lines


def refuse_rollback(apps, schema_editor):
    problems = check_all(apps)
    if problems:
        raise IrreversibleError(
            "Going back to an earlier FleetOps version is stopped before anything was changed. Fix this first, "
            "then run it again (python manage.py fleetops_srp_rollback_check shows the same list without "
            "changing anything):\n- " + "\n- ".join(describe(problems))
        )


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0040_srp_notify_on_unpaid"),
    ]

    # The guard is the last operation, so going back runs it first, before the table is dropped.
    operations = [
        migrations.CreateModel(
            name="DoctrineShipOrder",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("doctrine_source", models.CharField(max_length=50, verbose_name="Doctrine source")),
                ("doctrine_external_id", models.CharField(max_length=100, verbose_name="Doctrine external ID")),
                ("ships", models.JSONField(default=list, verbose_name="Ships")),
                ("marks", models.JSONField(default=list, verbose_name="Marks")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Updated at")),
            ],
            options={
                "verbose_name": "doctrine ship order",
                "verbose_name_plural": "doctrine ship orders",
                "ordering": ["doctrine_source", "doctrine_external_id"],
                "default_permissions": (),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("doctrine_source", "doctrine_external_id"),
                        name="fleetops_unique_doctrine_ship_order",
                    )
                ],
            },
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_rollback),
    ]
