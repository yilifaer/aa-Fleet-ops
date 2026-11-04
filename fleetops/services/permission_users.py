# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Which users hold a permission, answered with one query."""

from django.contrib.auth.models import Permission, User
from django.db.models import Q


def users_with_any_perm(perms):
    """Users holding any of ``perms`` (``app_label.codename``) directly, through a group or their
    state, plus superusers."""
    query = Q()
    for perm in perms:
        app_label, codename = perm.split(".", 1)
        query |= Q(content_type__app_label=app_label, codename=codename)
    permissions = Permission.objects.filter(query)
    return User.objects.filter(
        Q(user_permissions__in=permissions)
        | Q(groups__permissions__in=permissions)
        | Q(profile__state__permissions__in=permissions)
        | Q(is_superuser=True)
    ).distinct()
