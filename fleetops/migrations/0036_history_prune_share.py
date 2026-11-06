# AA FleetOps 0.3.0b1: pruning share, schema. Adds the largest share of departed members that one pruning may remove
# to the FleetOps settings (percent, 1 to 99, default 50). It comes after 0035 because 0035 was already applied on
# sites that ran the development branch; a column added to 0035 would never reach them.
#
# This is now the last migration of the chain, so going back undoes it first. Its operations are undone in reverse
# order: the rollback guard below runs before the column is dropped, so a refused rollback leaves the running version
# with a settings table it can read. The guard is the one of 0035 (the same checks, the same message); 0035 runs it a
# second time when the rollback goes on, which changes nothing. Going forward the guard does nothing.
#
# Migrations must not import FleetOps code: the guard and its constants below are copies of 0035, kept equal by
# tests/test_migration_srp.py.

import django.core.validators
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
        ("fleetops", "0035_srp_rollback_guard"),
    ]

    # The guard is the last operation, so going back runs it first, before the column is dropped.
    operations = [
        migrations.AddField(
            model_name="fleetopssettings",
            name="history_prune_max_share",
            field=models.PositiveSmallIntegerField(
                default=50,
                help_text="Percent of the users with PAP history that one pruning of departed members may remove. When more users would be pruned (usually a wrong Member alliance IDs), nothing of the departed members is deleted and a warning is logged. 1 to 99, default 50.",
                validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(99)],
                verbose_name="Largest share of departed members to prune",
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_rollback),
    ]
