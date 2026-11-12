# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Helpers shared by the FleetOps forms."""

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from fleetops import texts
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


class DoctrineSelect(forms.Select):
    """The doctrine dropdown; ``choose_placeholder`` makes the empty entry a disabled one the user has to leave."""

    choose_placeholder = False

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        if self.choose_placeholder and value == "":
            option["attrs"]["disabled"] = True
        return option


class DoctrineChoiceField(forms.ChoiceField):
    """The doctrine dropdown of a ping form: ``source|external id|name`` values, none of them required.

    The choices hold only the doctrines the user may ping. A value that is not among them but names a doctrine the
    user may not ping (``scope``, a ``doctrine_access.Scope``) gets the refusal text of the feature, not Django's
    "Select a valid choice", so a forged request is answered the same way the page would.
    """

    scope = None

    widget = DoctrineSelect

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        super().__init__(**kwargs)

    def validate(self, value):
        try:
            super().validate(value)
        except ValidationError as exc:
            if exc.code == "invalid_choice" and self._refused(value):
                raise ValidationError(texts.translate_stored(texts.DOCTRINE_NOT_ALLOWED()), code="doctrine_not_allowed")
            raise

    def _refused(self, value) -> bool:
        if self.scope is None:
            return False
        parts = str(value).split("|", 2)
        return len(parts) == 3 and not self.scope.can_use_doctrine(parts[0], parts[1])


def set_up_doctrine_fields(form, user, choices, *, keep=None):
    """Fill the doctrine dropdown of ``form`` with the doctrines of ``choices`` that ``user`` may ping.

    ``choices`` are the ``DoctrineChoice`` rows Fittings shows the user. ``keep`` is a ``(source, external id)`` that
    stays in the list although it is restricted, for a scheduled fleet that already has it. The "None / Custom" entry is
    left out for a user who may not use custom names, as long as there is a doctrine they may choose; the form gets
    ``custom_name_allowed`` (for the template) and ``doctrine_scope``. Returns the dropdown's ``(value, label)`` list.
    """
    from fleetops.services import doctrine_access

    scope = doctrine_access.Scope(user)
    form.doctrine_scope = scope
    allowed = scope.allowed(choices)
    if keep is not None and keep[0] not in doctrine_access.FREE_SOURCES:
        kept = next((c for c in choices if (c.source, c.external_id) == (keep[0], str(keep[1]))), None)
        if kept is not None and kept not in allowed:
            allowed = sorted([*allowed, kept], key=lambda row: row.name.casefold())
    options = [(f"{choice.source}|{choice.external_id}|{choice.name}", choice.name) for choice in allowed]
    custom_allowed = scope.can_use_custom_name()
    if keep is not None and keep[0] in ("custom", "none"):
        # A scheduled fleet with a typed name (or none) keeps the "None / Custom" entry it was scheduled with.
        custom_allowed = True
    form.custom_name_allowed = custom_allowed
    # Without a doctrine to choose, "None" stays: the user could not start a fleet at all.
    form.blank_doctrine_allowed = custom_allowed or not options
    field = form.fields["doctrine_choice"]
    if form.blank_doctrine_allowed:
        options.insert(0, ("", _("None / Custom")))
    else:
        # No blank entry to match an empty value: without a placeholder the browser would show (and post) the first
        # doctrine without the user choosing it.
        options.insert(0, ("", _("Choose a doctrine")))
    if hasattr(field.widget, "choose_placeholder"):
        field.widget.choose_placeholder = not form.blank_doctrine_allowed
    field.choices = options
    if isinstance(field, DoctrineChoiceField):
        field.scope = scope
    return options


def clean_doctrine_access(form, data, *, field_name="doctrine_choice"):
    """Add the refusal of the doctrine rules to ``form`` for the doctrine ``data`` names (after ``_clean_doctrine``)."""
    from fleetops.services import doctrine_access

    if field_name in form.errors:
        return
    message = doctrine_access.refusal(
        form.user, data, scope=form.doctrine_scope, blank_allowed=getattr(form, "blank_doctrine_allowed", True)
    )
    if message:
        source = data.get("doctrine_source") or ""
        # The text box is not on the page of a user who may not type a name: the error goes to the dropdown then.
        typed = source in doctrine_access.FREE_SOURCES and getattr(form, "custom_name_allowed", True)
        target = "custom_doctrine" if typed else field_name
        form.add_error(target if target in form.fields else None, texts.translate_stored(message))


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
