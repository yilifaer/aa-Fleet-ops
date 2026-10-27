# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The "PAP characters per player" field of the forms that start a fleet, and the form that changes it later."""

from django import forms
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from fleetops.constants import MAX_PAP_CHARACTER_LIMIT, OVERRIDE_REASON_MIN_LENGTH
from fleetops.forms_common import style_fields
from fleetops.models import FleetOpsSettings
from fleetops.services import pap_limit
from fleetops.texts import translate_stored

LABEL = _("PAP characters per player")


class PapLimitForm(forms.Form):
    """A form with the limit of the fleet it describes; the field starts with the setting.

    Everybody except FC leads may only tighten the limit: with a setting, their value must be a number up to it.
    The page offers only that (``max``, ``required``); ``clean_pap_character_limit`` decides.
    """

    pap_character_limit = forms.IntegerField(
        label=LABEL,
        required=False,
        min_value=1,
        max_value=MAX_PAP_CHARACTER_LIMIT,
        widget=forms.NumberInput(attrs={"inputmode": "numeric", "autocomplete": "off"}),
    )

    def __init__(self, *args, user=None, pap_settings=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        # The settings the page has read already, so the field does not read them a second time.
        self.pap_settings = pap_settings or FleetOpsSettings.get_solo()
        field = self.fields["pap_character_limit"]
        field.initial = pap_limit.setting_limit(self.pap_settings)
        highest = pap_limit.ceiling(user, self.pap_settings)
        field.widget.attrs["max"] = highest if highest is not None else MAX_PAP_CHARACTER_LIMIT
        if highest is None:
            field.widget.attrs["placeholder"] = gettext("No limit")
            field.help_text = gettext("Characters of one player that get PAP in this fleet. Empty means no limit.")
        else:
            field.widget.attrs["required"] = True
            field.help_text = gettext(
                "Characters of one player that get PAP in this fleet, up to %(max)s (the setting). "
                "Only FC leads can allow more."
            ) % {"max": highest}

    def omitted_limit(self):
        """The limit of a post that does not carry the field at all (not even empty): the setting."""
        return pap_limit.setting_limit(self.pap_settings)

    def clean_pap_character_limit(self):
        widget = self.fields["pap_character_limit"].widget
        if widget.value_omitted_from_data(self.data, self.files, self.add_prefix("pap_character_limit")):
            return self.omitted_limit()
        value = self.cleaned_data.get("pap_character_limit")
        message = pap_limit.refusal(self.user, value, self.pap_settings)
        if message:
            raise forms.ValidationError(translate_stored(message), code="invalid")
        return value


class PapLimitChangeForm(PapLimitForm):
    """The control that changes the limit of a fleet that runs, or (FC leads, with a reason) of one that closed.

    It only draws the control: the view hands the posted values to ``pap_limit.change_limit``, which makes the rules.
    """

    reason = forms.CharField(
        label=_("Reason"),
        max_length=255,
        required=False,
        help_text=_("Required for a closed fleet; the audit log keeps it."),
    )

    def __init__(self, *args, operation, user=None, **kwargs):
        kwargs.setdefault("initial", {"pap_character_limit": operation.pap_character_limit})
        super().__init__(*args, user=user, **kwargs)
        self.operation = operation
        self.mode = pap_limit.change_access(user, operation)
        if self.mode == "closed":
            self.fields["reason"].widget.attrs.update({"required": True, "minlength": OVERRIDE_REASON_MIN_LENGTH})
        else:
            del self.fields["reason"]
        style_fields(self.fields)
