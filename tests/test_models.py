import json
import math
import tempfile
import unittest
from pathlib import Path

from bot.models import (COMBINED_MODEL, CORRECTED_MODEL, LEGACY_MODEL, REGIME_MODEL,
                        model_release, probability_snapshot)
from bot.paper import PaperTrader


def quiet_components(gap=1.19, raw_sigma=.5):
    return {
        "settlement": {"spot_gap_bps": gap, "remaining_seconds": 120.0},
        "movement": {"volatility_120": raw_sigma / math.sqrt(80)},
    }


class ModelReleaseTests(unittest.TestCase):
    def test_legacy_and_corrected_releases_preserve_their_formulas(self):
        components = quiet_components()
        legacy = probability_snapshot(components, LEGACY_MODEL)
        corrected = probability_snapshot(components, CORRECTED_MODEL)
        self.assertGreater(legacy["p_up"], .99)
        self.assertAlmostEqual(legacy["applied_sigma_bps"], .5)
        self.assertLess(corrected["p_up"], .73)
        self.assertAlmostEqual(corrected["applied_sigma_bps"], 2)

    def test_regime_release_keeps_v2_probability_and_declares_its_threshold(self):
        components = quiet_components()
        corrected = probability_snapshot(components, CORRECTED_MODEL)
        regime = probability_snapshot(components, REGIME_MODEL)
        self.assertEqual(regime, {**corrected, "model_version": REGIME_MODEL})
        release = model_release(REGIME_MODEL)
        self.assertEqual(release["recommended_confidence"], .875)
        self.assertEqual(release["risk_rule"], "thin_gap_and_choppy_up")

        combined = probability_snapshot(components, COMBINED_MODEL)
        self.assertEqual(combined, {**corrected, "model_version": COMBINED_MODEL})

    def test_session_uses_its_selected_release(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "stage4-round-history.jsonl").write_text("")
            trader = PaperTrader(data, 50, model_version=LEGACY_MODEL)
            trader.add_signal({
                "slug": "btc-updown-5m-100", "start": 100,
                "decision_ms": 280_000, "direction": "SKIP",
                "p_up": .72, "p_down": .28, "fee_rate": .07,
                "components": quiet_components(),
                "agreement": {"movement": "Up"},
                "quotes": {"Up": {"ask": ".80"}},
            })
            candidate = trader.candidates["btc-updown-5m-100"]
            self.assertEqual(candidate["model_version"], LEGACY_MODEL)
            self.assertGreater(candidate["chance"], .99)
            trader.close()

    def test_session_enforces_selected_models_complete_market_decision(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "stage4-round-history.jsonl").write_text("")
            trader = PaperTrader(data, 50, model_version=LEGACY_MODEL)
            slug = "btc-updown-5m-100"
            trader.add_signal({
                "slug": slug, "start": 100, "decision_ms": 280_000,
                "direction": "Up", "p_up": .95, "p_down": .05,
                "fee_rate": .07, "quotes": {"Up": {"ask": ".80"}},
                "model_outputs": {
                    LEGACY_MODEL: {
                        "model_version": LEGACY_MODEL, "p_up": .99,
                        "p_down": .01, "side": "Up", "chance": .99,
                        "direction": "SKIP", "reason": "analytic_disagreement:market",
                        "agreement": {"market": "Down", "movement": "Up"},
                        "warnings": [],
                    }
                },
            })
            self.assertNotIn(slug, trader.candidates)
            rejection = [json.loads(line) for line in
                         trader.log_path.read_text().splitlines()
                         if 'signal_rejected' in line][0]
            self.assertEqual(rejection["reason"], "model_market_disagreement")
            trader.close()

    def test_v3_session_uses_its_regime_rule_instead_of_v2_combined_rule(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "stage4-round-history.jsonl").write_text("")
            trader = PaperTrader(data, 50, model_version=REGIME_MODEL,
                                 model_threshold=.875)
            slug = "btc-updown-5m-100"
            trader.add_signal({
                "slug": slug, "start": 100, "decision_ms": 280_000,
                "fee_rate": .07, "quotes": {"Up": {"ask": ".80"}},
                "components": {"settlement": {"spot_gap_bps": 2}},
                "model_outputs": {
                    REGIME_MODEL: {
                        "model_version": REGIME_MODEL, "p_up": .88,
                        "p_down": .12, "side": "Up", "chance": .88,
                        "direction": "Up", "reason": "analytic_unvalidated_signal",
                        "agreement": {"market": "Up", "movement": "Down"},
                        "warnings": ["thin_gap", "movement_disagreement"],
                        "combined_risk": True, "regime_risk": False,
                        "model_risk_blocked": False,
                    }
                },
            })
            self.assertIn(slug, trader.candidates)
            self.assertEqual(trader.candidates[slug]["risk_allocation_multiplier"], .25)
            trader.close()

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "stage4-round-history.jsonl").write_text("")
            trader = PaperTrader(data, 50, model_version=REGIME_MODEL,
                                 model_threshold=.875)
            slug = "btc-updown-5m-200"
            trader.add_signal({
                "slug": slug, "start": 200, "decision_ms": 580_000,
                "fee_rate": .07, "quotes": {"Up": {"ask": ".80"}},
                "components": {"settlement": {"spot_gap_bps": 2}},
                "model_outputs": {
                    REGIME_MODEL: {
                        "model_version": REGIME_MODEL, "p_up": .9,
                        "p_down": .1, "side": "Up", "chance": .9,
                        "direction": "SKIP", "reason": "analytic_regime_risk",
                        "agreement": {"market": "Up", "movement": "Up"},
                        "warnings": ["thin_gap", "choppy_up_regime"],
                        "combined_risk": False, "regime_risk": True,
                        "model_risk_blocked": True,
                    }
                },
            })
            self.assertNotIn(slug, trader.candidates)
            self.assertIn('"reason":"regime_risk"', trader.log_path.read_text())
            trader.close()


if __name__ == "__main__":
    unittest.main()
