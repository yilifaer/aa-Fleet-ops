"""FleetOps SRP: the member pages for starting a claim, the fleet SRP link and the claim page."""

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from fleetops import __version__
from fleetops.forms_srp import (
    ApproveForm,
    ClaimInputForm,
    NoteForm,
    PaidForm,
    ReasonForm,
    ReclassifyForm,
    SubmitClaimForm,
    WithdrawForm,
)
from fleetops.models import SrpClaim, SrpFleetLink, SrpSettings
from fleetops.providers.esi import FleetESIError
from fleetops.services import sde
from fleetops.services.periods import month_bounds
from fleetops.services.stats_access import BASIC_ACCESS
from fleetops.srp import access, claims, links, summaries
from fleetops.srp import policy as rules
from fleetops.srp.access import REQUEST_SRP
from fleetops.srp.types import Calculation, PolicySnapshot, SrpDuplicateError, SrpError, warnings_from_json
from fleetops.views import _period_render_context, _render, _year_month

CLAIMS_PER_PAGE = 25
NOT_ENABLED = "FleetOps SRP is not enabled."
OUT_OF_FLEET_OFF = "Only losses in FleetOps fleets are covered."

# The forms of the claim page, by the action that receives them. A form without an entry (recalculate and
# recheck) is just a button.
DECISION_FORMS = {
    "approve": ApproveForm,
    "reject": ReasonForm,
    "revoke": ReasonForm,
    "paid": PaidForm,
    "unpaid": ReasonForm,
    "reopen": ReasonForm,
    "reclassify": ReclassifyForm,
    "note": NoteForm,
}
# What the state machine allows on a claim of each status; everything else is refused by the service as well.
ACTIONS_BY_STATUS = {
    SrpClaim.Status.PENDING: ("approve", "reject", "reclassify", "recalculate"),
    SrpClaim.Status.APPROVED: ("approve", "revoke", "paid"),
    SrpClaim.Status.PAID: ("unpaid",),
    SrpClaim.Status.REJECTED: ("reopen",),
    SrpClaim.Status.WITHDRAWN: (),
}
# The decisions that need nothing but a reason: title, button, button style and the help text under the box.
REASON_ACTIONS = {
    "reject": ("Reject claim", "Reject", "btn-danger", "The pilot sees the reason."),
    "revoke": (
        "Revoke approval",
        "Revoke approval",
        "btn-outline-danger",
        "The claim is pending again and its amount no longer counts toward the limits. The pilot sees the reason.",
    ),
    "unpaid": (
        "Undo payment",
        "Undo payment",
        "btn-outline-danger",
        "Use this when the ISK was not sent after all. The claim is approved again.",
    ),
    "reopen": (
        "Reopen claim",
        "Reopen",
        "btn-outline-primary",
        "The claim is calculated again with the current rules and waits for a decision.",
    ),
}
# Fleets a claim can be moved into: open links of fleets that started shortly before the loss.
LINK_CANDIDATE_WINDOW = timedelta(hours=24)
LINK_CANDIDATES_SHOWN = 20

# Inventory flags by group, in the order the groups are listed.
ITEM_GROUPS = (
    ("High slots", range(27, 35)),
    ("Mid slots", range(19, 27)),
    ("Low slots", range(11, 19)),
    ("Rigs", range(92, 100)),
    ("Subsystems", range(125, 133)),
    ("Drone bay", (87,)),
    ("Fighters", range(158, 164)),
    ("Implants", (89,)),
)
OTHER_ITEMS = "Cargo and other"
MAX_ITEM_ROWS = 300
MAX_ITEM_DEPTH = 4


def srp_render(request, template_name, context=None, *, settings_obj=None):
    """Render an SRP page: the FleetOps navigation context plus the SRP settings every SRP page reads."""
    settings_obj = settings_obj or SrpSettings.get_solo()
    page = {"srp_settings": settings_obj, "srp_enabled": settings_obj.enabled, "fleetops_version": __version__}
    page.update(context or {})
    return _render(request, template_name, page)


def claim_queryset():
    """Claims with what the claim page and the decision checks read."""
    return SrpClaim.objects.select_related(
        "killmail", "user__profile__main_character", "link", "operation", "decided_by", "paid_by"
    )


def get_claim(claim_uuid):
    return get_object_or_404(claim_queryset(), uuid=claim_uuid)


def claim_redirect(claim):
    return redirect("fleetops:srp_claim", claim_uuid=claim.uuid)


def error_text(exc) -> str:
    """The sentence for a failed lookup or claim step; ESI texts are written for people."""
    return exc.message if isinstance(exc, SrpError) else str(exc)


# --- My SRP ------------------------------------------------------------------------------------------------


def home_context(request, year, month, claim_form, settings_obj):
    """The allowance, recent fleets and the claims of the month, or just the form while SRP is off."""
    context = _period_render_context(year, month, request=request, claim_form=claim_form)
    if not settings_obj.enabled:
        return context
    user = request.user
    personal = summaries.personal(user, year, month)
    start, end = month_bounds(year, month)
    month_claims = SrpClaim.objects.filter(user=user, killmail_time__gte=start, killmail_time__lt=end).order_by(
        "-killmail_time", "-pk"
    )
    page = Paginator(month_claims, CLAIMS_PER_PAGE).get_page(request.GET.get("page"))
    context.update(
        {
            "personal": personal,
            "totals": personal.totals,
            "limits": personal.limits,
            "recent_links": links.recent_links_for_user(user),
            "page": page,
            "claims": page.object_list,
        }
    )
    return context


@require_GET
@permission_required(REQUEST_SRP, raise_exception=True)
def srp_home(request):
    """My SRP: this month's allowance, the out-of-fleet form, recent fleets with SRP and my claims."""
    year, month = _year_month(request)
    settings_obj = SrpSettings.get_solo()
    context = home_context(request, year, month, ClaimInputForm(), settings_obj)
    return srp_render(request, "fleetops/srp/home.html", context, settings_obj=settings_obj)


def preview_response(request, preview, *, link=None, input_text="", settings_obj=None):
    """The preview of a checked loss with the form that submits it."""
    return srp_render(
        request,
        "fleetops/srp/preview.html",
        {
            "preview": preview,
            "link": link,
            "operation": link.operation if link is not None else None,
            "input_text": input_text,
            "submit_form": SubmitClaimForm(initial={"token": preview.token}),
        },
        settings_obj=settings_obj,
    )


def check_loss(request, form, *, link=None):
    """Check what a member pasted. Returns the preview, or None after adding what is wrong to the form."""
    try:
        return claims.prepare_claim(request.user, form.cleaned_data["killmail"], link=link)
    except (SrpError, FleetESIError) as exc:
        form.add_error(None, error_text(exc))
        return None


@require_POST
@permission_required(REQUEST_SRP, raise_exception=True)
def srp_check(request):
    """Check an out-of-fleet loss: the preview, or the home page again with what is wrong with the input."""
    settings_obj = SrpSettings.get_solo()
    form = ClaimInputForm(request.POST)
    if settings_obj.enabled:
        if not settings_obj.out_of_fleet_enabled:
            form.is_valid()
            form.add_error(None, OUT_OF_FLEET_OFF)
        elif form.is_valid():
            preview = check_loss(request, form)
            if preview is not None:
                return preview_response(request, preview, input_text=form.cleaned_data["killmail"], settings_obj=settings_obj)
    year, month = _year_month(request)
    context = home_context(request, year, month, form, settings_obj)
    return srp_render(request, "fleetops/srp/home.html", context, settings_obj=settings_obj)


# --- The SRP page of a fleet ---------------------------------------------------------------------------------


@require_http_methods(["GET", "POST"])
@permission_required(REQUEST_SRP, raise_exception=True)
def srp_link(request, code):
    """The SRP page of one fleet; a POST checks the pasted loss and shows the preview.

    A closed link, or SRP switched off, shows a notice instead of the form.
    """
    fleet_links = SrpFleetLink.objects.select_related("operation__fleet_type", "operation__fc_user")
    link = get_object_or_404(fleet_links, code=code)
    settings_obj = SrpSettings.get_solo()
    usable = settings_obj.enabled and link.is_open
    form = ClaimInputForm(request.POST if request.method == "POST" and usable else None)
    if usable and request.method == "POST" and form.is_valid():
        preview = check_loss(request, form, link=link)
        if preview is not None:
            return preview_response(
                request, preview, link=link, input_text=form.cleaned_data["killmail"], settings_obj=settings_obj
            )
    context = {"link": link, "operation": link.operation, "claim_form": form, "link_open": link.is_open}
    if settings_obj.enabled:
        context["my_claims"] = SrpClaim.objects.filter(user=request.user, link=link).order_by("-killmail_time")
    return srp_render(request, "fleetops/srp/link.html", context, settings_obj=settings_obj)


@require_POST
@permission_required(REQUEST_SRP, raise_exception=True)
def srp_submit(request):
    """Submit a previewed loss. A token that does not check out goes back to My SRP with a message; a claim
    that was created (or already existed) opens its page."""
    settings_obj = SrpSettings.get_solo()
    if not settings_obj.enabled:
        messages.error(request, NOT_ENABLED)
        return redirect("fleetops:srp_home")
    form = SubmitClaimForm(request.POST)
    if not form.is_valid():
        messages.error(request, "That claim could not be read. Check the loss again and submit it.")
        return redirect("fleetops:srp_home")
    try:
        claim = claims.submit_claim(request.user, form.cleaned_data["token"], comment=form.cleaned_data["comment"])
    except SrpDuplicateError as exc:
        messages.error(request, exc.message)
        if exc.claim is not None and exc.claim.user_id == request.user.pk:
            return claim_redirect(exc.claim)
        return redirect("fleetops:srp_home")
    except SrpError as exc:
        messages.error(request, exc.message)
        return redirect("fleetops:srp_home")
    if claim.status == SrpClaim.Status.REJECTED:
        messages.warning(request, f"{claim.reference} was rejected automatically. {claim.decision_reason}".strip())
    else:
        messages.success(request, f"{claim.reference} submitted. A reviewer will decide on it.")
    return claim_redirect(claim)


# --- A claim -----------------------------------------------------------------------------------------------------


def _int(value, default=0):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _item_rows(items):
    """The victim's items as (flag, type id, destroyed, dropped), nested items included; unreadable entries
    are left out."""
    stack = [(item, 0) for item in reversed(items if isinstance(items, list) else [])]
    seen = 0
    while stack and seen < 20_000:
        item, depth = stack.pop()
        if not isinstance(item, dict):
            continue
        seen += 1
        type_id = _int(item.get("item_type_id"))
        if type_id:
            yield _int(item.get("flag")), type_id, _int(item.get("quantity_destroyed")), _int(item.get("quantity_dropped"))
        nested = item.get("items")
        if depth < MAX_ITEM_DEPTH and isinstance(nested, list):
            stack.extend((child, depth + 1) for child in reversed(nested))


def item_groups(items):
    """The items of a killmail by slot group: ``[{"name", "rows": [{"type_id", "name", "destroyed", "dropped"}]}]``.

    Names come from the SDE when it is installed and are the type ID otherwise; nothing is fetched.
    """
    group_of = {flag: name for name, flags in ITEM_GROUPS for flag in flags}
    grouped: dict[str, dict[int, list[int]]] = {}
    for flag, type_id, destroyed, dropped in _item_rows(items):
        row = grouped.setdefault(group_of.get(flag, OTHER_ITEMS), {}).setdefault(type_id, [0, 0])
        row[0] += destroyed
        row[1] += dropped
    names = sde.item_type_names({type_id for rows in grouped.values() for type_id in rows}) if grouped else {}
    order = [name for name, _flags in ITEM_GROUPS] + [OTHER_ITEMS]
    return [
        {
            "name": name,
            "rows": [
                {"type_id": type_id, "name": names.get(type_id) or f"Type {type_id}", "destroyed": counts[0], "dropped": counts[1]}
                for type_id, counts in sorted(grouped[name].items(), key=lambda pair: (names.get(pair[0]) or "", pair[0]))
            ][:MAX_ITEM_ROWS],
        }
        for name in order
        if name in grouped
    ]


def decision_form(action, data=None, claim=None):
    """The form of one decision on ``claim``. Every form has its own element IDs, so the page can hold them all."""
    initial = {}
    if claim is not None and action == "approve":
        initial["amount"] = claim.approved_amount if claim.status == SrpClaim.Status.APPROVED else claim.suggested_amount
    if claim is not None and action == "reclassify":
        initial["scope"] = (
            ReclassifyForm.OUT_OF_FLEET if claim.scope == SrpClaim.Scope.IN_FLEET else ReclassifyForm.IN_FLEET
        )
    return DECISION_FORMS[action](data, initial=initial, auto_id=f"id_{action}_%s")


def within_limit_reason(claim) -> str:
    """The reason that goes with "Approve within limit" when the reviewer wrote none."""
    kind = Calculation.from_json(claim.calculation).binding_limit
    if kind in ("personal", "corporation"):
        return f"Reduced to the remaining monthly {kind} limit."
    return "Reduced to the remaining monthly limit."


def link_candidates(claim):
    """Open links of fleets that had started when the ship was lost, for moving a claim into a fleet."""
    return list(
        SrpFleetLink.objects.filter(
            is_open=True,
            operation__started_at__lte=claim.killmail_time + timedelta(hours=1),
            operation__started_at__gte=claim.killmail_time - LINK_CANDIDATE_WINDOW,
        )
        .select_related("operation__fleet_type")
        .order_by("-operation__started_at")[:LINK_CANDIDATES_SHOWN]
    )


def claim_page_context(request, claim, *, bound=None, settings_obj=None):
    """What claim.html shows. ``bound`` maps an action to the form the person just sent, with its input and errors."""
    user = request.user
    settings_obj = settings_obj or SrpSettings.get_solo()
    is_manager = access.is_manager(user)
    is_owner = claim.user_id == user.pk
    can_decide = access.can_review(user, claim, settings_obj=settings_obj)
    events = claim.events.select_related("actor").order_by("created_at", "pk")
    if not access.can_view_internal_notes(user):
        events = events.filter(internal=False)
    calculation = Calculation.from_json(claim.calculation)
    show_limits = access.can_view_limits(user, claim)
    context = {
        "claim": claim,
        "killmail": claim.killmail,
        "events": events,
        "is_owner": is_owner,
        "is_manager": is_manager,
        "can_decide": can_decide,
        "can_withdraw": is_owner and claim.status == SrpClaim.Status.PENDING,
        "can_resubmit": is_owner and claim.status == SrpClaim.Status.WITHDRAWN,
        "calculation": calculation,
        "policy": PolicySnapshot.from_json(claim.policy),
        "show_limits": show_limits,
        "limits": calculation.limits if show_limits else (),
        "claim_warnings": warnings_from_json(claim.warnings),
        "item_groups": item_groups(claim.killmail.items),
        "withdraw_form": WithdrawForm(auto_id="id_withdraw_%s"),
    }
    if is_manager:
        bound = bound or {}
        context["self_review_blocked"] = not can_decide
        context["pay_to"] = rules.pay_to(claim)[1]
        context["note_form"] = bound.get("note") or decision_form("note")
        context["actions"] = ()
        if can_decide:
            actions = ACTIONS_BY_STATUS.get(claim.status, ())
            forms = {
                action: bound.get(action) or decision_form(action, claim=claim)
                for action in DECISION_FORMS
                if action != "note"
            }
            context.update(
                {
                    "actions": actions,
                    "forms": forms,
                    "limit_reason": within_limit_reason(claim),
                    "reason_actions": [
                        {"action": action, "title": title, "button": button, "css": css, "help": text, "form": forms[action]}
                        for action, (title, button, css, text) in REASON_ACTIONS.items()
                        if action in actions
                    ],
                    "link_candidates": link_candidates(claim) if "reclassify" in actions else [],
                }
            )
    return context


def claim_response(request, claim, *, bound=None, settings_obj=None):
    return srp_render(
        request,
        "fleetops/srp/claim.html",
        claim_page_context(request, claim, bound=bound, settings_obj=settings_obj),
        settings_obj=settings_obj,
    )


@require_GET
@permission_required(BASIC_ACCESS, raise_exception=True)
def srp_claim_detail(request, claim_uuid):
    """A claim. Who may open it is decided by ``access.can_view_claim``; everyone else gets a 404.

    Internal notes stay with the SRP managers; the claimant and the other viewers see the timeline without them.
    """
    claim = get_claim(claim_uuid)
    if not access.can_view_claim(request.user, claim):
        raise Http404
    return claim_response(request, claim)


@require_POST
@permission_required(REQUEST_SRP, raise_exception=True)
def srp_claim_withdraw(request, claim_uuid):
    """Withdraw a pending claim; only the claimant. A claim in another state is left as it is, with a message."""
    claim = get_claim(claim_uuid)
    if claim.user_id != request.user.pk:
        raise Http404
    form = WithdrawForm(request.POST)
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    try:
        claims.withdraw_claim(claim, actor=request.user, reason=reason)
    except SrpError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, f"{claim.reference} withdrawn. You can submit it again while it is within the deadline.")
    return claim_redirect(claim)


@require_POST
@permission_required(REQUEST_SRP, raise_exception=True)
def srp_claim_resubmit(request, claim_uuid):
    """Resubmit a withdrawn claim; only the claimant. A claim in another state is left as it is, with a message."""
    claim = get_claim(claim_uuid)
    if claim.user_id != request.user.pk:
        raise Http404
    try:
        claims.resubmit_claim(claim, actor=request.user)
    except SrpError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, f"{claim.reference} submitted again with the current rules.")
    return claim_redirect(claim)
