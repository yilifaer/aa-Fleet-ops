# AA FleetOps 0.3.0b1: aa-srp import, data. Guard for going back to 0032: the external ID of the claims goes away
# with 0033, and with it the only way to tell an imported claim from the others, so going back refuses to run while
# claims imported from aa-srp exist. This is the second line of defence; the first is the guard at the end of the
# migration chain (0035). Nothing is changed going forward.
#
# Migrations must not import FleetOps code: the constants below are copies.

from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError

AASRP = "aasrp"


def refuse_if_imported(apps, schema_editor):
    """Refuse to go back while a claim was imported from aa-srp."""
    SrpClaim = apps.get_model("fleetops", "SrpClaim")
    count = SrpClaim.objects.filter(source=AASRP).count()
    if count:
        raise IrreversibleError(
            f"{count} SRP claims were imported from aa-srp and cannot be kept without their external ID. "
            "Run fleetops_import_aasrp --undo first (it deletes the imported claims, killmails and links), "
            "or restore the database from before the upgrade."
        )


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0033_srp_import"),
    ]

    # Going back runs this before 0033 is undone.
    operations = [
        migrations.RunPython(migrations.RunPython.noop, refuse_if_imported),
    ]
