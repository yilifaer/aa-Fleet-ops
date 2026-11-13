# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Payment batches (REV-5) and the filters of the claims waiting for payment (REV-4).

A batch is made for every round of payments: ``review.mark_paid`` creates it in a transaction of its own, marks the
claims one by one and then writes the numbers of the moment of payment (claims, payees, total). They are not changed
when a payment is undone later. Only payments after the upgrade have a batch; older ones show a dash.
"""

from datetime import timedelta

from allianceauth.services.hooks import get_extension_logger
from django.db import transaction
from django.utils import timezone

from fleetops.models import SrpClaim, SrpPaymentBatch
from fleetops.services.audit import audit
from fleetops.srp import policy as rules
from fleetops.srp.types import SrpPayRow

logger = get_extension_logger(__name__)

RECENT_DAYS = 30
RECENT_SHOWN = 50
# A batch without claims older than this is left over from a payment that was cut short.
ORPHAN_AFTER = timedelta(hours=1)
# The filters a batch remembers; nothing but the values the page offers.
FILTER_NAMES = ("category", "link", "corporation")


def payee_key(claim) -> tuple:
    """Who gets the ISK of a claim: the account when there is one, else the main character (or the pilot)."""
    if claim.user_id:
        return ("user", claim.user_id)
    return ("main", claim.main_character_id or claim.character_id)


def clean_filters(filters) -> dict:
    """The filters of the payments page as a plain dict of the three known names (empty values left out)."""
    found = {}
    for name in FILTER_NAMES:
        value = (filters or {}).get(name)
        if value not in (None, ""):
            found[name] = value if isinstance(value, int) else str(value)[:64]
    return found


def parse_filters(data) -> dict:
    """The filters out of query or form data (``request.GET`` or ``request.POST``): a category ID or ``none``, a link
    code and a corporation ID. A value that does not look like one is dropped."""
    category = (data.get("category") or "").strip()
    corporation = (data.get("corporation") or "").strip()
    found = {
        "category": category
        if category == "none" or (category.isascii() and category.isdigit() and len(category) < 20)
        else "",
        "link": (data.get("link") or "").strip()[:32],
        "corporation": int(corporation) if corporation.isascii() and corporation.isdigit() and len(corporation) < 20 else None,
    }
    return clean_filters(found)


def apply_filters(claims, filters):
    """Narrow a claim queryset by the category, link and corporation filters (``clean_filters``)."""
    filters = filters or {}
    category = filters.get("category")
    if category == "none":
        claims = claims.filter(category__isnull=True)
    elif category not in (None, ""):
        try:
            claims = claims.filter(category_id=int(category))
        except (TypeError, ValueError):
            pass
    if filters.get("link"):
        claims = claims.filter(link__code=filters["link"])
    if filters.get("corporation"):
        claims = claims.filter(corporation_id=filters["corporation"])
    return claims


def create_batch(actor, note: str, filters=None) -> SrpPaymentBatch:
    """A new batch (with zero counts until ``finish_batch``), committed at once so the claims can point to it."""
    with transaction.atomic():
        return SrpPaymentBatch.objects.create(
            created_by=actor,
            created_by_name=payer_name(actor),
            note=note,
            filters=clean_filters(filters),
        )


def payer_name(user) -> str:
    """The name written on a batch: the main character of the payer, else the user name."""
    try:
        main = user.profile.main_character
    except Exception:
        main = None
    return (getattr(main, "character_name", "") or getattr(user, "username", "") or "")[:255]


def finish_batch(batch: SrpPaymentBatch, done, note: str) -> SrpPaymentBatch | None:
    """Write the numbers of the claims that were paid into the batch and audit it; an empty batch is deleted.

    Returns the batch, or None when it was deleted because nothing was paid.
    """
    done = list(done)
    if not done:
        delete_if_empty(batch)
        return None
    batch.claim_count = len(done)
    batch.payee_count = len({payee_key(claim) for claim in done})
    batch.total = sum(claim.approved_amount or 0 for claim in done)
    batch.save(update_fields=["claim_count", "payee_count", "total"])
    audit(
        batch.created_by,
        "srp.payment_batch",
        batch,
        None,
        {"count": batch.claim_count, "payees": batch.payee_count, "total": batch.total, "note_length": len(note)},
        note,
    )
    return batch


def delete_if_empty(batch: SrpPaymentBatch) -> bool:
    """Delete the batch when no claim points to it; returns whether it was deleted."""
    deleted, _by_model = SrpPaymentBatch.objects.filter(pk=batch.pk, claims__isnull=True).delete()
    return bool(deleted)


def repair_cut_batches(now=None) -> int:
    """Write the numbers of batches older than an hour that were never completed but did pay claims (a payment
    cut off in the middle, for example by a killed worker); returns how many were repaired."""
    cutoff = (now or timezone.now()) - ORPHAN_AFTER
    repaired = 0
    for batch in SrpPaymentBatch.objects.filter(created_at__lt=cutoff, claim_count=0, claims__isnull=False).distinct():
        if finish_batch(batch, SrpClaim.objects.filter(payment_batch=batch), "") is not None:
            repaired += 1
    return repaired


def prune_empty_batches(now=None) -> int:
    """Delete batches older than an hour that have no claims and were never completed (a payment cut short).

    A cut batch that did pay claims is completed first (``repair_cut_batches``), so it is listed and counted."""
    repair_cut_batches(now)
    cutoff = (now or timezone.now()) - ORPHAN_AFTER
    deleted, _by_model = SrpPaymentBatch.objects.filter(
        created_at__lt=cutoff, claim_count=0, claims__isnull=True
    ).delete()
    return deleted


def recent_batches(now=None):
    """The batches of the last 30 days that paid at least one claim, newest first (at most 50)."""
    since = (now or timezone.now()) - timedelta(days=RECENT_DAYS)
    return list(SrpPaymentBatch.objects.filter(created_at__gte=since, claim_count__gt=0).order_by("-created_at", "-pk")[:RECENT_SHOWN])


def batch_claims(batch):
    """The claims that are paid in the batch now, with what the detail page shows."""
    return list(
        SrpClaim.objects.filter(payment_batch=batch)
        .select_related("user__profile__main_character", "category", "link")
        .order_by("killmail_time", "pk")
    )


def undone_count(batch, current: int) -> int:
    """How many of the claims of the batch have had their payment undone since (the snapshot minus those still in it)."""
    return max(0, batch.claim_count - current)


def pay_rows(claims):
    """Approved claims grouped by the player to pay, the largest sums first.

    A row is named after the player's main character as it is now (``policy.pay_to``), not the one recorded
    when the claim was made. Read the claims with ``select_related("user__profile__main_character")``.
    """
    groups: dict[tuple, SrpPayRow] = {}
    for claim in claims:
        key = ("user", claim.user_id) if claim.user_id else ("main", claim.main_character_id or claim.character_id)
        row = groups.get(key)
        if row is None:
            main_id, name = rules.pay_to(claim)
            row = groups[key] = SrpPayRow(claim.user_id, main_id, name)
        amount = claim.approved_amount or 0
        row.total += amount
        # Claims without the ``payable`` mark (batch pages, exports) are all counted.
        if getattr(claim, "payable", True):
            row.payable_total += amount
        row.claims.append(claim)
    return sorted(groups.values(), key=lambda row: (-row.total, row.main_character_name.casefold()))
