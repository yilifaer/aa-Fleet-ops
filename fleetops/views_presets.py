"""Fleet Start presets of the signed-in FC."""

from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from fleetops.forms_presets import PRESET_FORM_PREFIX, PresetRenameForm, PresetSaveForm
from fleetops.models import FleetStartPreset
from fleetops.services.presets import FC_CHARACTER, PresetConflict, save_preset
from fleetops.views import _render

# Labels of the stored values a preset list row shows, in this order.
SUMMARY_FIELDS = ("fleet_type", "doctrine_choice", "comms", "ping_channel")


def _own_preset(request, pk):
    # Presets of other FCs do not exist for this user.
    return get_object_or_404(FleetStartPreset, pk=pk, owner=request.user, is_last_used=False)


def _summary(preset):
    values = preset.values or {}
    fields = values.get("fields") or {}
    labels = values.get("labels") or {}
    parts = [str(labels[name]) for name in SUMMARY_FIELDS if labels.get(name)]
    if not labels.get("doctrine_choice") and fields.get("custom_doctrine"):
        parts.insert(1 if labels.get("fleet_type") else 0, str(fields["custom_doctrine"]))
    if preset.fc_character_id:
        parts.append(f"FC {labels.get(FC_CHARACTER) or preset.fc_character_id}")
    return " · ".join(parts)


@permission_required("fleetops.start_fleet", raise_exception=True)
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


@permission_required("fleetops.start_fleet", raise_exception=True)
@require_POST
def preset_save_view(request):
    form = PresetSaveForm(request.POST, prefix=PRESET_FORM_PREFIX)
    if not form.is_valid():
        errors = form.errors.get("name") or ["Enter a name for the preset."]
        return _save_result(request, ok=False, message=f"Preset not saved: {errors[0]}")
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
        message = f"A preset named {name} exists."
        if _from_page(request):
            message += " Choose another name, or rename or delete the old preset under My presets."
        return _save_result(request, ok=False, conflict=True, message=message)
    message = f"Preset {preset.name} saved."
    if data["remember_fc"] and preset.fc_character_id is None:
        message += " No FC character of yours was selected, so none was remembered."
    if preset.is_default:
        message += " It is your default preset."
    return _save_result(request, ok=True, message=message, preset=preset)


@permission_required("fleetops.start_fleet", raise_exception=True)
@require_POST
def preset_rename_view(request, pk):
    preset = _own_preset(request, pk)
    form = PresetRenameForm(request.POST)
    if not form.is_valid():
        messages.error(request, f"Preset not renamed: {form.errors['name'][0]}")
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
            messages.success(request, f"Preset {old_name} renamed to {name}.")
    if taken:
        messages.error(request, f"You already have a preset named {name}.")
    return redirect("fleetops:preset_list")


@permission_required("fleetops.start_fleet", raise_exception=True)
@require_POST
def preset_delete_view(request, pk):
    preset = _own_preset(request, pk)
    name = preset.name
    preset.delete()
    messages.success(request, f"Preset {name} deleted.")
    return redirect("fleetops:preset_list")


@permission_required("fleetops.start_fleet", raise_exception=True)
@require_POST
def preset_default_view(request, pk):
    preset = _own_preset(request, pk)
    make_default = request.POST.get("is_default") == "1"
    if preset.is_default != make_default:
        preset.is_default = make_default
        preset.save(update_fields=["is_default", "updated_at"])
    if make_default:
        messages.success(request, f"Preset {preset.name} is now your default; Start Fleet opens with it.")
    else:
        messages.success(request, f"Preset {preset.name} is no longer your default.")
    return redirect("fleetops:preset_list")
