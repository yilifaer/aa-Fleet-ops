from django.db import migrations

PING = """{{ ping_target }}\n**{{ fleet_type }}**\nFC: {{ fc }}\nForm Up: {{ formup }}\nDoctrine: {{ doctrine }}\nComms: {{ comms }}{% if comms_link %} — {{ comms_link }}{% endif %}\n{% if logi_channel %}Logi: {{ logi_channel }}\n{% endif %}{% if boost_channel %}Boost: {{ boost_channel }}\n{% endif %}{% if fleet_time %}Time: {{ fleet_time }}\n{% endif %}{% if additional_message %}\n{{ additional_message }}{% endif %}"""
MOTD = """<b>{{ fleet_type }}</b><br>FC: {{ fc }}<br>Form Up: {{ formup }}<br>Doctrine: {{ doctrine }}<br>Comms: {{ comms }}{% if comms_link %} — {{ comms_link }}{% endif %}{% if logi_channel %}<br>Logi: {{ logi_channel }}{% endif %}{% if boost_channel %}<br>Boost: {{ boost_channel }}{% endif %}{% if additional_message %}<br><br>{{ additional_message }}{% endif %}"""


def seed(apps, schema_editor):
    Settings = apps.get_model("fleetops", "FleetOpsSettings")
    Template = apps.get_model("fleetops", "MessageTemplate")
    Settings.objects.get_or_create(pk=1)
    Template.objects.get_or_create(
        template_type="ping",
        name="Default Ping",
        defaults={"content": PING, "is_default": True, "is_active": True},
    )
    Template.objects.get_or_create(
        template_type="motd",
        name="Default MOTD",
        defaults={"content": MOTD, "is_default": True, "is_active": True},
    )


class Migration(migrations.Migration):
    dependencies = [("fleetops", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
