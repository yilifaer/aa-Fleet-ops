"""PAP multiplier rules: which multipliers a fleet's duration allows.

A fleet qualifies for a tier when it ran strictly longer than the tier's threshold, so a
90-minute tier needs 90 min 01 s. 1x needs no tier and is always allowed.
"""

from datetime import timedelta

from django.utils import timezone

from fleetops.models import FleetOperation, FleetOpsSettings, MultiplierTier

Source = FleetOperation.MultiplierSource


def tiers():
    """The configured tiers, lowest threshold first."""
    return list(MultiplierTier.objects.order_by("threshold_minutes"))


def allowed_multipliers(duration):
    """1 plus the multiplier of every tier the duration is strictly longer than, sorted."""
    allowed = {1}
    if duration is not None:
        allowed.update(tier.multiplier for tier in tiers() if duration > timedelta(minutes=tier.threshold_minutes))
    return sorted(allowed)


def auto_multiplier(duration):
    """The multiplier the automatic mode applies: the highest one the duration allows."""
    return max(allowed_multipliers(duration))


def all_multipliers():
    """Every configured multiplier including 1, for overrides beyond the duration rule."""
    return sorted({1, *(tier.multiplier for tier in tiers())})


def needs_choice(duration, settings=None):
    """Whether the FC must pick the multiplier when ending a fleet that ran this long."""
    settings = settings or FleetOpsSettings.get_solo()
    if settings.multiplier_mode != FleetOpsSettings.MultiplierMode.FC:
        return False
    return len(allowed_multipliers(duration)) > 1


def effective_end(operation):
    """When the fleet ended for the duration rules: the end request, the close, or now."""
    return operation.end_requested_at or operation.ended_at or timezone.now()


def fleet_duration(operation, end=None):
    """How long the fleet ran (or runs) for the duration rules; None before it started."""
    if operation.started_at is None:
        return None
    return max(timedelta(0), (end or effective_end(operation)) - operation.started_at)


def requirements_text():
    """What the multipliers above 1x need, such as "2x > 90 min, 3x > 180 min"."""
    return ", ".join(f"{tier.multiplier}x > {tier.threshold_minutes} min" for tier in tiers())


def not_allowed_message(multiplier):
    """Why a fleet that did not run long enough cannot get ``multiplier``."""
    needed = [tier.threshold_minutes for tier in tiers() if tier.multiplier == multiplier]
    if not needed:
        return f"{multiplier}x is not a configured PAP multiplier."
    return f"{multiplier}x needs the fleet to run longer than {min(needed)} minutes."


def close_multiplier(operation, requested=None, settings=None):
    """The multiplier a fleet closes with: ``(multiplier, source, pending)``.

    The automatic mode takes the highest multiplier the duration allows. In the FC mode the FC's
    choice is checked against the duration again (the end may have been finished later, by the
    recovery) and lowered to the highest allowed one below it. Without a choice, as after an
    automatic end, the fleet closes at 1x; the choice is marked pending when the fleet ran long
    enough for more.
    """
    settings = settings or FleetOpsSettings.get_solo()
    allowed = allowed_multipliers(fleet_duration(operation))
    if settings.multiplier_mode == FleetOpsSettings.MultiplierMode.AUTO:
        return max(allowed), Source.AUTO, False
    if requested is not None:
        requested = int(requested)
        multiplier = requested if requested in allowed else max(x for x in allowed if x <= max(requested, 1))
        return multiplier, Source.FC, False
    return 1, Source.DEFAULT, len(allowed) > 1
