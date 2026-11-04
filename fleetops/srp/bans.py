# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""SRP bans: a member who may not submit SRP claims for a while (or for good).

A ban is bound to the Alliance Auth account, so it covers every character of the member, and a member who moves to
another account is not followed. ``active_ban`` is the one place that decides whether a ban is in force; the
eligibility check asks it first, so previews, submissions, new submissions of withdrawn claims, the list of
discovered losses and member edits are all stopped. Withdrawing a pending claim stays possible.
"""

from datetime import datetime, timedelta, timezone as dt_timezone

from allianceauth.services.hooks import get_extension_logger
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext

from fleetops import texts
from fleetops.models import SrpBan, SrpClaim, SrpSettings
from fleetops.services.audit import audit
from fleetops.services.periods import format_eve
from fleetops.srp import access, notify
from fleetops.srp.types import (
    LIMIT_CATEGORY_PERSONAL,
    LIMIT_PERSONAL,
    BulkResult,
    SrpError,
    SrpStateError,
)
from fleetops.texts import translate_stored

logger = get_extension_logger(__name__)

MAX_REASON_LENGTH = 1000
MAX_NOTE_LENGTH = 1000
# Limits that are the member's own: using up a corporation limit is not the member's doing.
PERSONAL_LIMIT_KINDS = (LIMIT_PERSONAL, LIMIT_CATEGORY_PERSONAL)


def active_ban(user, now=None) -> SrpBan | None:
    """The ban in force for ``user`` (one query), or None.

    In force: not lifted, started, and not yet expired (a ban without an end date never expires). When several are
    in force, the one made last.
    """
    if user is None or getattr(user, "pk", None) is None:
        return None
    now = now or timezone.now()
    return (
        SrpBan.objects.filter(user_id=user.pk, lifted_at__isnull=True, starts_at__lte=now)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .order_by("-created_at", "-pk")
        .first()
    )


def ban_message(ban) -> str:
    """The sentence a banned member reads when a claim is refused (in the active language)."""
    reason = translate_stored(ban.reason)
    if ban.expires_at is None:
        return gettext("You are banned from submitting SRP claims: %(reason)s") % {"reason": reason}
    return gettext("You are banned from submitting SRP claims until %(until)s EVE: %(reason)s") % {
        "until": format_eve(ban.expires_at),
        "reason": reason,
    }


def _require_reviewer(actor) -> None:
    if actor is None or not access.is_reviewer(actor):
        raise SrpError("forbidden", texts.SRP_BAN_NOT_ALLOWED())


def _main_name(user) -> str:
    profile = getattr(user, "profile", None)
    main = getattr(profile, "main_character", None)
    return getattr(main, "character_name", "") or ""


def _iso(value):
    return value.isoformat() if value else None


def ban_user(
    user,
    *,
    actor,
    days=None,
    permanent=False,
    reason,
    internal_note="",
    reject_pending=False,
    reject_reason="",
    now=None,
) -> tuple[SrpBan, BulkResult]:
    """Ban ``user`` from submitting SRP claims; reviewers only, never oneself, not while a ban is in force.

    ``permanent`` makes the ban endless (``days`` must then be empty and ``ban_max_days`` does not limit it);
    otherwise ``days`` is between 1 and ``ban_max_days``. With ``reject_pending`` every pending claim of the member is
    rejected with ``reject_reason`` (required) after the ban is made, one by one with the usual rules; claims that
    changed in the meantime are skipped and listed in the returned ``BulkResult``. Approved claims are not touched.
    The reason is shown to the member and goes to the audit log; the internal note stays on the ban.
    Returns ``(ban, result)``.
    """
    from fleetops.srp import review

    _require_reviewer(actor)
    if user is None or user.pk == actor.pk:
        raise SrpError("self", texts.SRP_BAN_SELF())
    reason = (reason or "").strip()
    if not reason:
        raise SrpError("reason_required", texts.SRP_BAN_REASON_REQUIRED())
    if len(reason) > MAX_REASON_LENGTH:
        raise SrpError("reason_too_long", texts.SRP_REASON_TOO_LONG(max=MAX_REASON_LENGTH))
    internal_note = (internal_note or "").strip()
    if len(internal_note) > MAX_NOTE_LENGTH:
        raise SrpError("note_too_long", texts.SRP_BAN_NOTE_TOO_LONG(max=MAX_NOTE_LENGTH))
    srp = SrpSettings.get_solo()
    if permanent:
        if days:
            raise SrpError("days", texts.SRP_BAN_PERMANENT_OR_DAYS())
        days = None
    else:
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= srp.ban_max_days:
            raise SrpError("days", texts.SRP_BAN_DAYS(max=srp.ban_max_days))
    reject_reason = (reject_reason or "").strip()
    if reject_pending and not reject_reason:
        raise SrpError("reject_reason_required", texts.SRP_BAN_REJECT_REASON_REQUIRED())
    now = now or timezone.now()
    with transaction.atomic():
        # One ban at a time per member: concurrent requests queue up on the member's row.
        owner = get_user_model().objects.select_for_update().get(pk=user.pk)
        if active_ban(owner, now) is not None:
            raise SrpError("exists", texts.SRP_BAN_EXISTS())
        ban = SrpBan.objects.create(
            user=owner,
            main_character_name=_main_name(owner),
            reason=reason,
            internal_note=internal_note,
            kind=SrpBan.Kind.MANUAL,
            starts_at=now,
            expires_at=None if permanent else now + timedelta(days=days),
            created_by=actor,
        )
        pending_ids = (
            list(SrpClaim.objects.filter(user=owner, status=SrpClaim.Status.PENDING).order_by("pk").values_list("pk", flat=True))
            if reject_pending
            else []
        )
        transaction.on_commit(lambda: notify.notify_banned(ban))
    result = BulkResult()
    try:
        for claim_id in pending_ids:
            claim = SrpClaim.objects.filter(pk=claim_id).first()
            if claim is None:
                result.skipped.append((claim_id, texts.SRP_CLAIM_GONE()))
                continue
            try:
                result.done.append(review.reject_claim(claim, actor=actor, reason=reject_reason))
            except SrpError as exc:
                # Decided, withdrawn or moved while the ban was made.
                result.skipped.append((claim, exc.message))
    finally:
        audit(
            actor,
            "srp.ban_create",
            ban,
            None,
            {
                "user_id": ban.user_id,
                "expires_at": _iso(ban.expires_at),
                "days": days,
                "permanent": bool(permanent),
                "kind": ban.kind,
                "rejected_count": len(result.done),
            },
            reason,
        )
    return ban, result


def lift_ban(ban: SrpBan, *, actor, reason: str, now=None) -> SrpBan:
    """Lift a ban that is in force; reviewers only. The reason is required and goes to the audit log."""
    _require_reviewer(actor)
    reason = (reason or "").strip()
    if not reason:
        raise SrpError("reason_required", texts.SRP_BAN_LIFT_REASON_REQUIRED())
    if len(reason) > MAX_REASON_LENGTH:
        raise SrpError("reason_too_long", texts.SRP_REASON_TOO_LONG(max=MAX_REASON_LENGTH))
    now = now or timezone.now()
    with transaction.atomic():
        try:
            locked = SrpBan.objects.select_for_update().get(pk=ban.pk)
        except SrpBan.DoesNotExist:
            raise SrpError("missing", texts.SRP_BAN_NOT_FOUND()) from None
        if not locked.in_force(now):
            raise SrpStateError("not_in_force", texts.SRP_BAN_NOT_IN_FORCE())
        locked.lifted_at = now
        locked.lifted_by = actor
        locked.lift_reason = reason
        locked.save(update_fields=["lifted_at", "lifted_by", "lift_reason"])
        audit(
            actor,
            "srp.ban_lift",
            locked,
            {"expires_at": _iso(locked.expires_at), "lifted_at": None},
            {"lifted_at": _iso(locked.lifted_at), "lifted_by_id": actor.pk},
            reason,
        )
    for field in ("lifted_at", "lifted_by", "lift_reason"):
        setattr(ban, field, getattr(locked, field))
    return ban


# --- The automatic ban for a member who keeps submitting over the monthly cap -----------------------------------


def next_month_start(now) -> datetime:
    """00:00 UTC of the first day of the EVE month after ``now``."""
    now = now.astimezone(dt_timezone.utc)
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(year, month, 1, tzinfo=dt_timezone.utc)


def exhausted_personal_limit(calculation):
    """The personal monthly limit that is already used up before this claim, or None.

    Used up means: it applies to the claim, has a cap, and nothing is left of it after the approved and paid
    claims of the month without this one. A limit that only runs out because of this claim, and the limits of a
    corporation, do not count.
    """
    for state in calculation.limits:
        if (
            state.kind in PERSONAL_LIMIT_KINDS
            and state.applies
            and state.cap is not None
            and state.remaining == 0
        ):
            return state
    return None


def auto_ban_for_cap(user, claim, calculation, srp, *, now):
    """Ban ``user`` until the end of the EVE month when the settings say so and the claim was submitted although a
    personal monthly limit is used up. Call it inside ``quota.quota_lock()``, right after the claim was saved
    (it only writes to the database). Returns the new ban or None.

    The ban lasts until the end of the current EVE month (not the month of the loss). Nothing happens while a ban
    is in force already.
    """
    if not srp.auto_ban_over_monthly_cap:
        return None
    state = exhausted_personal_limit(calculation)
    if state is None or active_ban(user, now) is not None:
        return None
    from fleetops.srp.claims import limit_name

    ban = SrpBan.objects.create(
        user=user,
        main_character_name=_main_name(user),
        reason=texts.SRP_AUTO_BANNED_MONTHLY_CAP(limit=limit_name(state.kind)),
        kind=SrpBan.Kind.AUTO_MONTHLY_CAP,
        starts_at=now,
        expires_at=next_month_start(now),
        created_by=None,
    )
    audit(
        None,
        "srp.ban_create",
        ban,
        None,
        {
            "user_id": ban.user_id,
            "expires_at": _iso(ban.expires_at),
            "days": None,
            "permanent": False,
            "kind": ban.kind,
            "rejected_count": 0,
            "trigger_claim": claim.reference,
        },
        ban.reason,
    )
    transaction.on_commit(lambda: notify.notify_banned(ban))
    return ban

