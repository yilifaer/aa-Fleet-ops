# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

import re
from dataclasses import dataclass

import requests

from fleetops import texts
from fleetops.models import DISCORD_WEBHOOK_URL_RE
from fleetops.services.mentions import DISCORD_MESSAGE_LIMIT, SUPPRESS_EMBEDS, discord_length

# The secret token is the path segment right after the numeric webhook id.
WEBHOOK_TOKEN_RE = re.compile(r"/webhooks/\d+/([^/?#\s]+)")


@dataclass(slots=True)
class PingResult:
    success: bool
    status_code: int | None = None
    message: str = ""


def _redact(text: str, webhook_url: str) -> str:
    match = WEBHOOK_TOKEN_RE.search(webhook_url)
    for secret in (webhook_url, match.group(1) if match else ""):
        if secret:
            text = text.replace(secret, "***")
    return text


def send_discord_webhook(webhook_url: str, content: str, allowed_mentions: dict | None = None) -> PingResult:
    """Post ``content`` to a Discord webhook.

    Only the mentions in ``allowed_mentions`` notify anybody; without it nobody is notified.
    """
    webhook_url = (webhook_url or "").strip()
    if not webhook_url:
        return PingResult(False, message=texts.PING_NO_WEBHOOK())
    # Rows saved before the URL was validated may point anywhere; never send to them.
    if not DISCORD_WEBHOOK_URL_RE.fullmatch(webhook_url):
        return PingResult(False, message=texts.PING_NOT_A_WEBHOOK())
    length = discord_length(content)
    if length > DISCORD_MESSAGE_LIMIT:
        # Discord would reject it; never cut a ping silently.
        return PingResult(False, message=texts.PING_TOO_LONG(length=length, limit=DISCORD_MESSAGE_LIMIT))
    payload = {
        "content": content,
        "allowed_mentions": allowed_mentions or {"parse": []},
        "flags": SUPPRESS_EMBEDS,
    }
    try:
        response = requests.post(webhook_url, json=payload, timeout=15, allow_redirects=False)
    except (requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema, requests.exceptions.InvalidURL):
        return PingResult(False, message=texts.PING_BAD_WEBHOOK())
    except requests.RequestException as exc:
        # Exception text from requests/urllib3 embeds the request URL, which contains the token.
        return PingResult(False, message=texts.PING_REQUEST_FAILED(error_type=type(exc).__name__))
    if 200 <= response.status_code < 300:
        return PingResult(True, status_code=response.status_code)
    message = _redact(response.text, webhook_url)[:500] or texts.PING_HTTP(status=response.status_code)
    return PingResult(False, status_code=response.status_code, message=message)
