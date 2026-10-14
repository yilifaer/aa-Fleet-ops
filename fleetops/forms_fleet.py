# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Configuration forms of the Fleet tab: ship functions, their rules and fleet structure templates."""

from django import forms
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from fleetops.forms import _style
from fleetops.models import MAX_DATABASE_ID, FleetStructureTemplate, ShipFunction, ShipFunctionRule
from fleetops.services import composition_config, sde
from fleetops.services.fleet_rules import COMMANDER_ROLES, format_fleet_roles
from fleetops.texts import FLEET_ROLE_LABELS

NEEDS_SDE = _("Enter the numeric ID; name lookup needs django-eveonline-sde.")
SDE_UNREADABLE = _("The SDE could not be read. Enter the numeric ID.")


class ShipFunctionForm(forms.ModelForm):
    fleet_roles = forms.MultipleChoiceField(
        label=_("Fleet positions"),
        choices=[(role, FLEET_ROLE_LABELS[role]) for role in COMMANDER_ROLES],
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=_("Members in these positions count under this function whatever they fly."),
    )

    class Meta:
        model = ShipFunction
        fields = ["name", "color", "fleet_roles", "is_default", "sort_order", "is_active"]
        widgets = {"color": forms.TextInput(attrs={"type": "color"})}
        labels = {"color": _("Colour"), "is_default": _("Default function")}
        help_texts = {
            "color": _("Shown as a small swatch next to the function in the fleet composition."),
            "sort_order": _("Lower numbers come first; when two rules match a ship, the lower one wins."),
            "is_active": _("Inactive functions and their rules are ignored."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)
        self.fields["color"].widget.attrs["class"] = "form-control form-control-color"
        if self.instance.pk:
            self.initial["fleet_roles"] = list(self.instance.roles)

    def clean_fleet_roles(self):
        return format_fleet_roles(self.cleaned_data.get("fleet_roles") or ())

    def save(self, commit=True):
        function = super().save(commit=commit)
        # Shown after saving: the previous default lost its flag.
        demoted = getattr(function, "demoted_defaults", ())
        self.warnings = [_("“%(name)s” is no longer the default function.") % {"name": other.name} for other in demoted]
        return function


class ShipFunctionRuleForm(forms.ModelForm):
    target = forms.CharField(
        label=_("Ship type or group"),
        max_length=255,
        help_text=_("The SDE ID, or the exact English name (ship types by name need django-eveonline-sde)."),
    )

    class Meta:
        model = ShipFunctionRule
        fields = ["function", "kind", "target", "label"]
        labels = {"function": _("Ship function"), "label": pgettext_lazy("FleetOps", "Name")}
        help_texts = {
            "label": _("Filled from the SDE when it knows the ID; shown in the lists without the SDE."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)
        if self.instance.pk:
            self.initial["target"] = str(self.instance.target_id)

    def _resolve(self, kind, text):
        """``(id, English name)`` for the entered ID or name; adds the error to "target" and returns None."""
        if text.isascii() and text.isdigit():
            target_id = int(text)
            if not 0 < target_id <= MAX_DATABASE_ID:
                self.add_error("target", _("Enter a valid ID."))
                return None
            return target_id, composition_config.target_label(kind, target_id)
        try:
            found = composition_config.find_target(kind, text)
        except composition_config.SDEUnavailable:
            self.add_error("target", SDE_UNREADABLE if sde.sde_installed() else NEEDS_SDE)
            return None
        if found is None:
            noun = str(composition_config.KIND_LABELS[kind]).lower()
            self.add_error(
                "target",
                _("No %(noun)s is called “%(text)s”. Enter the exact English name or the ID.")
                % {"noun": noun, "text": text},
            )
        return found

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("kind")
        text = " ".join(str(cleaned.get("target") or "").split())
        if not kind or not text:
            return cleaned
        resolved = self._resolve(kind, text)
        if resolved is None:
            return cleaned
        target_id, name = resolved
        label = name or (cleaned.get("label") or "").strip() or composition_config.default_label(kind, target_id)
        duplicate = (
            ShipFunctionRule.objects.filter(kind=kind, target_id=target_id)
            .exclude(pk=self.instance.pk)
            .select_related("function")
            .first()
        )
        if duplicate is not None:
            self.add_error(
                "target",
                _("%(rule)s is already mapped to %(function)s.")
                % {"rule": composition_config.rule_text(duplicate), "function": duplicate.function.name},
            )
            return cleaned
        cleaned["label"] = label
        self.instance.target_id = target_id
        return cleaned


class FleetStructureTemplateForm(forms.ModelForm):
    class Meta:
        model = FleetStructureTemplate
        fields = ["name", "layout", "description", "is_active", "sort_order"]
        widgets = {"layout": forms.Textarea(attrs={"rows": 8, "data-structure-layout": "1", "spellcheck": "false"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)
        self.fields["layout"].widget.attrs["class"] = "form-control font-monospace"
        self.fields["layout"].widget.attrs["placeholder"] = "Main: Alpha, Bravo, Charlie\nLogi: Logi"
