import math
import unittest

from bot.strategy import probability_snapshot


class StrategyTests(unittest.TestCase):
    def test_probability_floor_and_cap_are_applied(self):
        components = {
            "settlement": {"spot_gap_bps": 1.19, "remaining_seconds": 120.0},
            "movement": {"volatility_120": .5 / math.sqrt(80)},
        }
        result = probability_snapshot(components)
        self.assertLess(result["p_up"], .73)
        self.assertAlmostEqual(result["applied_sigma_bps"], 2)
        self.assertEqual(result["probability_cap"], .98)


if __name__ == "__main__":
    unittest.main()
