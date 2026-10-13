"""PAP links: creating and revoking them, the registration page and PAP-link fleets."""

from allianceauth.authentication.decorators import permissions_required
from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from esi.decorators import token_required
from esi.models import CallbackRedirect, Token

from fleetops.constants import CLICK_SCOPES
from fleetops.forms_links import LinkOnlyFleetForm, PapLinkForm
from fleetops.models import PapLink, PapRegistration, ReviewStatus
from fleetops.services.identity import owned_characters
from fleetops.services.links import absolute_url
from fleetops.services.pap_links import (
    CREATE_STATUSES,
    LEGACY_FLEET,
    LINK_CLOSED,
    MAX_CHARACTERS_PER_REGISTRATION,
    PapLinkError,
    accepts_new_links,
    create_link,
    link_is_valid,
    link_state,
    register_characters,
    revoke_link,
    start_link_only_fleet,
)
from fleetops.services.periods import format_eve
from fleetops.views import _can_manage_operation, _database_id, _operation_for_user, _render


def _link_url(request, link):
    # Built from SITE_URL like every other link FleetOps shares; the request only without one.
    path = reverse("fleetops:pap_link_register", args=[link.code])
    url = absolute_url(path)
    return url if url != path else request.build_absolute_uri(path)


def links_context(request, operation):
    """What the fleet page's PAP link card needs; only the people who manage the fleet see it."""
    if not _can_manage_operation(request.user, operation):
        return {}
    now = timezone.now()
    links = list(
        operation.pap_links.select_related("created_by__profile__main_character").annotate(
            registration_count=Count("registrations")
        )
    )
    for link in links:
        link.operation = operation
        link.state = link_state(link, now)
        link.url = _link_url(request, link)
    can_create = accepts_new_links(operation)
    return {
        "show_pap_links": True,
        "pap_links": links,
        # Own ids, so they never clash with the other forms of the fleet page.
        "pap_link_form": PapLinkForm(auto_id="pap_link_%s") if can_create else None,
        # Closed before the upgrade: registrations could not give PAP there.
        "pap_link_unavailable": LEGACY_FLEET if operation.status in CREATE_STATUSES and not can_create else "",
    }


def _link(code):
    return get_object_or_404(PapLink.objects.select_related("operation__fleet_type"), code=code)


def _register_context(request, link):
    operation = link.operation
    now = timezone.now()
    state = link_state(link, now)
    ownerships = list(owned_characters(request.user))
    ids = [ownership.character.character_id for ownership in ownerships]
    # Only whether a token exists: refreshing every token on each page view would call EVE SSO.
    with_token = set(
        Token.objects.filter(user=request.user, character_id__in=ids)
        .require_scopes(CLICK_SCOPES)
        .values_list("character_id", flat=True)
    )
    registered = set(
        PapRegistration.objects.filter(operation=operation, character_id__in=ids).values_list(
            "character_id", flat=True
        )
    )
    main_id = getattr(getattr(request.user.profile, "main_character", None), "character_id", None)
    characters = []
    for ownership in ownerships:
        character = ownership.character
        has_token = character.character_id in with_token
        is_registered = character.character_id in registered
        characters.append(
            {
                "id": character.character_id,
                "name": character.character_name,
                "corporation": character.corporation_name,
                "is_main": character.character_id == main_id,
                "has_token": has_token,
                "registered": is_registered,
                "eligible": state == "open" and has_token and not is_registered,
            }
        )
    return {
        "link": link,
        "operation": operation,
        "link_state": state,
        "link_open": state == "open",
        "seconds_left": max(0, int((link.expires_at - now).total_seconds())),
        "characters": characters,
        "any_eligible": any(row["eligible"] for row in characters),
        "character_limit": operation.pap_character_limit,
        "max_per_submit": MAX_CHARACTERS_PER_REGISTRATION,
        "pending_review": operation.review_status == ReviewStatus.PENDING,
        "rejected_review": operation.review_status == ReviewStatus.REJECTED,
    }


def _report(request, result):
    for _character_id, name in result.registered:
        messages.success(request, f"{name} is registered for PAP in this fleet.")
    for _character_id, name, reason in result.rejected:
        messages.warning(request, f"{name} was not registered: {reason}")


@permission_required("fleetops.basic_access", raise_exception=True)
def pap_link_register(request, code):
    link = _link(code)
    if request.method == "POST":
        ids = [_database_id(value) for value in request.POST.getlist("character_ids")]
        ids = [value for value in ids if value is not None]
        if ids:
            _report(request, register_characters(link, request.user, ids))
        else:
            messages.warning(request, "Select the characters to register.")
        return redirect("fleetops:pap_link_register", code=link.code)
    return _render(request, "fleetops/pap_link_register.html", _register_context(request, link))


def _back_from_login(request):
    """Whether this request returns from an EVE login that this view started and that holds a new token."""
    session_key = request.session.session_key
    return bool(
        session_key
        and CallbackRedirect.objects.filter(
            session_key=session_key, url=request.get_full_path(), token__isnull=False
        ).exists()
    )


@permission_required("fleetops.basic_access", raise_exception=True)
def pap_link_authorize(request, code):
    link = _link(code)
    if not link_is_valid(link) and not _back_from_login(request):
        # Nothing to log in for.
        messages.error(request, LINK_CLOSED)
        return redirect("fleetops:pap_link_register", code=link.code)
    # Back from an EVE login during which the link closed, the token is still taken from the login
    # (and the registration refused), so it is not handed to the user's next ESI login on another page.
    return _register_new_character(request, code=link.code)


@token_required(scopes=CLICK_SCOPES, new=True)
def _register_new_character(request, token, code):
    """Back from the EVE login with a new token: register its character through the link."""
    link = _link(code)
    _report(request, register_characters(link, request.user, [token.character_id]))
    return redirect("fleetops:pap_link_register", code=link.code)


def _form_errors(form):
    texts = []
    for name, errors in form.errors.items():
        label = form.fields[name].label if name in form.fields else ""
        texts.extend(f"{label}: {error}" if label else str(error) for error in errors)
    return " ".join(texts)


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_POST
def create_pap_link_view(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid, manage=True)
    form = PapLinkForm(request.POST)
    if not form.is_valid():
        messages.error(request, f"PAP link not created. {_form_errors(form)}")
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    try:
        link = create_link(
            operation,
            actor=request.user,
            minutes=form.cleaned_data["minutes"],
            require_online=form.cleaned_data["require_online"],
        )
    except PapLinkError as exc:
        messages.error(request, str(exc))
    else:
        online = "characters must be online" if link.require_online else "no online check"
        messages.success(request, f"PAP link created; open until {format_eve(link.expires_at)} EVE, {online}.")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_POST
def revoke_pap_link_view(request, pk):
    link = get_object_or_404(PapLink.objects.select_related("operation"), pk=pk)
    if not _can_manage_operation(request.user, link.operation):
        raise Http404
    if link.revoked_at is not None:
        messages.info(request, "This PAP link was already revoked.")
    else:
        revoke_link(link, actor=request.user)
        messages.success(request, "PAP link revoked; it no longer accepts registrations.")
    return redirect("fleetops:operation_detail", operation_uuid=link.operation.uuid)


@permission_required("fleetops.start_fleet", raise_exception=True)
def start_link_only_fleet_view(request):
    form = LinkOnlyFleetForm(request.POST if request.method == "POST" else None, user=request.user)
    if request.method == "POST" and form.is_valid():
        try:
            operation = start_link_only_fleet(request.user, form.cleaned_data)
        except PermissionError as exc:
            form.add_error("fc_character_id", str(exc))
        else:
            messages.success(request, "PAP-link fleet started. Share its PAP link with your pilots.")
            if operation.review_status == ReviewStatus.PENDING:
                messages.info(request, "The fleet counts in statistics once an FC lead has approved it.")
            return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    return _render(request, "fleetops/pap_fleet_start.html", {"form": form})
