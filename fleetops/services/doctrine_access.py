# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Who may ping which doctrine, and who may use a custom doctrine name.

Every entry point that names a doctrine for a ping (start fleet, group fleet, ping without a fleet, scheduled fleets,
start presets) asks this module; there is no second rule anywhere else.

* A doctrine without a row in ``DoctrineAccess``, or with a row without groups, is open to everybody who may start
  fleets (the rule before this feature existed). With groups, only users in one of those Alliance Auth groups may.
* Superusers and users with ``fleetops.manage_configuration`` (the permission that manages the FleetOps
  configuration and the Doctrine settings page) are never restricted.
* The custom doctrine name ("None / Custom") follows ``DoctrineSettings`` the same way.
* Fittings' own visibility (the category groups) is applied first, by ``providers.doctrines.get_doctrines``; this is
  an extra filter on top of it.

A ``Scope`` reads the rules once (at most four queries, whatever the number of doctrines) and answers for one user.
Build a fresh one when the answer has to be current (a start, a scheduled start); a form keeps its own for the page.
Retrying the MOTD or ping of a running fleet and SRP links are not covered: the doctrine was checked at the start,
and SRP links are no pings.
"""

from fleetops.models import DoctrineAccess, DoctrineSettings

MANAGE_CONFIGURATION = "fleetops.manage_configuration"
# Sources that are no doctrine of a doctrine app: a typed name, nothing, or a manual record.
FREE_SOURCES = frozenset({"", "custom", "none", "manual"})


def is_administrator(user) -> bool:
    """Superusers and fleet administrators are never restricted."""
    if user is None:
        return True
    if not getattr(user, "is_authenticated", False):
        return False
    return bool(user.is_active and (user.is_superuser or user.has_perm(MANAGE_CONFIGURATION)))


class Scope:
    """What one user may ping, read from the rules now."""

    def __init__(self, user):
        self.user = user
        self.unrestricted = is_administrator(user)
        self._restricted = {}
        self._custom_groups = None
        self._group_ids = None
        if self.unrestricted:
            return
        through = DoctrineAccess.groups.through.objects.values_list(
            "doctrineaccess__doctrine_source", "doctrineaccess__doctrine_external_id", "group_id"
        )
        for source, external_id, group_id in through:
            self._restricted.setdefault((source, external_id), set()).add(group_id)
        settings_row = DoctrineSettings.objects.filter(pk=1).values_list("pk", "custom_name_restricted").first()
        if settings_row is not None and settings_row[1]:
            self._custom_groups = set(
                DoctrineSettings.custom_name_groups.through.objects.filter(
                    doctrinesettings_id=settings_row[0]
                ).values_list("group_id", flat=True)
            )

    @property
    def group_ids(self) -> set:
        if self._group_ids is None:
            user = self.user
            active = getattr(user, "is_authenticated", False) and user.is_active
            self._group_ids = set(user.groups.values_list("pk", flat=True)) if active else set()
        return self._group_ids

    def restricted(self, source, external_id) -> bool:
        """Whether the doctrine has groups (for a user who is not an administrator)."""
        return (str(source or ""), str(external_id or "")) in self._restricted

    def can_use_doctrine(self, source, external_id) -> bool:
        if self.unrestricted or str(source or "") in FREE_SOURCES:
            return True
        allowed = self._restricted.get((str(source), str(external_id or "")))
        if not allowed:
            return True
        return bool(allowed & self.group_ids)

    def can_use_custom_name(self) -> bool:
        if self.unrestricted or self._custom_groups is None:
            return True
        return bool(self._custom_groups & self.group_ids)

    @property
    def custom_name_limited(self) -> bool:
        """Whether the custom name is limited to groups at all (even if this user is in one)."""
        return self._custom_groups is not None

    def allowed(self, choices) -> list:
        """The ``DoctrineChoice`` rows of ``choices`` this user may ping, in order."""
        if self.unrestricted or not self._restricted:
            return list(choices)
        return [choice for choice in choices if self.can_use_doctrine(choice.source, choice.external_id)]


def can_use_doctrine(user, source, external_id) -> bool:
    """Whether ``user`` may ping the doctrine; a typed name or no doctrine is not a doctrine (see ``can_use_custom_name``)."""
    return Scope(user).can_use_doctrine(source, external_id)


def can_use_custom_name(user) -> bool:
    """Whether ``user`` may use the custom doctrine name option (None / Custom)."""
    return Scope(user).can_use_custom_name()


def allowed_doctrines(user, choices) -> list:
    """``choices`` without the doctrines ``user`` may not ping."""
    return Scope(user).allowed(choices)


def refusal(user, cleaned_data, *, scope=None, blank_allowed=None) -> str:
    """Why ``user`` may not ping with the doctrine of ``cleaned_data`` (the start, schedule and ping forms); "" if they may.

    Reads ``doctrine_source``, ``doctrine_external_id`` and ``doctrine_name`` as the forms' ``_clean_doctrine``
    leaves them: a doctrine of an app, a typed name (source ``custom``) or nothing. Nothing is the "None / Custom"
    option: it is refused for a user who may not use custom names as long as there is a doctrine they may choose
    (``blank_allowed`` says so when the caller knows, the forms do; otherwise the doctrines are looked up).
    """
    from fleetops import texts

    scope = scope or Scope(user)
    source = cleaned_data.get("doctrine_source") or ""
    if source not in FREE_SOURCES:
        return "" if scope.can_use_doctrine(source, cleaned_data.get("doctrine_external_id")) else texts.DOCTRINE_NOT_ALLOWED()
    if source == "custom" or (source in ("none", "") and cleaned_data.get("doctrine_name")):
        return "" if scope.can_use_custom_name() else texts.CUSTOM_DOCTRINE_NOT_ALLOWED()
    if scope.can_use_custom_name():
        return ""
    if blank_allowed is None:
        from fleetops.providers.doctrines import get_doctrines

        blank_allowed = not scope.allowed(get_doctrines(user))
    return "" if blank_allowed else texts.DOCTRINE_CHOOSE_ONE()
