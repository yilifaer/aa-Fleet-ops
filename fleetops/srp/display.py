"""Texts and number formats for the codes and amounts SRP stores.

Warnings, fleet checks and calculation notes are stored as a code with parameters and turned into text
only where they are shown, so the stored rows do not depend on the language. Unknown codes are shown as
their code, never as an error.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

LEVEL_INFO = "info"
LEVEL_WARNING = "warning"
LEVEL_DANGER = "danger"
LEVEL_OK = "ok"
WARNING_LEVELS = (LEVEL_INFO, LEVEL_WARNING, LEVEL_DANGER)
PRESENCE_LEVELS = (LEVEL_OK, LEVEL_WARNING, LEVEL_DANGER)

# Codes of the fleet presence check (PresenceResult.code), also used as warning codes.
IN_FLEET = "in_fleet"
NEAR = "near"
NOT_IN_FLEET = "not_in_fleet"
NEVER_SEEN = "never_seen"
REGISTERED = "registered"
OUTSIDE_WINDOW = "outside_window"
NOT_APPLICABLE = "not_applicable"

# Which limit kept an amount down: "loss_cap" is the per-loss cap, the others the monthly limits.
LIMIT_KIND_LABELS = {
    "loss_cap": "per-loss cap",
    "personal": "personal monthly limit",
    "corporation": "corporation monthly limit",
}
# Name of the limit inside "the monthly ... limit".
MONTHLY_KIND_LABELS = {"personal": "personal", "corporation": "corporation"}

MATCH_LABELS = {
    "doctrine_ship": "Doctrine and ship",
    "doctrine": "Doctrine",
    "ship": "Ship",
}

# Amounts with more digits than this are not ISK; they are shown as missing instead of failing the page.
MAX_DISPLAY_DIGITS = 30


def to_decimal(value) -> Decimal | None:
    """``value`` as a finite Decimal; None for None, booleans and anything unreadable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def whole_isk(value) -> int | None:
    """``value`` rounded to whole ISK (half up); None when it is not a number."""
    number = to_decimal(value)
    if number is None or number.adjusted() >= MAX_DISPLAY_DIGITS:
        return None
    return int(number.to_integral_value(rounding=ROUND_HALF_UP))


def format_isk(value) -> str:
    """``1,234,567,890 ISK``; empty for a missing value."""
    amount = whole_isk(value)
    return "" if amount is None else f"{amount:,} ISK"


def format_isk_plain(value) -> str:
    """Digits only, for the in-game "Give ISK" field; empty for a missing value."""
    amount = whole_isk(value)
    return "" if amount is None else str(max(amount, 0))


_SHORT_UNITS = ((10**3, "k"), (10**6, "m"), (10**9, "b"), (10**12, "t"))
TWO_PLACES = Decimal("0.01")


def format_isk_short(value) -> str:
    """``1.23b ISK``, ``250m ISK``; amounts below one thousand are written out."""
    amount = whole_isk(value)
    if amount is None:
        return ""
    sign = "-" if amount < 0 else ""
    magnitude = abs(amount)
    index = -1
    for position, (size, _suffix) in enumerate(_SHORT_UNITS):
        if magnitude >= size:
            index = position
    if index < 0:
        return f"{sign}{magnitude} ISK"
    scaled = (Decimal(magnitude) / _SHORT_UNITS[index][0]).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if scaled >= 1000 and index < len(_SHORT_UNITS) - 1:
        # 999,999,999 is 1000.00m after rounding: show it in the next unit.
        index += 1
        scaled = (Decimal(magnitude) / _SHORT_UNITS[index][0]).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    text = f"{scaled:f}".rstrip("0").rstrip(".")
    return f"{sign}{text}{_SHORT_UNITS[index][1]} ISK"


def _isk(params, name) -> str:
    text = format_isk((params or {}).get(name))
    return text or "0 ISK"


def _monthly_limit(params) -> str:
    """``monthly personal limit``, ``monthly corporation limit`` or just ``monthly limit``."""
    kind = (params or {}).get("kind", "")
    kind = MONTHLY_KIND_LABELS.get(kind, str(kind or ""))
    return f"monthly {kind} limit" if kind else "monthly limit"


def _text_over_limit(params):
    return (
        f"Over the {_monthly_limit(params)}: {_isk(params, 'requested')} requested, "
        f"{_isk(params, 'remaining')} left."
    )


def _text_reduced_by_limit(params):
    return (
        f"Reduced to the remaining {_monthly_limit(params)}: {_isk(params, 'remaining')} "
        f"of {_isk(params, 'requested')} requested."
    )


def _text_external_claim(params):
    claims = [str(item) for item in (params or {}).get("claims") or ()]
    return f"Also claimed in {', '.join(claims)}." if claims else "Also claimed in another SRP system."


def _text_ship_mismatch(params):
    ship = (params or {}).get("ship") or "another ship"
    return f"Tracked ship before the loss: {ship}."


def _text_near(params):
    params = params or {}
    action = "leaving" if params.get("direction") == "leave" else "joining"
    seconds = params.get("seconds")
    interval = params.get("interval")
    text = "Not in the fleet at the loss time by the tracking data"
    if isinstance(seconds, int):
        text += f", but within {seconds} s of {action}"
    if isinstance(interval, int):
        text += f"; tracking polls every {interval} s"
    return text + "."


# How serious a fleet presence code is; the claim stores the code only.
PRESENCE_LEVEL_BY_CODE = {
    IN_FLEET: LEVEL_OK,
    NEAR: LEVEL_WARNING,
    NOT_IN_FLEET: LEVEL_DANGER,
    NEVER_SEEN: LEVEL_DANGER,
    REGISTERED: LEVEL_WARNING,
    OUTSIDE_WINDOW: LEVEL_DANGER,
    NOT_APPLICABLE: LEVEL_OK,
}

PRESENCE_TEXTS = {
    IN_FLEET: lambda params: "In the fleet at the time of the loss.",
    NEAR: _text_near,
    NOT_IN_FLEET: lambda params: "Not in the fleet at the time of the loss.",
    NEVER_SEEN: lambda params: "This character was never seen in the fleet.",
    REGISTERED: lambda params: "No tracking data; registered for PAP in this fleet.",
    OUTSIDE_WINDOW: lambda params: "No tracking data; the loss is outside the time of the fleet.",
    NOT_APPLICABLE: lambda params: "Not a fleet loss.",
}

WARNING_TEXTS = {
    "over_limit": _text_over_limit,
    "reduced_by_limit": _text_reduced_by_limit,
    "value_estimate": lambda params: "Value is an ESI market estimate.",
    "value_pending": lambda params: "The value from zKillboard is not available yet.",
    "value_missing": lambda params: "No value available.",
    "external_claim": _text_external_claim,
    "ship_mismatch": _text_ship_mismatch,
}

NOTE_TEXTS = {
    "no_rule": lambda params: "No payout rule for this ship.",
    "no_value": lambda params: "Value not available yet.",
    "percentage_zero": lambda params: "The payout percentage is 0.",
    "loss_cap": lambda params: f"Capped at the per-loss cap of {_isk(params, 'cap')}.",
    "monthly_limit": lambda params: f"Limited to the remaining {_monthly_limit(params)} of {_isk(params, 'remaining')}.",
}


def _humanize(code) -> str:
    return str(code or "").replace("_", " ").strip().capitalize()


def _render(table, code, params) -> str:
    renderer = table.get(code)
    if renderer is None:
        return _humanize(code)
    return renderer(params if isinstance(params, dict) else {})


def presence_text(code, params=None) -> str:
    """The sentence for a fleet presence code."""
    return _render(PRESENCE_TEXTS, code, params)


def warning_text(code, params=None) -> str:
    """The sentence for a claim warning code; the fleet presence codes are warnings too."""
    if code in WARNING_TEXTS:
        return _render(WARNING_TEXTS, code, params)
    return _render(PRESENCE_TEXTS, code, params)


def presence_level(code) -> str:
    """``ok``, ``warning`` or ``danger`` for a fleet presence code; unknown codes are ``warning``."""
    return PRESENCE_LEVEL_BY_CODE.get(code, LEVEL_WARNING)


def note_text(code, params=None) -> str:
    """The sentence for a calculation note code."""
    return _render(NOTE_TEXTS, code, params)


def limit_kind_label(kind) -> str:
    return LIMIT_KIND_LABELS.get(kind, _humanize(kind))


def match_label(match) -> str:
    return MATCH_LABELS.get(match, _humanize(match))
