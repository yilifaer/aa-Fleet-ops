# AA FleetOps 0.3.0b7: continuous payment batch numbers. Adds SrpPaymentBatch.number (unique, empty until a batch is
# kept) and gives every existing batch the number it already had as PAY-n, which was its primary key, so the numbers
# already written in transfer reasons still match. New batches get the highest number plus one when they are
# finished; a batch that paid nothing is deleted and never takes a number.
#
# This is now the last migration of the chain, so going back undoes it first. Its operations are undone in reverse
# order: the rollback guard below runs before the column is dropped, so a rollback below 0035 is still refused first
# and a refused rollback leaves the running version with a batch table it can read. The guard is the one of 0035 (the
# same checks, the same message); 0035 runs it a second time when the rollback goes on, which changes nothing. Going
# forward the guard does nothing. Dropping the column loses only the new numbering: the earlier version shows every
# batch as PAY-<primary key> again, which is the number of every batch made before this version.
#
# Migrations must not import FleetOps code: the guard and its constants below are copies of 0035, kept equal by
# tests/test_migration_srp.py. Going back is also refused while a batch of this version has a number that is not
# its primary key (``refuse_renumbering``, the same check as ``rollback.check_batch_numbers``). The field is declared with literals.

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


def refuse_renumbering(apps, schema_editor):
    """Going back drops the column and the earlier version shows PAY-<primary key>: refused while a batch has a
    number that is not its primary key, because a PAY-n written in a transfer reason would then name another batch
    (and going forward again would number the batches by their key for good). Reads only."""
    Batch = apps.get_model("fleetops", "SrpPaymentBatch")
    moved = Batch.objects.filter(number__isnull=False).exclude(number=models.F("pk"))
    count = moved.count()
    if count:
        listed = ", ".join(f"PAY-{n}" for n in moved.order_by("number").values_list("number", flat=True)[:MAX_LISTED])
        if count > MAX_LISTED:
            listed += f" and {count - MAX_LISTED} more, {count} in total"
        raise IrreversibleError(
            "Going back to an earlier FleetOps version is stopped before anything was changed: "
            f"{count} payment batches have a number that is not their database ID ({listed}). The earlier version "
            "shows every batch as PAY-<database ID>, so these numbers would name other batches. Stay on this "
            "version, or restore the database from before the upgrade."
        )


def number_existing_batches(apps, schema_editor):
    """Every batch that exists keeps the number it showed so far: its primary key."""
    Batch = apps.get_model("fleetops", "SrpPaymentBatch")
    Batch.objects.filter(number__isnull=True).update(number=models.F("pk"))


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0046_member_rank_scope"),
    ]

    # The guard is the last operation, so going back runs it first, before the column is dropped.
    operations = [
        migrations.AddField(
            model_name="srppaymentbatch",
            name="number",
            field=models.PositiveIntegerField(
                blank=True, editable=False, null=True, unique=True, verbose_name="Number"
            ),
        ),
        migrations.RunPython(number_existing_batches, migrations.RunPython.noop),
        migrations.RunPython(migrations.RunPython.noop, refuse_renumbering),
        migrations.RunPython(migrations.RunPython.noop, refuse_rollback),
    ]
