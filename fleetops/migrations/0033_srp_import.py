# AA FleetOps 0.3.0b1: aa-srp import, schema. Adds the external ID of a claim (unique, empty for every claim that
# was not imported), the "imported" payout basis and the "import" claim event. Nothing is filled in.
# Going back drops the column; the guard of the next migration refuses to go back while imported claims exist.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0032_srp_review_payments"),
    ]

    operations = [
        migrations.AddField(
            model_name="srpclaim",
            name="external_id",
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=64,
                null=True,
                unique=True,
                verbose_name="External ID",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="payout_basis",
            field=models.CharField(
                choices=[
                    ("rule", "Payout table"),
                    ("percentage", "Percentage of the value"),
                    ("none", "No automatic amount"),
                    ("imported", "Amount as recorded in aa-srp"),
                ],
                default="none",
                max_length=12,
                verbose_name="Payout basis",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaimevent",
            name="action",
            field=models.CharField(
                choices=[
                    ("submit", "Submitted"),
                    ("auto_reject", "Rejected automatically"),
                    ("withdraw", "Withdrawn"),
                    ("resubmit", "Submitted again"),
                    ("approve", "Approved"),
                    ("amount_change", "Amount changed"),
                    ("reject", "Rejected"),
                    ("revoke", "Approval revoked"),
                    ("paid", "Marked paid"),
                    ("unpaid", "Payment undone"),
                    ("reopen", "Reopened"),
                    ("reclassify", "Moved between in fleet and out of fleet"),
                    ("recategorize", "Category changed"),
                    ("recalculate", "Recalculated"),
                    ("recheck", "Fleet presence checked again"),
                    ("value_update", "Loss value updated"),
                    ("member_edit", "Changed by the claimant"),
                    ("import", "Imported from aa-srp"),
                    ("note", "Internal note"),
                ],
                max_length=30,
                verbose_name="Action",
            ),
        ),
    ]
