# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FleetOps SRP: the authorization for loss reading, the refresh, and the actions on the losses found for a member.

The losses are listed on My SRP (``views_srp.home_context``). Everything here works on the losses of the characters
the signed-in member owns now; a loss of anybody else is a 404.
"""

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.services.hooks import get_extension_logger
from django.contrib import messages
from django.core.cache import cache
from django.http import Http404
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_GET, require_POST
from esi.decorators import token_required

from fleetops import texts
from fleetops.constants import LOSS_SCOPES
from fleetops.decorators import fleetops_permission_required
from fleetops.models import MAX_DATABASE_ID, SrpCategory, SrpFleetLink, SrpSettings
from fleetops.providers.esi import FleetESIError
from fleetops.services.audit import audit
from fleetops.services.errors import log_failure
from fleetops.srp import claims, discovery
from fleetops.srp.access import REQUEST_SRP
from fleetops.srp.types import KillmailRef, SrpError
from fleetops.texts import translate_stored
from fleetops.views_srp import error_text, preview_response

logger = get_extension_logger(__name__)

NOT_ENABLED = gettext_lazy("Finding losses automatically is not enabled.")


def _home():
    return redirect("fleetops:srp_home")


def _discovery_on():
    """The settings when losses are found automatically, else None."""
    srp = SrpSettings.get_solo()
    return srp if discovery.discovery_active(srp) else None


@require_GET
@fleetops_permission_required(REQUEST_SRP)
def srp_losses_authorize(request):
    """Send the member to EVE login to authorize loss reading for one character; the way back is the same URL."""
    if _discovery_on() is None:
        messages.error(request, NOT_ENABLED)
        return _home()
    try:
        return _authorized(request)
    except Exception as exc:
        # An EVE application without the scope is refused during the login. Nothing but the class of the error is
        # logged: the exception texts of the login libraries can carry codes and tokens.
        log_failure(logger, "SRP loss reading authorization", exc)
        messages.error(
            request,
            _(
                "Loss reading could not be authorized. If EVE reports an invalid scope, ask an administrator to add "
                "the scope esi-killmails.read_killmails.v1 to the EVE developer application."
            ),
        )
        return _home()


@token_required(scopes=LOSS_SCOPES, new=True)
def _authorized(request, token):
    """Back from the EVE login with a new token. A character that is not on the member's account is not read."""
    owned = CharacterOwnership.objects.filter(user=request.user, character__character_id=token.character_id).exists()
    if not owned:
        messages.error(request, _("That character is not on your account. It will not be read."))
        return _home()
    discovery.mark_due([token.character_id])
    messages.success(request, _("Loss reading is authorized. Your recent losses will appear shortly."))
    return _home()


@require_POST
@fleetops_permission_required(REQUEST_SRP, page=False)
def srp_losses_refresh(request):
    """Read the member's characters again now. At most once in five minutes; ESI is called by the task, not here."""
    if _discovery_on() is None:
        messages.error(request, NOT_ENABLED)
        return _home()
    try:
        allowed = cache.add(f"fleetops:srp:loss-refresh:{request.user.pk}", 1, discovery.REFRESH_COOLDOWN_SECONDS)
    except Exception as exc:
        log_failure(logger, "SRP loss refresh cooldown", exc)
        allowed = True
    if not allowed:
        messages.warning(request, translate_stored(texts.SRP_LOSS_REFRESH_WAIT()))
        return _home()
    ids = CharacterOwnership.objects.filter(user=request.user).values_list("character__character_id", flat=True)
    discovery.mark_due(list(ids))
    from fleetops import tasks

    try:
        tasks.scan_srp_losses_for_user.delay(request.user.pk)
    except Exception as exc:
        log_failure(logger, "Queueing the SRP loss refresh", exc)
        messages.error(request, _("Your losses could not be refreshed right now. They are read in the next regular run."))
        return _home()
    messages.success(request, _("Your characters are being read. New losses appear on this page shortly."))
    return _home()


def _loss_or_404(request, killmail_id):
    if not 0 < killmail_id <= MAX_DATABASE_ID:
        raise Http404
    loss = discovery.owned_loss(request.user, killmail_id)
    if loss is None:
        raise Http404
    return loss


@require_POST
@fleetops_permission_required(REQUEST_SRP, page=False)
def srp_loss_ignore(request, killmail_id):
    """Hide a loss from the member's list. Pasting its link still works."""
    loss = _loss_or_404(request, killmail_id)
    loss.ignored_by, loss.ignored_at = request.user, timezone.now()
    loss.save(update_fields=["ignored_by", "ignored_at"])
    audit(request.user, "srp.loss_ignore", loss, None, {"killmail_id": loss.killmail_id, "character_id": loss.character_id})
    messages.success(request, _("The loss is hidden from your list."))
    return _home()


@require_POST
@fleetops_permission_required(REQUEST_SRP, page=False)
def srp_loss_unignore(request, killmail_id):
    loss = _loss_or_404(request, killmail_id)
    loss.ignored_by, loss.ignored_at = None, None
    loss.save(update_fields=["ignored_by", "ignored_at"])
    audit(request.user, "srp.loss_unignore", loss, None, {"killmail_id": loss.killmail_id, "character_id": loss.character_id})
    messages.success(request, _("The loss is back on your list."))
    return _home()


def _target(loss, text):
    """``(link, category)`` for the ``target`` of the form: ``fleet:<code>``, ``link:<code>``, ``category:<id>`` or
    empty (the fleet the loss was matched to, else a loss outside a fleet). Raises ``SrpError``."""
    text = (text or "").strip()
    if not text:
        return discovery.matched_open_link(loss), None
    kind, _separator, value = text.partition(":")
    if kind in ("fleet", "link"):
        wanted = SrpFleetLink.Kind.FLEET if kind == "fleet" else SrpFleetLink.Kind.STANDING
        link = (
            SrpFleetLink.objects.select_related("operation__fleet_type", "fc_user", "category")
            .filter(code=value, kind=wanted)
            .first()
        )
        if link is None:
            raise SrpError("link_unavailable", texts.SRP_LOSS_LINK_UNAVAILABLE())
        return link, None
    if kind == "category" and value.isdigit() and int(value) <= MAX_DATABASE_ID:
        category = SrpCategory.objects.filter(pk=int(value)).first()
        if category is None:
            raise SrpError("category_unavailable", texts.SRP_CATEGORY_UNAVAILABLE())
        return None, category
    raise SrpError("invalid_target", texts.SRP_LOSS_LINK_UNAVAILABLE())


@require_POST
@fleetops_permission_required(REQUEST_SRP, page=False)
def srp_loss_check(request, killmail_id):
    """Check a found loss for a claim: the same preview as for a pasted link, then the usual submit."""
    loss = _loss_or_404(request, killmail_id)
    srp = SrpSettings.get_solo()
    if not srp.enabled:
        messages.error(request, _("FleetOps SRP is not enabled."))
        return _home()
    try:
        link, category = _target(loss, request.POST.get("target"))
        preview = claims.prepare_claim_for_ref(
            request.user,
            KillmailRef(loss.killmail_id, loss.killmail_hash or None, "discovered"),
            link=link,
            category=category,
            source="discovered",
        )
    except (SrpError, FleetESIError) as exc:
        messages.error(request, error_text(exc))
        return _home()
    return preview_response(request, preview, link=link, settings_obj=srp, loss=loss)
