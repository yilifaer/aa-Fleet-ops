# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fleet Start presets of the signed-in FC."""

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from fleetops.decorators import fleet_starter_required
from fleetops.forms_presets import PRESET_FORM_PREFIX, PresetRenameForm, PresetSaveForm
from fleetops.models import FleetStartPreset
from fleetops.services.presets import FC_CHARACTER, PresetConflict, save_preset
from fleetops.views import _render

# Labels of the stored values a preset list row shows, in this order.
SUMMARY_FIELDS = ("fleet_group", "fleet_type", "doctrine_choice", "comms", "ping_channel")


def _own_preset(request, pk):
    # Presets of other FCs do not exist for this user.
    return get_object_or_404(FleetStartPreset, pk=pk, owner=request.user, is_last_used=False)


def _summary(preset):
    values = preset.values or {}
    fields = values.get("fields") or {}
    labels = values.get("labels") or {}
    parts = [str(labels[name]) for name in SUMMARY_FIELDS if labels.get(name)]
    if not labels.get("doctrine_choice") and fields.get("custom_doctrine"):
        before = sum(1 for name in ("fleet_group", "fleet_type") if labels.get(name))
        parts.insert(before, str(fields["custom_doctrine"]))
    if preset.fc_character_id:
        parts.append(_("FC %(name)s") % {"name": labels.get(FC_CHARACTER) or preset.fc_character_id})
    return " · ".join(parts)


@fleet_starter_required
def preset_list_view(request):
    presets = FleetStartPreset.objects.filter(owner=request.user, is_last_used=False)
    rows = [{"preset": preset, "summary": _summary(preset)} for preset in presets]
    return _render(request, "fleetops/start_presets.html", {"rows": rows})


def _from_page(request):
    # Without script the Save button posts the start form here as a browser page request.
    return "text/html" in request.headers.get("Accept", "")


def _save_result(request, *, ok, message, conflict=False, preset=None):
    if not _from_page(request):
        return JsonResponse(
            {"ok": ok, "conflict": conflict, "preset_id": preset.pk if preset else None, "message": message}
        )
    # Go back to the start page, with the saved preset loaded.
    (messages.success if ok else messages.error)(request, message)
    url = reverse("fleetops:start_fleet")
    return redirect(f"{url}?preset={preset.pk}" if preset else f"{url}?preset=none")


@fleet_starter_required(page=False)
@require_POST
def preset_save_view(request):
    form = PresetSaveForm(request.POST, prefix=PRESET_FORM_PREFIX)
    if not form.is_valid():
        errors = form.errors.get("name") or [_("Enter a name for the preset.")]
        return _save_result(request, ok=False, message=_("Preset not saved: %(error)s") % {"error": errors[0]})
    data = form.cleaned_data
    name = data["name"]
    try:
        preset = save_preset(
            request.user,
            name,
            request.POST,
            overwrite=data["overwrite"],
            remember_fc=data["remember_fc"],
            make_default=data["make_default"],
        )
    except PresetConflict:
        sentences = [_("A preset named %(name)s exists.") % {"name": name}]
        if _from_page(request):
            sentences.append(_("Choose another name, or rename or delete the old preset under My presets."))
        return _save_result(request, ok=False, conflict=True, message=" ".join(sentences))
    sentences = [_("Preset %(name)s saved.") % {"name": preset.name}]
    if data["remember_fc"] and preset.fc_character_id is None:
        sentences.append(_("No FC character of yours was selected, so none was remembered."))
    if preset.is_default:
        sentences.append(_("It is your default preset."))
    return _save_result(request, ok=True, message=" ".join(sentences), preset=preset)


@fleet_starter_required(page=False)
@require_POST
def preset_rename_view(request, pk):
    preset = _own_preset(request, pk)
    form = PresetRenameForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Preset not renamed: %(error)s") % {"error": form.errors["name"][0]})
        return redirect("fleetops:preset_list")
    name = form.cleaned_data["name"]
    taken = (
        FleetStartPreset.objects.filter(owner=request.user, is_last_used=False, name__iexact=name)
        .exclude(pk=preset.pk)
        .exists()
    )
    if not taken:
        old_name = preset.name
        preset.name = name
        try:
            with transaction.atomic():
                preset.save(update_fields=["name", "updated_at"])
        except IntegrityError:
            taken = True
        else:
            messages.success(request, _("Preset %(old)s renamed to %(new)s.") % {"old": old_name, "new": name})
    if taken:
        messages.error(request, _("You already have a preset named %(name)s.") % {"name": name})
    return redirect("fleetops:preset_list")


@fleet_starter_required(page=False)
@require_POST
def preset_delete_view(request, pk):
    preset = _own_preset(request, pk)
    name = preset.name
    preset.delete()
    messages.success(request, _("Preset %(name)s deleted.") % {"name": name})
    return redirect("fleetops:preset_list")


@fleet_starter_required(page=False)
@require_POST
def preset_default_view(request, pk):
    preset = _own_preset(request, pk)
    make_default = request.POST.get("is_default") == "1"
    if preset.is_default != make_default:
        preset.is_default = make_default
        preset.save(update_fields=["is_default", "updated_at"])
    if make_default:
        messages.success(
            request, _("Preset %(name)s is now your default; Start Fleet opens with it.") % {"name": preset.name}
        )
    else:
        messages.success(request, _("Preset %(name)s is no longer your default.") % {"name": preset.name})
    return redirect("fleetops:preset_list")
