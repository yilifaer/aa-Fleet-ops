# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Who may see and decide what in FleetOps SRP.

Members see their own claims. ``view_all_stats`` sees every SRP summary and claim read-only,
``view_corp_stats`` the summary and the claims of its own corporation, FCs the claims and totals of the
fleets they led, and SRP managers (``manage_srp``) everything. Only managers see internal notes and only
managers decide, and by default not on their own claims.

A single object out of scope (a claim, a corporation, another FC's summary) is answered by the views with the
FleetOps 404 page, the same for an ID that exists and one that does not; a corporation manager's own corporation
by ID is their own summary. A missing permission is the FleetOps 403 page with its reason (``fleetops.views_errors``).
"""

from __future__ import annotations

from django.db.models import Q

from fleetops.models import SrpClaim, SrpFleetLink, SrpSettings
from fleetops.services import group_access
from fleetops.services.stats_access import VIEW_ALL_STATS, VIEW_CORP_STATS, own_corporation

REQUEST_SRP = "fleetops.request_srp"
MANAGE_SRP = "fleetops.manage_srp"

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


def is_manager(user) -> bool:
    """May review, change and pay claims (``manage_srp``); sees every claim and the internal notes."""
    return _signed_in(user) and user.has_perm(MANAGE_SRP)


def sees_everything(user) -> bool:
    """Managers and ``view_all_stats`` see every summary and claim; the latter read-only."""
    return _signed_in(user) and (user.has_perm(MANAGE_SRP) or user.has_perm(VIEW_ALL_STATS))


def can_view_internal_notes(user) -> bool:
    """Internal notes of reviewers stay manager-only."""
    return is_manager(user)


def can_review(user, claim, *, settings_obj=None) -> bool:
    """May decide on ``claim`` (approve, reject, change the amount, mark paid, revoke, reopen, reclassify).

    Needs ``manage_srp``; a manager's own claim is decided by another manager unless the settings allow
    self review.
    """
    if not is_manager(user):
        return False
    if claim.user_id is None or claim.user_id != user.pk:
        return True
    return bool((settings_obj or SrpSettings.get_solo()).allow_self_review)


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
    whether the user decides on claims, so the alliance summary points them to the review page instead of
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
        "manager": is_manager(user),
    }


def srp_nav(user) -> dict:
    """What the SRP menu shows; runs for every page, so it needs as few queries as it takes.

    ``enabled`` is the SRP switch, ``request`` whether "My SRP" is offered (permission and enabled),
    ``review`` the queue and payments for managers, ``pending_count`` the pending claims a manager can still
    decide (one query, managers only) and ``summary`` whether any summary opens. ``own_fleets`` is whether the
    menu has "My fleets": for every FC, whether or not a fleet of theirs has an SRP link yet, and for the leaders
    of groups with SRP on. ``visible`` is True when the menu has an entry. Users without any SRP-related
    permission cost no query beyond the group lookup every page makes for people who are not FCs.
    """
    hidden = {
        "enabled": False,
        "request": False,
        "review": False,
        "pending_count": 0,
        "summary": False,
        "own_fleets": False,
        "visible": False,
    }
    if not _signed_in(user):
        return hidden
    wants_request = user.has_perm(REQUEST_SRP)
    manager = user.has_perm(MANAGE_SRP)
    everything = manager or user.has_perm(VIEW_ALL_STATS)
    corporation_stats = user.has_perm(VIEW_CORP_STATS)
    is_fc = has_fc_permission(user)
    own_fleets = is_fc or leads_srp_group(user)
    if not (wants_request or everything or corporation_stats or own_fleets):
        return hidden

    srp_settings = SrpSettings.get_solo()
    summary = everything or own_fleets or (corporation_stats and own_corporation(user) is not None)
    pending_count = 0
    if manager:
        pending = SrpClaim.objects.filter(status=SrpClaim.Status.PENDING)
        if not srp_settings.allow_self_review:
            pending = pending.exclude(user=user)
        pending_count = pending.count()
    request = wants_request and srp_settings.enabled
    return {
        "enabled": srp_settings.enabled,
        "request": request,
        "review": manager,
        "pending_count": pending_count,
        "summary": bool(summary),
        "own_fleets": own_fleets,
        "visible": request or manager or bool(summary),
    }
