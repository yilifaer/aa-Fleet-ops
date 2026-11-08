# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Helpers shared by the FleetOps forms."""

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from fleetops.services.ingame_text import plain_text

# The longest doctrine name the database holds, and the longest text a field accepts before it is cleaned: room for
# a fitting link pasted from the game (about 600 characters), so the form can say what is wrong instead of the
# browser cutting the text off without a word.
DOCTRINE_NAME_MAX_LENGTH = 255
DOCTRINE_PASTE_MAX_LENGTH = 4000


class DoctrineNameField(forms.CharField):
    """A doctrine name typed or pasted by a person: game rich text is cleaned to plain text first.

    A name that is still longer than the database holds is an error, never silently shortened.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs["max_length"] = DOCTRINE_PASTE_MAX_LENGTH
        super().__init__(**kwargs)

    def clean(self, value):
        name = plain_text(super().clean(value))
        if len(name) > DOCTRINE_NAME_MAX_LENGTH:
            raise ValidationError(
                _("The doctrine name is %(length)s characters long without formatting; the limit is %(limit)s."),
                code="max_length",
                params={"length": len(name), "limit": DOCTRINE_NAME_MAX_LENGTH},
            )
        return name


def keep_stored_name(form, field_name, cleaned):
    """``cleaned``, or the stored name when the person posted it back unchanged.

    A record saved before the cleaning existed may hold game markup; saving it again without touching the name must
    not rewrite it (a payout rule and a fleet would stop matching by doctrine key).
    """
    stored = str(form.initial.get(field_name) or "").strip()
    posted = form[field_name].data
    if stored and posted is not None and str(posted).strip() == stored:
        return stored
    return cleaned


def style_fields(fields):
    """Give each widget of ``fields`` the Bootstrap class of its kind."""
    for field in fields.values():
        widget = field.widget
        if isinstance(widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple, forms.RadioSelect)):
            widget.attrs["class"] = "form-check-input"
        else:
            widget.attrs["class"] = "form-select" if isinstance(widget, forms.Select) else "form-control"


def to_minute(value):
    """``value`` without seconds: the forms show times to the minute."""
    return value.replace(second=0, microsecond=0) if value else None


# The names the forms used before the helpers moved here.
_style = style_fields
_to_minute = to_minute
