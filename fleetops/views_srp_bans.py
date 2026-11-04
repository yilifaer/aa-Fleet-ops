# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FleetOps SRP: the ban list and the pages to make and lift an SRP ban. Reviewers only."""

import uuid

from allianceauth.authentication.models import UserProfile
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from fleetops.decorators import fleetops_permission_required
from fleetops.forms_srp import BanFilterForm, BanForm, ReasonForm
from fleetops.models import SrpBan, SrpClaim, SrpSettings
from fleetops.srp import bans
from fleetops.srp.access import MANAGE_SRP, REVIEW_SRP
from fleetops.srp.types import SrpError
from fleetops.texts import translate_stored
from fleetops.views_srp import srp_render
from fleetops.views_srp_review import back, report_skipped

REVIEWER = (MANAGE_SRP, REVIEW_SRP)
BANS_PER_PAGE = 50


def _state_filter(qs, state, now):
    in_force = Q(lifted_at__isnull=True, starts_at__lte=now) & (Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    if state == "lifted":
        return qs.filter(lifted_at__isnull=False)
    if state == "expired":
        return qs.filter(lifted_at__isnull=True, expires_at__lte=now)
    if state == "all":
        return qs
    return qs.filter(in_force)


@require_GET
@fleetops_permission_required(REVIEWER, any_perm=True)
def srp_bans(request):
    """The SRP bans, newest first; by default the ones in force. 50 per page."""
    form = BanFilterForm(request.GET)
    form.is_valid()
    now = timezone.now()
    qs = SrpBan.objects.select_related("user", "created_by", "lifted_by")
    qs = _state_filter(qs, form.cleaned_data.get("state") or "active", now)
    if form.cleaned_data.get("kind"):
        qs = qs.filter(kind=form.cleaned_data["kind"])
    if form.cleaned_data.get("member"):
        qs = qs.filter(main_character_name__icontains=form.cleaned_data["member"])
    page = Paginator(qs, BANS_PER_PAGE).get_page(request.GET.get("page"))
    rows = list(page.object_list)
    for ban in rows:
        ban.in_force_now = ban.in_force(now)
    query = request.GET.copy()
    query.pop("page", None)
    return srp_render(
        request,
        "fleetops/srp/bans.html",
        {"filter_form": form, "page": page, "bans": rows, "next_url": request.get_full_path(), "filter_query": query.urlencode()},
    )


def _member(user_id, name):
    """The user a ban form names: by ID (from the claim page) or by the exact name of the main character."""
    users = get_user_model().objects
    if user_id:
        return users.filter(pk=user_id).first()
    if name:
        profile = (
            UserProfile.objects.select_related("user")
            .filter(main_character__character_name__iexact=name)
            .order_by("pk")
            .first()
        )
        return profile.user if profile is not None else None
    return None


@require_http_methods(["GET", "POST"])
@fleetops_permission_required(REVIEWER, any_perm=True)
def srp_ban_new(request):
    """Ban a member: a length or permanent, the reason the member sees, an internal note and, if wanted, the
    rejection of the pending claims."""
    srp = SrpSettings.get_solo()
    source = request.POST if request.method == "POST" else request.GET
    member = _member(_int(source.get("user")), (source.get("member") or "").strip() if request.method == "POST" else "")
    claim = None
    try:
        claim_uuid = uuid.UUID(request.GET.get("claim", ""))
    except ValueError:
        claim_uuid = None
    if claim_uuid is not None:
        claim = SrpClaim.objects.filter(uuid=claim_uuid).first()
    if request.method == "POST":
        form = BanForm(request.POST, default_days=srp.ban_default_days)
        if form.is_valid():
            data = form.cleaned_data
            if member is None:
                form.add_error("member", _("No member with this main character was found."))
            else:
                try:
                    ban, result = bans.ban_user(
                        member,
                        actor=request.user,
                        days=data["days"],
                        permanent=data["permanent"],
                        reason=data["reason"],
                        internal_note=data["internal_note"],
                        reject_pending=data["reject_pending"],
                        reject_reason=data["reject_reason"],
                    )
                except SrpError as exc:
                    messages.error(request, translate_stored(exc.message))
                else:
                    messages.success(
                        request,
                        _("%(name)s can no longer submit SRP claims.") % {"name": ban.main_character_name or ban.user_id},
                    )
                    if data["reject_pending"]:
                        messages.info(
                            request,
                            _("%(count)s pending claims were rejected.") % {"count": len(result.done)},
                        )
                        report_skipped(request, [(c, t) for c, t in result.skipped if hasattr(c, "reference")])
                    return back(request, redirect("fleetops:srp_bans"))
    else:
        initial = {"user": member.pk} if member is not None else {}
        form = BanForm(initial=initial, default_days=srp.ban_default_days)
    pending = (
        SrpClaim.objects.filter(user=member, status=SrpClaim.Status.PENDING).count() if member is not None else 0
    )
    return srp_render(
        request,
        "fleetops/srp/ban_form.html",
        {
            "form": form,
            "member": member,
            "member_name": getattr(getattr(getattr(member, "profile", None), "main_character", None), "character_name", ""),
            "claim": claim,
            "pending_count": pending,
            "existing": bans.active_ban(member) if member is not None else None,
            "ban_max_days": srp.ban_max_days,
            "next_url": request.GET.get("next", ""),
        },
        settings_obj=srp,
    )


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@require_POST
@fleetops_permission_required(REVIEWER, any_perm=True, page=False)
def srp_ban_lift(request, ban_id):
    """Lift a ban that is in force; the reason is required."""
    ban = get_object_or_404(SrpBan, pk=ban_id)
    form = ReasonForm(request.POST)
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    try:
        bans.lift_ban(ban, actor=request.user, reason=reason)
    except SrpError as exc:
        messages.error(request, translate_stored(exc.message))
    else:
        messages.success(request, _("The SRP ban was lifted."))
    return back(request, redirect("fleetops:srp_bans"))
