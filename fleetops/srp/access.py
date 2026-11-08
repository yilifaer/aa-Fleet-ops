# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Who may see and decide what in FleetOps SRP.

Members see their own claims. ``view_all_stats`` sees every SRP summary and claim read-only,
``view_corp_stats`` the summary and the claims of its own corporation, FCs the claims and totals of the
fleets they led, and SRP staff everything. Staff are the holders of ``manage_srp`` (review and pay), of
``review_srp`` (review only) and of ``pay_srp`` (pay only). Only staff see internal notes; reviewers decide on
claims and payers pay them, and by default neither on their own claims.

A single object out of scope (a claim, a corporation, another FC's summary) is answered by the views with the
FleetOps 404 page, the same for an ID that exists and one that does not; a corporation manager's own corporation
by ID is their own summary. A missing permission is the FleetOps 403 page with its reason (``fleetops.views_errors``).
"""

from __future__ import annotations

from django.db.models import Exists, OuterRef, Q

from fleetops.models import SrpClaim, SrpClaimEvent, SrpFleetLink, SrpSettings
from fleetops.services import group_access
from fleetops.services.stats_access import VIEW_ALL_STATS, VIEW_CORP_STATS, own_corporation

REQUEST_SRP = "fleetops.request_srp"
MANAGE_SRP = "fleetops.manage_srp"
REVIEW_SRP = "fleetops.review_srp"
PAY_SRP = "fleetops.pay_srp"

# Permissions of people who lead fleets; the navigation only looks for fleets led by such users.
FC_PERMISSIONS = (
    "fleetops.start_fleet",
    "fleetops.manage_own_fleet",
    "fleetops.manage_fleets",
    "fleetops.create_manual_fleet",
)


def _signed_in(user) -> bool:
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.pk is not None)


def can_request(user) -> bool:
    """May submit SRP claims (``request_srp``)."""
    return _signed_in(user) and user.has_perm(REQUEST_SRP)


def is_reviewer(user) -> bool:
    """May review claims: approve, reject, change, reopen, move (``manage_srp`` or ``review_srp``)."""
    return _signed_in(user) and (user.has_perm(MANAGE_SRP) or user.has_perm(REVIEW_SRP))


def is_payer(user) -> bool:
    """May mark claims paid and undo a payment (``manage_srp`` or ``pay_srp``)."""
    return _signed_in(user) and (user.has_perm(MANAGE_SRP) or user.has_perm(PAY_SRP))


def is_srp_staff(user) -> bool:
    """Reviewers and payers: they see every claim and the internal notes."""
    return _signed_in(user) and any(user.has_perm(name) for name in (MANAGE_SRP, REVIEW_SRP, PAY_SRP))


# The name the pages used before the permission was split.
is_manager = is_srp_staff


def sees_everything(user) -> bool:
    """SRP staff and ``view_all_stats`` see every summary and claim; the latter read-only."""
    return is_srp_staff(user) or (_signed_in(user) and user.has_perm(VIEW_ALL_STATS))


def can_view_internal_notes(user) -> bool:
    """Internal notes of reviewers stay with the SRP staff."""
    return is_srp_staff(user)


def can_review(user, claim, *, settings_obj=None) -> bool:
    """May decide on ``claim`` (approve, reject, change the amount, revoke, reopen, reclassify, recategorize).

    Needs ``review_srp`` (or ``manage_srp``); a reviewer's own claim is decided by another reviewer unless the
    settings allow self review.
    """
    if not is_reviewer(user):
        return False
    if claim.user_id is None or claim.user_id != user.pk:
        return True
    return bool((settings_obj or SrpSettings.get_solo()).allow_self_review)


# Why ``user`` may not pay a claim; the codes of ``SrpError`` in ``review``.
NOT_A_PAYER = "forbidden"
OWN_CLAIM_PAY = "own_claim_pay"
OWN_APPROVAL_PAY = "own_approval_pay"


# The steps of a claim's history that make someone an approver: they set the amount that is paid.
APPROVER_ACTIONS = (SrpClaimEvent.Action.APPROVE, SrpClaimEvent.Action.AMOUNT_CHANGE)


def approver_ids(claim) -> set[int]:
    """The users who approved ``claim`` or changed its amount (from its history), and the user of its last decision.
    ``prefetch_approvers`` fills it for a list of claims in one query."""
    known = getattr(claim, "_approver_ids", None)
    if known is not None:
        return known
    ids = set(
        SrpClaimEvent.objects.filter(claim_id=claim.pk, action__in=APPROVER_ACTIONS, actor__isnull=False).values_list(
            "actor_id", flat=True
        )
    )
    if claim.decided_by_id is not None:
        ids.add(claim.decided_by_id)
    return ids


def prefetch_approvers(claims) -> None:
    """Load ``approver_ids`` of many claims with one query, so a list does not ask once per claim."""
    claims = [claim for claim in claims if claim.pk is not None]
    found: dict[int, set[int]] = {claim.pk: set() for claim in claims}
    if found:
        rows = SrpClaimEvent.objects.filter(
            claim_id__in=list(found), action__in=APPROVER_ACTIONS, actor__isnull=False
        ).values_list("claim_id", "actor_id")
        for claim_id, actor_id in rows:
            found[claim_id].add(actor_id)
    for claim in claims:
        ids = found[claim.pk]
        if claim.decided_by_id is not None:
            ids.add(claim.decided_by_id)
        claim._approver_ids = ids


def payment_block(user, claim, *, settings_obj=None, approver_rule=True) -> str:
    """Why ``user`` may not pay ``claim``, as a code (``""`` when they may).

    Needs ``pay_srp`` (or ``manage_srp``). A payer's own claim is paid by another payer unless
    ``allow_self_payment`` is on. With ``require_different_payer`` neither the approver of the claim nor anyone who
    changed its amount may pay it; ``approver_rule=False`` leaves that rule out (undoing a payment is not bound by it).
    """
    if not is_payer(user):
        return NOT_A_PAYER
    srp = settings_obj or SrpSettings.get_solo()
    if claim.user_id is not None and claim.user_id == user.pk and not srp.allow_self_payment:
        return OWN_CLAIM_PAY
    if approver_rule and srp.require_different_payer and user.pk in approver_ids(claim):
        return OWN_APPROVAL_PAY
    return ""


def can_pay(user, claim, *, settings_obj=None) -> bool:
    """May mark ``claim`` paid (see ``payment_block``)."""
    return not payment_block(user, claim, settings_obj=settings_obj)


def _sees_corporation_of(user, claim) -> bool:
    """``view_corp_stats`` of the corporation the claim was made for."""
    if not (claim.corporation_id and user.has_perm(VIEW_CORP_STATS)):
        return False
    corporation = own_corporation(user)
    return corporation is not None and corporation[0] == claim.corporation_id


def can_view_limits(user, claim) -> bool:
    """May read the monthly usage on the claim page: the claimant, managers, ``view_all_stats`` and
    ``view_corp_stats`` of the claim's corporation.

    The usage adds up the claimant's other claims and the corporation's, so the FC of an in-fleet claim, who
    sees the claims and totals of the fleets they led, does not see it.
    """
    if not _signed_in(user):
        return False
    if claim.user_id is not None and claim.user_id == user.pk:
        return True
    return sees_everything(user) or _sees_corporation_of(user, claim)


def can_view_claim(user, claim) -> bool:
    """The claimant, managers, ``view_all_stats``, the FC of an in-fleet claim and ``view_corp_stats`` of the
    corporation the claim was made for."""
    if not _signed_in(user):
        return False
    if can_view_limits(user, claim):
        return True
    return claim.scope == SrpClaim.Scope.IN_FLEET and claim.fc_user_id is not None and claim.fc_user_id == user.pk


def viewable_claims(user):
    """The claims ``can_view_claim`` lets ``user`` open, as a queryset."""
    claims = SrpClaim.objects.all()
    if not _signed_in(user):
        return claims.none()
    if sees_everything(user):
        return claims
    allowed = Q(user=user) | Q(scope=SrpClaim.Scope.IN_FLEET, fc_user=user)
    if user.has_perm(VIEW_CORP_STATS):
        corporation = own_corporation(user)
        if corporation is not None:
            allowed |= Q(corporation_id=corporation[0])
    return claims.filter(allowed)


def can_view_alliance_summary(user) -> bool:
    return sees_everything(user)


def can_view_corporation_summary(user, corporation_id) -> bool:
    """Managers and ``view_all_stats`` see every corporation, ``view_corp_stats`` only its own."""
    if not _signed_in(user):
        return False
    if sees_everything(user):
        return True
    if user.has_perm(VIEW_CORP_STATS):
        corporation = own_corporation(user)
        return corporation is not None and corporation[0] == corporation_id
    return False


def can_view_fc_summary(user, fc_user_id) -> bool:
    """Everybody sees the summary of the fleets they led themselves; other FCs only managers and
    ``view_all_stats``."""
    if not _signed_in(user):
        return False
    return sees_everything(user) or fc_user_id == user.pk


def led_fleets(user) -> bool:
    """Whether ``user`` is the FC of a fleet that has an SRP link."""
    return _signed_in(user) and SrpFleetLink.objects.filter(fc_user=user).exists()


def has_fc_permission(user) -> bool:
    """Whether ``user`` holds a permission of people who lead fleets (``FC_PERMISSIONS``)."""
    return _signed_in(user) and any(user.has_perm(name) for name in FC_PERMISSIONS)


def leads_srp_group(user) -> bool:
    """Whether ``user`` leads a group that runs fleets and has "SRP link for group fleets" on.

    Such a leader is the FC of their group fleets as far as SRP goes: the fleets get a link and the leader
    sees the claims and totals of them. Read from the request's cached ``leader_access``, which the page
    already needs, so it costs no extra query.
    """
    return _signed_in(user) and any(
        grant.runs_fleets and grant.srp_enabled for grant in group_access.leader_access(user).grants
    )


def leads_fleets(user) -> bool:
    """Whether ``user`` has a "My fleets" summary: FCs always have one, even before a fleet of theirs has an
    SRP link; so do leaders of SRP-enabled groups and anyone who led a fleet with an SRP link."""
    return has_fc_permission(user) or leads_srp_group(user) or led_fleets(user)


def summary_access(user) -> dict:
    """The summaries ``user`` may open.

    ``alliance``, ``corporations`` (any corporation) and ``fcs`` (any FC) are True for managers and
    ``view_all_stats``. ``own_corporation`` is the ``(id, name)`` of the user's corporation when its summary
    is allowed, ``own_fc`` whether the user has a "My fleets" page (see ``leads_fleets``) and ``manager``
    whether the user reviews claims, so the alliance summary points them to the review page instead of
    calling itself read only.
    """
    if not _signed_in(user):
        return {
            "alliance": False,
            "corporations": False,
            "fcs": False,
            "own_corporation": None,
            "own_fc": False,
            "manager": False,
        }
    everything = sees_everything(user)
    corporation = own_corporation(user) if (everything or user.has_perm(VIEW_CORP_STATS)) else None
    return {
        "alliance": everything,
        "corporations": everything,
        "fcs": everything,
        "own_corporation": corporation,
        "own_fc": leads_fleets(user),
        "manager": is_reviewer(user),
    }


def srp_nav(user) -> dict:
    """What the SRP menu shows; runs for every page, so it needs as few queries as it takes.

    ``enabled`` is the SRP switch, ``request`` whether "My SRP" is offered (permission and enabled),
    ``review`` the review queue for reviewers, ``pay`` the payments page for payers, ``links`` the link list for
    SRP staff, ``pending_count`` the
    pending claims a reviewer can still decide (one query, reviewers only), ``to_pay_count`` the approved
    claims a payer can still pay (one query, payers only), ``summary`` whether any summary opens and
    ``summary_all`` whether the alliance summary opens (staff and ``view_all_stats``). ``own_fleets`` is whether the
    menu has "My fleets": for every FC, whether or not a fleet of theirs has an SRP link yet, and for the leaders
    of groups with SRP on. ``visible`` is True when the menu has an entry. Users without any SRP-related
    permission cost no query beyond the group lookup every page makes for people who are not FCs.
    """
    hidden = {
        "enabled": False,
        "request": False,
        "review": False,
        "pay": False,
        "links": False,
        "pending_count": 0,
        "to_pay_count": 0,
        "summary": False,
        "summary_all": False,
        "own_fleets": False,
        "visible": False,
    }
    if not _signed_in(user):
        return hidden
    wants_request = user.has_perm(REQUEST_SRP)
    reviewer = is_reviewer(user)
    payer = is_payer(user)
    everything = is_srp_staff(user) or user.has_perm(VIEW_ALL_STATS)
    corporation_stats = user.has_perm(VIEW_CORP_STATS)
    is_fc = has_fc_permission(user)
    own_fleets = is_fc or leads_srp_group(user)
    if not (wants_request or everything or corporation_stats or own_fleets):
        return hidden

    srp_settings = SrpSettings.get_solo()
    summary = everything or own_fleets or (corporation_stats and own_corporation(user) is not None)
    pending_count = 0
    if reviewer:
        pending = SrpClaim.objects.filter(status=SrpClaim.Status.PENDING)
        if not srp_settings.allow_self_review:
            pending = pending.exclude(user=user)
        pending_count = pending.count()
    to_pay_count = 0
    if payer:
        to_pay = SrpClaim.objects.filter(status=SrpClaim.Status.APPROVED)
        if not srp_settings.allow_self_payment:
            to_pay = to_pay.exclude(user=user)
        if srp_settings.require_different_payer:
            by_user = SrpClaimEvent.objects.filter(claim=OuterRef("pk"), actor=user, action__in=APPROVER_ACTIONS)
            to_pay = to_pay.exclude(Q(decided_by=user) | Exists(by_user))
        to_pay_count = to_pay.count()
    request = wants_request and srp_settings.enabled
    return {
        "enabled": srp_settings.enabled,
        "request": request,
        "review": reviewer,
        "pay": payer,
        "links": reviewer or payer,
        "pending_count": pending_count,
        "to_pay_count": to_pay_count,
        "summary": bool(summary),
        "summary_all": everything,
        "own_fleets": own_fleets,
        "visible": request or reviewer or payer or bool(summary),
    }
