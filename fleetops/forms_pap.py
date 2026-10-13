"""Forms of the PAP rules: multiplier tiers."""

from operator import attrgetter

from django import forms

from fleetops.forms import _style
from fleetops.models import MAX_MULTIPLIER, MultiplierTier


class MultiplierTierForm(forms.ModelForm):
    class Meta:
        model = MultiplierTier
        fields = ["threshold_minutes", "multiplier"]
        labels = {"threshold_minutes": "Longer than (minutes)", "multiplier": "PAP multiplier"}
        help_texts = {"multiplier": f"2 to {MAX_MULTIPLIER}. Shorter fleets get 1x without a tier."}

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
                f"Multipliers must rise with the time: fleets longer than {shorter.threshold_minutes} minutes "
                f"already get {shorter.multiplier}x.",
            )
        elif longer is not None and longer.multiplier <= multiplier:
            self.add_error(
                "multiplier",
                f"Multipliers must rise with the time: fleets longer than {longer.threshold_minutes} minutes "
                f"get only {longer.multiplier}x.",
            )
        return cleaned
