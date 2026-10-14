# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Read the Discord roles FCs may mention from the Alliance Auth Discord service."""

from dataclasses import dataclass

from allianceauth.services.hooks import get_extension_logger
from django.apps import apps
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy

from fleetops import texts
from fleetops.models import MAX_DATABASE_ID, AuditLog, PingRole

logger = get_extension_logger(__name__)

DISCORD_SERVICE_APP = "allianceauth.services.modules.discord"
ROLE_NAME_MAX_LENGTH = PingRole._meta.get_field("name").max_length

NOT_INSTALLED_MESSAGE = gettext_lazy(
    "The Alliance Auth Discord service is not installed, so roles cannot be read from Discord. "
    "Add role IDs manually."
)
NO_GUILD_MESSAGE = gettext_lazy(
    "DISCORD_GUILD_ID is not set for the Alliance Auth Discord service. Add role IDs manually."
)
READY_MESSAGE = gettext_lazy(
    "Reads the roles of the Discord server linked to Alliance Auth. "
    "New roles are added inactive; enable the ones FCs may ping."
)


@dataclass(frozen=True)
class RoleSyncResult:
    created: int = 0
    updated: int = 0
    missing: int = 0
    message: str = ""
    ok: bool = False


def _discord_api():
    """The Discord service API module, or None when the service is not installed.

    Importing the module loads the service's models, which fails when the app is not installed.
    """
    if not apps.is_installed(DISCORD_SERVICE_APP):
        return None
    try:
        from allianceauth.services.modules.discord import api
    except ImportError:
        return None
    return api


def _guild_id(api):
    try:
        return api.discord_guild_id()
    except (TypeError, ValueError):
        return None


def discord_service_status() -> tuple[bool, str]:
    """Whether roles can be read from Discord, and a line for the configuration page."""
    api = _discord_api()
    if api is None:
        return False, str(NOT_INSTALLED_MESSAGE)
    if not _guild_id(api):
        return False, str(NO_GUILD_MESSAGE)
    return True, str(READY_MESSAGE)


def _read_error(exc) -> str:
    # Only the class name: the exception text can contain request details.
    return gettext(
        "Could not read the roles from Discord (%(error_type)s). "
        "Check the Discord service, or add role IDs manually."
    ) % {"error_type": type(exc).__name__}


def _guild_roles(api, guild_id) -> dict[int, str]:
    """Role id -> name of the server's roles, without the @everyone role (its id is the server id)."""
    roles = {}
    for role in api.create_bot_client().guild_roles(guild_id, use_cache=False):
        role_id = int(role.id)
        if role_id == int(guild_id) or not 0 < role_id <= MAX_DATABASE_ID:
            continue
        roles[role_id] = str(role.name)[:ROLE_NAME_MAX_LENGTH]
    return roles


def _summary(created, updated, missing) -> str:
    parts = [
        gettext("%(count)s new (inactive until enabled)") % {"count": created},
        gettext("%(count)s updated") % {"count": updated},
    ]
    if missing:
        parts.append(gettext("%(count)s no longer on Discord (disabled)") % {"count": missing})
    return gettext("Roles synced from Discord: %(details)s.") % {"details": ", ".join(parts)}


def sync_discord_roles(*, actor) -> RoleSyncResult:
    """Add new Discord roles as inactive ping roles, rename known ones and disable vanished ones.

    Roles added by hand keep working when they are not on the Discord server (pings may go to
    another server); only roles that came from an earlier sync are marked missing.
    """
    available, message = discord_service_status()
    if not available:
        return RoleSyncResult(message=message)
    api = _discord_api()
    guild_id = _guild_id(api)
    try:
        roles = _guild_roles(api, guild_id)
    except Exception as exc:
        logger.warning("Reading the Discord roles failed (%s)", type(exc).__name__)
        return RoleSyncResult(message=_read_error(exc))

    now = timezone.now()
    created = updated = 0
    try:
        with transaction.atomic():
            existing = {
                role.role_id: role for role in PingRole.objects.select_for_update().filter(role_id__in=list(roles))
            }
            for role_id, name in roles.items():
                role = existing.get(role_id)
                if role is None:
                    PingRole.objects.create(
                        name=name,
                        role_id=role_id,
                        is_active=False,
                        source=PingRole.Source.DISCORD,
                        last_synced_at=now,
                    )
                    created += 1
                    continue
                if role.name != name or role.source != PingRole.Source.DISCORD or role.missing_from_discord:
                    updated += 1
                role.name = name
                role.source = PingRole.Source.DISCORD
                role.missing_from_discord = False
                role.last_synced_at = now
                role.save(update_fields=["name", "source", "missing_from_discord", "last_synced_at"])
            missing = (
                PingRole.objects.filter(source=PingRole.Source.DISCORD)
                .exclude(role_id__in=list(roles))
                .exclude(missing_from_discord=True, is_active=False)
                .update(missing_from_discord=True, is_active=False)
            )
            AuditLog.objects.create(
                actor=actor,
                action="configuration.discord_role_sync",
                object_type=PingRole.__name__,
                object_id="",
                old_value=None,
                new_value={"created": created, "updated": updated, "missing": missing, "roles": len(roles)},
                reason=texts.DISCORD_ROLE_SYNC_REASON(),
            )
    except IntegrityError:
        return RoleSyncResult(message=gettext("The roles changed while they were synced. Try again."))
    return RoleSyncResult(
        created=created, updated=updated, missing=missing, message=_summary(created, updated, missing), ok=True
    )
