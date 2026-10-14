# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Forms of the Fleet Start presets."""

from django import forms
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from fleetops.forms import _style

# The save fields are posted together with the start form, so their names must not clash with it.
PRESET_FORM_PREFIX = "preset"


class PresetSaveForm(forms.Form):
    # The fields belong to the start form (``form_id``); a required preset name must not stop a start.
    use_required_attribute = False

    name = forms.CharField(label=_("Preset name"), max_length=100)
    remember_fc = forms.BooleanField(label=_("Remember FC character"), required=False)
    make_default = forms.BooleanField(label=_("Set as default"), required=False)
    overwrite = forms.BooleanField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, form_id="", **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)
        if form_id:
            for field in self.fields.values():
                field.widget.attrs["form"] = form_id


class PresetRenameForm(forms.Form):
    name = forms.CharField(label=pgettext_lazy("FleetOps", "Name"), max_length=100)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)
