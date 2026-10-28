# AA FleetOps 0.2.0b3: a group access row can count every member of its Alliance Auth group as a leader, for
# alliances that keep a members group and a leaders group per small group and record no group leaders. Off by default,
# so nothing changes until an administrator switches it on.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0021_group_access"),
    ]

    operations = [
        migrations.AddField(
            model_name="groupaccess",
            name="members_are_leaders",
            field=models.BooleanField(
                default=False,
                help_text="For alliances that keep two Alliance Auth groups per small group, such as a members group and a leaders group, and record no group leaders. Choose the leaders group here and switch this on: every active member of it who holds FleetOps access counts as a leader, with the same rights as a leader recorded in Alliance Auth.",
                verbose_name="All members of this group count as its leaders",
            ),
        ),
    ]
