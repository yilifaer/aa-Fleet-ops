from decimal import Decimal, ROUND_FLOOR


def corporation_average(total_attendance: int | float, main_character_count: int) -> float:
    return float(total_attendance) / main_character_count if main_character_count else 0.0


def calculate_payouts(budget: int, rows: list[dict]) -> dict[int, int]:
    """Distribute budget by points across eligible, non-waived FCs.

    Rounds each share down to whole ISK. Any remainder goes to the eligible
    FC with the highest points; user_id ascending is the deterministic tie-break.
    """
    eligible = [
        r for r in rows
        if r.get("eligible") and not r.get("waived") and Decimal(str(r.get("points", 0))) > 0
    ]
    result = {int(r["user_id"]): 0 for r in rows}
    if not eligible or budget <= 0:
        return result
    total_points = sum((Decimal(str(r["points"])) for r in eligible), Decimal("0"))
    paid = 0
    for row in eligible:
        exact = Decimal(budget) * Decimal(str(row["points"])) / total_points
        value = int(exact.quantize(Decimal("1"), rounding=ROUND_FLOOR))
        result[int(row["user_id"])] = value
        paid += value
    remainder = budget - paid
    if remainder:
        winner = sorted(eligible, key=lambda r: (-Decimal(str(r["points"])), int(r["user_id"])))[0]
        result[int(winner["user_id"])] += remainder
    return result
