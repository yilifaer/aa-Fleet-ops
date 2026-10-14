# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Who may see which statistics.

Failures on a single object (a member, a corporation) are answered with ``Http404`` by the views, as
elsewhere in FleetOps; a leaderboard that is switched off for the user is a ``PermissionDenied``.
"""

from __future__ import annotations

from fleetops.models import FleetOpsSettings
from fleetops.services import population

BASIC_ACCESS = "fleetops.basic_access"
VIEW_CORP_STATS = "fleetops.view_corp_stats"
VIEW_ALL_STATS = "fleetops.view_all_stats"


def own_corporation(user) -> tuple[int, str] | None:
    """``(id, name)`` of the corporation of the user's current main; None without a main or corporation."""
    main = getattr(getattr(user, "profile", None), "main_character", None)
    if main is None or not main.corporation_id:
        return None
    return main.corporation_id, main.corporation_name or ""


def can_view_corporation(viewer, corporation_id) -> bool:
    """``view_all_stats`` sees every corporation, ``view_corp_stats`` only its own."""
    if viewer.has_perm(VIEW_ALL_STATS):
        return True
    if viewer.has_perm(VIEW_CORP_STATS):
        corporation = own_corporation(viewer)
        return corporation is not None and corporation[0] == corporation_id
    return False


def can_view_member(viewer, target_user) -> bool:
    """Whether ``viewer`` may open the statistics of a member; ``target_user`` is a user or a user ID.

    Everybody may see themselves. Members outside the population (no main, disabled account, departed from
    the member alliances) have no page for anybody else. ``view_all_stats`` sees every member,
    ``view_corp_stats`` the members whose main is in the viewer's own corporation.
    """
    target_id = getattr(target_user, "pk", target_user)
    if viewer.pk == target_id:
        return True
    identity = population.identity_of(target_id)
    if identity is None:
        return False
    return can_view_corporation(viewer, identity.corporation_id)


def leaderboard_access(user) -> dict:
    """The leaderboards a user may open.

    ``alliance`` is the alliance board, ``own_corporation`` the ``(id, name)`` of the user's own corporation
    when its board is allowed, ``any_corporation`` whether every corporation's board is allowed. With
    the visibility setting ``members`` everybody with FleetOps access sees the alliance board and their own
    corporation's board; with ``managers`` only the statistics permissions open boards.
    """
    members_see = (
        user.has_perm(BASIC_ACCESS)
        and FleetOpsSettings.get_solo().leaderboard_visibility == FleetOpsSettings.LeaderboardVisibility.MEMBERS
    )
    view_all = user.has_perm(VIEW_ALL_STATS)
    corporation = own_corporation(user) if (user.has_perm(VIEW_CORP_STATS) or members_see) else None
    return {
        "alliance": view_all or members_see,
        "own_corporation": corporation,
        "any_corporation": view_all,
    }


def leaderboard_visible(user) -> bool:
    """Whether the user has a leaderboard to open, for the navigation.

    The same answer as :func:`leaderboard_access`, with as few queries as it takes: this runs for every page.
    """
    if user.has_perm(VIEW_ALL_STATS):
        return True
    visibility = FleetOpsSettings.get_solo().leaderboard_visibility
    if visibility == FleetOpsSettings.LeaderboardVisibility.MEMBERS and user.has_perm(BASIC_ACCESS):
        return True
    return user.has_perm(VIEW_CORP_STATS) and own_corporation(user) is not None
