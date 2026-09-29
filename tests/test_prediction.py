import math
import unittest

from bot.models import COMBINED_MODEL, CORRECTED_MODEL, REGIME_MODEL
from bot.prediction import (ALL_COMPONENTS, COMPONENT_FEATURES, MicrostructureState,
                            PredictionEngine, analytic_releases, analytic_signal, build_components,
                            market_book_view, movement_stability)
from tests.helpers import NOW, MemoryRecorder, fixture


def micro_fixture():
    tokens = {"up": "Up", "down": "Down"}
    micro = MicrostructureState(tokens)
    micro.reset(NOW - 40_000)
    micro.observe({"event_type": "book", "asset_id": "up",
                   "bids": [{"price": ".79", "size": "20"}],
                   "asks": [{"price": ".80", "size": "10"}]}, NOW - 40_000)
    micro.observe({"event_type": "price_change", "timestamp": str(NOW - 10_000),
                   "price_changes": [{"asset_id": "up", "side": "BUY", "price": ".79",
                                      "size": "30", "best_bid": ".79", "best_ask": ".80"}]},
                  NOW - 10_000)
    micro.observe({"event_type": "last_trade_price", "asset_id": "up", "side": "BUY",
                   "size": "5", "price": ".80"}, NOW - 5_000)
    micro.observe({"event_type": "price_change", "timestamp": str(NOW),
                   "price_changes": [{"asset_id": "up", "side": "SELL", "price": ".80",
                                      "size": "8", "best_bid": ".79", "best_ask": ".80"}]}, NOW)
    return micro


def component_row():
    return {component: {name: 0.0 for name in names}
            for component, names in COMPONENT_FEATURES.items()}


def path_points(values, step_ms=1000):
    start = NOW - step_ms * (len(values) - 1)
    return [(start + index * step_ms, value) for index, value in enumerate(values)]


class PredictionTests(unittest.TestCase):
    def test_components_can_be_enabled_independently(self):
        _, context, history, quotes = fixture()
        basic = build_components(context, history, quotes, None, NOW, 120,
                                 ("settlement", "movement", "market"))
        self.assertEqual(set(basic), {"settlement", "movement", "market"})
        self.assertNotIn("order_flow", basic)
        with self.assertRaisesRegex(ValueError, "missing_component:order_flow"):
            build_components(context, history, quotes, None, NOW, 120, ("order_flow",))
        flow = build_components(context, history, quotes, micro_fixture(), NOW, 120,
                                ("order_flow",))
        self.assertGreater(flow["order_flow"]["depth_imbalance_1"], 0)
        self.assertGreater(flow["order_flow"]["trade_imbalance_30"], 0)
        self.assertIn("spot_minus_open", basic["settlement"])
        self.assertIn("twap_minus_open", basic["settlement"])
        self.assertIn("final_twap_p_up", basic["settlement"])
        self.assertIn("crossing_probability", basic["settlement"])

        # Missing settlement metadata must not suppress unrelated components.
        context["reference_ready"] = False
        self.assertIn("movement", build_components(context, history, {}, None, NOW, 120,
                                                   ("movement",)))
        self.assertIn("market", build_components({}, [], quotes, None, NOW, 120, ("market",)))
        self.assertIn("order_flow", build_components({}, [], {}, micro_fixture(), NOW, 120,
                                                     ("order_flow",)))
        with self.assertRaisesRegex(ValueError, "opening_or_twap_unavailable"):
            build_components(context, history, quotes, None, NOW, 120, ("settlement",))

    def test_one_sided_complementary_book_keeps_direction_but_not_fake_ask(self):
        quotes = {
            "Up": {"ready": True, "bid": ".99", "ask": None,
                   "source_age_ms": 1, "connection_age_ms": 0},
            "Down": {"ready": True, "bid": None, "ask": ".01",
                     "source_age_ms": 1, "connection_age_ms": 0},
        }
        view = market_book_view(quotes)
        self.assertEqual(view["book_mode"], "complementary_bounds")
        self.assertEqual(view["probability_lower"], .99)
        self.assertEqual(view["probability_upper"], 1)
        self.assertAlmostEqual(view["p_up"], .995)
        components = component_row()
        components["settlement"]["gap_over_expected_move"] = 4
        components["settlement"]["spot_gap_bps"] = 6
        components["movement"].update(momentum_10=1, momentum_30=1,
                                       momentum_60=1, twap_slope_30=1)
        components["market"].update(view)
        signal = analytic_signal(components, quotes, .07)
        self.assertEqual(signal["direction"], "SKIP")
        self.assertEqual(signal["reason"], "analytic_liquidity_unavailable")

    def test_order_flow_uses_only_up_token_and_absolute_size_deltas(self):
        micro = micro_fixture()
        before = micro.features(NOW)
        micro.observe({"event_type": "last_trade_price", "asset_id": "down", "side": "SELL",
                       "size": "999", "price": ".2"}, NOW)
        self.assertEqual(micro.features(NOW), before)
        self.assertGreater(before["order_flow_30"], 0)

    def test_quiet_order_flow_uses_socket_activity(self):
        micro = micro_fixture()
        micro.connection_activity(NOW + 20_000)
        features = micro.features(NOW + 20_000)
        self.assertEqual(features["market_momentum_10"], 0)

    def test_movement_stability_distinguishes_steady_and_choppy_paths(self):
        steady = movement_stability(path_points([84000 + index for index in range(121)]), NOW)
        choppy = movement_stability(path_points(
            [84000 + (8 if index % 2 else 0) for index in range(121)]), NOW)
        self.assertEqual(steady["regime"], "steady_up")
        self.assertEqual(choppy["regime"], "choppy_flat")
        self.assertGreater(steady["stability_score"], choppy["stability_score"])
        self.assertGreater(choppy["reversal_rate_120"], .9)

    def test_old_market_event_does_not_corrupt_newer_microstructure(self):
        micro = micro_fixture()
        before = micro.features(NOW)
        micro.observe({"event_type": "price_change", "timestamp": str(NOW - 20_000),
                       "price_changes": [{"asset_id": "up", "side": "BUY", "price": ".79",
                                          "size": "999", "best_bid": ".79", "best_ask": ".80"}]},
                      NOW + 1)
        self.assertEqual(micro.features(NOW + 1)["depth_imbalance_1"],
                         before["depth_imbalance_1"])

    def test_live_engine_can_emit_unvalidated_analytic_signal_without_model(self):
        market, context, history, quotes = fixture()
        context["reference"]["price_to_beat"] = "83990"
        market["tokens"] = {"up": "Up", "down": "Down"}
        recorder = MemoryRecorder()
        engine = PredictionEngine(recorder)
        engine.microstructures[market["slug"]] = micro_fixture()
        engine.observe(market, context, history, quotes, NOW)
        decision = next(row for row in recorder.records if row["kind"] == "stage4_decision")
        self.assertEqual(set(decision["components"]), set(ALL_COMPONENTS))
        self.assertEqual(decision["direction"], "Up")
        self.assertEqual(decision["reason"], "analytic_unvalidated_signal")
        self.assertFalse(decision["probability_calibrated"])
        self.assertEqual(set(decision["model_outputs"]),
                         {"btc_twap_v1", "btc_twap_v2", "btc_regime_v3", "btc_guard_v4"})
        self.assertEqual(decision["model_outputs"]["btc_twap_v2"]["direction"],
                         decision["direction"])

    def test_each_model_release_runs_a_complete_decision(self):
        _, _, _, quotes = fixture()
        components = component_row()
        components["settlement"].update(spot_gap_bps=1.19,
                                         remaining_seconds=120)
        components["movement"]["volatility_120"] = .5 / math.sqrt(80)
        components["movement"].update(momentum_10=1, momentum_30=1,
                                      momentum_60=1, twap_slope_30=1)
        components["market"]["p_up"] = .9
        outputs = analytic_releases(components, quotes, .07)
        self.assertGreater(outputs["btc_twap_v1"]["chance"], .99)
        self.assertLess(outputs["btc_twap_v2"]["chance"], .9)
        self.assertEqual(outputs["btc_twap_v1"]["direction"], "Up")
        self.assertEqual(outputs["btc_twap_v2"]["direction"], "SKIP")

    def test_live_engine_waits_for_complete_snapshot_inside_fixed_window(self):
        market, context, history, quotes = fixture()
        market["tokens"] = {"up": "Up", "down": "Down"}
        recorder = MemoryRecorder()
        engine = PredictionEngine(recorder)
        engine.microstructures[market["slug"]] = micro_fixture()
        incomplete = {name: dict(quote) for name, quote in quotes.items()}
        incomplete["Up"]["ready"] = False
        engine.observe(market, context, history, incomplete, NOW)
        self.assertNotIn(market["slug"], engine.decisions)
        engine.observe(market, context, history, quotes, NOW + 1000)
        self.assertEqual(engine.decisions[market["slug"]]["decision_ms"], NOW + 1000)
        decisions = [row for row in recorder.records if row["kind"] == "stage4_decision"]
        self.assertEqual(len(decisions), 1)

    def test_live_engine_freezes_incomplete_snapshot_at_window_close(self):
        market, context, history, quotes = fixture()
        market["tokens"] = {"up": "Up", "down": "Down"}
        engine = PredictionEngine(MemoryRecorder())
        engine.microstructures[market["slug"]] = micro_fixture()
        incomplete = {name: dict(quote) for name, quote in quotes.items()}
        incomplete["Up"]["ready"] = False
        engine.observe(market, context, history, incomplete, NOW)
        engine.observe(market, context, history, incomplete, NOW + 10_000)
        decision = engine.decisions[market["slug"]]
        self.assertEqual(decision["decision_ms"], NOW + 10_000)
        self.assertIn("market", decision["component_errors"])
        self.assertEqual(decision["model_outputs"]["btc_twap_v2"]["direction"],
                         "SKIP")

    def test_missing_settlement_snapshot_has_clean_model_reasons(self):
        market, context, history, quotes = fixture()
        market["tokens"] = {"up": "Up", "down": "Down"}
        context["reference_ready"] = False
        recorder = MemoryRecorder()
        engine = PredictionEngine(recorder)
        engine.microstructures[market["slug"]] = micro_fixture()
        engine.observe(market, context, history, quotes, NOW)
        engine.observe(market, context, history, quotes, NOW + 10_000)
        decision = engine.decisions[market["slug"]]
        self.assertTrue(decision["reason"].startswith(
            "analytic_missing_components:settlement"))
        self.assertEqual(set(decision["model_outputs"]),
                         {"btc_twap_v1", "btc_twap_v2", "btc_regime_v3", "btc_guard_v4"})

    def test_analytic_signal_treats_movement_disagreement_as_warning(self):
        _, _, _, quotes = fixture()
        missing = analytic_signal({}, quotes, .07)
        self.assertEqual(missing["direction"], "SKIP")
        self.assertIn("analytic_missing_components", missing["reason"])
        components = component_row()
        components["settlement"]["gap_over_expected_move"] = 4
        components["settlement"]["spot_gap_bps"] = 6
        components["movement"].update(momentum_10=-1, momentum_30=-1,
                                      momentum_60=-1, twap_slope_30=-1)
        components["market"]["p_up"] = .9
        components["order_flow"].update(depth_imbalance_1=1, depth_imbalance_3=1,
                                         order_flow_5=1, order_flow_30=1,
                                         trade_imbalance_10=1, trade_imbalance_30=1,
                                         market_momentum_10=1)
        disagrees = analytic_signal(components, quotes, .07)
        self.assertEqual(disagrees["direction"], "Up")
        self.assertEqual(disagrees["reason"], "analytic_unvalidated_signal")
        self.assertEqual(disagrees["warnings"], ["movement_disagreement"])

        components["market"]["p_up"] = .1
        market_disagrees = analytic_signal(components, quotes, .07)
        self.assertEqual(market_disagrees["direction"], "SKIP")
        self.assertEqual(market_disagrees["reason"], "analytic_disagreement:market")
        self.assertEqual(market_disagrees["warnings"], ["movement_disagreement"])
        self.assertIsNotNone(market_disagrees["estimated_edge"])

    def test_thin_gap_and_movement_disagreement_are_blocked_together(self):
        _, _, _, quotes = fixture()
        components = component_row()
        components["settlement"].update(gap_over_expected_move=4,
                                         spot_gap_bps=2)
        components["movement"].update(momentum_10=-1, momentum_30=-1,
                                      momentum_60=-1, twap_slope_30=-1)
        components["market"]["p_up"] = .9
        result = analytic_signal(components, quotes, .07)
        self.assertEqual(result["direction"], "SKIP")
        self.assertEqual(result["reason"], "analytic_combined_risk")
        self.assertTrue(result["combined_risk"])
        self.assertEqual(set(result["warnings"]),
                         {"thin_gap", "movement_disagreement"})

    def test_v3_blocks_thin_choppy_up_instead_of_every_movement_disagreement(self):
        _, _, _, quotes = fixture()
        components = component_row()
        components["settlement"].update(gap_over_expected_move=4,
                                         spot_gap_bps=2)
        components["movement"].update(momentum_10=1, momentum_30=1,
                                      momentum_60=1, twap_slope_30=1,
                                      regime="choppy_up")
        components["market"]["p_up"] = .9
        v2 = analytic_signal(components, quotes, .07, CORRECTED_MODEL)
        v3 = analytic_signal(components, quotes, .07, REGIME_MODEL)
        self.assertEqual(v2["direction"], "Up")
        self.assertEqual(v3["direction"], "SKIP")
        self.assertEqual(v3["reason"], "analytic_regime_risk")
        self.assertTrue(v3["model_risk_blocked"])

        components["movement"].update(momentum_10=-1, momentum_30=-1,
                                      momentum_60=-1, twap_slope_30=-1,
                                      regime="mixed_down")
        v2 = analytic_signal(components, quotes, .07, CORRECTED_MODEL)
        v3 = analytic_signal(components, quotes, .07, REGIME_MODEL)
        self.assertEqual(v2["reason"], "analytic_combined_risk")
        self.assertEqual(v3["direction"], "Up")
        self.assertFalse(v3["model_risk_blocked"])

    def test_v3_release_uses_its_875_recommended_threshold(self):
        _, _, _, quotes = fixture()
        components = component_row()
        components["settlement"].update(gap_over_expected_move=1.16,
                                         spot_gap_bps=6)
        components["movement"].update(momentum_10=1, momentum_30=1,
                                      momentum_60=1, twap_slope_30=1,
                                      regime="mixed_up")
        components["market"]["p_up"] = .9
        v2 = analytic_signal(components, quotes, .07, CORRECTED_MODEL)
        v3 = analytic_signal(components, quotes, .07, REGIME_MODEL)
        self.assertEqual(v2["reason"], "analytic_probability_below_90pct")
        self.assertEqual(v3["direction"], "Up")
        self.assertEqual(v3["minimum_probability"], .875)

    def test_v4_combines_risk_guards_and_uses_spot_or_twap_gap(self):
        _, _, _, quotes = fixture()
        components = component_row()
        components["settlement"].update(gap_over_expected_move=4,
                                         spot_gap_bps=5.2, twap_gap_bps=4.6)
        components["movement"].update(momentum_10=-1, momentum_30=-1,
                                      momentum_60=-1, twap_slope_30=-1,
                                      regime="mixed_down")
        components["market"]["p_up"] = .9
        result = analytic_signal(components, quotes, .07, COMBINED_MODEL)
        self.assertEqual(result["direction"], "SKIP")
        self.assertEqual(result["reason"], "analytic_v4_dual_risk")
        self.assertTrue(result["thin_gap"])
        self.assertEqual(result["thin_gap_source"], "spot_or_twap")
        self.assertAlmostEqual(result["thin_gap_distance_bps"], 4.6)

    def test_extreme_analytic_probability_is_capped(self):
        _, _, _, quotes = fixture()
        components = component_row()
        components["settlement"].update(gap_over_expected_move=8,
                                         spot_gap_bps=20)
        components["movement"].update(momentum_10=1, momentum_30=1,
                                      momentum_60=1, twap_slope_30=1)
        components["market"]["p_up"] = .9
        result = analytic_signal(components, quotes, .07)
        self.assertAlmostEqual(result["p_up"], .98)
        self.assertGreater(result["probability_before_cap"], .999)

    def test_estimated_edge_is_available_below_confidence_threshold(self):
        _, _, _, quotes = fixture()
        result = analytic_signal(component_row(), quotes, .07)
        self.assertEqual(result["direction"], "SKIP")
        self.assertEqual(result["reason"], "analytic_probability_below_90pct")
        self.assertAlmostEqual(result["estimated_edge"], .5 - .8 - .07 * .8 * .2)

    def test_displayed_decision_gap_uses_twap_not_spot(self):
        _, _, _, quotes = fixture()
        components = component_row()
        components["settlement"].update(spot_minus_open=99, twap_minus_open=7)
        result = analytic_signal(components, quotes, .07)
        self.assertEqual(result["distance_to_beat"], 7)

    def test_analytic_core_does_not_require_order_flow(self):
        _, _, _, quotes = fixture()
        components = component_row()
        components.pop("order_flow")
        components["settlement"]["gap_over_expected_move"] = 4
        components["movement"].update(momentum_10=1, momentum_30=1,
                                       momentum_60=1, twap_slope_30=1)
        components["market"]["p_up"] = .7
        result = analytic_signal(components, quotes, .07)
        self.assertEqual(result["direction"], "Up")
        self.assertEqual(result["agreement"]["order_flow"], "Unavailable")


if __name__ == "__main__":
    unittest.main()
