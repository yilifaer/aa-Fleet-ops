# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""SRP links: the link of a fleet, standing links, opening, closing and completing them, the links a member can
use and the fleet page card.

A link is open while members can submit through it. Closing stops new claims. A closed link is completed once all
its claims are settled (paid, rejected or withdrawn); completing a link that still has a claim waiting for review or
payment is refused, with no way around it.
"""

import secrets
from datetime import date, datetime, timedelta, timezone as dt_timezone

from allianceauth.services.hooks import get_extension_logger
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils import timezone

from fleetops import texts
from fleetops.models import (
    AttendanceRecord,
    FleetMemberState,
    FleetOperation,
    FleetOpsSettings,
    PapRegistration,
    SrpCategory,
    SrpClaim,
    SrpFleetLink,
    SrpSettings,
)
from fleetops.services import group_access
from fleetops.services import links as site_links
from fleetops.services.audit import audit
from fleetops.srp import access, quota, summaries
from fleetops.srp.types import BulkResult, SrpError, SrpStateError
from fleetops.validators import render_link_name

logger = get_extension_logger(__name__)

# Attempts to find a free random code; a collision of 96 random bits will not happen in practice.
CODE_ATTEMPTS = 5
CODE_BYTES = 12
# Claims listed on the fleet page card.
CARD_CLAIMS = 25
# A fleet page shows the card for fleets in these states: the link exists from the start of the fleet.
CARD_STATUSES = (
    FleetOperation.Status.STARTING,
    FleetOperation.Status.ACTIVE,
    FleetOperation.Status.ENDING,
    FleetOperation.Status.CLOSED,
)


def _fc_main(operation):
    """``(id, name)`` of the FC's main character when the link is created: the fleet's own snapshot, else
    the FC user's current main."""
    if operation.fc_main_character_id:
        return operation.fc_main_character_id, operation.fc_main_character_name or ""
    main = getattr(getattr(operation.fc_user, "profile", None), "main_character", None)
    if main is not None:
        return main.character_id, main.character_name or ""
    return None, ""


def ensure_fleet_link(operation: FleetOperation, *, actor=None) -> tuple[SrpFleetLink, bool]:
    """The fleet's link and whether it was created now; one link per fleet, so a second call returns the
    first. Retries the random code on a collision."""
    existing = SrpFleetLink.objects.filter(operation=operation).first()
    if existing is not None:
        return existing, False
    main_id, main_name = _fc_main(operation)
    for _attempt in range(CODE_ATTEMPTS):
        try:
            with transaction.atomic():
                link = SrpFleetLink.objects.create(
                    operation=operation,
                    code=secrets.token_urlsafe(CODE_BYTES),
                    fc_user=operation.fc_user,
                    fc_main_character_id=main_id,
                    fc_main_character_name=main_name,
                    created_by=actor,
                )
        except IntegrityError:
            # Either another request created the fleet's link first, or the code is taken. A locking read sees
            # the rows other requests have committed, also inside a transaction that started before them.
            with transaction.atomic():
                existing = SrpFleetLink.objects.select_for_update().filter(operation=operation).first()
            if existing is not None:
                return existing, False
            continue
        audit(
            actor,
            "srp.link_create",
            link,
            None,
            {"operation_id": operation.pk, "fc_user_id": link.fc_user_id, "is_open": link.is_open},
        )
        return link, True
    raise RuntimeError("No free SRP link code was found.")


def link_path(link: SrpFleetLink) -> str:
    """The site-relative URL members open to claim a loss of the fleet."""
    return reverse("fleetops:srp_link", args=[link.code])


def link_url(link: SrpFleetLink) -> str:
    """The link with the site's address in front, to paste into a message; a path without ``SITE_URL``."""
    return site_links.absolute_url(link_path(link))


# Claims in these statuses are not settled: a link with one of them cannot be completed.
UNSETTLED_STATUSES = (SrpClaim.Status.PENDING, SrpClaim.Status.APPROVED)
# Links one bulk action may touch.
MAX_BULK = 100
BULK_ACTIONS = ("close", "complete", "open")
NAME_LENGTH = 255


def _iso(value):
    return value.isoformat() if value else None


def _state_values(link, *, completion=False, closer=True) -> dict:
    """The fields that open and close a link, as the audit log keeps them (``closer`` leaves out who closed it, as the
    new values of an open or close always did); with ``completion`` also those of its completion."""
    values = {"is_open": link.is_open, "closed_at": _iso(link.closed_at)}
    if closer:
        values["closed_by_id"] = link.closed_by_id
    if completion:
        values.update(completed_at=_iso(link.completed_at), completed_by_id=link.completed_by_id)
    return values


def _require(actor, allowed, refusal) -> None:
    """``SrpError`` unless ``actor`` passes ``allowed``; a missing actor (the system) always does."""
    if actor is not None and not allowed(actor):
        raise SrpError("forbidden", refusal())


def _require_staff(actor) -> None:
    _require(actor, access.is_srp_staff, texts.SRP_NOT_STAFF)


def _require_reviewer(actor) -> None:
    _require(actor, access.is_reviewer, texts.SRP_NOT_A_REVIEWER)


def open_claim_counts(link) -> tuple[int, int]:
    """``(pending, approved)``: the claims of the link that wait for review and those that wait for payment."""
    rows = (
        SrpClaim.objects.filter(link_id=link.pk, status__in=UNSETTLED_STATUSES)
        .order_by()
        .values_list("status")
        .annotate(total=Count("pk"))
    )
    counts = dict(rows)
    return counts.get(SrpClaim.Status.PENDING, 0), counts.get(SrpClaim.Status.APPROVED, 0)


def _close_locked(link, *, actor, reason="") -> None:
    """Close a locked, open link and audit it."""
    old = _state_values(link)
    link.is_open = False
    link.closed_at = timezone.now()
    link.closed_by = actor
    link.save(update_fields=["is_open", "closed_at", "closed_by"])
    audit(actor, "srp.link_close", link, old, _state_values(link, closer=False), reason)


def _open_locked(link, *, actor, reason="") -> None:
    """Open a locked, closed or completed link and audit it; opening clears the completion."""
    completed = link.completed_at is not None
    old = _state_values(link, completion=completed)
    link.is_open = True
    link.closed_at = None
    link.closed_by = None
    link.completed_at = None
    link.completed_by = None
    link.save(update_fields=["is_open", "closed_at", "closed_by", "completed_at", "completed_by"])
    audit(actor, "srp.link_open", link, old, _state_values(link, completion=completed, closer=False), reason)


def _complete_locked(link, *, actor, reason="") -> None:
    """Close (when open) and complete a locked link whose claims are settled, and audit both steps."""
    if link.is_open:
        _close_locked(link, actor=actor)
    old = _state_values(link, completion=True)
    link.completed_at = timezone.now()
    link.completed_by = actor
    link.save(update_fields=["completed_at", "completed_by"])
    audit(actor, "srp.link_complete", link, old, _state_values(link, completion=True), reason)


def _uncomplete_locked(link, *, reason="") -> None:
    """A completed link has an unsettled claim again: it is closed, no longer completed."""
    old = _state_values(link, completion=True)
    link.completed_at = None
    link.completed_by = None
    link.save(update_fields=["completed_at", "completed_by"])
    audit(None, "srp.link_uncomplete", link, old, _state_values(link, completion=True), reason)


def _apply_open(link_pk, *, actor, is_open, reason="") -> tuple[SrpFleetLink, bool]:
    """Open or close the link under a row lock; the locked row and whether anything changed."""
    with transaction.atomic():
        current = SrpFleetLink.objects.select_for_update().get(pk=link_pk)
        if is_open and current.origin == SrpFleetLink.Origin.AASRP:
            # What aa-srp recorded stays recorded: members cannot claim through a link that was imported.
            raise SrpStateError("imported", texts.SRP_IMPORTED_LINK_CLOSED())
        changed = current.is_open != bool(is_open)
        if changed:
            if is_open:
                _open_locked(current, actor=actor, reason=reason)
            else:
                _close_locked(current, actor=actor, reason=reason)
        if not is_open:
            # A closed link may now be completed (when the switch is on).
            transaction.on_commit(lambda: maybe_complete(link_pk))
    return current, changed


def set_link_open(link: SrpFleetLink, *, actor, is_open: bool, reason: str = "") -> SrpFleetLink:
    """Open or close the link. Closing stops new claims through it; claims already made stay as they are.
    Opening clears the completion. Setting the state the link already has changes and audits nothing. ``actor``
    is None for the system (closing at the set time). The views decide who may open (reviewers) and close (SRP
    staff)."""
    current, _changed = _apply_open(link.pk, actor=actor, is_open=bool(is_open), reason=reason)
    for field in ("is_open", "closed_at", "closed_by", "completed_at", "completed_by"):
        setattr(link, field, getattr(current, field))
    return link


def complete_link(link: SrpFleetLink, *, actor) -> SrpFleetLink:
    """Mark a link completed. There is no way around the rule: a link with a claim waiting for review or for
    payment is refused and stays as it is, also when it is still open.

    Without such a claim an open link is closed first. The check and the change happen under the lock of the
    link and of the SRP settings, the lock every submission takes, so a claim submitted at the same moment either
    is seen here or finds the link closed.
    """
    _require_staff(actor)
    with quota.quota_lock():
        current = SrpFleetLink.objects.select_for_update().get(pk=link.pk)
        if current.completed_at is not None and not current.is_open:
            raise SrpStateError("already_completed", texts.SRP_LINK_ALREADY_COMPLETED())
        pending, approved = open_claim_counts(current)
        if pending or approved:
            raise SrpStateError(
                "link_has_open_claims", texts.SRP_LINK_HAS_OPEN_CLAIMS(pending=pending, approved=approved)
            )
        _complete_locked(current, actor=actor)
    for field in ("is_open", "closed_at", "closed_by", "completed_at", "completed_by"):
        setattr(link, field, getattr(current, field))
    return link


def maybe_complete(link_id) -> str:
    """Bring the completion of a link in line with its claims; run after the claims or the link changed.

    - A closed link without unsettled claims is completed when ``auto_complete_links`` is on (actor: none).
    - A completed link that has an unsettled claim again (a payment undone, a claim reopened or submitted
      again) goes back to closed, whatever the switch says.

    Returns ``"completed"``, ``"uncompleted"`` or ``""``. A failure is logged with the exception class only and
    never reaches the person whose action triggered this.
    """
    try:
        state = SrpFleetLink.objects.filter(pk=link_id).values_list("is_open", "completed_at").first()
        if state is None or state[0]:
            return ""
        if state[1] is None and not SrpSettings.get_solo().auto_complete_links:
            return ""
        with quota.quota_lock() as srp:
            link = SrpFleetLink.objects.select_for_update().filter(pk=link_id).first()
            if link is None or link.is_open:
                return ""
            pending, approved = open_claim_counts(link)
            unsettled = bool(pending or approved)
            if link.completed_at is not None:
                if unsettled:
                    _uncomplete_locked(link, reason=texts.SRP_LINK_AUTO_UNCOMPLETED())
                    return "uncompleted"
                return ""
            if srp.auto_complete_links and not unsettled:
                _complete_locked(link, actor=None, reason=texts.SRP_LINK_AUTO_COMPLETED())
                return "completed"
    except Exception as exc:  # noqa: BLE001 - runs after the commit of someone else's action
        logger.warning("SRP link %s: the completion check failed (%s).", link_id, type(exc).__name__)
    return ""


def settle_after_commit(*link_ids) -> None:
    """Run ``maybe_complete`` for the links once the current transaction is committed."""
    for link_id in {pk for pk in link_ids if pk}:
        transaction.on_commit(lambda link_id=link_id: maybe_complete(link_id))


# The minute beat runs the maintenance of the standing links at most once in this many seconds (cache key).
STANDING_LINKS_KEY = "fleetops:srp:standing-links"
STANDING_LINKS_SECONDS = 600


def auto_close_time(category, link_date):
    """When a standing link of the category closes by itself: ``auto_close_after_hours`` after 00:00 EVE of its
    date; None when the category never closes its links."""
    hours = getattr(category, "auto_close_after_hours", None)
    if not hours:
        return None
    return datetime.combine(link_date, datetime.min.time(), tzinfo=dt_timezone.utc) + timedelta(hours=hours)


def _has_standing_link(category, link_date) -> bool:
    """Whether the category has a standing link (made by hand or by the upkeep) for the date."""
    return SrpFleetLink.objects.filter(kind=SrpFleetLink.Kind.STANDING, category=category, link_date=link_date).exists()


def maintain_standing_links(now=None) -> tuple[int, int]:
    """The daily upkeep of the standing links; returns ``(links created, links closed)``.

    While SRP is enabled, every active category with ``auto_create_daily`` that has no standing link for today (EVE
    date) gets one (the system makes it: no actor, ``auto_created``); past days are never filled in. Then every open
    standing link whose ``auto_close_at`` has passed is closed (audited as ``srp.link_close`` without an actor).
    Closing only stops new claims; claims that exist go on to review and payment. Safe to run twice and from two
    workers at once: the daily key allows one link per category and day.
    """
    now = now or timezone.now()
    created = closed = 0
    if SrpSettings.get_solo().enabled:
        today = eve_today(now)
        for category in SrpCategory.objects.filter(is_active=True, auto_create_daily=True).order_by("sort_order", "pk"):
            if _has_standing_link(category, today):
                continue
            key = daily_key(category, today)
            link = create_standing_link(
                actor=None,
                category=category,
                link_date=today,
                doctrine_text=category.default_doctrine_text,
                auto_close_at=auto_close_time(category, today),
                daily=True,
                auto_created=True,
            )
            # Another worker may have made the link between the check and the creation.
            created += 1 if link.daily_key == key and link.auto_created else 0
    due = SrpFleetLink.objects.filter(
        kind=SrpFleetLink.Kind.STANDING, is_open=True, auto_close_at__isnull=False, auto_close_at__lte=now
    )
    for link in due:
        set_link_open(link, actor=None, is_open=False, reason=texts.SRP_LINK_AUTO_CLOSED())
        closed += 1
    return created, closed


def link_label(link) -> str:
    """The name of a link for lists and messages: its name, else the fleet, else its code."""
    if link.name:
        return link.name
    if link.operation_id is not None:
        from fleetops.srp.claims import fleet_label

        return fleet_label(link.operation)
    return str(link)


def eve_today(now=None) -> date:
    """The current date in EVE time (UTC)."""
    return (now or timezone.now()).astimezone(dt_timezone.utc).date()


def eve_date_of(moment) -> date:
    """The EVE (UTC) date of a moment."""
    return moment.astimezone(dt_timezone.utc).date()


def resolve_standing_link(category, eve_date) -> SrpFleetLink | None:
    """The open standing link of ``category`` for the EVE date; the one made last when there are several. None when
    the category has none that day (or no category is given)."""
    if category is None or eve_date is None:
        return None
    return (
        SrpFleetLink.objects.filter(
            kind=SrpFleetLink.Kind.STANDING, category=category, link_date=eve_date, is_open=True
        )
        .select_related("category")
        .order_by("-created_at", "-pk")
        .first()
    )


# Standing links the "My SRP" card lists.
STANDING_LINKS_SHOWN = 30


def open_standing_links(limit: int = STANDING_LINKS_SHOWN) -> list[SrpFleetLink]:
    """The open standing links for the "My SRP" card: newest date first, then by category; at most ``limit``."""
    return list(
        SrpFleetLink.objects.filter(kind=SrpFleetLink.Kind.STANDING, is_open=True)
        .select_related("category")
        .order_by("-link_date", "category__sort_order", "category__name", "-pk")[:limit]
    )


def daily_key(category, link_date) -> str:
    return f"{category.pk}:{link_date.isoformat()}"


def default_link_name(category, link_date) -> str:
    """The name a standing link gets when none is given: the category's template filled in."""
    template = category.daily_link_name or "{category} {date}"
    return render_link_name(template, category=category.name, date=link_date.isoformat())[:NAME_LENGTH]


def _validation_error(exc: ValidationError) -> SrpError:
    details = " ".join(f"{message}" for message in exc.messages)
    return SrpError("invalid", details)


def create_standing_link(
    *,
    actor,
    name="",
    category,
    link_date=None,
    doctrine_text="",
    aar_url="",
    auto_close_at=None,
    daily=False,
    auto_created=False,
) -> SrpFleetLink:
    """Create a standing link: a link without a fleet that collects the losses of one day for a category.

    An empty ``name`` is made from the category's template. ``daily`` makes the link the one of the category and
    date; when that exists already (another worker, another click) the existing link is returned and nothing is
    created or audited. The FC fields stay empty, so the person who made the link is no FC; ``created_by`` holds
    them. Needs a reviewer unless ``actor`` is None (the system).
    """
    _require_reviewer(actor)
    if category is None:
        raise SrpError("category_required", texts.SRP_LINK_NO_DAILY_CATEGORY())
    link_date = link_date or eve_today()
    name = (name or "").strip() or default_link_name(category, link_date)
    key = daily_key(category, link_date) if daily else None
    for _attempt in range(CODE_ATTEMPTS):
        link = SrpFleetLink(
            kind=SrpFleetLink.Kind.STANDING,
            code=secrets.token_urlsafe(CODE_BYTES),
            name=name,
            category=category,
            link_date=link_date,
            doctrine_text=(doctrine_text or "").strip(),
            aar_url=(aar_url or "").strip(),
            auto_close_at=auto_close_at,
            auto_created=bool(auto_created),
            daily_key=key,
            created_by=actor,
        )
        try:
            link.full_clean(exclude=["operation", "code", "daily_key", "external_id", "created_by", "closed_by"])
        except ValidationError as exc:
            raise _validation_error(exc) from None
        try:
            with transaction.atomic():
                link.save()
        except IntegrityError:
            if key:
                with transaction.atomic():
                    existing = SrpFleetLink.objects.select_for_update().filter(daily_key=key).first()
                if existing is not None:
                    return existing
            continue
        audit(
            actor,
            "srp.link_create",
            link,
            None,
            {
                "kind": link.kind,
                "name": link.name,
                "category_id": category.pk,
                "link_date": link_date.isoformat(),
                "doctrine_text": link.doctrine_text,
                "aar_url": link.aar_url,
                "auto_close_at": _iso(auto_close_at),
                "daily": bool(daily),
                "auto_created": link.auto_created,
                "is_open": link.is_open,
            },
        )
        return link
    raise RuntimeError("No free SRP link code was found.")


# What each kind of link lets a reviewer change.
STANDING_FIELDS = ("name", "category", "link_date", "doctrine_text", "aar_url", "auto_close_at")
FLEET_FIELDS = ("doctrine_text", "aar_url")


def _field_value(link, field):
    value = getattr(link, field)
    if field == "category":
        return link.category_id
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def update_link(link: SrpFleetLink, *, actor, **fields) -> SrpFleetLink:
    """Change a link; reviewers only. A standing link can change name, category, date, doctrine, report and the
    time it closes; a fleet link only the doctrine text and the report. Claims already made keep their category
    and their fleet. Changing the category or date of the link that stands for a day ends that role (its daily
    key is cleared), so the day's link can be made again. Nothing changes, and nothing is audited, when no value
    differs."""
    _require_reviewer(actor)
    allowed = STANDING_FIELDS if link.kind == SrpFleetLink.Kind.STANDING else FLEET_FIELDS
    unknown = set(fields) - set(allowed)
    if unknown:
        raise SrpError("invalid", texts.SRP_LINK_NOT_EDITABLE())
    with transaction.atomic():
        current = SrpFleetLink.objects.select_for_update().get(pk=link.pk)
        old, new = {}, {}
        previous_key = current.daily_key
        for field, value in fields.items():
            if isinstance(value, str):
                value = value.strip()
            before = _field_value(current, field)
            setattr(current, field, value)
            after = _field_value(current, field)
            if before != after:
                old[field], new[field] = before, after
        if not new:
            return current
        if ("category" in new or "link_date" in new) and current.daily_key:
            current.daily_key = None
            old["daily_key"], new["daily_key"] = previous_key, None
        try:
            current.full_clean(exclude=["operation", "code", "daily_key", "external_id", "created_by", "closed_by"])
        except ValidationError as exc:
            raise _validation_error(exc) from None
        current.save()
        audit(actor, "srp.link_update", current, old, new)
    for field in set(new) - {"daily_key"}:
        setattr(link, field, getattr(current, field))
    return current


def bulk_link_action(codes, *, actor, action: str) -> BulkResult:
    """Close, complete or open links by code, each in its own transaction: one that cannot be done is skipped
    with the reason and the others go on. ``done`` holds the links changed, ``skipped`` ``(link, reason)``
    pairs. Opening is for reviewers only; at most ``MAX_BULK`` links."""
    if action not in BULK_ACTIONS:
        raise SrpError("invalid", texts.SRP_LINK_UNKNOWN_ACTION())
    if action == "open":
        _require_reviewer(actor)
    else:
        _require_staff(actor)
    codes = list(dict.fromkeys(code for code in codes if code))
    if len(codes) > MAX_BULK:
        raise SrpError("too_many", texts.SRP_LINK_TOO_MANY(count=MAX_BULK))
    found = {
        link.code: link
        for link in SrpFleetLink.objects.select_related("operation__fleet_type").filter(code__in=codes)
    }
    result = BulkResult()
    for code in codes:
        link = found.get(code)
        if link is None:
            result.skipped.append((code, texts.SRP_LINK_NOT_FOUND()))
            continue
        try:
            if action == "complete":
                complete_link(link, actor=actor)
            else:
                _current, changed = _apply_open(link.pk, actor=actor, is_open=action == "open")
                if not changed:
                    text = texts.SRP_LINK_ALREADY_OPEN() if action == "open" else texts.SRP_LINK_ALREADY_CLOSED()
                    result.skipped.append((link, text))
                    continue
        except SrpError as exc:
            result.skipped.append((link, exc.message))
            continue
        result.done.append(link)
    return result


def recent_links_for_user(user, *, days: int = 30, limit: int = 20) -> list[SrpFleetLink]:
    """Open links of fleets started in the last ``days`` days in which the user was a member, was granted
    PAP or registered for PAP, or that the user led; the newest fleets first."""
    since = timezone.now() - timedelta(days=days)
    mine = (
        Q(operation__fc_user=user)
        | Q(operation_id__in=FleetMemberState.objects.filter(auth_user=user).values("operation_id"))
        | Q(operation_id__in=AttendanceRecord.objects.filter(auth_user=user).values("operation_id"))
        | Q(operation_id__in=PapRegistration.objects.filter(auth_user=user).values("operation_id"))
    )
    return list(
        SrpFleetLink.objects.filter(
            mine,
            kind=SrpFleetLink.Kind.FLEET,
            operation__isnull=False,
            is_open=True,
            operation__started_at__gte=since,
        )
        .select_related("operation", "operation__fleet_type")
        .order_by("-operation__started_at", "-pk")[:limit]
    )


def link_made_at_start() -> bool:
    """Whether a fleet started now gets a FleetOps SRP link: SRP is enabled, the link is made automatically
    and the provider setting picks FleetOps SRP."""
    if not SrpSettings.get_solo().enabled:
        return False
    fleetops_settings = FleetOpsSettings.get_solo()
    return bool(fleetops_settings.srp_auto_create) and fleetops_settings.srp_provider in ("auto", "fleetops")


def _uses_fleetops_srp(operation, srp_settings) -> bool:
    """Whether a link created now would be a FleetOps link: SRP is enabled and the provider setting picks it."""
    if not srp_settings.enabled:
        return False
    return FleetOpsSettings.get_solo().srp_provider in ("auto", "fleetops")


def operation_srp_context(operation: FleetOperation, user) -> dict:
    """Template context of the SRP card on the fleet page.

    ``srp_card`` is missing when the card has nothing for this user, which costs no query for a user without
    SRP permissions who does not lead the fleet. Otherwise it holds the link and its state, what the user
    may do with it (``can_request``, ``can_toggle``, ``can_create``) and, for SRP managers, ``view_all_stats``
    and the fleet's FC, the claims of the fleet with their totals.
    """
    if operation.status not in CARD_STATUSES or not getattr(user, "is_authenticated", False):
        return {}
    wants_request = access.can_request(user)
    sees_everything = access.sees_everything(user)
    leads = operation.fc_user_id == user.pk
    from fleetops.services.fleet_access import can_manage_fleet

    manages = can_manage_fleet(user, operation)
    if not (wants_request or sees_everything or leads or manages):
        return {}

    srp_settings = SrpSettings.get_solo()
    link = SrpFleetLink.objects.filter(operation=operation).first()
    manager = access.is_srp_staff(user)
    # The fleet may carry a link of another SRP app; FleetOps then offers no second one.
    has_other_link = link is None and bool(operation.srp_reference or operation.srp_url)
    # PAP-only fleets never get an SRP link (the retry refuses them too).
    creatable = operation.send_ping or operation.is_manual
    can_create = bool(
        manages
        and creatable
        and link is None
        and not has_other_link
        and _uses_fleetops_srp(operation, srp_settings)
        # Group fleets get a link only when their group allows it (one query, group fleets only).
        and group_access.group_srp_allowed(operation)
    )
    if link is None and not can_create:
        return {}
    card = {
        "enabled": srp_settings.enabled,
        "link": link,
        "link_path": link_path(link) if link else "",
        "link_url": link_url(link) if link else "",
        "is_open": bool(link and link.is_open),
        "can_request": bool(link and link.is_open and wants_request and srp_settings.enabled),
        "can_toggle": bool(link and manager),
        "can_reopen": bool(link and access.is_reviewer(user)),
        "can_complete": bool(link and manager and link.state != SrpFleetLink.State.COMPLETED),
        "state": link.state if link else "",
        "can_copy": bool(link and (manages or leads or manager)),
        "can_create": can_create,
        "show_claims": False,
        "claims": [],
        "totals": None,
        "claims_hidden": 0,
    }
    if link and (sees_everything or link.fc_user_id == user.pk):
        claims = SrpClaim.objects.filter(link=link)
        totals = summaries.claims_totals(claims)
        listed = list(claims.exclude(status=SrpClaim.Status.WITHDRAWN).order_by("-submitted_at")[:CARD_CLAIMS])
        card.update(
            show_claims=True, totals=totals, claims=listed, claims_hidden=max(totals.claims - len(listed), 0)
        )
    return {"srp_card": card}
