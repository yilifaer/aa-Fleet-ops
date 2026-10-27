# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Pings without a fleet (the Send Ping page): the checks, the record, the post to Discord and the audit entry.

A ping without a fleet creates no fleet record and no PAP; it is a ``StandalonePing`` row and one ``ping.send``
audit entry. This module and ``services.operations`` are the only places that post to Discord. Right before the
post the channel row is read again under a lock and the sender's right to it checked once more
(``group_access.channel_refusal``): the form may have been crafted, or an administrator may have switched the
channel off or reserved it for groups in the meantime. The lock also serialises the cooldown check of a
channel, so two leaders cannot both post within the cooldown. The webhook address is read from the channel
only for the post; the row and the audit entry never hold it (``PingResult.message`` is already scrubbed).
"""

import logging
from datetime import timedelta
from types import SimpleNamespace

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from fleetops import texts
from fleetops.constants import PING_COOLDOWN_SECONDS
from fleetops.models import DiscordWebhook, StandalonePing
from fleetops.providers.pings import PingResult, send_discord_webhook
from fleetops.services import group_access
from fleetops.services.audit import audit
from fleetops.services.errors import log_failure
from fleetops.services.mentions import (
    DISCORD_MESSAGE_LIMIT,
    MentionSelection,
    allowed_mentions,
    choice_values,
    limit_mass_mentions,
    selection_from_operation,
    snapshot_roles,
    validate_for_channel,
)
from fleetops.services.messages import render_standalone_ping
from fleetops.services.periods import format_eve

logger = logging.getLogger(__name__)

AUDIT_ACTION = "ping.send"
START_FLEET = "fleetops.start_fleet"
VIEW_AUDIT_LOG = "fleetops.view_audit_log"
# A ping still pending this long after it was created is "not confirmed": whatever posted it stopped before
# Discord answered, so nobody knows whether it went out.
UNCONFIRMED_AFTER = timedelta(minutes=5)
# The "Recent pings" list of the Send Ping page.
RECENT_DAYS = 30
RECENT_ROWS = 20
# Discord's own failure text, as kept in the row; longer text is cut for the audit entry.
AUDIT_REASON_LENGTH = 500


class PingRefused(PermissionError):
    """The user may not post this ping to this channel (now). The message is a stored text."""


class PingNotSent(ValueError):
    """The ping was not posted: its mentions or its length do not fit the channel, or the channel received a
    ping moments ago (``cooldown``). The message is a stored text."""

    def __init__(self, message, *, cooldown=False):
        super().__init__(message)
        self.cooldown = cooldown


def _sender_name(user) -> str:
    main = getattr(getattr(user, "profile", None), "main_character", None)
    return getattr(main, "character_name", "") or user.get_username()


def selection_of(ping) -> MentionSelection:
    """Who a ping notifies, from its stored mention mode and role snapshot."""
    return selection_from_operation(SimpleNamespace(ping_mention_mode=ping.mention_mode, ping_roles=ping.mention_roles))


def build_ping(user, cleaned_data, *, now=None) -> StandalonePing:
    """An unsaved ping from the cleaned data of the Send Ping form, sent by ``user`` (the preview and the send)."""
    group = cleaned_data.get("group")
    channel = cleaned_data.get("ping_channel")
    selection = cleaned_data.get("mention_selection") or MentionSelection()
    return StandalonePing(
        request_id=cleaned_data.get("request_id"),
        sender=user,
        sender_name=_sender_name(user),
        group=group,
        group_name=group.name if group is not None else "",
        channel=channel,
        channel_name=channel.name if channel is not None else "",
        template=cleaned_data.get("ping_template"),
        mention_mode=selection.mode,
        mention_roles=snapshot_roles(selection),
        fleet_time=cleaned_data.get("fleet_time"),
        formup=cleaned_data.get("formup") or "",
        doctrine_name=cleaned_data.get("doctrine_name") or "",
        doctrine_source=cleaned_data.get("doctrine_source") or "",
        doctrine_external_id=cleaned_data.get("doctrine_external_id") or "",
        comms=cleaned_data.get("comms"),
        additional_message=cleaned_data.get("additional_message") or "",
        created_at=now or timezone.now(),
    )


def ping_refusal(user, channel, group) -> str:
    """Why ``user`` may not post a new ping to ``channel`` for ``group`` (None: an alliance ping); "" if allowed.

    An alliance ping needs ``start_fleet`` (fleet managers without it only retry their fleets' pings); the rest is
    the channel rule of ``group_access.channel_refusal``.
    """
    if group is None and not user.has_perm(START_FLEET):
        return texts.CHANNEL_NOT_ALLOWED(name=channel.name)
    return group_access.channel_refusal(user, channel, group_id=group.pk if group is not None else None)


def cooldown_problem(channel, *, now=None) -> str:
    """The cooldown text when ``channel`` received a ping without a fleet less than the cooldown ago; else ""."""
    now = now or timezone.now()
    last = (
        StandalonePing.objects.filter(
            channel_id=channel.pk,
            status__in=(StandalonePing.Status.PENDING, StandalonePing.Status.SENT),
            created_at__gt=now - timedelta(seconds=PING_COOLDOWN_SECONDS),
        )
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    if last is None:
        return ""
    seconds = max(0, int((now - last).total_seconds()))
    wait = max(1, PING_COOLDOWN_SECONDS - seconds)
    return texts.PING_COOLDOWN(name=channel.name, seconds=seconds, wait=wait)


def _existing(request_id, user):
    """The ping already stored for this form, or None; a form id of another sender is refused."""
    existing = StandalonePing.objects.filter(request_id=request_id).first()
    if existing is not None and existing.sender_id != user.pk:
        raise PingRefused(texts.PING_REQUEST_USED())
    return existing


def _post(channel, ping, selection) -> PingResult:
    try:
        return send_discord_webhook(
            channel.webhook_url, limit_mass_mentions(ping.text, selection), allowed_mentions(selection)
        )
    except Exception as exc:
        # The provider answers every expected failure itself; this keeps the row from staying pending.
        log_failure(logger, "Ping without a fleet", exc)
        return PingResult(False, message=texts.PING_REQUEST_FAILED(error_type=type(exc).__name__))


def send_standalone_ping(*, user, cleaned_data, now=None) -> StandalonePing:
    """Check, record and post a ping without a fleet; return its row (status sent or failed).

    The same form sent twice (same ``request_id``) posts once: the second call returns the first row. Raises
    ``PingRefused`` when the user may not post to the channel for the group and ``PingNotSent`` when the ping does
    not fit the channel (mentions, length) or the channel is cooling down; nothing is stored then.
    """
    existing = _existing(cleaned_data["request_id"], user)
    if existing is not None:
        return existing
    group = cleaned_data.get("group")
    channel = cleaned_data["ping_channel"]
    selection = cleaned_data.get("mention_selection") or MentionSelection()
    refusal = ping_refusal(user, channel, group)
    if refusal:
        raise PingRefused(refusal)
    now = now or timezone.now()
    ping = build_ping(user, cleaned_data, now=now)
    rendered = render_standalone_ping(ping, actor=user, now=now)
    if rendered.blocking:
        raise PingNotSent(texts.PING_TOO_LONG(length=rendered.ping_length, limit=DISCORD_MESSAGE_LIMIT))

    with transaction.atomic():
        # The channel as it is now: switched off, reserved for other groups, other mentions or another webhook.
        locked = DiscordWebhook.objects.select_for_update().filter(pk=channel.pk).first()
        if locked is None:
            raise PingRefused(texts.PING_CHANNEL_GONE(name=channel.name))
        refusal = ping_refusal(user, locked, group)
        if refusal:
            raise PingRefused(refusal)
        problems = validate_for_channel(selection, locked)
        if problems:
            raise PingNotSent(" ".join(problems))
        problem = cooldown_problem(locked, now=now)
        if problem:
            raise PingNotSent(problem, cooldown=True)
        ping.channel = locked
        ping.channel_name = locked.name
        ping.text = rendered.ping
        ping.display_text = rendered.ping_display
        ping.status = StandalonePing.Status.PENDING
        try:
            with transaction.atomic():
                ping.save()
        except IntegrityError:
            # The same form was sent twice at the same moment; the other request posts it.
            existing = _existing(cleaned_data["request_id"], user)
            if existing is not None:
                return existing
            raise

    result = _post(locked, ping, selection)
    ping.status = StandalonePing.Status.SENT if result.success else StandalonePing.Status.FAILED
    ping.result_message = result.message or ""
    ping.http_status = result.status_code
    ping.sent_at = timezone.now() if result.success else None
    ping.save(update_fields=["status", "result_message", "http_status", "sent_at"])
    audit(
        user,
        AUDIT_ACTION,
        ping,
        None,
        {
            "group_id": ping.group_id,
            "group": ping.group_name,
            "channel_id": ping.channel_id,
            "channel": ping.channel_name,
            "mention_mode": ping.mention_mode,
            "roles": [role.get("name") or role.get("id") for role in ping.mention_roles],
            "fleet_time": ping.fleet_time.isoformat() if ping.fleet_time else "",
            "template_id": ping.template_id,
            "status": ping.status,
            "http_status": ping.http_status,
        },
        reason=ping.result_message[:AUDIT_REASON_LENGTH],
    )
    return ping


def visible_pings(user):
    """Every ping without a fleet ``user`` may see: their own and those of the groups they lead now; holders of
    ``view_audit_log`` see all (every ping is in the audit log anyway)."""
    pings = StandalonePing.objects.all()
    if user.has_perm(VIEW_AUDIT_LOG):
        return pings
    rule = Q(sender_id=user.pk)
    group_ids = group_access.visible_group_ids(user)
    if group_ids:
        rule |= Q(group_id__in=group_ids)
    return pings.filter(rule)


def recent_pings(user, *, mine_only=False, now=None) -> list:
    """The "Recent pings" of the Send Ping page: visible pings of the last 30 days, newest first, 20 at most."""
    now = now or timezone.now()
    pings = StandalonePing.objects.filter(sender_id=user.pk) if mine_only else visible_pings(user)
    pings = pings.filter(created_at__gte=now - timedelta(days=RECENT_DAYS))
    return list(pings.order_by("-created_at", "-pk")[:RECENT_ROWS])


def is_unconfirmed(ping, *, now=None) -> bool:
    """A ping still pending five minutes after it was created: nobody knows whether Discord got it."""
    now = now or timezone.now()
    return ping.status == StandalonePing.Status.PENDING and ping.created_at <= now - UNCONFIRMED_AFTER


def form_initial(ping, *, now=None) -> dict:
    """The Send Ping form filled in from an earlier ping ("Use again"); a fleet time that has passed is left out.

    The form decides again what is still offered: a channel, group or role the user can no longer use is not
    selected, and the ping is checked in full when it is sent.
    """
    now = now or timezone.now()
    doctrine_choice, custom = "", ping.doctrine_name
    if ping.doctrine_source not in ("", "custom", "none", "manual") and ping.doctrine_external_id:
        doctrine_choice = f"{ping.doctrine_source}|{ping.doctrine_external_id}|{ping.doctrine_name}"
        custom = ""
    initial = {
        "group": ping.group_id,
        "ping_channel": ping.channel_id,
        "ping_mentions": choice_values(selection_of(ping)),
        "ping_template": ping.template_id,
        "additional_message": ping.additional_message,
        "formup": ping.formup,
        "doctrine_choice": doctrine_choice,
        "custom_doctrine": custom,
        "comms": ping.comms_id,
    }
    if ping.fleet_time is not None and ping.fleet_time > now:
        initial["fleet_date"] = format_eve(ping.fleet_time, "%Y-%m-%d")
        initial["fleet_clock"] = format_eve(ping.fleet_time, "%H:%M")
    return initial
