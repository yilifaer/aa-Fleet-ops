# AA FleetOps 0.3.0b1: help text of "The approver of a claim may not pay it", schema state only. The rule now also keeps
# a reviewer who changed the approved amount from paying the claim; the setting and the stored data are the same. No
# column changes in the database and nothing is read or changed; going back restores the earlier help text.
#
# Migrations must not import FleetOps code.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0037_srp_loss_killmail_index"),
    ]

    operations = [
        migrations.AlterField(
            model_name="srpsettings",
            name="require_different_payer",
            field=models.BooleanField(
                default=False,
                help_text="Four-eyes rule: the person who approved a claim, and anyone who changed its approved amount, cannot mark it paid. Undoing a payment is not affected.",
                verbose_name="The approver of a claim may not pay it",
            ),
        ),
    ]
