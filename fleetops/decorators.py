# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Access decorators of the FleetOps views.

``fleetops_permission_required`` replaces Django's and Alliance Auth's permission decorators;
``fleet_starter_required``, ``ping_sender_required`` and ``fleet_manager_required`` also let the leaders of
configured Alliance Auth groups in (``services.group_access``).

With ``page=True`` (pages a person opens) a GET or HEAD without the right shows the FleetOps 403 page with the
reason, and a GET or HEAD whose view raises ``Http404`` shows the FleetOps 404 page with one neutral sentence.
Every other method, and every view with ``page=False`` (JSON, fragments fetched by scripts, POST endpoints),
raises ``PermissionDenied``/``Http404`` as before. Anonymous users never get here: Alliance Auth's login check
wraps every FleetOps URL except the public comms join page.
"""

from functools import wraps

from django.core.exceptions import PermissionDenied
from django.http import Http404

from fleetops.services import group_access
from fleetops.views_errors import (
    FLEET_MANAGER_REASON,
    NOT_FOUND_REASON,
    PING_SENDER_REASON,
    STARTER_REASON,
    denied,
    not_found,
    reason_for,
)

PAGE_METHODS = ("GET", "HEAD")
MANAGE_PERMISSIONS = ("fleetops.manage_own_fleet", "fleetops.manage_fleets")


def _is_page_request(request, page):
    return page and request.method in PAGE_METHODS


def _guard(view, *, allowed, reason, permission, page):
    """Wrap ``view``: ``allowed(user)`` decides, ``reason(user)`` and ``permission`` fill the 403 page."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not allowed(request.user):
            if _is_page_request(request, page):
                return denied(request, reason(request.user), permission=permission)
            raise PermissionDenied
        if not _is_page_request(request, page):
            return view(request, *args, **kwargs)
        try:
            return view(request, *args, **kwargs)
        except Http404:
            return not_found(request, NOT_FOUND_REASON)

    return wrapper


def fleetops_permission_required(perm_or_perms, *, any_perm=False, page=True):
    """Require one permission, all of several, or with ``any_perm`` any one of several."""
    perms = (perm_or_perms,) if isinstance(perm_or_perms, str) else tuple(perm_or_perms)
    if not perms:
        raise ValueError("fleetops_permission_required needs at least one permission")

    def allowed(user):
        checks = (user.has_perm(perm) for perm in perms)
        return any(checks) if any_perm else all(checks)

    def reason(user):
        if any_perm:
            return reason_for(perms[0])
        missing = next((perm for perm in perms if not user.has_perm(perm)), perms[0])
        return reason_for(missing)

    permission = " / ".join(perms) if any_perm else ", ".join(perms)

    def decorator(view):
        return _guard(view, allowed=allowed, reason=reason, permission=permission, page=page)

    return decorator


def _group_decorator(view, *, allowed, reason, permission, page):
    def decorator(func):
        return _guard(func, allowed=allowed, reason=lambda user: reason, permission=permission, page=page)

    # Used bare (@fleet_starter_required) or with options (@fleet_starter_required(page=False)).
    return decorator(view) if view is not None else decorator


def fleet_starter_required(view=None, *, page=True):
    """FCs (``start_fleet``) and the leaders of groups that run fleets."""
    return _group_decorator(
        view,
        allowed=group_access.can_start_fleets,
        reason=STARTER_REASON,
        permission="fleetops.start_fleet",
        page=page,
    )


def ping_sender_required(view=None, *, page=True):
    """FCs (``start_fleet``) and the leaders of every configured group."""
    return _group_decorator(
        view,
        allowed=group_access.can_send_pings,
        reason=PING_SENDER_REASON,
        permission="fleetops.start_fleet",
        page=page,
    )


def fleet_manager_required(*also, page=False):
    """Fleet managers: ``manage_own_fleet``, ``manage_fleets``, any permission in ``also``, or a group leader.

    It replaces ``permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"))``. Each view
    still checks the fleet itself (``views._can_manage_operation``) and answers 404 for a fleet the user does not
    manage, so letting group leaders past this check gives them nothing but their own group fleets.
    """
    if any(callable(perm) for perm in also):
        raise TypeError("fleet_manager_required is used with parentheses: @fleet_manager_required()")
    perms = MANAGE_PERMISSIONS + tuple(also)

    def allowed(user):
        if any(user.has_perm(perm) for perm in perms):
            return True
        return bool(group_access.leader_access(user).grants)

    def decorator(view):
        return _guard(
            view,
            allowed=allowed,
            reason=lambda user: FLEET_MANAGER_REASON,
            permission=" / ".join(perms),
            page=page,
        )

    return decorator
