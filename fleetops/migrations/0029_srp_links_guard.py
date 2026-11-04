# AA FleetOps 0.3.0b1: SRP links, data. Guard for going back to 0023: the fleet of a link becomes required again,
# so links without a fleet have to go. Standing links without claims are deleted; going back refuses to run while a
# link without a fleet has claims, or came from another SRP app, because deleting it would detach those claims from
# their link or lose imported data. This is the second line of defence; the first is the guard at the end of the
# migration chain.
#
# Migrations must not import FleetOps code: the constants below are copies.

from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError

FLEET_KIND = "fleet"
AASRP = "aasrp"
MAX_LISTED = 50


def refusing_links(SrpFleetLink):
    """Links without a fleet that cannot be removed: they have claims, or they were imported."""
    from django.db.models import Count, Q

    return (
        SrpFleetLink.objects.filter(operation__isnull=True)
        .annotate(claim_count=Count("claims"))
        .filter(Q(claim_count__gt=0) | Q(origin=AASRP))
    )


def refuse_if_links_in_use(apps, schema_editor):
    """Refuse to go back while a link without a fleet has claims or was imported. Only the link codes are listed."""
    SrpFleetLink = apps.get_model("fleetops", "SrpFleetLink")
    links = refusing_links(SrpFleetLink)
    count = links.count()
    if count:
        codes = list(links.order_by("pk").values_list("code", flat=True)[:MAX_LISTED])
        listed = ", ".join(codes)
        if count > MAX_LISTED:
            listed += f" and {count - MAX_LISTED} more, {count} links in total"
        quoted = ", ".join(f"'{code}'" for code in codes)
        more = " Run the migration again afterwards; it lists the next ones." if count > MAX_LISTED else ""
        raise IrreversibleError(
            f"SRP links without a fleet that have claims, or that were imported (codes: {listed}), cannot be kept "
            "without the optional fleet column. Nothing in the product deletes a link. Either restore the database "
            "from before the upgrade, or delete these links (their claims stay, without a link; imported links lose "
            "their import data) with: python manage.py shell -c \"from django.apps import apps; "
            f"apps.get_model('fleetops', 'SrpFleetLink').objects.filter(code__in=[{quoted}]).delete()\"." + more
        )


def delete_links_without_claims(apps, schema_editor):
    SrpFleetLink = apps.get_model("fleetops", "SrpFleetLink")
    SrpFleetLink.objects.filter(operation__isnull=True).delete()


def check_links_have_a_fleet(apps, schema_editor):
    """Going forward: every link that exists now belongs to a fleet (the column was required until 0028)."""
    SrpFleetLink = apps.get_model("fleetops", "SrpFleetLink")
    if SrpFleetLink.objects.filter(kind=FLEET_KIND, operation__isnull=True).exclude(origin=AASRP).exists():
        raise IrreversibleError("An SRP fleet link without a fleet exists; fix it before migrating.")


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0028_srp_links"),
    ]

    # Going back runs the operations in reverse order: the check first, then the deletion, then 0028 makes the
    # fleet column required again.
    operations = [
        migrations.RunPython(check_links_have_a_fleet, delete_links_without_claims),
        migrations.RunPython(migrations.RunPython.noop, refuse_if_links_in_use),
    ]
