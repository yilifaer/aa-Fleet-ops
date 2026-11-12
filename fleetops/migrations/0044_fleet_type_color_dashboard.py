# AA FleetOps 0.3.0b3: fleet type colour and "Show on dashboard". Adds two columns to the fleet types: a colour (empty =
# the automatic colour as before; or one of the six theme colours "theme-1" to "theme-6"; or a custom "#rrggbb") and a
# switch for the dashboard card. The colour starts empty everywhere, so every type keeps the colour it has today. The
# switch is set from the dashboard as it works today: on for the first four active types in the dashboard's order (sort
# order, then name), off for every other type, so the dashboard shows the same cards after the upgrade. Nothing else is
# read or changed.
#
# This is now the last migration of the chain, so going back undoes it first. The operations are undone in reverse
# order: the rollback guard below runs first, before the two columns are dropped, so a rollback below 0035 is still
# refused first and a refused rollback leaves the running version with the columns it can read. The guard is the one of
# 0035 (the same checks, the same message); 0035 runs it a second time when the rollback goes on, which changes
# nothing. Going forward the guard does nothing. Dropping the columns only loses the chosen colours and switches. The
# data step has nothing to undo.
#
# Migrations must not import FleetOps code: the guard and its constants below are copies of 0035, kept equal by
# tests/test_migration_srp.py. The names, the column layout and the number of cards (four) are literals.

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


DASHBOARD_CARDS = 4


def set_dashboard_cards(apps, schema_editor):
    """On for the first four active types in the dashboard's order, off for every other type."""
    FleetType = apps.get_model("fleetops", "FleetType")
    keep = list(
        FleetType.objects.filter(is_active=True).order_by("sort_order", "name").values_list("pk", flat=True)[:DASHBOARD_CARDS]
    )
    FleetType.objects.exclude(pk__in=keep).update(show_on_dashboard=False)
    FleetType.objects.filter(pk__in=keep).update(show_on_dashboard=True)


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0043_doctrine_access"),
    ]

    # The guard is the last operation, so going back runs it first, before the columns are dropped.
    operations = [
        migrations.AddField(
            model_name="fleettype",
            name="color",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Empty: the type gets one of the theme colours automatically. Otherwise one of the six theme colours (theme-1 to theme-6) or a custom colour such as #e67e22.",
                max_length=7,
                validators=[
                    django.core.validators.RegexValidator(
                        "^(theme-[1-6]|#[0-9a-f]{6})$",
                        "Enter theme-1 to theme-6 or a colour as # and six lower-case hex digits, such as #e67e22.",
                    )
                ],
                verbose_name="Colour",
            ),
        ),
        migrations.AddField(
            model_name="fleettype",
            name="show_on_dashboard",
            field=models.BooleanField(
                default=True,
                help_text="Show a PAP card of this type on the dashboard. The cards follow the sort order.",
                verbose_name="Show on dashboard",
            ),
        ),
        migrations.RunPython(set_dashboard_cards, migrations.RunPython.noop),
        migrations.RunPython(migrations.RunPython.noop, refuse_rollback),
    ]
