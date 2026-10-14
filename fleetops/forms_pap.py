# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Forms of the PAP rules: multiplier tiers."""

from operator import attrgetter

from django import forms
from django.utils.text import format_lazy
from django.utils.translation import gettext_lazy as _

from fleetops.forms import _style
from fleetops.models import MAX_MULTIPLIER, MultiplierTier


class MultiplierTierForm(forms.ModelForm):
    class Meta:
        model = MultiplierTier
        fields = ["threshold_minutes", "multiplier"]
        labels = {"threshold_minutes": _("Longer than (minutes)"), "multiplier": _("PAP multiplier")}
        help_texts = {
            "multiplier": format_lazy(_("2 to {max}. Shorter fleets get 1x without a tier."), max=MAX_MULTIPLIER)
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)

    def clean(self):
        cleaned = super().clean()
        threshold = cleaned.get("threshold_minutes")
        multiplier = cleaned.get("multiplier")
        if threshold is None or multiplier is None:
            return cleaned
        # Longer fleets must always get more: above every shorter tier and below every longer one.
        others = list(MultiplierTier.objects.exclude(pk=self.instance.pk))
        shorter = max((o for o in others if o.threshold_minutes < threshold), key=attrgetter("multiplier"), default=None)
        longer = min((o for o in others if o.threshold_minutes > threshold), key=attrgetter("multiplier"), default=None)
        if shorter is not None and shorter.multiplier >= multiplier:
            self.add_error(
                "multiplier",
                _(
                    "Multipliers must rise with the time: fleets longer than %(minutes)s minutes "
                    "already get %(multiplier)sx."
                )
                % {"minutes": shorter.threshold_minutes, "multiplier": shorter.multiplier},
            )
        elif longer is not None and longer.multiplier <= multiplier:
            self.add_error(
                "multiplier",
                _(
                    "Multipliers must rise with the time: fleets longer than %(minutes)s minutes "
                    "get only %(multiplier)sx."
                )
                % {"minutes": longer.threshold_minutes, "multiplier": longer.multiplier},
            )
        return cleaned
