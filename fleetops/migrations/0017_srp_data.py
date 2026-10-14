# AA FleetOps 0.2.0b1: FleetOps SRP data: the SRP settings row (SRP is disabled until an admin turns it on),
# the new permissions and request_srp for everyone who holds basic_access (groups, states and users), and the
# SRP line in the untouched default MOTD. manage_srp is not granted to anybody: it decides on money.
# Customised MOTD templates are left alone; they need {{ srp_url }} added by hand.

from django.conf import settings
from django.db import migrations

APP_LABEL = "fleetops"
SETTINGS_MODEL = "fleetopssettings"
BASIC_ACCESS = "basic_access"
NEW_PERMISSIONS = (
    ("request_srp", "Can request SRP"),
    ("manage_srp", "Can review and pay SRP claims"),
)
REQUEST_SRP = "request_srp"

# The default MOTD before and after the SRP line was added.
OLD_MOTD = """<font color="#bfffffff"><b>{{ fleet_type }}</b><br>FC: {{ fc }}<br>Form Up: {{ formup }}{% if doctrine_name %}<br>Doctrine: {% if doctrine_url %}<a href="{{ doctrine_url }}">{{ doctrine_name }}</a>{% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}{% endif %}{% if comms %}<br>Comms: {% if comms_link %}<a href="{{ comms_link }}">{{ comms }}</a>{% else %}{{ comms }}{% endif %}{% endif %}{% if logi_channel %}<br>Logi: {{ logi_channel_link }}{% endif %}{% if boost_channel %}<br>Boost: {{ boost_channel_link }}{% endif %}{% if additional_message %}<br><br>{{ additional_message|linebreaksbr }}{% endif %}</font>"""
NEW_MOTD = """<font color="#bfffffff"><b>{{ fleet_type }}</b><br>FC: {{ fc }}<br>Form Up: {{ formup }}{% if doctrine_name %}<br>Doctrine: {% if doctrine_url %}<a href="{{ doctrine_url }}">{{ doctrine_name }}</a>{% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}{% endif %}{% if comms %}<br>Comms: {% if comms_link %}<a href="{{ comms_link }}">{{ comms }}</a>{% else %}{{ comms }}{% endif %}{% endif %}{% if logi_channel %}<br>Logi: {{ logi_channel_link }}{% endif %}{% if boost_channel %}<br>Boost: {{ boost_channel_link }}{% endif %}{% if srp_url %}<br>SRP: <a href="{{ srp_url }}">Request SRP</a>{% endif %}{% if additional_message %}<br><br>{{ additional_message|linebreaksbr }}{% endif %}</font>"""


def _normalized(content):
    return (content or "").replace("\r\n", "\n").strip()


def create_srp_settings(apps, schema_editor):
    SrpSettings = apps.get_model("fleetops", "SrpSettings")
    SrpSettings.objects.get_or_create(pk=1)


def grant_request_srp(apps, schema_editor):
    # post_migrate creates the permissions only after every migration has run, so they are created here.
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    content_type, _created = ContentType.objects.get_or_create(app_label=APP_LABEL, model=SETTINGS_MODEL)
    permissions = {}
    for codename, name in NEW_PERMISSIONS:
        permissions[codename], _created = Permission.objects.get_or_create(
            codename=codename, content_type=content_type, defaults={"name": name}
        )
    basic_access = Permission.objects.filter(codename=BASIC_ACCESS, content_type=content_type).first()
    if basic_access is None:
        # A new installation: nobody holds basic_access yet.
        return
    request_srp = permissions[REQUEST_SRP]

    holders = [apps.get_model("auth", "Group"), apps.get_model("authentication", "State")]
    user_model = apps.get_model(settings.AUTH_USER_MODEL)
    # Users with permissions of their own; custom user models without the field have none.
    if hasattr(user_model, "user_permissions"):
        holders.append(user_model)
    for model in holders:
        field = "user_permissions" if model is user_model else "permissions"
        # By primary key: the permissions of a state are a proxy model of Alliance Auth.
        for holder in model.objects.filter(**{field: basic_access.pk}).exclude(**{field: request_srp.pk}):
            getattr(holder, field).add(request_srp.pk)


def add_srp_line_to_default_motd(apps, schema_editor):
    # An MOTD an admin has edited is left alone.
    MessageTemplate = apps.get_model("fleetops", "MessageTemplate")
    for pk, content in MessageTemplate.objects.filter(template_type="motd", name="Default MOTD").values_list(
        "pk", "content"
    ):
        if _normalized(content) == _normalized(OLD_MOTD):
            MessageTemplate.objects.filter(pk=pk).update(content=NEW_MOTD)


def forward(apps, schema_editor):
    create_srp_settings(apps, schema_editor)
    grant_request_srp(apps, schema_editor)
    add_srp_line_to_default_motd(apps, schema_editor)


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0016_srp_schema"),
        ("authentication", "0026_alter_characterownership_user_and_more"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
