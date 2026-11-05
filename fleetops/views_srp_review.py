# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FleetOps SRP: the review queue, the decisions on claims and the payments list. SRP reviewers decide on claims,
SRP payers pay them (``manage_srp`` is both)."""

import re
from datetime import datetime, time, timedelta, timezone as dt_timezone
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from fleetops.decorators import fleetops_permission_required
from fleetops.forms_srp import (
    NO_CATEGORY,
    ClaimSelectionForm,
    PaidForm,
    PayableFilterForm,
    PaymentFilterForm,
    ReviewFilterForm,
)
from fleetops.models import SrpCategory, SrpClaim, SrpFleetLink, SrpPaymentBatch, SrpSettings
from fleetops.services.periods import eve_now, month_bounds
from fleetops.srp import access, bans, display, payments as srp_payments, review, summaries
from fleetops.srp.access import MANAGE_SRP, PAY_SRP, REVIEW_SRP
from fleetops.srp.display import category_label
from fleetops.srp.types import SrpError, SrpStateError
from fleetops.texts import translate_stored
from fleetops.ui_notices import push_notice
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
REVIEW_FILTERS = (
    "status",
    "scope",
    "flag",
    "corporation",
    "fc",
    "q",
    "category",
    "link",
    "submitted_from",
    "submitted_to",
    "user",
)
PAYMENT_FILTERS = ("paid_from", "paid_to", "category", "link", "corporation")
# Links offered by the link filters (a datalist of the most recent ones).
LINK_HINTS_SHOWN = 100
REVIEW_PER_PAGE = 50
PAID_SHOWN = 200
PAID_DEFAULT_DAYS = 30
# Lines of "skipped" reasons shown after a bulk action; the rest is counted.
SKIPPED_SHOWN = 5

# The buttons of the claim page by the role they need. Reviewers decide on claims (everything but a re-check
# follows the self-review rule: reviewers do not decide their own claims unless the settings allow it); payers
# mark claims paid and undo a payment (``allow_self_payment`` and ``require_different_payer``); an internal note
# is open to all SRP staff.
REVIEW_ACTIONS = frozenset(
    {"approve", "reject", "revoke", "reopen", "reclassify", "recategorize", "recalculate", "recheck"}
)
PAY_ACTIONS = frozenset({"paid", "unpaid"})
STAFF_ACTIONS = frozenset({"note"})
CLAIM_ACTIONS = REVIEW_ACTIONS | PAY_ACTIONS | STAFF_ACTIONS
# A re-check needs the reviewer role but not the self-review rule.
REVIEW_ACTIONS_WITHOUT_SELF_RULE = frozenset({"recheck"})

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
    lines = [f"{claim.reference}: {translate_stored(text)}" for claim, text in skipped]
    if lines:
        shown = lines[:SKIPPED_SHOWN]
        if len(lines) > SKIPPED_SHOWN:
            shown.append(_("and %(count)s more.") % {"count": len(lines) - SKIPPED_SHOWN})
        messages.warning(request, _("Skipped: %(details)s") % {"details": " ".join(shown)})


def expected_amounts(form, claims):
    """``{claim ID: amount}`` the list showed for the ticked ``claims`` (``ClaimSelectionForm.shown_amounts``)."""
    shown = form.shown_amounts()
    return {claim.pk: shown[claim.uuid] for claim in claims if claim.uuid in shown}


def revision_kwargs(form) -> dict:
    """``{"expected_revision": n}`` when the form carried the number of changes by the claimant that the page showed."""
    shown = form.cleaned_data.get("shown_revision")
    return {} if shown is None else {"expected_revision": shown}


def expected_revisions(form, claims):
    """``{claim ID: member_revision}`` the list showed for the ticked ``claims`` (``ClaimSelectionForm.shown_revisions``)."""
    shown = form.shown_revisions()
    return {claim.pk: shown[claim.uuid] for claim in claims if claim.uuid in shown}


def split_by_review_right(user, claims, settings_obj, kind="review"):
    """The claims ``user`` may decide on (``kind="review"``) or pay (``kind="pay"``), and the skip entries of the
    others (their own, or in a payment the ones the user approved)."""
    allowed, skipped = [], []
    for claim in claims:
        if kind == "pay":
            block = access.payment_block(user, claim, settings_obj=settings_obj)
            if not block:
                allowed.append(claim)
            elif block == access.OWN_APPROVAL_PAY:
                skipped.append((claim, _("you approved it; a different payer has to pay.")))
            else:
                skipped.append((claim, _("your own claim; another payer has to pay.")))
        elif access.can_review(user, claim, settings_obj=settings_obj):
            allowed.append(claim)
        else:
            skipped.append((claim, _("your own claim; another reviewer has to decide.")))
    return allowed, skipped


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
        fcs.setdefault(pk, name or _("User %(id)s") % {"id": pk})
    return list(corporations.items()), list(fcs.items())


def category_choices_list():
    """``[(id, label)]`` of the categories for the filter drop-downs; one that is switched off says so."""
    rows = []
    for category in SrpCategory.objects.order_by("sort_order", "pk"):
        label = category_label(category)
        rows.append((category.pk, label if category.is_active else _("%(name)s (off)") % {"name": label}))
    return rows


def link_hints():
    """``[(code, name)]`` of the latest links, for the ``datalist`` of the link filters (one query)."""
    return list(SrpFleetLink.objects.order_by("-created_at", "-pk").values_list("code", "name")[:LINK_HINTS_SHOWN])


def filtered_claims(filters, bounds=None):
    """Claims for the queue's filters (``ReviewFilterForm.cleaned_data``), the month of the loss when given."""
    claims = SrpClaim.objects.select_related("category", "link")
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
    category = filters.get("category")
    if category == NO_CATEGORY:
        claims = claims.filter(category__isnull=True)
    elif category and category.isdigit():
        claims = claims.filter(category_id=int(category))
    if filters.get("link"):
        claims = claims.filter(link__code=filters["link"])
    if filters.get("user"):
        claims = claims.filter(user_id=filters["user"])
    submitted_lower, submitted_upper = day_bounds(filters.get("submitted_from"), filters.get("submitted_to"))
    if submitted_lower is not None:
        claims = claims.filter(submitted_at__gte=submitted_lower)
    if submitted_upper is not None:
        claims = claims.filter(submitted_at__lt=submitted_upper)
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
@fleetops_permission_required((MANAGE_SRP, REVIEW_SRP), any_perm=True)
def review_queue(request):
    """Claims waiting for a decision, with filters; 50 per page."""
    settings_obj = SrpSettings.get_solo()
    year, month, bounds = month_filter(request)
    data = request.GET.copy()
    if data.get("status") not in {ReviewFilterForm.ANY_STATUS, *dict(ReviewFilterForm.STATUSES)}:
        data["status"] = SrpClaim.Status.PENDING
    corporations, fcs = filter_choices()
    form = ReviewFilterForm(data, corporations=corporations, fcs=fcs, categories=category_choices_list())
    form.is_valid()  # a value that does not check out is left out of cleaned_data
    page = Paginator(filtered_claims(form.cleaned_data, bounds), REVIEW_PER_PAGE).get_page(request.GET.get("page"))
    rows = list(page.object_list)
    banned = bans.banned_user_ids(claim.user_id for claim in rows)
    for claim in rows:
        claim.reviewable = access.can_review(request.user, claim, settings_obj=settings_obj)
        claim.member_banned = claim.user_id in banned
    # The queue is narrowed when it is not the default list of pending claims (the empty state then says so).
    filters = form.cleaned_data
    filters_active = (
        (filters.get("status") or SrpClaim.Status.PENDING) != SrpClaim.Status.PENDING
        or any(filters.get(name) for name in REVIEW_FILTERS if name != "status")
        or bounds is not None
    )
    return srp_render(
        request,
        "fleetops/srp/review.html",
        _period_render_context(
            year,
            month,
            request=request,
            earliest=summaries.earliest_claim_time(),
            preserve=REVIEW_FILTERS,
            filter_form=form,
            link_hints=link_hints(),
            page=page,
            claims=rows,
            month_filter_active=bounds is not None,
            filters_active=filters_active,
            all_months_query=without_period(request),
            next_url=request.get_full_path(),
            selectable=any(claim.reviewable and claim.status == SrpClaim.Status.PENDING for claim in rows),
        ),
        settings_obj=settings_obj,
    )


@require_POST
@fleetops_permission_required((MANAGE_SRP, REVIEW_SRP), any_perm=True, page=False)
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
        revisions = expected_revisions(form, reviewable)
        result = review.approve_clean_claims(
            reviewable,
            actor=request.user,
            expected_amounts=expected_amounts(form, reviewable),
            **({"expected_revisions": revisions} if revisions else {}),
        )
        skipped.extend(result.skipped)
        if result.done:
            count = len(result.done)
            text = ngettext(
                "%(count)s claim approved at the suggested amount.",
                "%(count)s claims approved at the suggested amount.",
                count,
            )
            messages.success(request, text % {"count": count})
    report_skipped(request, skipped)
    if not reviewable and not skipped:
        messages.error(request, _("None of the selected claims exist any more."))
    return back(request, redirect("fleetops:srp_review"))


@require_POST
@fleetops_permission_required((MANAGE_SRP, REVIEW_SRP), any_perm=True, page=False)
def recalculate_pending(request):
    """Recalculate every pending claim with the current policy."""
    count = review.recalculate_pending(actor=request.user)
    text = ngettext(
        "%(count)s pending claim recalculated with the current policy.",
        "%(count)s pending claims recalculated with the current policy.",
        count,
    )
    messages.success(request, text % {"count": count})
    return back(request, redirect("fleetops:srp_review"))


# --- Decisions on one claim -------------------------------------------------------------------------------------


def _isk(amount) -> str:
    return display.format_isk(amount)


def do_approve(request, claim, form):
    amount = form.amount_for(claim)
    reason = form.cleaned_data["reason"]
    if form.cleaned_data["mode"] == form.WITHIN_LIMIT:
        # The page offers the sentence in the language of the reviewer; left as it is, it is stored as the
        # message it is, in English.
        offered = within_limit_reason(claim)
        if reason in ("", offered, translate_stored(offered)):
            reason = offered
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
    text = _("%(reference)s: amount changed to %(amount)s.") if changed else _("%(reference)s approved for %(amount)s.")
    return text % {"reference": claim.reference, "amount": _isk(amount)}


def do_reject(request, claim, form):
    review.reject_claim(claim, actor=request.user, reason=form.cleaned_data["reason"], **revision_kwargs(form))
    return _("%(reference)s rejected.") % {"reference": claim.reference}


def do_revoke(request, claim, form):
    review.revoke_approval(claim, actor=request.user, reason=form.cleaned_data["reason"])
    return _("Approval of %(reference)s revoked. It is pending again and no longer counts toward the limits.") % {
        "reference": claim.reference
    }


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
    report_batch(request, result.batch)
    return _("%(reference)s marked paid.") % {"reference": claim.reference}


def do_unpaid(request, claim, form):
    review.undo_payment(claim, actor=request.user, reason=form.cleaned_data["reason"])
    return _("Payment of %(reference)s undone. It is approved again.") % {"reference": claim.reference}


def do_reopen(request, claim, form):
    review.reopen_claim(claim, actor=request.user, reason=form.cleaned_data["reason"])
    return _("%(reference)s reopened and recalculated.") % {"reference": claim.reference}


def do_reclassify(request, claim, form):
    link = None
    scope = form.cleaned_data["scope"]
    if scope == SrpClaim.Scope.IN_FLEET:
        # Only the link of a fleet makes a claim "in fleet"; a standing link is not found here.
        link = (
            SrpFleetLink.objects.select_related("operation")
            .filter(code=form.cleaned_data["link"], kind=SrpFleetLink.Kind.FLEET, operation__isnull=False)
            .first()
        )
        if link is None:
            problem = _("There is no fleet with this SRP link.")
            form.add_error("link", problem)
            raise SrpError("unknown_link", problem)
    review.reclassify_claim(
        claim,
        actor=request.user,
        scope=scope,
        link=link,
        reason=form.cleaned_data["reason"],
        category=form.cleaned_data.get("category"),
        **revision_kwargs(form),
    )
    if scope == SrpClaim.Scope.IN_FLEET:
        text = _("%(reference)s moved to in fleet and recalculated.")
    else:
        text = _("%(reference)s moved to out of fleet and recalculated.")
    return text % {"reference": claim.reference}


def do_recategorize(request, claim, form):
    review.recategorize_claim(
        claim,
        actor=request.user,
        category=form.cleaned_data.get("category"),
        reason=form.cleaned_data["reason"],
        **revision_kwargs(form),
    )
    return _("%(reference)s moved to another category and recalculated.") % {"reference": claim.reference}


def do_recalculate(request, claim, form):
    review.recalculate_claim(claim, actor=request.user)
    return _("%(reference)s recalculated with the current policy.") % {"reference": claim.reference}


def do_recheck(request, claim, form):
    review.recheck_presence(claim, actor=request.user)
    return _("Fleet presence of %(reference)s checked again.") % {"reference": claim.reference}


def do_note(request, claim, form):
    review.add_note(claim, actor=request.user, text=form.cleaned_data["text"])
    return _("Note added. Only SRP staff can see it.")


HANDLERS = {
    "approve": do_approve,
    "reject": do_reject,
    "revoke": do_revoke,
    "paid": do_paid,
    "unpaid": do_unpaid,
    "reopen": do_reopen,
    "reclassify": do_reclassify,
    "recategorize": do_recategorize,
    "recalculate": do_recalculate,
    "recheck": do_recheck,
    "note": do_note,
}


def claim_action_allowed(user, claim, action, settings_obj) -> bool:
    """Whether ``user`` may do ``action`` on ``claim``: the role of the action and its self-processing rule."""
    if action in PAY_ACTIONS:
        # Undoing a payment is not bound by the rule that the approver may not pay.
        return not access.payment_block(user, claim, settings_obj=settings_obj, approver_rule=action == "paid")
    if action in REVIEW_ACTIONS_WITHOUT_SELF_RULE:
        return access.is_reviewer(user)
    if action in REVIEW_ACTIONS:
        return access.can_review(user, claim, settings_obj=settings_obj)
    return access.is_srp_staff(user)


@require_POST
@fleetops_permission_required((MANAGE_SRP, REVIEW_SRP, PAY_SRP), any_perm=True, page=False)
def claim_action(request, claim_uuid, action):
    """One decision on one claim: approve, reject, revoke, paid, unpaid, reopen, reclassify, recategorize,
    recalculate, recheck (the presence check) or note. An unknown action or claim is a 404.

    Each action needs its role: the review actions ``review_srp``, paid and unpaid ``pay_srp``, a note any SRP
    staff permission (``manage_srp`` has them all); any other permission is a 403. A decision on the reviewer's own
    claim is a 403 unless ``allow_self_review`` is on, paying one's own claim unless ``allow_self_payment`` is on
    and paying a claim one approved when ``require_different_payer`` is on; the claim page does not offer those
    buttons then. A re-check and an internal note are not bound by these rules.

    Input that does not check out, or an amount that needs a confirmation, shows the claim page again with the
    form as it was sent. A claim that is no longer in the status the button needs (a second click, a decision of
    somebody else) is answered with a message and the claim page.
    """
    if action not in CLAIM_ACTIONS:
        raise Http404
    claim = get_claim(claim_uuid)
    settings_obj = SrpSettings.get_solo()
    if not claim_action_allowed(request.user, claim, action, settings_obj):
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
        messages.error(request, translate_stored(exc.message))
        return back(request, claim_redirect(claim))
    except SrpError as exc:
        messages.error(request, translate_stored(exc.message))
        if form is None:
            return back(request, claim_redirect(claim))
        return claim_response(request, claim, bound={action: form}, settings_obj=settings_obj)
    if action == "paid":
        # Marking a claim paid can be taken back by the same people who made it: the notice says so and links to
        # the form (every other decision asks for its reason when it is made, so it has no undo of its own).
        push_notice(
            request,
            message,
            "success",
            _("Undo payment…"),
            reverse("fleetops:srp_claim", kwargs={"claim_uuid": claim.uuid}) + "#undo-payment",
        )
    else:
        messages.success(request, message)
    return back(request, claim_redirect(claim))


# --- Payments --------------------------------------------------------------------------------------------------------


def pay_rows(claims):
    """Approved claims grouped by the player to pay, the largest sums first (see ``payments.pay_rows``)."""
    return srp_payments.pay_rows(claims)


def day_bounds(start, end):
    """Datetimes (EVE time) from the start of the first to the end of the last day; either may be None."""
    lower = datetime.combine(start, time.min, tzinfo=dt_timezone.utc) if start else None
    upper = None
    if end:
        try:
            upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=dt_timezone.utc)
        except OverflowError:  # the last day the calendar knows: no upper bound
            upper = None
    return lower, upper


def report_batch(request, batch):
    """Name the payment batch a payment made."""
    if batch is not None:
        messages.info(request, _("Payment batch %(batch)s.") % {"batch": batch.reference})


def posted_filters(request):
    """The filters of the payments page that a payment form carries along (hidden fields), kept with the batch."""
    return srp_payments.parse_filters(request.POST)


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
    reviewable, skipped = split_by_review_right(request.user, ticked, settings_obj, kind="pay")
    if reviewable:
        result = review.mark_paid(
            reviewable,
            actor=request.user,
            note=note_form.cleaned_data["note"],
            expected_amounts=expected_amounts(form, reviewable),
            **({"filters": filters} if (filters := posted_filters(request)) else {}),
        )
        skipped.extend(result.skipped)
        if result.done:
            count = len(result.done)
            total = sum(claim.approved_amount or 0 for claim in result.done)
            text = ngettext(
                "%(count)s claim marked paid (%(total)s).", "%(count)s claims marked paid (%(total)s).", count
            )
            messages.success(request, text % {"count": count, "total": _isk(total)})
            report_batch(request, result.batch)
    report_skipped(request, skipped)
    if not reviewable and not skipped:
        messages.error(request, _("None of the selected claims exist any more."))
    return back(request, redirect("fleetops:srp_payments"))


def payable_corporations():
    """The corporations that have approved claims, for the filter of the payments page."""
    rows = {}
    for pk, name in (
        SrpClaim.objects.filter(status=SrpClaim.Status.APPROVED)
        .exclude(corporation_id=None)
        .values_list("corporation_id", "corporation_name")
        .order_by("corporation_name")
    ):
        rows.setdefault(pk, name or str(pk))
    return list(rows.items())


@require_GET
@fleetops_permission_required((MANAGE_SRP, PAY_SRP), any_perm=True)
def payment_batch(request, batch_uuid):
    """One payment batch: who was paid how much, and the claims whose payment was undone since. Payers only (a
    reviewer without a payment permission gets a 403)."""
    batch = get_object_or_404(SrpPaymentBatch, uuid=batch_uuid)
    rows = srp_payments.batch_claims(batch)
    return srp_render(
        request,
        "fleetops/srp/payment_batch.html",
        {
            "batch": batch,
            "claims": rows,
            "undone": srp_payments.undone_count(batch, len(rows)),
            "current_total": sum(claim.approved_amount or 0 for claim in rows),
            "pay_rows": pay_rows(rows),
        },
    )


@require_http_methods(["GET", "POST"])
@fleetops_permission_required((MANAGE_SRP, PAY_SRP), any_perm=True)
def payments(request):
    """Approved claims waiting for payment, grouped by main; a POST marks the ticked claims paid.

    Claims the payer may not pay (their own, or ones they approved when a different payer is required) are
    skipped with a message. The month narrows both lists by
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
    payable_form = PayableFilterForm(
        request.GET, corporations=payable_corporations(), categories=category_choices_list()
    )
    payable_form.is_valid()
    payable_filters = srp_payments.clean_filters(payable_form.cleaned_data)
    to_pay = list(
        srp_payments.apply_filters(base.filter(status=SrpClaim.Status.APPROVED), payable_filters)
        .select_related("user__profile__main_character", "category", "link")
        .order_by("killmail_time", "pk")
    )
    for claim in to_pay:
        claim.pay_block = access.payment_block(request.user, claim, settings_obj=settings_obj)
        claim.payable = not claim.pay_block
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
            earliest=summaries.earliest_claim_time(),
            preserve=PAYMENT_FILTERS,
            pay_rows=rows,
            to_pay_total=sum(row.total for row in rows),
            to_pay_count=len(to_pay),
            paid_claims=list(
                paid.select_related("paid_by", "category", "link", "payment_batch").order_by("-paid_at", "-pk")[:PAID_SHOWN]
            ),
            paid_dates={name: request.GET[name] for name in ("paid_from", "paid_to") if request.GET.get(name)},
            payable_form=payable_form,
            payable_filters=payable_filters,
            payable_filtered=bool(payable_filters),
            export_query=urlencode({**payable_filters, **({"year": year, "month": month} if bounds is not None else {})}),
            link_hints=link_hints(),
            recent_batches=srp_payments.recent_batches(),
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
