# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FleetOps SRP: the link list and the pages to make and change links. SRP staff list links and close or complete
them; reviewers also make and change standing links and open links again (``views_srp_summary.link_toggle`` opens and
closes one link from a fleet page)."""

from datetime import datetime, time, timedelta, timezone as dt_timezone

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from fleetops.decorators import fleetops_permission_required
from fleetops.forms_srp import FleetLinkForm, LinkFilterForm, StandingLinkForm
from fleetops.models import SrpCategory, SrpClaim, SrpFleetLink
from fleetops.srp import access, links
from fleetops.srp.access import MANAGE_SRP, PAY_SRP, REVIEW_SRP
from fleetops.srp.types import SrpError
from fleetops.texts import translate_stored
from fleetops.views_srp import error_text, srp_render
from fleetops.views_srp_review import SKIPPED_SHOWN, back

STAFF = (MANAGE_SRP, REVIEW_SRP, PAY_SRP)
REVIEWER = (MANAGE_SRP, REVIEW_SRP)
LINKS_PER_PAGE = 50
# Links the list can preselect with ``?codes=``; the same limit as one bulk action.
MAX_CODES = links.MAX_BULK
CODE_LENGTH = 32


def _day_start(day):
    return datetime.combine(day, time.min, tzinfo=dt_timezone.utc)


def parse_codes(value) -> list[str]:
    """The link codes of ``?codes=a,b,c``: at most ``MAX_CODES``, each no longer than a code can be."""
    codes = [code.strip() for code in (value or "").split(",")]
    return [code for code in dict.fromkeys(codes) if code and len(code) <= CODE_LENGTH][:MAX_CODES]


def filtered_links(filters, codes=()):
    """Links for the list's filters (``LinkFilterForm.cleaned_data``), each with the number of its claims waiting
    for review and for payment and the total paid, from one query."""
    qs = SrpFleetLink.objects.select_related("category", "operation__fleet_type").annotate(
        pending_total=Count("claims", filter=Q(claims__status=SrpClaim.Status.PENDING)),
        approved_total=Count("claims", filter=Q(claims__status=SrpClaim.Status.APPROVED)),
        paid_amount=Sum("claims__approved_amount", filter=Q(claims__status=SrpClaim.Status.PAID)),
    )
    if codes:
        return qs.filter(code__in=codes).order_by("-created_at", "-pk")
    state = filters.get("state")
    if state == SrpFleetLink.State.OPEN:
        qs = qs.filter(is_open=True)
    elif state == SrpFleetLink.State.CLOSED:
        qs = qs.filter(is_open=False, completed_at__isnull=True)
    elif state == SrpFleetLink.State.COMPLETED:
        qs = qs.filter(is_open=False, completed_at__isnull=False)
    if filters.get("kind"):
        qs = qs.filter(kind=filters["kind"])
    if filters.get("category"):
        qs = qs.filter(category=filters["category"])
    start, end = filters.get("date_from"), filters.get("date_to")
    if start:
        # The date of a standing link, else the start of its fleet, else the day the link was made.
        qs = qs.filter(
            Q(link_date__gte=start)
            | Q(link_date__isnull=True, operation__started_at__gte=_day_start(start))
            | Q(link_date__isnull=True, operation__isnull=True, created_at__gte=_day_start(start))
        )
    if end:
        before = _day_start(end + timedelta(days=1))
        qs = qs.filter(
            Q(link_date__lte=end)
            | Q(link_date__isnull=True, operation__started_at__lt=before)
            | Q(link_date__isnull=True, operation__isnull=True, created_at__lt=before)
        )
    return qs.order_by("-created_at", "-pk")


@require_GET
@fleetops_permission_required(STAFF, any_perm=True)
def srp_links(request):
    """The SRP links with their state and what is still open on them; filters; 50 per page."""
    codes = parse_codes(request.GET.get("codes"))
    form = LinkFilterForm(request.GET)
    form.is_valid()  # a value that does not check out is left out of cleaned_data
    page = Paginator(filtered_links(form.cleaned_data, codes), LINKS_PER_PAGE).get_page(request.GET.get("page"))
    rows = list(page.object_list)
    for link in rows:
        link.label = links.link_label(link)
        link.url = links.link_url(link) if link.is_open else ""
    filters_active = bool(codes) or any(form.cleaned_data.get(name) for name in form.fields)
    query = request.GET.copy()
    query.pop("page", None)
    return srp_render(
        request,
        "fleetops/srp/links.html",
        {
            "filter_form": form,
            "page": page,
            "links": rows,
            "preselected": set(codes),
            "filters_active": filters_active,
            "filter_query": query.urlencode(),
            "can_reopen": access.is_reviewer(request.user),
            "next_url": request.get_full_path(),
            "State": SrpFleetLink.State,
        },
    )


def _initial_category(request):
    try:
        return SrpCategory.objects.filter(pk=int(request.GET.get("category", ""))).first()
    except ValueError:
        return None


@require_http_methods(["GET", "POST"])
@fleetops_permission_required(REVIEWER, any_perm=True)
def srp_link_new(request):
    """Make a standing link: a name, a category, the date and the rest."""
    if request.method == "POST":
        form = StandingLinkForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            if data.get("daily") and SrpFleetLink.objects.filter(
                daily_key=links.daily_key(data["category"], data["link_date"])
            ).exists():
                form.add_error("daily", _("This category has a link of the day for this date already."))
            else:
                try:
                    link = links.create_standing_link(
                        actor=request.user,
                        name=data["name"],
                        category=data["category"],
                        link_date=data["link_date"],
                        doctrine_text=data["doctrine_text"],
                        aar_url=data["aar_url"],
                        auto_close_at=data["auto_close_at"],
                        daily=data["daily"],
                    )
                except SrpError as exc:
                    messages.error(request, error_text(exc))
                else:
                    messages.success(
                        request,
                        _("Standing link “%(name)s” created. Members open it at %(url)s.")
                        % {"name": link.name, "url": links.link_url(link)},
                    )
                    return redirect("fleetops:srp_links")
    else:
        initial = {"link_date": links.eve_today()}
        category = _initial_category(request)
        if category is not None and category.is_active:
            initial["category"] = category.pk
            initial["doctrine_text"] = category.default_doctrine_text
        form = StandingLinkForm(initial=initial)
    return srp_render(
        request,
        "fleetops/srp/link_form.html",
        {"form": form, "link": None, "category_data": form.category_data(), "claim_count": 0},
    )


@require_http_methods(["GET", "POST"])
@fleetops_permission_required(REVIEWER, any_perm=True)
def srp_link_edit(request, code):
    """Change a link. A standing link keeps the category of its claims when its category changes; the link of a
    fleet only has the doctrine text and the report to change."""
    link = get_object_or_404(SrpFleetLink.objects.select_related("category", "operation__fleet_type"), code=code)
    standing = link.kind == SrpFleetLink.Kind.STANDING
    if standing:
        initial = {
            "name": link.name,
            "category": link.category_id,
            "link_date": link.link_date,
            "doctrine_text": link.doctrine_text,
            "aar_url": link.aar_url,
            "auto_close_at": link.auto_close_at,
        }
        form = StandingLinkForm(request.POST or None, initial=initial, link=link)
    else:
        form = FleetLinkForm(
            request.POST or None, initial={"doctrine_text": link.doctrine_text, "aar_url": link.aar_url}
        )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        fields = {"doctrine_text": data["doctrine_text"], "aar_url": data["aar_url"]}
        if standing:
            if not data["name"].strip():
                data["name"] = links.default_link_name(data["category"], data["link_date"])
            fields.update(
                name=data["name"],
                category=data["category"],
                link_date=data["link_date"],
                auto_close_at=data["auto_close_at"],
            )
        try:
            links.update_link(link, actor=request.user, **fields)
        except SrpError as exc:
            messages.error(request, error_text(exc))
        else:
            messages.success(request, _("SRP link saved."))
            return redirect("fleetops:srp_links")
    return srp_render(
        request,
        "fleetops/srp/link_form.html",
        {
            "form": form,
            "link": link,
            "link_label": links.link_label(link),
            "standing": standing,
            "category_data": form.category_data() if standing else {},
            "claim_count": SrpClaim.objects.filter(link=link).count(),
        },
    )


@require_POST
@fleetops_permission_required(STAFF, any_perm=True, page=False)
def srp_link_complete(request, code):
    """Mark one link completed; refused while a claim of the link waits for review or payment."""
    link = get_object_or_404(SrpFleetLink.objects.select_related("operation__fleet_type"), code=code)
    try:
        links.complete_link(link, actor=request.user)
    except SrpError as exc:
        messages.error(request, translate_stored(exc.message))
    else:
        messages.success(request, _("SRP link completed."))
    if link.operation_id is not None and request.POST.get("next") is None:
        return redirect("fleetops:operation_detail", operation_uuid=link.operation.uuid)
    return back(request, redirect("fleetops:srp_links"))


@require_POST
@fleetops_permission_required(STAFF, any_perm=True, page=False)
def srp_links_bulk(request):
    """Close, complete or open the ticked links (at most 100); the others are listed with the reason."""
    action = request.POST.get("action", "")
    if action == "open" and not access.is_reviewer(request.user):
        raise PermissionDenied
    try:
        result = links.bulk_link_action(request.POST.getlist("codes"), actor=request.user, action=action)
    except SrpError as exc:
        messages.error(request, translate_stored(exc.message))
        return back(request, redirect("fleetops:srp_links"))
    count = len(result.done)
    if count:
        text = {
            "close": ngettext("%(count)s link closed.", "%(count)s links closed.", count),
            "complete": ngettext("%(count)s link completed.", "%(count)s links completed.", count),
            "open": ngettext("%(count)s link opened.", "%(count)s links opened.", count),
        }[action]
        messages.success(request, text % {"count": count})
    lines = []
    for item, reason in result.skipped:
        name = item if isinstance(item, str) else links.link_label(item)
        lines.append(f"{name}: {translate_stored(reason)}")
    if lines:
        shown = lines[:SKIPPED_SHOWN]
        if len(lines) > SKIPPED_SHOWN:
            shown.append(_("and %(count)s more.") % {"count": len(lines) - SKIPPED_SHOWN})
        messages.warning(request, _("Skipped: %(details)s") % {"details": " ".join(shown)})
    if not count and not lines:
        messages.info(request, _("No link was chosen."))
    return back(request, redirect("fleetops:srp_links"))
