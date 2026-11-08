# AA FleetOps 0.3.0b1: index on the killmail ID of the found losses, schema. Looking a loss up by killmail and kind (the
# ignore, restore and claim buttons of My SRP, the preview of a claim) had no index to use, because the existing ones
# start with the character. Adds one index; no data is read or changed. Going back drops the index.
#
# Migrations must not import FleetOps code.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0036_history_prune_share"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="srploss",
            index=models.Index(fields=["killmail_id", "kind"], name="fleetops_srploss_km_idx"),
        ),
    ]
