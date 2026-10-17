# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Send Ping: a Discord ping without a fleet, for FCs and the leaders of configured groups.

The page shows the form, a live preview (``preview_ping``) and the recent pings the viewer may see. Sending
goes through ``services.standalone_pings``, which checks everything again right before the post. The result is
an Alliance Auth message, which ``fleetops.js`` shows as a toast: a success disappears, a failure stays and
names the cause and the next step. A failed ping opens the page again filled in with it ("Use again").
"""

import logging
import uuid

from django.contrib import messages
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, pgettext
from django.views.decorators.http import require_POST

from fleetops import texts
from fleetops.decorators import ping_sender_required
from fleetops.forms_groups import SendPingForm
from fleetops.models import StandalonePing
from fleetops.services import standalone_pings
from fleetops.services.errors import log_failure
from fleetops.services.mentions import DISCORD_MESSAGE_LIMIT, mention_text
from fleetops.services.messages import render_standalone_ping
from fleetops.texts import translate_stored
from fleetops.ui import TONE_BAD, TONE_OK, TONE_WARN
from fleetops.views import _render

logger = logging.getLogger(__name__)

# What to do after a failed post, by what Discord answered. The hint is shown, never stored.
HINT_MAYBE_POSTED = gettext_lazy("The ping may not have been posted; check the channel before sending it again.")
HINT_REFUSED = gettext_lazy("Discord refused the ping. Check the channel's webhook in FleetOps Administration.")
HINT_NOTHING_POSTED = gettext_lazy("Nothing was posted. Check the channel's webhook in FleetOps Administration.")
RENDER_FAILED = gettext_lazy("The ping template could not be rendered. Check the message templates.")


def _nothing_posted_texts():
    # Failures the provider reports before it sends anything: the channel's webhook is missing or not Discord's.
    return {texts.PING_NO_WEBHOOK(), texts.PING_NOT_A_WEBHOOK(), texts.PING_BAD_WEBHOOK()}


def failure_text(ping) -> str:
    """The toast of a failed ping: the cause in the viewer's language and the next step."""
    status = ping.http_status
    if status is not None:
        cause = translate_stored(texts.PING_HTTP(status=status))
        hint = HINT_MAYBE_POSTED if status >= 500 else HINT_REFUSED
    else:
        cause = translate_stored(ping.result_message) or translate_stored(
            texts.PING_REQUEST_FAILED(error_type="Error")
        )
        hint = HINT_NOTHING_POSTED if ping.result_message in _nothing_posted_texts() else HINT_MAYBE_POSTED
    return f"{cause} {hint}"


def _status_of(ping, now):
    """Tone and words of a ping's status in the recent list."""
    if ping.status == StandalonePing.Status.SENT:
        return TONE_OK, pgettext("FleetOps", "Sent")
    if ping.status == StandalonePing.Status.FAILED:
        return TONE_BAD, _("Failed")
    if standalone_pings.is_unconfirmed(ping, now=now):
        return TONE_WARN, _("Not confirmed")
    return TONE_WARN, _("Sending")


def _recent_rows(user, *, mine_only):
    now = timezone.now()
    rows = []
    for ping in standalone_pings.recent_pings(user, mine_only=mine_only, now=now):
        tone, label = _status_of(ping, now)
        rows.append(
            {
                "ping": ping,
                "tone": tone,
                "label": label,
                "targets": mention_text(standalone_pings.selection_of(ping), display=True) or _("No ping"),
                "result": translate_stored(ping.result_message) if ping.status == StandalonePing.Status.FAILED else "",
            }
        )
    return rows


def _page(request, form):
    can_see_all = request.user.has_perm(standalone_pings.VIEW_AUDIT_LOG)
    mine_only = can_see_all and request.GET.get("pings") == "mine"
    return _render(
        request,
        "fleetops/send_ping.html",
        {
            "form": form,
            "channel_rules": form.channel_rules(),
            "group_rules": form.group_rules(),
            "ping_limit": DISCORD_MESSAGE_LIMIT,
            "eve_now": timezone.now(),
            "recent": _recent_rows(request.user, mine_only=mine_only),
            "can_see_all": can_see_all,
            "mine_only": mine_only,
        },
    )


def _again_initial(request):
    """The form filled in from the ping named by ``?again=<uuid>``, if the viewer may see it; 404 otherwise."""
    value = request.GET.get("again", "").strip()
    if not value:
        return {}
    try:
        ping_uuid = uuid.UUID(value)
    except ValueError:
        raise Http404
    ping = standalone_pings.visible_pings(request.user).filter(uuid=ping_uuid).first()
    if ping is None:
        raise Http404
    return standalone_pings.form_initial(ping)


def _after_send(request, ping):
    page = reverse("fleetops:send_ping")
    if ping.status == StandalonePing.Status.SENT:
        messages.success(request, _("Ping sent to %(channel)s.") % {"channel": ping.channel_name})
        return redirect(page)
    if ping.status == StandalonePing.Status.FAILED:
        messages.error(request, failure_text(ping))
        # The form opens again with the same ping, under a new form id.
        return redirect(f"{page}?again={ping.uuid}")
    messages.info(request, _("This ping is being sent. Its result is shown under Recent pings."))
    return redirect(page)


@ping_sender_required
def send_ping_view(request):
    """The Send Ping page (GET) and the send (POST)."""
    if request.method == "POST":
        form = SendPingForm(request.POST, user=request.user)
        if form.is_valid():
            try:
                ping = standalone_pings.send_standalone_ping(user=request.user, cleaned_data=form.cleaned_data)
            except (standalone_pings.PingRefused, standalone_pings.PingNotSent) as exc:
                # Nothing was stored or posted; the form stays as it was sent.
                messages.error(request, translate_stored(str(exc)))
            else:
                return _after_send(request, ping)
        return _page(request, form)
    initial = {**_again_initial(request), "request_id": uuid.uuid4()}
    return _page(request, SendPingForm(user=request.user, initial=initial))


def _form_errors_payload(form):
    # The preview runs while the form is filled in, so an incomplete form is not an error.
    missing = [
        str(form[name].label)
        for name, errors in form.errors.as_data().items()
        if name in form.fields
        and not form.fields[name].widget.is_hidden
        and any(error.code == "required" for error in errors)
    ]
    return JsonResponse({"ok": False, "missing": missing, "errors": form.errors.get_json_data()})


@ping_sender_required(page=False)
@require_POST
def preview_ping(request):
    """The preview of a ping without a fleet, as JSON: the text as posted and as read, its length, warnings in
    the viewer's language and the problems that stop the send. Nothing is stored or posted."""
    form = SendPingForm(request.POST, user=request.user)
    if not form.is_valid():
        return _form_errors_payload(form)
    ping = standalone_pings.build_ping(request.user, form.cleaned_data)
    try:
        rendered = render_standalone_ping(ping, actor=request.user)
    except Exception as exc:
        log_failure(logger, "Ping without a fleet preview", exc)
        return JsonResponse({"ok": False, "error": str(RENDER_FAILED)}, status=400)
    warnings = [*(translate_stored(error) for error in rendered.errors), *rendered.warnings]
    cooldown = standalone_pings.cooldown_problem(form.cleaned_data["ping_channel"])
    if cooldown:
        warnings.append(translate_stored(cooldown))
    return JsonResponse(
        {
            "ok": True,
            "ping": rendered.ping,
            "ping_display": rendered.ping_display or rendered.ping,
            "warnings": warnings,
            "blocking": list(rendered.blocking),
            "ping_length": rendered.ping_length,
            "ping_limit": DISCORD_MESSAGE_LIMIT,
        }
    )
