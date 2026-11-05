# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FleetOps SRP: the claimant changes a pending claim (the comment, where it belongs, the loss). The page is a form, then
a preview of the changed claim, then the confirmation; only the claimant opens it (everybody else gets a 404)."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from fleetops import texts
from fleetops.decorators import fleetops_permission_required
from fleetops.forms_srp import ClaimEditForm
from fleetops.models import SrpClaim, SrpSettings
from fleetops.providers.esi import FleetESIError
from fleetops.srp import bans, categories, claims, discovery, links
from fleetops.srp.access import REQUEST_SRP
from fleetops.srp.display import category_label
from fleetops.srp.types import Calculation, PolicySnapshot, SrpDuplicateError, SrpError, SrpStateError, warnings_from_json
from fleetops.services.stats_access import BASIC_ACCESS
from fleetops.texts import translate_stored
from fleetops.views_srp import claim_redirect, get_claim, srp_render


def _refusal(request, claim, srp):
    """The message and redirect that answer a claim the member cannot change now, or None when it can be changed."""
    if not srp.member_edit_enabled:
        return texts.SRP_MEMBER_EDIT_OFF()
    if claim.status != SrpClaim.Status.PENDING:
        return texts.SRP_ONLY_PENDING_EDITED()
    ban = bans.active_ban(request.user)
    if ban is not None:
        return bans.ban_message(ban)
    return None


def target_choices(user):
    """The optgroups of the "Category or link" select: fleets of the last days the member took part in, the open standing
    links and the categories members can choose."""
    fleets = [(f"fleet:{link.code}", links.link_label(link)) for link in links.recent_links_for_user(user)]
    standing = [(f"link:{link.code}", links.link_label(link)) for link in links.open_standing_links()]
    cats = [(f"category:{c.pk}", category_label(c)) for c in categories.selectable_categories().order_by("sort_order", "pk")]
    groups = []
    for title, rows in ((_("Fleets"), fleets), (_("Standing links"), standing), (_("Categories (outside a fleet)"), cats)):
        if rows:
            groups.append((title, rows))
    return groups


def loss_choices(user, srp):
    """``[(killmail id, label)]`` of the member's recent losses nobody claimed, or None while found losses are not read."""
    if not discovery.discovery_active(srp):
        return None
    rows = discovery.recent_losses(user, srp)
    return [
        (
            row.killmail_id,
            f"{row.ship_type_name or row.ship_type_id} - {row.character_name} - {row.killmail_time:%Y-%m-%d %H:%M}",
        )
        for row in rows
    ]


def _form(request, claim, srp, data=None):
    moved = claim.events.filter(action__in=claims.REVIEWER_MOVE_ACTIONS).exists()
    allow_swap = srp.member_swap_loss_enabled
    form = ClaimEditForm(
        data,
        initial={"comment": claim.member_comment},
        target_choices=None if moved else target_choices(request.user),
        loss_choices=loss_choices(request.user, srp) if allow_swap else None,
        allow_swap=allow_swap,
    )
    return form, moved


def _page(request, claim, srp, form, moved):
    return srp_render(
        request,
        "fleetops/srp/claim_edit.html",
        {"claim": claim, "form": form, "reviewer_moved": moved},
        settings_obj=srp,
    )


def _arguments(request, form):
    """``(comment, target, ref)`` from a valid form; raises ``SrpError`` for a loss the member cannot use."""
    data = form.cleaned_data
    comment = data.get("comment", claims.UNSET)
    ref = claims.edit_reference(request.user, data.get("killmail", ""), data.get("loss"))
    return comment, data.get("target", ""), ref


@require_http_methods(["GET", "POST"])
@login_required
@fleetops_permission_required(BASIC_ACCESS)
def srp_claim_edit(request, claim_uuid):
    """Change a pending claim. GET shows the form; a POST checks the change and shows the claim as it would be; a POST
    with the token of that preview applies it.

    Everybody with FleetOps access but the claimant gets a 404 first (SRP staff and superusers included, with or
    without the permission to request); the claimant needs ``request_srp``."""
    claim = get_claim(claim_uuid)
    if claim.user_id != request.user.pk:
        raise Http404
    return _edit_claim(request, claim)


@fleetops_permission_required(REQUEST_SRP)
def _edit_claim(request, claim):
    srp = SrpSettings.get_solo()
    refused = _refusal(request, claim, srp)
    if refused:
        messages.error(request, translate_stored(refused))
        return claim_redirect(claim)
    if request.method == "POST" and request.POST.get("token"):
        return _confirm(request, claim, srp)
    form, moved = _form(request, claim, srp, request.POST if request.method == "POST" else None)
    if request.method == "GET" or not form.is_valid():
        return _page(request, claim, srp, form, moved)
    try:
        comment, target, ref = _arguments(request, form)
        result, token = claims.preview_edit(request.user, claim, comment=comment, target=target, ref=ref)
    except SrpDuplicateError as exc:
        form.add_error(None, translate_stored(exc.message))
        return _page(request, claim, srp, form, moved)
    except (SrpError, FleetESIError) as exc:
        form.add_error(None, translate_stored(exc.message) if isinstance(exc, SrpError) else str(exc))
        return _page(request, claim, srp, form, moved)
    after = result.claim
    return srp_render(
        request,
        "fleetops/srp/claim_edit_preview.html",
        {
            "claim": claim,
            "after": after,
            "edit": result,
            "token": token,
            "killmail": result.killmail,
            "calculation": Calculation.from_json(after.calculation),
            "policy": PolicySnapshot.from_json(after.policy),
            "claim_warnings": warnings_from_json(after.warnings),
            "changed_loss": "loss" in result.changed,
            "changed_place": "link" in result.changed or "category" in result.changed,
            "changed_comment": "comment" in result.changed,
            "previous_calculation": result.previous,
            "insurance": _insurance(after, srp),
        },
        settings_obj=srp,
    )


def _insurance(claim, srp):
    from fleetops.srp import insurance

    return insurance.estimate(claim.ship_type_id, claim.suggested_amount, srp)


def _confirm(request, claim, srp):
    try:
        result = claims.submit_edit(request.user, claim, request.POST.get("token", ""))
    except SrpStateError as exc:
        messages.error(request, translate_stored(exc.message))
        return claim_redirect(claim)
    except SrpDuplicateError as exc:
        messages.error(request, translate_stored(exc.message))
        return claim_redirect(claim)
    except (SrpError, FleetESIError) as exc:
        messages.error(request, translate_stored(exc.message) if isinstance(exc, SrpError) else str(exc))
        return claim_redirect(claim)
    messages.success(request, _("%(reference)s changed. A reviewer will decide on it.") % {"reference": claim.reference})
    return claim_redirect(result.claim)
