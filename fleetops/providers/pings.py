from dataclasses import dataclass

import requests


@dataclass(slots=True)
class PingResult:
    success: bool
    status_code: int | None = None
    message: str = ""


def send_discord_webhook(webhook_url: str, content: str) -> PingResult:
    if not webhook_url:
        return PingResult(False, message="No Discord webhook is configured for this ping target.")
    try:
        response = requests.post(webhook_url, json={"content": content}, timeout=15)
    except requests.RequestException as exc:
        return PingResult(False, message=str(exc))
    if 200 <= response.status_code < 300:
        return PingResult(True, status_code=response.status_code)
    return PingResult(False, status_code=response.status_code, message=response.text[:500])
