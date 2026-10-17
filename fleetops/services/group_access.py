# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Group leaders: which configured Alliance Auth groups a user leads, and what that allows in FleetOps.

Leadership is read from Alliance Auth on every request: the group leaders of a group and the members of its
leader groups. ``auth.group_management`` alone makes nobody a leader. An active ``GroupAccess`` row for the
group is the grant; there is no FleetOps permission for it. The answer is kept on the user object for one
request, so the navigation, a form and the send in the same request share one lookup; a change in Alliance
Auth (leader removed, row deactivated, group deleted) counts from the next request on.

Every question "may this user ... for a group" goes through this module: forms, views, decorators, the start
and schedule services, the Discord choke point, the Send Ping service, the SRP gate and the statistics.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.db.models import Q

from fleetops import texts
from fleetops.models import DiscordWebhook, GroupAccess

BASIC_ACCESS = "fleetops.basic_access"
START_FLEET = "fleetops.start_fleet"
MANAGE_OWN_FLEET = "fleetops.manage_own_fleet"
MANAGE_FLEETS = "fleetops.manage_fleets"
# Where one request keeps the answer of ``leader_access``.
CACHE_ATTRIBUTE = "_fleetops_leader_access"


@dataclass(frozen=True)
class GroupGrant:
    """What the leaders of one group may do: the group's active access row, as far as a request needs it."""

    group_id: int
    group_name: str
    mode: str
    # Active fleet types only; empty for a ping-only group.
    fleet_type_ids: frozenset = frozenset()
    srp_enabled: bool = False

    @property
    def runs_fleets(self) -> bool:
        return self.mode == GroupAccess.Mode.FLEETS


@dataclass(frozen=True)
class LeaderAccess:
    """The grants of the groups a user leads now."""

    grants: tuple = ()

    def grant(self, group_id) -> GroupGrant | None:
        for grant in self.grants:
            if grant.group_id == group_id:
                return grant
        return None

    @property
    def group_ids(self) -> set:
        return {grant.group_id for grant in self.grants}

    @property
    def fleet_group_ids(self) -> set:
        return {grant.group_id for grant in self.grants if grant.runs_fleets}

    @property
    def fleet_grants(self) -> tuple:
        return tuple(grant for grant in self.grants if grant.runs_fleets)


@dataclass(frozen=True)
class LeaderRow:
    """One leader of a group for the administration pages."""

    user_id: int
    name: str
    has_access: bool
    # The leader group the user leads through; "" for a direct leader.
    via_group: str = ""


def leader_q(user, prefix="") -> Q:
    """The groups ``user`` leads: as a group leader, or as a member of one of the group's leader groups."""
    return Q(**{f"{prefix}authgroup__group_leaders": user.pk}) | Q(
        **{f"{prefix}authgroup__group_leader_groups__user": user.pk}
    )


def leader_access(user) -> LeaderAccess:
    """The active grants of ``user``.

    No query without ``basic_access`` (or for an inactive or anonymous user), one when the user leads no
    configured group or only ping groups, two when they lead a group that runs fleets. Cached on ``user``.
    """
    cached = getattr(user, CACHE_ATTRIBUTE, None)
    if cached is not None:
        return cached
    access = LeaderAccess()
    if getattr(user, "is_authenticated", False) and user.is_active and user.has_perm(BASIC_ACCESS):
        led = Group.objects.filter(leader_q(user)).values("pk")
        rows = list(
            GroupAccess.objects.filter(is_active=True, group_id__in=led)
            .order_by("group__name", "group_id")
            .values_list("group_id", "group__name", "mode", "srp_enabled")
        )
        fleet_ids = [row[0] for row in rows if row[2] == GroupAccess.Mode.FLEETS]
        types = defaultdict(set)
        if fleet_ids:
            through = GroupAccess.fleet_types.through.objects.filter(
                groupaccess__group_id__in=fleet_ids, fleettype__is_active=True
            )
            for group_id, type_id in through.values_list("groupaccess__group_id", "fleettype_id"):
                types[group_id].add(type_id)
        access = LeaderAccess(
            tuple(
                GroupGrant(group_id, name, mode, frozenset(types[group_id]), srp)
                for group_id, name, mode, srp in rows
            )
        )
    try:
        setattr(user, CACHE_ATTRIBUTE, access)
    except AttributeError:
        pass
    return access


def forget(user) -> None:
    """Drop the cached answer of ``leader_access`` (after a change within the same request, and in tests)."""
    try:
        delattr(user, CACHE_ATTRIBUTE)
    except AttributeError:
        pass


def _has(user, perm) -> bool:
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.has_perm(perm))


def can_start_fleets(user) -> bool:
    """FCs (``start_fleet``) and the leaders of groups that run fleets may open the start page."""
    return _has(user, START_FLEET) or bool(user is not None and leader_access(user).fleet_group_ids)


def can_send_pings(user) -> bool:
    """FCs (``start_fleet``) and the leaders of every configured group may send pings."""
    return _has(user, START_FLEET) or bool(user is not None and leader_access(user).grants)


def _assigned_channel_ids():
    return DiscordWebhook.allowed_groups.through.objects.values("discordwebhook_id")


def alliance_channels(user):
    """Active channels for alliance pings: every one for FC leads; for FCs those without a group or with
    "Alliance FCs can use this channel"; none for anybody else.

    A channel without any group is an alliance channel whatever its stored switch says (owner decision 5-7).
    """
    active = DiscordWebhook.objects.filter(is_active=True)
    if _has(user, MANAGE_FLEETS):
        return active
    if _has(user, START_FLEET):
        return active.filter(Q(fcs_allowed=True) | ~Q(pk__in=_assigned_channel_ids()))
    return active.none()


def group_channels(group_ids):
    """Active channels assigned to any of ``group_ids``."""
    group_ids = list(group_ids or ())
    if not group_ids:
        return DiscordWebhook.objects.none()
    assigned = DiscordWebhook.allowed_groups.through.objects.filter(group_id__in=group_ids)
    return DiscordWebhook.objects.filter(is_active=True, pk__in=assigned.values("discordwebhook_id"))


def _channel_group_ids(channel) -> set:
    cache = getattr(channel, "_prefetched_objects_cache", {})
    if "allowed_groups" in cache:
        return {group.pk for group in cache["allowed_groups"]}
    return set(
        DiscordWebhook.allowed_groups.through.objects.filter(discordwebhook_id=channel.pk).values_list(
            "group_id", flat=True
        )
    )


def _group_name(group_id) -> str:
    name = Group.objects.filter(pk=group_id).values_list("name", flat=True).first()
    return name or str(group_id)


def channel_refusal(user, channel, *, group_id=None) -> str:
    """Why ``user`` must not post to ``channel`` for the group ``group_id`` (None: an alliance ping); "" when allowed.

    A stored message, so a step result and a form show it in the reader's language. Rules: no channel (copy
    only) is never refused; an inactive channel always is; FC leads post to every active channel; an alliance
    ping needs ``start_fleet`` or ``manage_own_fleet`` (retries of the FC's own fleets) and a channel without
    groups or with "Alliance FCs can use this channel"; a group ping needs a current grant for the group and
    the channel assigned to it.
    """
    if channel is None:
        return ""
    if not channel.is_active:
        return texts.PING_CHANNEL_DISABLED(name=channel.name)
    if _has(user, MANAGE_FLEETS):
        return ""
    if group_id is None:
        if not (_has(user, START_FLEET) or _has(user, MANAGE_OWN_FLEET)):
            return texts.CHANNEL_NOT_ALLOWED(name=channel.name)
        if channel.fcs_allowed or not _channel_group_ids(channel):
            return ""
        return texts.CHANNEL_NOT_ALLOWED(name=channel.name)
    grant = leader_access(user).grant(group_id) if user is not None else None
    if grant is None:
        return texts.GROUP_NOT_LED(group=_group_name(group_id))
    if group_id not in _channel_group_ids(channel):
        return texts.GROUP_CHANNEL_NOT_ASSIGNED(name=channel.name, group=grant.group_name)
    return ""


def fleet_start_refusal(user, *, group_id, fleet_type, channel, sends, pre_ping) -> tuple[str, str]:
    """``(field, message)`` of the first problem of a fleet start or a pre ping; ``("", "")`` when allowed.

    The field names the start form field the message belongs to (``fleet_group``, ``fleet_type``,
    ``ping_channel`` or ``fleet_timing``). An alliance fleet needs ``start_fleet`` and, when it posts, a
    channel FCs may use. A group fleet needs a grant that runs fleets, is never pre pinged, uses one of the
    group's fleet types and, when it posts, one of the group's channels. A channel that was switched off is
    not refused here: nothing is ever posted to it, and the start records why (as before).
    """
    if channel is not None and not channel.is_active:
        sends = False
    if group_id is None:
        if not _has(user, START_FLEET):
            return "fleet_group", texts.GROUP_REQUIRED()
        if sends:
            refusal = channel_refusal(user, channel, group_id=None)
            if refusal:
                return "ping_channel", refusal
        return "", ""
    grant = leader_access(user).grant(group_id) if user is not None else None
    if grant is None:
        return "fleet_group", texts.GROUP_NOT_LED(group=_group_name(group_id))
    if not grant.runs_fleets:
        return "fleet_group", texts.GROUP_NO_FLEETS(group=grant.group_name)
    if pre_ping:
        return "fleet_timing", texts.GROUP_NO_PRE_PING()
    if fleet_type is None or fleet_type.pk not in grant.fleet_type_ids:
        return "fleet_type", texts.GROUP_FLEET_TYPE_NOT_ALLOWED(type=str(fleet_type or ""), group=grant.group_name)
    if sends:
        refusal = channel_refusal(user, channel, group_id=group_id)
        if refusal:
            return "ping_channel", refusal
    return "", ""


def manages_group_fleet(user, operation) -> bool:
    """The leader who started a group fleet manages it while they lead the group and its row is active."""
    return bool(
        operation.group_id
        and user is not None
        and operation.fc_user_id == user.pk
        and leader_access(user).grant(operation.group_id)
    )


def visible_group_ids(user) -> set:
    """The groups whose fleets ``user`` may open read-only: the groups they lead now."""
    if user is None:
        return set()
    return leader_access(user).group_ids


def group_srp_allowed(operation) -> bool:
    """Alliance fleets: True. Group fleets: the group's "SRP link for group fleets" (False without its row)."""
    if not operation.group_id:
        return True
    return GroupAccess.objects.filter(group_id=operation.group_id, srp_enabled=True).exists()


def _users_with_access(user_ids) -> set:
    """Which of ``user_ids`` hold ``basic_access`` (directly, through a group, their state, or as superuser)."""
    if not user_ids:
        return set()
    app_label, codename = BASIC_ACCESS.split(".")
    permission = Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()
    users = get_user_model().objects.filter(pk__in=user_ids, is_active=True)
    rule = Q(is_superuser=True)
    if permission is not None:
        rule |= (
            Q(user_permissions=permission)
            | Q(groups__permissions=permission)
            | Q(profile__state__permissions=permission)
        )
    return set(users.filter(rule).values_list("pk", flat=True).distinct())


def leaders_of(group_ids) -> dict:
    """``{group_id: [LeaderRow, ...]}``: the direct leaders and the members of the leader groups of each group.

    For the administration pages. A fixed number of queries, whatever the number of groups and leaders: the
    two leader tables of Alliance Auth, the members of the leader groups, the users with their main
    characters and who of them has FleetOps access.
    """
    from allianceauth.groupmanagement.models import AuthGroup

    group_ids = list(group_ids or ())
    result = {group_id: [] for group_id in group_ids}
    if not group_ids:
        return result
    direct = list(
        AuthGroup.group_leaders.through.objects.filter(authgroup_id__in=group_ids).values_list(
            "authgroup_id", "user_id"
        )
    )
    via = list(
        AuthGroup.group_leader_groups.through.objects.filter(authgroup_id__in=group_ids).values_list(
            "authgroup_id", "group_id", "group__name"
        )
    )
    members = defaultdict(set)
    if via:
        for leader_group_id, user_id in get_user_model().groups.through.objects.filter(
            group_id__in={row[1] for row in via}
        ).values_list("group_id", "user_id"):
            members[leader_group_id].add(user_id)
    user_ids = {user_id for _group, user_id in direct} | {uid for ids in members.values() for uid in ids}
    names = {}
    for user in get_user_model().objects.filter(pk__in=user_ids).select_related("profile__main_character"):
        main = getattr(getattr(user, "profile", None), "main_character", None)
        names[user.pk] = getattr(main, "character_name", "") or user.username
    access = _users_with_access(user_ids)
    for group_id, user_id in direct:
        result[group_id].append(LeaderRow(user_id, names.get(user_id, ""), user_id in access))
    for group_id, leader_group_id, leader_group_name in via:
        known = {row.user_id for row in result[group_id]}
        for user_id in sorted(members[leader_group_id]):
            if user_id not in known:
                result[group_id].append(
                    LeaderRow(user_id, names.get(user_id, ""), user_id in access, leader_group_name)
                )
    for rows in result.values():
        rows.sort(key=lambda row: (row.name.casefold(), row.user_id))
    return result
