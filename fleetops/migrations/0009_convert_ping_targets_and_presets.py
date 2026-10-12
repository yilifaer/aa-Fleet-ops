# AA FleetOps 0.1.0a6: convert ping targets into ping channels and mentions, split in-game channel links,
# remove personal user names and spaces from voice links, update the untouched default templates and
# link the channels in the other MOTD templates

import html
import re
import uuid
from urllib.parse import quote, urlsplit

from django.db import migrations

MAX_DATABASE_ID = 2**63 - 1

ROLE_MENTION_RE = re.compile(r"<@&(\d{1,20})>")
JOIN_CHANNEL_RE = re.compile(r"(?i:joinchannel):(player_[0-9a-fA-F]{32})")
LINK_TEXT_RE = re.compile(r">([^<]+)</(a|url)>", re.IGNORECASE)
VOICE_URL_SCHEMES = ("mumble", "ts3server", "teamspeak", "discord", "https", "http")
SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
SPACE_RE = re.compile(r"\s")
# {{ logi_channel }} and {{ boost_channel }} output tags; {% if logi_channel %} stays as it is.
CHANNEL_OUTPUT_RE = re.compile(r"{{\s*(logi_channel|boost_channel)\s*}}")

# Default templates as 0002 and fleetops_seed created them.
OLD_PING = """{{ ping_target }}\n**{{ fleet_type }}**\nFC: {{ fc }}\nForm Up: {{ formup }}\nDoctrine: {{ doctrine }}\nComms: {{ comms }}{% if comms_link %} — {{ comms_link }}{% endif %}\n{% if logi_channel %}Logi: {{ logi_channel }}\n{% endif %}{% if boost_channel %}Boost: {{ boost_channel }}\n{% endif %}{% if fleet_time %}Time: {{ fleet_time }}\n{% endif %}{% if additional_message %}\n{{ additional_message }}{% endif %}"""
OLD_MOTD = """<b>{{ fleet_type }}</b><br>FC: {{ fc }}<br>Form Up: {{ formup }}<br>Doctrine: {{ doctrine }}<br>Comms: {{ comms }}{% if comms_link %} — {{ comms_link }}{% endif %}{% if logi_channel %}<br>Logi: {{ logi_channel }}{% endif %}{% if boost_channel %}<br>Boost: {{ boost_channel }}{% endif %}{% if additional_message %}<br><br>{{ additional_message }}{% endif %}"""

NEW_PING = """{% if ping_mention %}{{ ping_mention }}
{% endif %}{% if additional_message %}{{ additional_message }}

{% endif %}**FC Name:** {{ fc }}
**Formup Location:** {{ formup }}
**PAP Type:** {{ fleet_type }}
{% if comms %}**Comms:** {% if comms_link %}[{{ comms }}]({{ comms_link }}){% else %}{{ comms }}{% endif %}
{% endif %}{% if doctrine_name %}**Doctrine:** {% if doctrine_url %}[{{ doctrine_name }}]({{ doctrine_url }}){% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}
{% endif %}{% if is_pre_ping %}**Fleet Time:** {{ fleet_time_eve }} ({{ fleet_time_discord }})
{% endif %}
-# This was a FleetOps broadcast from {{ sender }} to {{ ping_target_label }} at {{ sent_at }} EVE"""
NEW_MOTD = """<font color="#bfffffff"><b>{{ fleet_type }}</b><br>FC: {{ fc }}<br>Form Up: {{ formup }}{% if doctrine_name %}<br>Doctrine: {% if doctrine_url %}<a href="{{ doctrine_url }}">{{ doctrine_name }}</a>{% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}{% endif %}{% if comms %}<br>Comms: {% if comms_link %}<a href="{{ comms_link }}">{{ comms }}</a>{% else %}{{ comms }}{% endif %}{% endif %}{% if logi_channel %}<br>Logi: {{ logi_channel_link }}{% endif %}{% if boost_channel %}<br>Boost: {{ boost_channel_link }}{% endif %}{% if additional_message %}<br><br>{{ additional_message|linebreaksbr }}{% endif %}</font>"""

DEFAULT_TEMPLATES = (
    ("ping", "Default Ping", OLD_PING, NEW_PING),
    ("motd", "Default MOTD", OLD_MOTD, NEW_MOTD),
)


def fill_join_tokens(apps, schema_editor):
    CommsPreset = apps.get_model("fleetops", "CommsPreset")
    for pk in CommsPreset.objects.filter(join_token=None).values_list("pk", flat=True):
        CommsPreset.objects.filter(pk=pk).update(join_token=uuid.uuid4())


def strip_url_userinfo(url):
    """Remove ``user@`` or ``user:password@`` from a voice link; anything else stays as it is."""
    try:
        netloc = urlsplit(url).netloc
    except ValueError:
        return url
    if "@" not in netloc:
        return url
    prefix, _slashes, rest = url.partition("//")
    if not rest.startswith(netloc):
        return url
    return f"{prefix}//{netloc.rsplit('@', 1)[1]}{rest[len(netloc):]}"


def strip_voice_userinfo(apps, schema_editor):
    # The voice link is shown on the public join page, so a personal login must not stay in it.
    CommsPreset = apps.get_model("fleetops", "CommsPreset")
    for pk, url in CommsPreset.objects.filter(voice_url__contains="@").values_list("pk", "voice_url"):
        cleaned = strip_url_userinfo(url)
        if cleaned != url:
            CommsPreset.objects.filter(pk=pk).update(voice_url=cleaned)


def encode_voice_spaces(url):
    """Percent-encode the spaces inside a voice link with a supported scheme; anything else stays."""
    stripped = (url or "").strip()
    match = SCHEME_RE.match(stripped)
    if not match or match.group(1).lower() not in VOICE_URL_SCHEMES or not SPACE_RE.search(stripped):
        return url
    return SPACE_RE.sub(lambda space: quote(space.group()), stripped)


def encode_voice_url_spaces(apps, schema_editor):
    # Voice links with spaces, e.g. a Mumble channel path, could be saved before; they are refused now.
    CommsPreset = apps.get_model("fleetops", "CommsPreset")
    for pk, url in CommsPreset.objects.exclude(voice_url="").values_list("pk", "voice_url"):
        encoded = encode_voice_spaces(url)
        if encoded != url:
            CommsPreset.objects.filter(pk=pk).update(voice_url=encoded)


def extract_channel_links(apps, schema_editor):
    # Channel presets used to hold the HTML copied in game; the link and the name are now separate.
    ChannelPreset = apps.get_model("fleetops", "ChannelPreset")
    for preset in ChannelPreset.objects.filter(ingame_channel_id="", channel_value__icontains="joinchannel:"):
        match = JOIN_CHANNEL_RE.search(preset.channel_value)
        if match is None:
            continue
        text = LINK_TEXT_RE.search(preset.channel_value)
        name = html.unescape(text.group(1)).strip() if text else ""
        ChannelPreset.objects.filter(pk=preset.pk).update(
            ingame_channel_id=match.group(1),
            channel_value=(name or preset.name)[:255],
        )


def _mention_mode(value):
    if "@everyone" in value:
        return "everyone"
    if "@here" in value:
        return "here"
    if ROLE_MENTION_RE.search(value):
        return "roles"
    return "none"


def _role_ids(value):
    found = []
    for raw in ROLE_MENTION_RE.findall(value):
        role_id = int(raw)
        if 0 < role_id <= MAX_DATABASE_ID and role_id not in found:
            found.append(role_id)
    return found


def convert_ping_targets(apps, schema_editor):
    # A ping target was a webhook plus a mention text. The webhook becomes a ping channel that allows the
    # mention, role mentions become ping roles, and fleets sent through the target keep what they pinged.
    # Any other text in the target is dropped; the fleets' ping texts are already stored.
    PingTarget = apps.get_model("fleetops", "PingTarget")
    PingRole = apps.get_model("fleetops", "PingRole")
    DiscordWebhook = apps.get_model("fleetops", "DiscordWebhook")
    FleetOperation = apps.get_model("fleetops", "FleetOperation")
    for target in PingTarget.objects.order_by("pk"):
        value = target.target_value or ""
        mode = _mention_mode(value)
        role_ids = _role_ids(value)
        roles = []
        for role_id in role_ids:
            role, _created = PingRole.objects.get_or_create(
                role_id=role_id,
                defaults={
                    "name": (target.name if len(role_ids) == 1 else f"Role {role_id}")[:100],
                    "source": "manual",
                    "is_active": True,
                },
            )
            roles.append({"id": str(role.role_id), "name": role.name})

        webhook = DiscordWebhook.objects.filter(pk=target.webhook_id).first() if target.webhook_id else None
        # Only an active target allows a mass mention in its channel from now on.
        if webhook is not None and target.is_active:
            if mode == "everyone" and not webhook.allow_everyone:
                DiscordWebhook.objects.filter(pk=webhook.pk).update(allow_everyone=True)
            elif mode == "here" and not webhook.allow_here:
                DiscordWebhook.objects.filter(pk=webhook.pk).update(allow_here=True)

        FleetOperation.objects.filter(ping_target=target, ping_channel=None).update(
            ping_channel=webhook,
            ping_channel_name=webhook.name if webhook is not None else "",
            ping_mention_mode=mode,
            ping_roles=roles if mode == "roles" else [],
        )


def _normalized(content):
    return (content or "").replace("\r\n", "\n").strip()


def update_untouched_default_templates(apps, schema_editor):
    # Templates an admin has edited are left alone; they keep working with the new variables.
    MessageTemplate = apps.get_model("fleetops", "MessageTemplate")
    for template_type, name, old, new in DEFAULT_TEMPLATES:
        for pk, content in MessageTemplate.objects.filter(template_type=template_type, name=name).values_list(
            "pk", "content"
        ):
            if _normalized(content) == _normalized(old):
                MessageTemplate.objects.filter(pk=pk).update(content=new)


def link_channels_in_motd_templates(apps, schema_editor):
    # {{ logi_channel }} and {{ boost_channel }} are now the plain channel names, so an edited MOTD would
    # lose the in-game channel link that extract_channel_links moved out of the preset.
    MessageTemplate = apps.get_model("fleetops", "MessageTemplate")
    for pk, content in MessageTemplate.objects.filter(template_type="motd").values_list("pk", "content"):
        linked = CHANNEL_OUTPUT_RE.sub(lambda tag: "{{ %s_link }}" % tag.group(1), content or "")
        if linked != content:
            MessageTemplate.objects.filter(pk=pk).update(content=linked)


def forward(apps, schema_editor):
    fill_join_tokens(apps, schema_editor)
    strip_voice_userinfo(apps, schema_editor)
    encode_voice_url_spaces(apps, schema_editor)
    extract_channel_links(apps, schema_editor)
    convert_ping_targets(apps, schema_editor)
    update_untouched_default_templates(apps, schema_editor)
    link_channels_in_motd_templates(apps, schema_editor)


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0008_ping_channels_and_messages"),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
