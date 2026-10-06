# AA FleetOps 0.1.0a6: remove Discord webhook tokens from stored errors and keep FC incentives on upgrade

import re
from urllib.parse import parse_qsl, urlsplit

from django.db import migrations
from django.db.models import Q

# Releases up to 0.1.0a5 stored raw requests errors such as
# "Max retries exceeded with url: /api/webhooks/<id>/<token>", which carry the webhook token.
WEBHOOK_PATH_RE = re.compile(r"(/api/(?:v\d+/)?webhooks/\d+/)[\w-]+")
WEBHOOK_TOKEN_RE = re.compile(r"/webhooks/\d+/([^/?#\s]+)")
# Shorter values are not real tokens; replacing them everywhere would mangle ordinary words.
MIN_TOKEN_LENGTH = 16

STORED_ERRORS = (
    ("OperationAction", "error_message"),
    ("FleetOperation", "last_error"),
    ("FleetOperation", "srp_error"),
)


def stored_webhook_secrets(DiscordWebhook):
    secrets = set()
    for url in DiscordWebhook.objects.values_list("webhook_url", flat=True):
        url = (url or "").strip()
        if not url:
            continue
        secrets.add(url)
        match = WEBHOOK_TOKEN_RE.search(url)
        if match and len(match.group(1)) >= MIN_TOKEN_LENGTH:
            secrets.add(match.group(1))
        # Connection errors quote only the path and query ("with url: /hooks/relay?key=..."),
        # which is where a secret sits in URLs saved before webhook validation existed.
        parts = urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        if len(path) >= MIN_TOKEN_LENGTH:
            secrets.add(path)
        for _name, value in parse_qsl(parts.query):
            if len(value) >= MIN_TOKEN_LENGTH:
                secrets.add(value)
    # Longest first, so a full URL is replaced before the token inside it.
    return sorted(secrets, key=len, reverse=True)


def redact(text, secrets):
    text = WEBHOOK_PATH_RE.sub(r"\1***", text)
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


def redact_webhook_tokens(apps, schema_editor):
    secrets = stored_webhook_secrets(apps.get_model("fleetops", "DiscordWebhook"))
    for model_name, field in STORED_ERRORS:
        model = apps.get_model("fleetops", model_name)
        candidates = Q(**{f"{field}__contains": "webhooks/"})
        for secret in secrets:
            candidates |= Q(**{f"{field}__contains": secret})
        for pk, text in model.objects.filter(candidates).values_list("pk", field).iterator():
            cleaned = redact(text, secrets)
            if cleaned != text:
                # update() leaves auto_now timestamps such as updated_at untouched.
                model.objects.filter(pk=pk).update(**{field: cleaned})


def keep_incentives_for_existing_users(apps, schema_editor):
    # FC incentive pages now follow the "Incentive enabled" setting, which is off by default.
    # Installs that already have incentive periods keep the feature after upgrading.
    IncentivePeriod = apps.get_model("fleetops", "IncentivePeriod")
    if IncentivePeriod.objects.exists():
        FleetOpsSettings = apps.get_model("fleetops", "FleetOpsSettings")
        FleetOpsSettings.objects.update_or_create(pk=1, defaults={"incentive_enabled": True})


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0005_settings_validation_and_ordering"),
    ]

    operations = [
        migrations.RunPython(redact_webhook_tokens, migrations.RunPython.noop),
        migrations.RunPython(keep_incentives_for_existing_users, migrations.RunPython.noop),
    ]
