# AA FleetOps 0.3.0b1: SRP self payment, data. Before this version "SRP managers may decide on their own claims"
# also covered paying them; a site that allowed it keeps that behaviour by copying the setting to the new
# "SRP payers may mark their own claims paid". Going back needs nothing: the column is dropped with 0026.
#
# Migrations must not import FleetOps code.

from django.db import migrations


def copy_self_review_to_self_payment(apps, schema_editor):
    SrpSettings = apps.get_model("fleetops", "SrpSettings")
    SrpSettings.objects.filter(allow_self_review=True).update(allow_self_payment=True)


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0026_srp_review_permissions"),
    ]

    operations = [
        migrations.RunPython(copy_self_review_to_self_payment, migrations.RunPython.noop),
    ]
