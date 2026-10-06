import unittest
from decimal import Decimal

from fleetops.calculations import calculate_payouts, corporation_average


class CorporationAverageTests(unittest.TestCase):
    def test_simple_average(self):
        self.assertEqual(corporation_average(300, 50), 6.0)

    def test_zero_main_characters(self):
        self.assertEqual(corporation_average(300, 0), 0.0)


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
