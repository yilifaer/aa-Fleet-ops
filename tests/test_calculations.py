import unittest
from decimal import Decimal

from fleetops.calculations import calculate_payouts, corporation_average
from fleetops.services.statistics import average_pap


class CorporationAverageTests(unittest.TestCase):
    def test_simple_average(self):
        self.assertEqual(corporation_average(300, 50), 6.0)

    def test_zero_main_characters(self):
        self.assertEqual(corporation_average(300, 0), 0.0)


class AveragePapTests(unittest.TestCase):
    """The average shown on the statistics pages: Decimal, two places, half up."""

    def test_exact_values_have_two_places(self):
        self.assertEqual(average_pap(300, 50), Decimal("6.00"))
        self.assertEqual(str(average_pap(300, 50)), "6.00")

    def test_rounds_half_up_not_half_even(self):
        # 0.625, 0.125 and 2.675 are exact halves or close to them in binary floating point.
        self.assertEqual(average_pap(5, 8), Decimal("0.63"))
        self.assertEqual(average_pap(1, 8), Decimal("0.13"))
        self.assertEqual(average_pap(107, 40), Decimal("2.68"))

    def test_recurring_values(self):
        self.assertEqual(average_pap(5, 3), Decimal("1.67"))
        self.assertEqual(average_pap(1, 3), Decimal("0.33"))
        self.assertEqual(average_pap(2, 3), Decimal("0.67"))

    def test_no_mains_is_zero(self):
        self.assertEqual(average_pap(300, 0), Decimal("0.00"))
        self.assertEqual(average_pap(0, 0), Decimal("0.00"))

    def test_no_pap_is_zero(self):
        self.assertEqual(average_pap(0, 12), Decimal("0.00"))

    def test_large_totals(self):
        self.assertEqual(average_pap(10**9 + 1, 7), Decimal("142857143.00"))
        self.assertEqual(average_pap(123_456_789, 1_000), Decimal("123456.79"))

    def test_accepts_decimal_totals(self):
        self.assertEqual(average_pap(Decimal("7"), 4), Decimal("1.75"))


class IncentiveTests(unittest.TestCase):
    def test_proportional_payout(self):
        rows = [
            {"user_id": 1, "points": Decimal("10"), "eligible": True, "waived": False},
            {"user_id": 2, "points": Decimal("90"), "eligible": True, "waived": False},
        ]
        self.assertEqual(calculate_payouts(1_000_000_000, rows), {1: 100_000_000, 2: 900_000_000})

    def test_ineligible_and_waived_are_excluded(self):
        rows = [
            {"user_id": 1, "points": 10, "eligible": True, "waived": False},
            {"user_id": 2, "points": 90, "eligible": False, "waived": False},
            {"user_id": 3, "points": 50, "eligible": True, "waived": True},
        ]
        self.assertEqual(calculate_payouts(100, rows), {1: 100, 2: 0, 3: 0})

    def test_remainder_goes_to_highest_points(self):
        rows = [
            {"user_id": 1, "points": 2, "eligible": True, "waived": False},
            {"user_id": 2, "points": 1, "eligible": True, "waived": False},
        ]
        self.assertEqual(calculate_payouts(10, rows), {1: 7, 2: 3})

    def test_equal_points_remainder_tie_break_user_id(self):
        rows = [
            {"user_id": 9, "points": 1, "eligible": True, "waived": False},
            {"user_id": 2, "points": 1, "eligible": True, "waived": False},
            {"user_id": 5, "points": 1, "eligible": True, "waived": False},
        ]
        result = calculate_payouts(10, rows)
        self.assertEqual(result[2], 4)
        self.assertEqual(sum(result.values()), 10)

    def test_zero_points(self):
        rows = [{"user_id": 1, "points": 0, "eligible": True, "waived": False}]
        self.assertEqual(calculate_payouts(100, rows), {1: 0})


if __name__ == "__main__":
    unittest.main()
