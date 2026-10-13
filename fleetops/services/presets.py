"""Fleet Start presets: Fleet Start values an FC saves under a name and loads again later.

A preset keeps the Fleet Start field names with their form values (ids for configuration items)
in ``values["fields"]`` and the text each value showed in ``values["labels"]``, so loading can
name an item that was disabled or deleted since. The fleet time and the request id are never
kept; the FC character only when the FC asks for it.
"""

from django import forms
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from fleetops.models import FleetStartPreset
from fleetops.services.identity import get_owned_character

# Start form fields a preset never keeps: they belong to one start.
EXCLUDED_FIELDS = frozenset({"request_id", "fleet_timing", "fleet_date", "fleet_clock", "fc_character_id"})
FC_CHARACTER = "fc_character_id"
DOCTRINE = "doctrine_choice"
CUSTOM_DOCTRINE = "custom_doctrine"
# What a warning calls an item that is gone.
ITEM_NAMES = {
    "fleet_type": "Fleet type",
    "comms": "Comms preset",
    "logi_channel": "Logi channel",
    "boost_channel": "Boost channel",
    "ping_channel": "Ping channel",
    "ping_template": "Ping template",
    "motd_template": "MOTD template",
    "ping_mentions": "Ping target",
}


class PresetConflict(Exception):
    """The FC already has a preset with this name and did not ask to overwrite it."""

    def __init__(self, name):
        super().__init__(name)
        self.name = name


def _start_form_class(form_class):
    if form_class is not None:
        return form_class
    # Imported here: the forms import the services.
    from fleetops.forms import StartFleetForm

    return StartFleetForm


def preset_field_names(form):
    return [name for name in form.fields if name not in EXCLUDED_FIELDS]


def _stored(value):
    """A cleaned form value as it is kept in a preset."""
    if value is None:
        return ""
    if hasattr(value, "pk"):
        return str(value.pk)
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return str(value)


def _choice_labels(field):
    return {str(key): str(label) for key, label in field.choices}


def _label(field, name, value, cleaned):
    """The text a stored value showed, for the warning when it is gone; None when nothing to name."""
    if hasattr(cleaned, "pk"):
        return str(cleaned)
    if name == DOCTRINE and value:
        parts = value.split("|", 2)
        return parts[2] if len(parts) == 3 else value
    if isinstance(field, forms.MultipleChoiceField):
        labels = _choice_labels(field)
        return [labels.get(item, item) for item in value]
    return None


def _preset_values(form, cleaned_values):
    fields, labels = {}, {}
    for name, cleaned in cleaned_values.items():
        field = form.fields[name]
        value = _stored(cleaned)
        fields[name] = value
        label = _label(field, name, value, cleaned)
        if label:
            labels[name] = label
    return {"fields": fields, "labels": labels}


def _owned_character(user, character_id):
    text = str(character_id or "").strip()
    if not (text.isascii() and text.isdigit()):
        return None
    ownership = get_owned_character(user, int(text))
    return ownership.character if ownership else None


def values_from_data(form, data):
    """Preset values from posted start form data; values the form would refuse are left out."""
    cleaned_values = {}
    for name in preset_field_names(form):
        field = form.fields[name]
        raw = field.widget.value_from_datadict(data, {}, form.add_prefix(name))
        try:
            cleaned_values[name] = field.clean(raw)
        except ValidationError:
            continue
    return _preset_values(form, cleaned_values)


def values_from_cleaned_data(form, cleaned_data):
    """Preset values from the cleaned data of a start form that was submitted."""
    cleaned_values = {name: cleaned_data[name] for name in preset_field_names(form) if name in cleaned_data}
    return _preset_values(form, cleaned_values)


def _with_fc_character(values, character):
    if character is not None:
        values["labels"][FC_CHARACTER] = character.character_name
    return character.character_id if character is not None else None


def save_preset(user, name, data, *, overwrite=False, remember_fc=False, make_default=False, form_class=None):
    """Save the Fleet Start values in ``data`` as the user's preset ``name``; returns the preset.

    ``data`` is the posted start form, complete or not. Raises ``PresetConflict`` when the user
    already has a preset of that name (in any letter case) and ``overwrite`` is not set.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("A preset needs a name.")
    form = _start_form_class(form_class)(user=user)
    values = values_from_data(form, data)
    character = _owned_character(user, data.get(FC_CHARACTER)) if remember_fc else None
    fc_character_id = _with_fc_character(values, character)
    try:
        with transaction.atomic():
            existing = (
                FleetStartPreset.objects.select_for_update()
                .filter(owner=user, is_last_used=False, name__iexact=name)
                .first()
            )
            if existing is not None and not overwrite:
                raise PresetConflict(name)
            preset = existing or FleetStartPreset(owner=user)
            preset.name = name
            preset.values = values
            preset.fc_character_id = fc_character_id
            if make_default:
                preset.is_default = True
            preset.save()
    except IntegrityError:
        # Another request saved a preset of the same name first.
        raise PresetConflict(name)
    return preset


def load_preset(preset, form_class, user):
    """Initial values for ``form_class`` from a preset, and warnings about items that are gone."""
    form = form_class(user=user)
    stored = dict((preset.values or {}).get("fields") or {})
    labels = (preset.values or {}).get("labels") or {}
    initial, warnings = {}, []
    for name, value in stored.items():
        field = form.fields.get(name)
        if field is None or name in EXCLUDED_FIELDS or name == DOCTRINE:
            continue
        if isinstance(field, forms.ModelChoiceField):
            initial[name] = _model_choice(field, name, value, labels, warnings)
        elif isinstance(field, forms.MultipleChoiceField):
            initial[name] = _multiple_choice(field, name, value, labels, warnings)
        elif isinstance(field, forms.ChoiceField):
            if str(value) in _choice_labels(field):
                initial[name] = str(value)
        else:
            initial[name] = value if isinstance(value, str) else ""
    _load_doctrine(form, stored, labels, initial, warnings)
    _load_fc_character(preset, user, labels, initial, warnings)
    return initial, warnings


def _gone(name, field, label, warnings):
    item = ITEM_NAMES.get(name) or str(field.label or name)
    warnings.append(f"{item} '{label}' is no longer available.")


def _model_choice(field, name, value, labels, warnings):
    value = str(value or "")
    if not value:
        return ""
    try:
        exists = field.queryset.filter(pk=value).exists()
    except (TypeError, ValueError, ValidationError):
        exists = False
    if exists:
        return value
    _gone(name, field, labels.get(name) or value, warnings)
    return ""


def _multiple_choice(field, name, value, labels, warnings):
    values = [str(item) for item in (value if isinstance(value, list) else [value]) if str(item)]
    saved_labels = labels.get(name) or []
    available = _choice_labels(field)
    kept = []
    for index, item in enumerate(values):
        if item in available:
            kept.append(item)
        else:
            label = saved_labels[index] if index < len(saved_labels) else item
            _gone(name, field, label, warnings)
    if not kept and values:
        # Nothing the preset named is left: the form's own default instead of no choice at all.
        return list(field.initial or [])
    return kept


def _load_doctrine(form, stored, labels, initial, warnings):
    """A doctrine that is no longer offered comes back as a custom doctrine of the same name."""
    value = str(stored.get(DOCTRINE) or "")
    field = form.fields.get(DOCTRINE)
    if field is None:
        return
    if not value or value in _choice_labels(field):
        initial[DOCTRINE] = value
        return
    name = labels.get(DOCTRINE) or value
    initial[DOCTRINE] = ""
    initial[CUSTOM_DOCTRINE] = name
    warnings.append(f"Doctrine '{name}' is no longer available; it was entered as a custom doctrine.")


def _load_fc_character(preset, user, labels, initial, warnings):
    if not preset.fc_character_id:
        return
    if get_owned_character(user, preset.fc_character_id) is None:
        name = labels.get(FC_CHARACTER) or f"character {preset.fc_character_id}"
        warnings.append(f"FC character '{name}' is no longer one of your characters.")
        return
    initial[FC_CHARACTER] = str(preset.fc_character_id)


def save_last_used(user, cleaned_data, *, form_class=None):
    """Remember the values of a successful start as the user's last used settings."""
    form = _start_form_class(form_class)(user=user)
    values = values_from_cleaned_data(form, cleaned_data)
    fc_character_id = _with_fc_character(values, _owned_character(user, cleaned_data.get(FC_CHARACTER)))
    defaults = {"is_last_used": True, "is_default": False, "values": values, "fc_character_id": fc_character_id}
    try:
        with transaction.atomic():
            preset, _created = FleetStartPreset.objects.update_or_create(owner=user, name="", defaults=defaults)
    except IntegrityError:
        # A second start of the same FC created the row at the same moment.
        with transaction.atomic():
            preset, _created = FleetStartPreset.objects.update_or_create(owner=user, name="", defaults=defaults)
    return preset
