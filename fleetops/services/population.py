"""Who counts as a member in the statistics.

The population is every Alliance Auth user with a main character whose account is active and whose main is not
in Doomheim (a biomassed character). When Member alliance IDs are configured the main must also be in one of
those alliances. Alts never count as members; a member's corporation is the corporation of the current main.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from allianceauth.authentication.models import UserProfile

from fleetops.services.history import configured_alliance_ids

try:
    from allianceauth.eveonline.models import DOOMHEIM_CORPORATION_ID
except ImportError:  # pragma: no cover - the constant exists in every supported Alliance Auth release
    DOOMHEIM_CORPORATION_ID = 1000001

_FIELDS = (
    "user_id",
    "main_character__character_id",
    "main_character__character_name",
    "main_character__corporation_id",
    "main_character__corporation_name",
    "main_character__corporation_ticker",
    "main_character__alliance_id",
    "main_character__alliance_name",
    "user__date_joined",
)


@dataclass(frozen=True, slots=True)
class MemberIdentity:
    user_id: int
    main_character_id: int
    main_character_name: str
    corporation_id: int
    corporation_name: str
    corporation_ticker: str
    alliance_id: int | None
    alliance_name: str
    # When the Alliance Auth account was created.
    registered_at: datetime


def _identity(row) -> MemberIdentity:
    (
        user_id,
        main_character_id,
        main_character_name,
        corporation_id,
        corporation_name,
        corporation_ticker,
        alliance_id,
        alliance_name,
        registered_at,
    ) = row
    return MemberIdentity(
        user_id=user_id,
        main_character_id=main_character_id,
        main_character_name=main_character_name or "",
        corporation_id=corporation_id,
        corporation_name=corporation_name or "",
        corporation_ticker=corporation_ticker or "",
        alliance_id=alliance_id,
        alliance_name=alliance_name or "",
        registered_at=registered_at,
    )


def member_profiles(*, corporation_id: int | None = None, settings_obj=None):
    """Profiles of the members, optionally of one corporation (the corporation of the current main)."""
    profiles = (
        UserProfile.objects.exclude(main_character=None)
        .filter(user__is_active=True)
        .exclude(main_character__corporation_id=DOOMHEIM_CORPORATION_ID)
    )
    alliance_ids = configured_alliance_ids(settings_obj)
    if alliance_ids:
        profiles = profiles.filter(main_character__alliance_id__in=alliance_ids)
    if corporation_id is not None:
        profiles = profiles.filter(main_character__corporation_id=corporation_id)
    return profiles


def members(*, corporation_id: int | None = None, settings_obj=None) -> list[MemberIdentity]:
    """Every member with the details of the main; one query."""
    rows = (
        member_profiles(corporation_id=corporation_id, settings_obj=settings_obj)
        .order_by("main_character__character_name", "user_id")
        .values_list(*_FIELDS)
    )
    return [_identity(row) for row in rows]


def member_user_ids_subquery(*, corporation_id: int | None = None, settings_obj=None):
    """The members' user IDs as a subquery, so large id lists never become SQL parameters."""
    return member_profiles(corporation_id=corporation_id, settings_obj=settings_obj).values("user_id")


def identity_of(user) -> MemberIdentity | None:
    """The member identity of ``user``, or None when the user is not in the population."""
    row = member_profiles().filter(user_id=getattr(user, "pk", user)).values_list(*_FIELDS).first()
    return _identity(row) if row is not None else None


def scope_alliances(settings_obj=None) -> tuple[bool, list[tuple[int, str]]]:
    """``(configured, [(alliance id, name)])`` for the configured Member alliance IDs.

    Names come from the main characters of the members; an alliance without members is named by its ID.
    """
    alliance_ids = configured_alliance_ids(settings_obj)
    if not alliance_ids:
        return False, []
    names = {}
    rows = (
        member_profiles(settings_obj=settings_obj)
        .values_list("main_character__alliance_id", "main_character__alliance_name")
        .distinct()
    )
    for alliance_id, name in rows:
        if name:
            names.setdefault(alliance_id, name)
    alliances = [(alliance_id, names.get(alliance_id) or f"Alliance {alliance_id}") for alliance_id in alliance_ids]
    alliances.sort(key=lambda item: (item[1].lower(), item[0]))
    return True, alliances


def corporation_main_count(corporation_id: int) -> int:
    return member_profiles(corporation_id=corporation_id).count()
