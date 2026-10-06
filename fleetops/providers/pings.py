import re
from dataclasses import dataclass

import requests

from fleetops.models import DISCORD_WEBHOOK_URL_RE

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


def send_discord_webhook(webhook_url: str, content: str) -> PingResult:
    webhook_url = (webhook_url or "").strip()
    if not webhook_url:
        return PingResult(False, message="No Discord webhook is configured for this ping target.")
    # Rows saved before the URL was validated may point anywhere; never send to them.
    if not DISCORD_WEBHOOK_URL_RE.fullmatch(webhook_url):
        return PingResult(
            False, message="The configured webhook URL is not a Discord webhook URL, so nothing was sent."
        )
    try:
        response = requests.post(webhook_url, json={"content": content}, timeout=15, allow_redirects=False)
    except (requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema, requests.exceptions.InvalidURL):
        return PingResult(False, message="The configured Discord webhook URL is invalid.")
    except requests.RequestException as exc:
        # Exception text from requests/urllib3 embeds the request URL, which contains the token.
        return PingResult(False, message=f"Discord webhook request failed ({type(exc).__name__}).")
    if 200 <= response.status_code < 300:
        return PingResult(True, status_code=response.status_code)
    message = _redact(response.text, webhook_url)[:500] or f"Discord returned HTTP {response.status_code}."
    return PingResult(False, status_code=response.status_code, message=message)
