# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Forms of PAP links and PAP-link fleets."""

import uuid

from django import forms
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from fleetops.forms import _style
from fleetops.models import FleetOpsSettings, FleetType
from fleetops.providers.doctrines import get_doctrines
from fleetops.services.identity import owned_characters
from fleetops.services.pap_links import MAX_LINK_MINUTES, MIN_LINK_MINUTES

REQUIRE_ONLINE_HELP = _("Characters must be logged in to the game when they register. Untick for out-of-game events.")


class PapLinkForm(forms.Form):
    minutes = forms.IntegerField(
        label=_("Open for (minutes)"), min_value=MIN_LINK_MINUTES, max_value=MAX_LINK_MINUTES
    )
    require_online = forms.BooleanField(
        label=_("Require online"),
        required=False,
        initial=True,
        help_text=REQUIRE_ONLINE_HELP,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["minutes"].initial = FleetOpsSettings.get_solo().pap_link_minutes
        _style(self.fields)


def _character_choices(user):
    # No ESI check: a PAP-link fleet has no ESI fleet, so any of the user's characters can lead it.
    if user is None:
        return []
    main_id = getattr(getattr(user.profile, "main_character", None), "character_id", None)
    choices = []
    for ownership in owned_characters(user):
        character = ownership.character
        marker = _("MAIN") if character.character_id == main_id else _("ALT")
        choices.append((character.character_id, f"{character.character_name} — {marker}"))
    return choices


class LinkOnlyFleetForm(forms.Form):
    # One per page, so a double click on Start starts one fleet.
    request_id = forms.UUIDField(widget=forms.HiddenInput())
    fc_character_id = forms.TypedChoiceField(label=_("FC character"), coerce=int)
    fleet_type = forms.ModelChoiceField(label=_("Fleet type"), queryset=FleetType.objects.none())
    doctrine_choice = forms.ChoiceField(label=pgettext_lazy("FleetOps", "Doctrine"), required=False)
    custom_doctrine = forms.CharField(label=_("Custom doctrine"), required=False, max_length=255)
    formup = forms.CharField(label=_("Form up"), max_length=255)
    notes = forms.CharField(label=_("Notes"), required=False, widget=forms.Textarea(attrs={"rows": 2}))
    link_minutes = forms.IntegerField(
        label=_("PAP link open for (minutes)"), min_value=MIN_LINK_MINUTES, max_value=MAX_LINK_MINUTES
    )
    require_online = forms.BooleanField(
        label=_("Require online"), required=False, initial=True, help_text=REQUIRE_ONLINE_HELP
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["request_id"].initial = uuid.uuid4()
        self.fields["fc_character_id"].choices = _character_choices(user)
        self.fields["fleet_type"].queryset = FleetType.objects.filter(is_active=True)
        self.fields["doctrine_choice"].choices = [("", _("None / Custom"))] + [
            (f"{doctrine.source}|{doctrine.external_id}|{doctrine.name}", doctrine.name)
            for doctrine in get_doctrines(user)
        ]
        self.fields["link_minutes"].initial = FleetOpsSettings.get_solo().pap_link_minutes
        _style(self.fields)

    def clean(self):
        data = super().clean()
        choice = data.get("doctrine_choice") or ""
        custom = (data.get("custom_doctrine") or "").strip()
        if choice:
            source, external_id, name = choice.split("|", 2)
            data.update(doctrine_source=source, doctrine_external_id=external_id, doctrine_name=name)
        else:
            data.update(doctrine_source="custom" if custom else "none", doctrine_external_id="", doctrine_name=custom)
        return data
