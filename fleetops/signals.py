# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Signal receivers of FleetOps, connected when the app is ready."""

from django.contrib.auth.models import Group
from django.db.models.signals import pre_delete
from django.dispatch import receiver

GROUP_DELETED_ACTION = "group.deleted"


# No sender: Alliance Auth deletes groups through its own proxy model of ``auth.Group``, and the signal names
# the class of the deleted object.
@receiver(pre_delete, dispatch_uid="fleetops_group_deleted")
def group_deleted(sender, instance, **kwargs):
    """Record the deletion of an Alliance Auth group that FleetOps uses, before its rows go with it.

    The group's access row and its places in the ping channels are deleted by the database with the group;
    fleets and pings keep the group's name. A ping channel whose only group was this one is an alliance channel
    again (owner decision 5-7), so its switch "Alliance FCs can use this channel" is turned back on to match;
    a channel that keeps another group keeps its switch. The audit entry has no actor: the deletion happened in
    Alliance Auth. It runs in the transaction of the deletion, so it is kept only when the group is gone.
    """
    if not isinstance(instance, Group) or instance.pk is None:
        return
    from fleetops import texts
    from fleetops.models import AuditLog, DiscordWebhook, GroupAccess
    from fleetops.services.audit import config_snapshot

    access = GroupAccess.objects.filter(group_id=instance.pk).first()
    through = DiscordWebhook.allowed_groups.through.objects
    channel_ids = sorted(through.filter(group_id=instance.pk).values_list("discordwebhook_id", flat=True))
    if access is None and not channel_ids:
        return
    shared = set(
        through.filter(discordwebhook_id__in=channel_ids)
        .exclude(group_id=instance.pk)
        .values_list("discordwebhook_id", flat=True)
    )
    reopened = sorted(
        DiscordWebhook.objects.filter(pk__in=[pk for pk in channel_ids if pk not in shared], fcs_allowed=False)
        .values_list("pk", flat=True)
    )
    if reopened:
        DiscordWebhook.objects.filter(pk__in=reopened).update(fcs_allowed=True)
    AuditLog.objects.create(
        actor=None,
        action=GROUP_DELETED_ACTION,
        object_type="Group",
        object_id=str(instance.pk),
        old_value={
            "name": instance.name,
            "access": config_snapshot(access) if access is not None else None,
            "channels": channel_ids,
        },
        new_value={"channels_reopened": reopened},
        reason=texts.AUDIT_GROUP_DELETED(),
    )
