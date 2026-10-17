# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Notices that are shown once, as a toast, on the next FleetOps page.

Alliance Auth's own messages stay as they are. A notice differs in two ways: it can carry one action (a
link such as "Undo payment…"), and it is kept in ``request.session``, so it survives the redirect that follows
a POST and is shown exactly once. ``push_notice`` stores it, ``pop_notices`` hands it over and forgets it, and the
``{% fleetops_notices %}`` tag of ``fleetops_ui`` writes the pending ones into the page as JSON for
``fleetops.js``.
"""

from django.utils.encoding import force_str
from django.utils.http import url_has_allowed_host_and_scheme

SESSION_KEY = "fleetops_notices"
MAX_NOTICES = 3
MAX_TEXT_LENGTH = 300
MAX_LABEL_LENGTH = 60

LEVELS = ("success", "info", "warning", "danger")
_LEVEL_ALIASES = {"ok": "success", "warn": "warning", "error": "danger", "bad": "danger"}


def normalize_level(level):
    """One of ``LEVELS``; Django's ``error`` and the tone names are accepted, anything else is ``info``."""
    level = str(level or "").strip().lower()
    level = _LEVEL_ALIASES.get(level, level)
    return level if level in LEVELS else "info"


def _safe_action_url(request, url):
    """The URL when it is a path or a link to this site; otherwise empty."""
    url = force_str(url or "").strip()
    if not url:
        return ""
    host = request.get_host() if request is not None else ""
    allowed = url_has_allowed_host_and_scheme(
        url,
        allowed_hosts={host} if host else None,
        require_https=bool(request is not None and request.is_secure()),
    )
    return url if allowed else ""


def _clean(request, notice):
    """A stored or new notice reduced to the five known keys, or ``None`` when it has no text."""
    if not isinstance(notice, dict):
        return None
    text = force_str(notice.get("text") or "").strip()[:MAX_TEXT_LENGTH]
    if not text:
        return None
    label = force_str(notice.get("action_label") or "").strip()[:MAX_LABEL_LENGTH]
    url = _safe_action_url(request, notice.get("action_url"))
    if not (label and url):
        label = url = ""
    return {"text": text, "level": normalize_level(notice.get("level")), "action_label": label, "action_url": url}


def _store(request):
    """``request.session``, or ``None`` for a request that has none."""
    try:
        return request.session
    except AttributeError:
        return None


def push_notice(request, text, level, action_label=None, action_url=None):
    """Keep a notice for the next FleetOps page of this user; at most three are kept, the newest win.

    The action needs a label and a URL (a path of this site or a link to its host); without both it is dropped.
    The text is shown as plain text, never as HTML. Nothing happens when the request has no ``session``.
    """
    store = _store(request)
    if store is None:
        return
    notice = _clean(
        request, {"text": text, "level": level, "action_label": action_label, "action_url": action_url}
    )
    if notice is None:
        return
    pending = [item for item in store.get(SESSION_KEY, []) if isinstance(item, dict)]
    pending.append(notice)
    store[SESSION_KEY] = pending[-MAX_NOTICES:]


def pop_notices(request):
    """The pending notices of this user, oldest first. They are removed, so a notice is shown once."""
    store = _store(request)
    if store is None:
        return []
    stored = store.pop(SESSION_KEY, [])
    if not isinstance(stored, list):
        return []
    cleaned = (_clean(request, item) for item in stored)
    return [notice for notice in cleaned if notice is not None][-MAX_NOTICES:]
