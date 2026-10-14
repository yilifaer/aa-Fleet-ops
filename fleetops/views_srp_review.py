"""FleetOps SRP: the review queue, the decisions on claims and the payments list. SRP managers only."""

import re
from datetime import datetime, time, timedelta, timezone as dt_timezone

from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import Http404
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from fleetops.forms_srp import ClaimSelectionForm, PaidForm, PaymentFilterForm, ReviewFilterForm
from fleetops.models import SrpClaim, SrpFleetLink, SrpSettings
from fleetops.services.periods import eve_now, month_bounds
from fleetops.srp import access, display, review
from fleetops.srp import policy as rules
from fleetops.srp.access import MANAGE_SRP
from fleetops.srp.types import SrpError, SrpPayRow, SrpStateError
from fleetops.views import _period_render_context, _year_month
from fleetops.views_srp import (
    DECISION_FORMS,
    claim_redirect,
    claim_response,
    decision_form,
    get_claim,
    srp_render,
    within_limit_reason,
)

# The query parameters the review queue keeps when the month changes.
REVIEW_FILTERS = ("status", "scope", "flag", "corporation", "fc", "q")
PAYMENT_FILTERS = ("paid_from", "paid_to")
REVIEW_PER_PAGE = 50
PAID_SHOWN = 200
PAID_DEFAULT_DAYS = 30
# Lines of "skipped" reasons shown after a bulk action; the rest is counted.
SKIPPED_SHOWN = 5

# The buttons of the claim page. Deciding on a claim (everything but a re-check and an internal note) follows
# the self-review rule: managers do not decide their own claims unless the settings allow it.
DECISION_ACTIONS = frozenset({"approve", "reject", "revoke", "paid", "unpaid", "reopen", "reclassify", "recalculate"})
CLAIM_ACTIONS = DECISION_ACTIONS | {"recheck", "note"}

# Fleet checks that are not a warning.
SOUND_FLEET_CHECKS = [code for code, level in display.PRESENCE_LEVEL_BY_CODE.items() if level == display.LEVEL_OK]
_REFERENCE = re.compile(r"(?:srp-?)?(\d{1,15})", re.IGNORECASE | re.ASCII)


# --- Shared helpers ------------------------------------------------------------------------------------------


def back(request, fallback):
    """Redirect to the page a form came from (``next``, a path of this site); ``fallback`` is the redirect
    that answers otherwise."""
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(target)
    return fallback


def month_filter(request):
    """``(year, month, bounds)``; the month only narrows a list once the person picked one."""
    year, month = _year_month(request)
    picked = "year" in request.GET or "month" in request.GET
    return year, month, month_bounds(year, month) if picked else None


def without_period(request, *drop):
    """The query string of the page without the month, the page number and ``drop``."""
    query = request.GET.copy()
    for name in ("year", "month", "page", *drop):
        query.pop(name, None)
    return query.urlencode()


def form_errors(form):
    return " ".join(str(error) for errors in form.errors.values() for error in errors)


def report_skipped(request, skipped):
    """Tell what a bulk action left out: the first few reasons, then a count."""
    lines = [f"{claim.reference}: {text}" for claim, text in skipped]
    if lines:
        shown = lines[:SKIPPED_SHOWN]
        if len(lines) > SKIPPED_SHOWN:
            shown.append(f"and {len(lines) - SKIPPED_SHOWN} more.")
        messages.warning(request, "Skipped: " + " ".join(shown))


def expected_amounts(form, claims):
    """``{claim ID: amount}`` the list showed for the ticked ``claims`` (``ClaimSelectionForm.shown_amounts``)."""
    shown = form.shown_amounts()
    return {claim.pk: shown[claim.uuid] for claim in claims if claim.uuid in shown}


def split_by_review_right(user, claims, settings_obj):
    """The claims ``user`` may decide on, and the skip entries of the others (their own)."""
    reviewable, skipped = [], []
    for claim in claims:
        if access.can_review(user, claim, settings_obj=settings_obj):
            reviewable.append(claim)
        else:
            skipped.append((claim, "your own claim; another SRP manager has to decide."))
    return reviewable, skipped


# --- Review queue -------------------------------------------------------------------------------------------


def filter_choices():
    """The corporations and FCs that have claims, for the filter drop-downs."""
    corporations = {}
    for pk, name in (
        SrpClaim.objects.exclude(corporation_id=None).values_list("corporation_id", "corporation_name").order_by("corporation_name")
    ):
        corporations.setdefault(pk, name or str(pk))
    fcs = {}
    for pk, name in (
        SrpClaim.objects.exclude(fc_user=None).values_list("fc_user_id", "fc_main_character_name").order_by("fc_main_character_name")
    ):
        fcs.setdefault(pk, name or f"User {pk}")
    return list(corporations.items()), list(fcs.items())


def filtered_claims(filters, bounds=None):
    """Claims for the queue's filters (``ReviewFilterForm.cleaned_data``), the month of the loss when given."""
    claims = SrpClaim.objects.all()
    status = filters.get("status") or SrpClaim.Status.PENDING
    if status != ReviewFilterForm.ANY_STATUS:
        claims = claims.filter(status=status)
    if filters.get("scope"):
        claims = claims.filter(scope=filters["scope"])
    flag = filters.get("flag")
    if flag == "over_limit":
        claims = claims.filter(over_limit=True)
    elif flag == "fleet_check":
        claims = claims.exclude(fleet_check="").exclude(fleet_check__in=SOUND_FLEET_CHECKS)
    elif flag == "estimate":
        claims = claims.filter(Q(value_source="esi_estimate") | Q(warnings__icontains="value_estimate"))
    elif flag == "external":
        claims = claims.filter(warnings__icontains="external_claim")
    if filters.get("corporation"):
        claims = claims.filter(corporation_id=filters["corporation"])
    if filters.get("fc"):
        claims = claims.filter(fc_user_id=filters["fc"])
    text = filters.get("q")
    if text:
        match = Q(character_name__icontains=text) | Q(main_character_name__icontains=text) | Q(ship_type_name__icontains=text)
        number = _REFERENCE.fullmatch(text)
        if number:
            match |= Q(pk=int(number.group(1))) | Q(killmail_id=int(number.group(1)))
        claims = claims.filter(match)
    if bounds is not None:
        claims = claims.filter(killmail_time__gte=bounds[0], killmail_time__lt=bounds[1])
    # Pending claims are worked off oldest first, the history newest first.
    if status == SrpClaim.Status.PENDING:
        return claims.order_by("submitted_at", "pk")
    return claims.order_by("-submitted_at", "-pk")


@require_GET
@permission_required(MANAGE_SRP, raise_exception=True)
def review_queue(request):
    """Claims waiting for a decision, with filters; 50 per page."""
    settings_obj = SrpSettings.get_solo()
    year, month, bounds = month_filter(request)
    data = request.GET.copy()
    if data.get("status") not in {ReviewFilterForm.ANY_STATUS, *dict(ReviewFilterForm.STATUSES)}:
        data["status"] = SrpClaim.Status.PENDING
    corporations, fcs = filter_choices()
    form = ReviewFilterForm(data, corporations=corporations, fcs=fcs)
    form.is_valid()  # a value that does not check out is left out of cleaned_data
    page = Paginator(filtered_claims(form.cleaned_data, bounds), REVIEW_PER_PAGE).get_page(request.GET.get("page"))
    rows = list(page.object_list)
    for claim in rows:
        claim.reviewable = access.can_review(request.user, claim, settings_obj=settings_obj)
    return srp_render(
        request,
        "fleetops/srp/review.html",
        _period_render_context(
            year,
            month,
            request=request,
            preserve=REVIEW_FILTERS,
            filter_form=form,
            page=page,
            claims=rows,
            month_filter_active=bounds is not None,
            all_months_query=without_period(request),
            next_url=request.get_full_path(),
            selectable=any(claim.reviewable and claim.status == SrpClaim.Status.PENDING for claim in rows),
        ),
        settings_obj=settings_obj,
    )


@require_POST
@permission_required(MANAGE_SRP, raise_exception=True)
def bulk_approve(request):
    """Approve the ticked claims that are clean at their suggested amount; the others are listed as skipped."""
    form = ClaimSelectionForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_errors(form))
        return back(request, redirect("fleetops:srp_review"))
    settings_obj = SrpSettings.get_solo()
    ticked = list(SrpClaim.objects.filter(uuid__in=form.cleaned_data["claim"]))
    reviewable, skipped = split_by_review_right(request.user, ticked, settings_obj)
    if reviewable:
        result = review.approve_clean_claims(
            reviewable, actor=request.user, expected_amounts=expected_amounts(form, reviewable)
        )
        skipped.extend(result.skipped)
        if result.done:
            count = len(result.done)
            messages.success(request, f"{count} claim{'s' if count != 1 else ''} approved at the suggested amount.")
    report_skipped(request, skipped)
    if not reviewable and not skipped:
        messages.error(request, "None of the selected claims exist any more.")
    return back(request, redirect("fleetops:srp_review"))


@require_POST
@permission_required(MANAGE_SRP, raise_exception=True)
def recalculate_pending(request):
    """Recalculate every pending claim with the current policy."""
    count = review.recalculate_pending(actor=request.user)
    messages.success(request, f"{count} pending claim{'s' if count != 1 else ''} recalculated with the current policy.")
    return back(request, redirect("fleetops:srp_review"))


# --- Decisions on one claim -------------------------------------------------------------------------------------


def _isk(amount) -> str:
    return display.format_isk(amount)


def do_approve(request, claim, form):
    amount = form.amount_for(claim)
    reason = form.cleaned_data["reason"]
    if not reason and form.cleaned_data["mode"] == form.WITHIN_LIMIT:
        reason = within_limit_reason(claim)
    changed = claim.status == SrpClaim.Status.APPROVED
    review.approve_claim(
        claim,
        actor=request.user,
        amount=amount,
        reason=reason,
        confirm_above_policy=form.cleaned_data["confirm_above_policy"],
        confirm_over_limit=form.cleaned_data["confirm_over_limit"],
        **form.expected(),
    )
    return f"{claim.reference}: amount changed to {_isk(amount)}." if changed else f"{claim.reference} approved for {_isk(amount)}."


def do_reject(request, claim, form):
    review.reject_claim(claim, actor=request.user, reason=form.cleaned_data["reason"])
    return f"{claim.reference} rejected."


def do_revoke(request, claim, form):
    review.revoke_approval(claim, actor=request.user, reason=form.cleaned_data["reason"])
    return f"Approval of {claim.reference} revoked. It is pending again and no longer counts toward the limits."


def do_paid(request, claim, form):
    shown = form.cleaned_data.get("shown_amount")
    result = review.mark_paid(
        [claim],
        actor=request.user,
        note=form.cleaned_data["note"],
        expected_amounts=None if shown is None else {claim.pk: shown},
    )
    if result.skipped:
        raise SrpStateError("not_payable", result.skipped[0][1])
    return f"{claim.reference} marked paid."


def do_unpaid(request, claim, form):
    review.undo_payment(claim, actor=request.user, reason=form.cleaned_data["reason"])
    return f"Payment of {claim.reference} undone. It is approved again."


def do_reopen(request, claim, form):
    review.reopen_claim(claim, actor=request.user, reason=form.cleaned_data["reason"])
    return f"{claim.reference} reopened and recalculated."


def do_reclassify(request, claim, form):
    link = None
    scope = form.cleaned_data["scope"]
    if scope == SrpClaim.Scope.IN_FLEET:
        link = SrpFleetLink.objects.select_related("operation").filter(code=form.cleaned_data["link"]).first()
        if link is None:
            form.add_error("link", "There is no fleet with this SRP link.")
            raise SrpError("unknown_link", "There is no fleet with this SRP link.")
    review.reclassify_claim(claim, actor=request.user, scope=scope, link=link, reason=form.cleaned_data["reason"])
    where = "in fleet" if scope == SrpClaim.Scope.IN_FLEET else "out of fleet"
    return f"{claim.reference} moved to {where} and recalculated."


def do_recalculate(request, claim, form):
    review.recalculate_claim(claim, actor=request.user)
    return f"{claim.reference} recalculated with the current policy."


def do_recheck(request, claim, form):
    review.recheck_presence(claim, actor=request.user)
    return f"Fleet presence of {claim.reference} checked again."


def do_note(request, claim, form):
    review.add_note(claim, actor=request.user, text=form.cleaned_data["text"])
    return "Note added. Only SRP managers can see it."


HANDLERS = {
    "approve": do_approve,
    "reject": do_reject,
    "revoke": do_revoke,
    "paid": do_paid,
    "unpaid": do_unpaid,
    "reopen": do_reopen,
    "reclassify": do_reclassify,
    "recalculate": do_recalculate,
    "recheck": do_recheck,
    "note": do_note,
}


@require_POST
@permission_required(MANAGE_SRP, raise_exception=True)
def claim_action(request, claim_uuid, action):
    """One decision on one claim: approve, reject, revoke, paid, unpaid, reopen, reclassify, recalculate,
    recheck (the presence check) or note. An unknown action or claim is a 404.

    A decision on the manager's own claim is a 403 unless ``allow_self_review`` is on; the claim page does not
    offer those buttons then. A re-check and an internal note only need ``manage_srp``.

    Input that does not check out, or an amount that needs a confirmation, shows the claim page again with the
    form as it was sent. A claim that is no longer in the status the button needs (a second click, a decision of
    somebody else) is answered with a message and the claim page.
    """
    if action not in CLAIM_ACTIONS:
        raise Http404
    claim = get_claim(claim_uuid)
    settings_obj = SrpSettings.get_solo()
    if action in DECISION_ACTIONS and not access.can_review(request.user, claim, settings_obj=settings_obj):
        raise PermissionDenied
    form = None
    if action in DECISION_FORMS:
        form = decision_form(action, request.POST, claim)
        if not form.is_valid():
            messages.error(request, form_errors(form))
            return claim_response(request, claim, bound={action: form}, settings_obj=settings_obj)
    try:
        message = HANDLERS[action](request, claim, form)
    except SrpStateError as exc:
        messages.error(request, exc.message)
        return back(request, claim_redirect(claim))
    except SrpError as exc:
        messages.error(request, exc.message)
        if form is None:
            return back(request, claim_redirect(claim))
        return claim_response(request, claim, bound={action: form}, settings_obj=settings_obj)
    messages.success(request, message)
    return back(request, claim_redirect(claim))


# --- Payments --------------------------------------------------------------------------------------------------------


def pay_rows(claims):
    """Approved claims grouped by the player to pay, the largest sums first.

    A row is named after the player's main character as it is now (``policy.pay_to``), not the one recorded
    when the claim was made. Read the claims with ``select_related("user__profile__main_character")``.
    """
    groups: dict[tuple, SrpPayRow] = {}
    for claim in claims:
        key = ("user", claim.user_id) if claim.user_id else ("main", claim.main_character_id or claim.character_id)
        row = groups.get(key)
        if row is None:
            main_id, name = rules.pay_to(claim)
            row = groups[key] = SrpPayRow(claim.user_id, main_id, name)
        row.total += claim.approved_amount or 0
        row.claims.append(claim)
    return sorted(groups.values(), key=lambda row: (-row.total, row.main_character_name.casefold()))


def day_bounds(start, end):
    """Datetimes (EVE time) from the start of the first to the end of the last day; either may be None."""
    lower = datetime.combine(start, time.min, tzinfo=dt_timezone.utc) if start else None
    upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=dt_timezone.utc) if end else None
    return lower, upper


def mark_selected_paid(request, settings_obj):
    form = ClaimSelectionForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_errors(form))
        return back(request, redirect("fleetops:srp_payments"))
    note_form = PaidForm(request.POST)
    if not note_form.is_valid():
        messages.error(request, form_errors(note_form))
        return back(request, redirect("fleetops:srp_payments"))
    ticked = list(SrpClaim.objects.filter(uuid__in=form.cleaned_data["claim"]))
    reviewable, skipped = split_by_review_right(request.user, ticked, settings_obj)
    if reviewable:
        result = review.mark_paid(
            reviewable,
            actor=request.user,
            note=note_form.cleaned_data["note"],
            expected_amounts=expected_amounts(form, reviewable),
        )
        skipped.extend(result.skipped)
        if result.done:
            count = len(result.done)
            total = sum(claim.approved_amount or 0 for claim in result.done)
            messages.success(request, f"{count} claim{'s' if count != 1 else ''} marked paid ({_isk(total)}).")
    report_skipped(request, skipped)
    if not reviewable and not skipped:
        messages.error(request, "None of the selected claims exist any more.")
    return back(request, redirect("fleetops:srp_payments"))


@require_http_methods(["GET", "POST"])
@permission_required(MANAGE_SRP, raise_exception=True)
def payments(request):
    """Approved claims waiting for payment, grouped by main; a POST marks the ticked claims paid.

    Claims the manager may not decide on (their own) are skipped with a message. The month narrows both lists by
    the month of the loss once one is picked; the payment dates narrow the claims already paid (the last 30 days
    until a date is given).
    """
    settings_obj = SrpSettings.get_solo()
    if request.method == "POST":
        return mark_selected_paid(request, settings_obj)

    year, month, bounds = month_filter(request)
    today = eve_now().date()
    dated = "paid_from" in request.GET or "paid_to" in request.GET
    filter_form = PaymentFilterForm(request.GET if dated else None, initial={"paid_from": today - timedelta(days=PAID_DEFAULT_DAYS)})
    filter_form.is_valid()
    if dated:
        paid_from, paid_to = filter_form.cleaned_data.get("paid_from"), filter_form.cleaned_data.get("paid_to")
    else:
        paid_from, paid_to = today - timedelta(days=PAID_DEFAULT_DAYS), None

    base = SrpClaim.objects.all()
    if bounds is not None:
        base = base.filter(killmail_time__gte=bounds[0], killmail_time__lt=bounds[1])
    to_pay = list(
        base.filter(status=SrpClaim.Status.APPROVED)
        .select_related("user__profile__main_character")
        .order_by("killmail_time", "pk")
    )
    for claim in to_pay:
        claim.reviewable = access.can_review(request.user, claim, settings_obj=settings_obj)
    paid = base.filter(status=SrpClaim.Status.PAID)
    lower, upper = day_bounds(paid_from, paid_to)
    if lower is not None:
        paid = paid.filter(paid_at__gte=lower)
    if upper is not None:
        paid = paid.filter(paid_at__lt=upper)
    paid_sums = paid.aggregate(total=Sum("approved_amount"), count=Count("pk"))
    rows = pay_rows(to_pay)
    return srp_render(
        request,
        "fleetops/srp/payments.html",
        _period_render_context(
            year,
            month,
            request=request,
            preserve=PAYMENT_FILTERS,
            pay_rows=rows,
            to_pay_total=sum(row.total for row in rows),
            to_pay_count=len(to_pay),
            paid_claims=list(paid.select_related("paid_by").order_by("-paid_at", "-pk")[:PAID_SHOWN]),
            paid_total=paid_sums["total"] or 0,
            paid_count=paid_sums["count"],
            paid_shown=PAID_SHOWN,
            filter_form=filter_form,
            month_filter_active=bounds is not None,
            all_months_query=without_period(request),
            next_url=request.get_full_path(),
        ),
        settings_obj=settings_obj,
    )
