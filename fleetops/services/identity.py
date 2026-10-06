from dataclasses import dataclass

from allianceauth.authentication.models import CharacterOwnership, UserProfile
from allianceauth.eveonline.models import EveCharacter


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


def identity_for_character_id(character_id: int) -> CharacterIdentity:
    char = EveCharacter.objects.filter(character_id=character_id).first()
    ownership = (
        CharacterOwnership.objects.filter(character__character_id=character_id)
        .select_related("user__profile__main_character", "character")
        .first()
    )
    user = ownership.user if ownership else None
    main = None
    if user:
        try:
            main = user.profile.main_character
        except Exception:
            main = None
    source = main or char
    return CharacterIdentity(
        character_id=character_id,
        character_name=getattr(char, "character_name", "") or f"Character {character_id}",
        user=user,
        main_character_id=getattr(main, "character_id", None),
        main_character_name=getattr(main, "character_name", "") or "",
        corporation_id=getattr(source, "corporation_id", None),
        corporation_name=getattr(source, "corporation_name", "") or "",
        alliance_id=getattr(source, "alliance_id", None),
        alliance_name=getattr(source, "alliance_name", "") or "",
    )


def resolve_many(character_ids: list[int]) -> dict[int, CharacterIdentity]:
    ids = list({int(x) for x in character_ids})
    chars = {c.character_id: c for c in EveCharacter.objects.filter(character_id__in=ids)}
    ownerships = (
        CharacterOwnership.objects.filter(character__character_id__in=ids)
        .select_related("character", "user__profile__main_character")
    )
    by_id = {o.character.character_id: o for o in ownerships}
    result = {}
    for cid in ids:
        char = chars.get(cid)
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
            character_name=getattr(char, "character_name", "") or f"Character {cid}",
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
