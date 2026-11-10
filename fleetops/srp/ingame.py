# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""What a payer chose on the SRP payments page: the view of the list and the character that opens windows in the game.

Both live in one row per user (``SrpPayerSetting``). Nothing here calls ESI and nothing here touches a token: whether
a character can open windows is read from the token table only (``esi.scope_token_ids``), so drawing the page never
refreshes or deletes a token. Removing the in-game character leaves every token alone.
"""

from fleetops.constants import OPEN_WINDOW_SCOPE
from fleetops.models import SrpPayerSetting
from fleetops.providers import esi as esi_provider
from fleetops.services.identity import owned_characters

VIEW_PLAYER = "player"
VIEW_CLAIM = "claim"
VIEWS = (VIEW_PLAYER, VIEW_CLAIM)
# The claims that can be opened in the game: waiting for payment, or paid in the last days.
OPEN_PAID_DAYS = 30
# What can be opened: the player's main character, or the character that lost the ship.
KIND_MAIN = "main"
KIND_LOST = "lost"
KINDS = (KIND_MAIN, KIND_LOST)


def get_setting(user):
    """The row of ``user`` or None (none is made by looking)."""
    return SrpPayerSetting.objects.filter(user=user).first()


def remembered_view(setting) -> str:
    """The view the payer chose last; By player until they chose."""
    return setting.payment_view if setting is not None and setting.payment_view in VIEWS else VIEW_PLAYER


def choose_view(setting, asked) -> str:
    """The view to draw: ``asked`` (the ``view`` parameter) when it is one, for this request only; else the remembered one.

    Reading the page never writes: the choice is stored by ``save_view`` through a POST.
    """
    return asked if asked in VIEWS else remembered_view(setting)


def save_view(user, view) -> None:
    """Remember the view of ``user`` (one row per user, made on the first choice)."""
    SrpPayerSetting.objects.update_or_create(user=user, defaults={"payment_view": view})


def game_character(setting, characters) -> dict:
    """The in-game character for the page: ``{"id", "name", "ready"}``, or an empty dict when none is chosen.

    ``ready`` is whether the character is one of the user's (``characters`` from ``authorized_characters``) and has a
    token with the scope on file. Nothing is asked of the database here.
    """
    if setting is None or not setting.game_character_id:
        return {}
    ready = any(row["id"] == setting.game_character_id and row["authorized"] for row in characters)
    return {"id": setting.game_character_id, "name": setting.game_character_name, "ready": ready}


def authorized_characters(user) -> list:
    """The characters of ``user`` as ``[{"id", "name", "authorized"}]``: ``authorized`` when a token with the scope is on file."""
    ownerships = list(owned_characters(user))
    ids = [ownership.character.character_id for ownership in ownerships]
    with_token = esi_provider.scope_token_ids(user, ids, OPEN_WINDOW_SCOPE)
    return [
        {
            "id": ownership.character.character_id,
            "name": ownership.character.character_name,
            "authorized": ownership.character.character_id in with_token,
        }
        for ownership in ownerships
    ]


def set_game_character(user, character_id, name) -> None:
    SrpPayerSetting.objects.update_or_create(
        user=user, defaults={"game_character_id": int(character_id), "game_character_name": name[:255]}
    )


def clear_game_character(user) -> bool:
    """Forget the choice (the tokens stay); returns whether there was one."""
    return bool(
        SrpPayerSetting.objects.filter(user=user)
        .exclude(game_character_id=None)
        .update(game_character_id=None, game_character_name="")
    )

