"""Forms of the Fleet Start presets."""

from django import forms

from fleetops.forms import _style

# The save fields are posted together with the start form, so their names must not clash with it.
PRESET_FORM_PREFIX = "preset"


class PresetSaveForm(forms.Form):
    # The fields belong to the start form (``form_id``); a required preset name must not stop a start.
    use_required_attribute = False

    name = forms.CharField(label="Preset name", max_length=100)
    remember_fc = forms.BooleanField(label="Remember FC character", required=False)
    make_default = forms.BooleanField(label="Set as default", required=False)
    overwrite = forms.BooleanField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, form_id="", **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)
        if form_id:
            for field in self.fields.values():
                field.widget.attrs["form"] = form_id


class PresetRenameForm(forms.Form):
    name = forms.CharField(max_length=100)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)
