# AA FleetOps 0.2.0b3: help text of the members-are-leaders option.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0022_group_members_are_leaders"),
    ]

    operations = [
        migrations.AlterField(
            model_name="groupaccess",
            name="members_are_leaders",
            field=models.BooleanField(
                default=False,
                help_text="For alliances that keep two Alliance Auth groups per small group, such as a members group and a leaders group, and record no group leaders. Choose the leaders group here and switch this on: every active member of it who holds FleetOps access counts as a leader, with the same rights as a leader recorded in Alliance Auth. Tick it only for the leaders group: on a members group every member would be a leader. The group must be Internal in Alliance Auth.",
                verbose_name="All members of this group count as its leaders",
            ),
        ),
    ]
