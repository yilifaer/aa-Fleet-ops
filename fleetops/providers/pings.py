from dataclasses import dataclass
from urllib.parse import urlsplit

import requests


@dataclass(slots=True)
class PingResult:
    success: bool
    status_code: int | None = None
    message: str = ""


def _redact(text: str, webhook_url: str) -> str:
    # The last path segment of a Discord webhook URL is its secret token.
    token = urlsplit(webhook_url).path.rstrip("/").rpartition("/")[2]
    for secret in (webhook_url, token):
        if secret:
            text = text.replace(secret, "***")
    return text


def send_discord_webhook(webhook_url: str, content: str) -> PingResult:
    if not webhook_url:
        return PingResult(False, message="No Discord webhook is configured for this ping target.")
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
