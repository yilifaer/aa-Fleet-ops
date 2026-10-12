from dataclasses import dataclass

from allianceauth.authentication.models import CharacterOwnership, UserProfile
from allianceauth.eveonline.models import EveCharacter

from fleetops.models import EveEntityName
from fleetops.services.names import cached_identities, placeholder_name


@dataclass(slots=True)
class CharacterIdentity:
    character_id: int
    character_name: str
    user: object | None
    main_character_id: int | None
    main_character_name: str
    corporation_id: int | None
    corporation_name: str
    alliance_id: int | None
    alliance_name: str
    # Alliance Auth does not know the character; its details come from FleetOps' ESI name cache.
    external: bool = False


def owned_characters(user):
    return (
        CharacterOwnership.objects.filter(user=user)
        .select_related("character", "user__profile__main_character")
        .order_by("character__character_name")
    )


def get_owned_character(user, character_id: int):
    return (
        CharacterOwnership.objects.filter(user=user, character__character_id=character_id)
        .select_related("character", "user__profile__main_character")
        .first()
    )


def character_label(character_id) -> str:
    """Name of a character from Alliance Auth or the ESI name cache, otherwise ``character <id>``."""
    if not character_id:
        return "unknown"
    name = (
        EveCharacter.objects.filter(character_id=character_id).values_list("character_name", flat=True).first()
        or EveEntityName.objects.filter(id=character_id).exclude(name="").values_list("name", flat=True).first()
    )
    return name or f"character {character_id}"


def identity_for_character_id(character_id: int) -> CharacterIdentity:
    return resolve_many([character_id])[int(character_id)]


def _external_identity(character_id, cached) -> CharacterIdentity:
    return CharacterIdentity(
        character_id=character_id,
        character_name=cached.character_name if cached else placeholder_name(character_id),
        user=None,
        main_character_id=None,
        main_character_name="",
        corporation_id=cached.corporation_id if cached else None,
        corporation_name=cached.corporation_name if cached else "",
        alliance_id=cached.alliance_id if cached else None,
        alliance_name=cached.alliance_name if cached else "",
        external=True,
    )


def resolve_many(character_ids: list[int]) -> dict[int, CharacterIdentity]:
    ids = list({int(x) for x in character_ids})
    chars = {c.character_id: c for c in EveCharacter.objects.filter(character_id__in=ids)}
    ownerships = (
        CharacterOwnership.objects.filter(character__character_id__in=ids)
        .select_related("character", "user__profile__main_character")
    )
    by_id = {o.character.character_id: o for o in ownerships}
    external = cached_identities([cid for cid in ids if cid not in chars])
    result = {}
    for cid in ids:
        char = chars.get(cid)
        if char is None:
            result[cid] = _external_identity(cid, external.get(cid))
            continue
        ownership = by_id.get(cid)
        user = ownership.user if ownership else None
        main = None
        if user:
            try:
                main = user.profile.main_character
            except Exception:
                pass
        source = main or char
        result[cid] = CharacterIdentity(
            character_id=cid,
            character_name=getattr(char, "character_name", "") or placeholder_name(cid),
            user=user,
            main_character_id=getattr(main, "character_id", None),
            main_character_name=getattr(main, "character_name", "") or "",
            corporation_id=getattr(source, "corporation_id", None),
            corporation_name=getattr(source, "corporation_name", "") or "",
            alliance_id=getattr(source, "alliance_id", None),
            alliance_name=getattr(source, "alliance_name", "") or "",
        )
    return result


def corporation_main_count(corporation_id: int) -> int:
    return UserProfile.objects.filter(main_character__corporation_id=corporation_id).exclude(main_character=None).count()
