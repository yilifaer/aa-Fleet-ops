# AA FleetOps 0.2.0b1: comms join tokens are unique now that every preset has one

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0009_convert_ping_targets_and_presets"),
    ]

    operations = [
        migrations.AlterField(
            model_name="commspreset",
            name="join_token",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
