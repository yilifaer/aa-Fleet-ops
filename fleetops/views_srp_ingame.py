# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FleetOps SRP payments: opening a character in the game client and choosing the character that does it.

The button "Open in game" asks ESI to open the information window of a character in the client of the payer's
in-game character (scope ``esi-ui.open_window.v1``, an optional scope that is only requested when a payer adds that
character). The endpoint takes the claim and what to open (``main`` or ``lost``), never a character ID, and only
for claims the payments page shows: waiting for payment or paid in the last days.
"""

from datetime import timedelta

from allianceauth.services.hooks import get_extension_logger
from django.contrib import messages
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST
from esi.decorators import token_required

from fleetops.constants import OPEN_WINDOW_SCOPES
from fleetops.decorators import fleetops_permission_required
from fleetops.models import SrpClaim
from fleetops.providers import esi as esi_provider
from fleetops.providers.esi import FleetESIError
from fleetops.services.errors import log_failure
from fleetops.services.identity import owned_characters
from fleetops.srp import ingame, policy as rules
from fleetops.srp.access import MANAGE_SRP, PAY_SRP
from fleetops.texts import translate_stored
from fleetops.views_srp_review import back

logger = get_extension_logger(__name__)

PAYERS = (MANAGE_SRP, PAY_SRP)


def _payments():
    return redirect("fleetops:srp_payments")


def _wants_json(request) -> bool:
    return request.headers.get("x-requested-with") == "XMLHttpRequest" or "application/json" in request.headers.get(
        "accept", ""
    )


def _answer(request, ok: bool, text: str, status: int = 200):
    """A JSON answer for the script of the page, else a message and the page the button was on."""
    if _wants_json(request):
        return JsonResponse({"ok": ok, "message": text}, status=status)
    (messages.success if ok else messages.error)(request, text)
    return back(request, _payments())


def _claim_for_open(uuid):
    """The claim when the payments page shows it: approved (waiting for payment) or paid in the last days."""
    claim = get_object_or_404(SrpClaim.objects.select_related("user__profile__main_character"), uuid=uuid)
    recent = timezone.now() - timedelta(days=ingame.OPEN_PAID_DAYS)
    if claim.status == SrpClaim.Status.APPROVED or (
        claim.status == SrpClaim.Status.PAID and claim.paid_at is not None and claim.paid_at >= recent
    ):
        return claim
    raise Http404


@require_POST
@fleetops_permission_required(PAYERS, any_perm=True, page=False)
def open_in_game(request, claim_uuid, kind):
    """Open the main character of the player (``main``) or the character that lost the ship (``lost``) in the game.

    Not a data change: no audit entry and no stored result; the click is logged at debug level without any token.
    """
    if kind not in ingame.KINDS:
        raise Http404
    claim = _claim_for_open(claim_uuid)
    setting = ingame.get_setting(request.user)
    if setting is None or not setting.game_character_id:
        return _answer(
            request,
            False,
            _("Choose your in-game character first (Opening characters in game, on the payments page)."),
            400,
        )
    source = setting.game_character_id
    name = setting.game_character_name
    target = claim.character_id if kind == ingame.KIND_LOST else rules.pay_to(claim)[0] or claim.character_id
    logger.debug("SRP open in game: claim %s, %s, from character %s.", claim.pk, kind, source)
    try:
        esi_provider.open_character_window(request.user, source, target)
    except FleetESIError as exc:
        if exc.code == "MISSING_SCOPE":
            text = _("The token of %(character)s is missing or has expired. Add the character again.") % {
                "character": name
            }
            return _answer(request, False, text, 409)
        if exc.code == "OPEN_WINDOW_FORBIDDEN":
            text = _("EVE refused the request for %(character)s. Add the character again.") % {"character": name}
            return _answer(request, False, text, 409)
        status = 429 if exc.code == "OPEN_WINDOW_LIMITED" else 502
        return _answer(request, False, translate_stored(str(exc)), status)
    text = _(
        "Sent to the game client of %(character)s. If nothing opens, the client must be running and logged in with "
        "that character."
    ) % {"character": name}
    return _answer(request, True, text)


@require_GET
@fleetops_permission_required(PAYERS, any_perm=True)
def game_character_authorize(request):
    """Send the payer to EVE login to authorize one character for opening windows; the way back is the same URL."""
    try:
        return _authorized(request)
    except Exception as exc:
        # An EVE application without the scope is refused during the login. Nothing but the class of the error is
        # logged: the exception texts of the login libraries can carry codes and tokens.
        log_failure(logger, "In-game character authorization", exc)
        messages.error(
            request,
            _(
                "The character could not be authorized. If EVE reports an invalid scope, ask an administrator to add "
                "the scope esi-ui.open_window.v1 to the EVE developer application (the scope is optional and only "
                "needed for the Open in game button)."
            ),
        )
        return _payments()


@token_required(scopes=OPEN_WINDOW_SCOPES, new=True)
def _authorized(request, token):
    return authorized(request, token)


def authorized(request, token):
    """Back from the EVE login with a new token. A character that is not on the payer's account is not offered."""
    owned = any(row.character.character_id == token.character_id for row in owned_characters(request.user))
    if not owned:
        messages.error(request, _("That character is not on your account. It was not added."))
        return _payments()
    messages.success(
        request,
        _("The character is authorized. Choose it as your in-game character in the box Opening characters in game."),
    )
    return redirect(f"{_payments().url}#srp-game-character")


@require_POST
@fleetops_permission_required(PAYERS, any_perm=True, page=False)
def game_character(request):
    """Choose one of the payer's own characters (with the scope on file) as the in-game character, or remove the choice."""
    action = request.POST.get("action")
    if action == "remove":
        if ingame.clear_game_character(request.user):
            messages.success(request, _("The in-game character was removed. Your tokens were not touched."))
        return back(request, _payments())
    if action != "set":
        raise Http404
    chosen = request.POST.get("character_id", "")
    rows = {row["id"]: row for row in ingame.authorized_characters(request.user)}
    row = rows.get(int(chosen)) if chosen.isascii() and chosen.isdigit() and len(chosen) < 19 else None
    if row is None:
        messages.error(request, _("Choose one of your own characters."))
    elif not row["authorized"]:
        messages.error(request, _("That character has no authorization for this yet. Add it first."))
    else:
        ingame.set_game_character(request.user, row["id"], row["name"])
        messages.success(
            request, _("%(character)s is your in-game character.") % {"character": row["name"]}
        )
    return back(request, _payments())


@require_POST
@fleetops_permission_required(PAYERS, any_perm=True, page=False)
def payment_view(request):
    """Remember the view of the payments page (By player or By claim) for the payer and go back to the page."""
    view = request.POST.get("view")
    if view not in ingame.VIEWS:
        raise Http404
    ingame.save_view(request.user, view)
    return back(request, _payments())
