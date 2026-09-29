import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from bot.paper import (allocation, edge_per_share, maximum_value_price,
                       PaperTrader, risk_adjusted_allocation, session_allocation,
                       settle_fill, simulated_fill)
from bot.real import RealTrader


class PaperTraderTests(unittest.TestCase):
    def test_allocation_rule(self):
        self.assertEqual(allocation(50), 40)
        self.assertEqual(allocation(25), 20)
        self.assertEqual(allocation(20), 20)
        self.assertEqual(allocation(12.5), 12.5)
        self.assertEqual(session_allocation(50, .6), 30)
        self.assertEqual(session_allocation(50, .8, 25), 25)
        self.assertEqual(risk_adjusted_allocation(2.225, .8, None, .25), 1)
        self.assertEqual(risk_adjusted_allocation(2.225, .8, None, .5), 1)
        self.assertEqual(risk_adjusted_allocation(2.225, .8, .75, .5), 0)

    def test_value_price_replaces_fixed_two_cent_limit(self):
        maximum = maximum_value_price(.94, .07, .03)
        self.assertGreater(maximum, .89)
        self.assertLess(maximum, .92)
        self.assertAlmostEqual(edge_per_share(.94, maximum, .07), .03, places=8)

    def test_fill_uses_budget_including_fees_and_requires_visible_size(self):
        fill, reason = simulated_fill(50, .80, 100, .07)
        self.assertIsNone(reason)
        self.assertAlmostEqual(fill["cost"], 40)
        self.assertLessEqual(fill["shares"], 100)
        missing, reason = simulated_fill(50, .80, 10, .07)
        self.assertIsNone(missing)
        self.assertEqual(reason, "insufficient_size")

    def test_settlement_returns_payout_and_full_loss(self):
        fill, _ = simulated_fill(10, .80, 100, .07)
        win = settle_fill(fill, "Up", "Up")
        loss = settle_fill(fill, "Up", "Down")
        self.assertGreater(win["profit"], 0)
        self.assertEqual(loss["profit"], -fill["cost"])
        self.assertEqual(loss["payout"], 0)

    def test_sidecar_reads_future_signal_fills_and_stops_after_all_in_loss(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            history = data / "stage4-round-history.jsonl"
            history.write_text("")
            trader = PaperTrader(data, 10, stop_entry_at_remaining=50, poll=.05)
            slug, start, decision_ms = "btc-updown-5m-100", 100, 280_000
            decision = {"record_type": "decision", "slug": slug, "start": start,
                        "decision_ms": decision_ms, "direction": "Up", "p_up": .94,
                        "fee_rate": .07, "quotes": {"Up": {"ask": ".80"}}}
            with history.open("a") as output:
                output.write(json.dumps(decision) + "\n")
            active = data / "stage4-active"
            active.mkdir()
            (active / "test-100.jsonl").write_text(json.dumps({
                "kind": "quotes", "slug": slug, "received_ms": 281_000,
                "quotes": {"Up": {"ready": True, "ask": ".80", "ask_size": "100",
                                   "connection_age_ms": 0}}}) + "\n")
            trader.process_history()
            trader.process_quotes()
            self.assertIn(slug, trader.positions)
            self.assertEqual(trader.positions[slug]["entry_remaining_seconds"], 119)
            self.assertAlmostEqual(trader.bankroll, 0)
            with history.open("a") as output:
                output.write(json.dumps({"record_type": "resolution", "slug": slug,
                                         "actual_result": "Down"}) + "\n")
            trader.process_history()
            self.assertFalse(trader.running)
            self.assertEqual(trader.bankroll, 0)
            self.assertEqual(trader.losses, 1)
            events = [json.loads(line)["event"] for line in trader.log_path.read_text().splitlines()]
            self.assertIn("trade_filled", events)
            self.assertIn("trade_resolved", events)
            self.assertIn("game_over", events)
            trader.close()

    def test_quotes_after_fifty_seconds_remaining_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            history = data / "stage4-round-history.jsonl"
            history.write_text("")
            trader = PaperTrader(data, 50, stop_entry_at_remaining=50, poll=.05)
            slug, start = "btc-updown-5m-100", 100
            with history.open("a") as output:
                output.write(json.dumps({"record_type": "decision", "slug": slug, "start": start,
                    "decision_ms": 280_000, "direction": "Up", "p_up": .94,
                    "fee_rate": .07, "quotes": {"Up": {"ask": ".80"}}}) + "\n")
            active = data / "stage4-active"
            active.mkdir()
            (active / "test-100.jsonl").write_text(json.dumps({
                "kind": "quotes", "slug": slug, "received_ms": 351_000,
                "quotes": {"Up": {"ready": True, "ask": ".80", "ask_size": "100",
                                   "connection_age_ms": 0}}}) + "\n")
            trader.process_history()
            trader.process_quotes()
            self.assertNotIn(slug, trader.positions)
            self.assertIn(slug, trader.candidates)
            trader.close()

    def test_skip_becomes_candidate_when_model_probability_is_at_least_ninety_percent(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "stage4-round-history.jsonl").write_text("")
            trader = PaperTrader(data, 50)
            trader.add_signal({
                "slug": "btc-updown-5m-100", "start": 100, "decision_ms": 280_000,
                "direction": "SKIP", "p_up": .93, "p_down": .07, "fee_rate": .07,
                "quotes": {"Up": {"ask": ".80"}},
            })
            candidate = trader.candidates["btc-updown-5m-100"]
            self.assertEqual(candidate["side"], "Up")
            self.assertEqual(candidate["main_bot_direction"], "SKIP")
            self.assertAlmostEqual(candidate["chance"], .93)
            self.assertEqual(candidate["deadline_ms"], (100 + 300 - 30) * 1000)
            trader.close()

    def test_main_bot_signal_is_ignored_when_model_probability_is_below_threshold(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "stage4-round-history.jsonl").write_text("")
            trader = PaperTrader(data, 50)
            trader.add_signal({
                "slug": "btc-updown-5m-100", "start": 100, "decision_ms": 280_000,
                "direction": "Up", "p_up": .89, "p_down": .11, "fee_rate": .07,
                "quotes": {"Up": {"ask": ".80"}},
            })
            self.assertFalse(trader.candidates)
            trader.close()

    def test_combined_risk_is_rejected_by_paper_trader(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "stage4-round-history.jsonl").write_text("")
            trader = PaperTrader(data, 50)
            slug = "btc-updown-5m-100"
            trader.add_signal({
                "slug": slug, "start": 100, "decision_ms": 280_000,
                "direction": "SKIP", "p_up": .94, "p_down": .06,
                "fee_rate": .07, "warnings": ["thin_gap", "movement_disagreement"],
                "agreement": {"movement": "Down"},
                "components": {"settlement": {"spot_gap_bps": 2}},
                "quotes": {"Up": {"ask": ".80"}},
            })
            self.assertNotIn(slug, trader.candidates)
            self.assertIn(slug, trader.handled)
            events = [json.loads(line) for line in trader.log_path.read_text().splitlines()]
            rejection = next(row for row in events if row["event"] == "signal_rejected")
            self.assertEqual(rejection["reason"], "combined_risk")
            trader.close()

    def test_single_risk_flag_uses_session_size_rule(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "stage4-round-history.jsonl").write_text("")
            trader = PaperTrader(data, 50, allocation_fraction=.8,
                                 thin_gap_allocation_fraction=.25)
            slug = "btc-updown-5m-100"
            trader.add_signal({
                "slug": slug, "start": 100, "decision_ms": 280_000,
                "direction": "Up", "p_up": .94, "p_down": .06,
                "fee_rate": .07, "warnings": ["thin_gap"],
                "agreement": {"movement": "Up"},
                "components": {"settlement": {"spot_gap_bps": 2}},
                "quotes": {"Up": {"ask": ".80"}},
            })
            candidate = trader.candidates[slug]
            self.assertEqual(candidate["risk_allocation_multiplier"], .25)
            trader.consider_quotes(candidate, {
                "received_ms": 281_000,
                "quotes": {"Up": {"ready": True, "ask": ".80",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            self.assertAlmostEqual(trader.positions[slug]["fill"]["cost"], 10)
            trader.close()

    def test_zero_single_risk_size_means_skip(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "stage4-round-history.jsonl").write_text("")
            trader = PaperTrader(data, 50,
                                 movement_disagreement_allocation_fraction=0)
            slug = "btc-updown-5m-100"
            trader.add_signal({
                "slug": slug, "start": 100, "decision_ms": 280_000,
                "direction": "Up", "p_up": .94, "p_down": .06,
                "fee_rate": .07, "warnings": ["movement_disagreement"],
                "agreement": {"movement": "Down"},
                "components": {"settlement": {"spot_gap_bps": 8}},
                "quotes": {"Up": {"ask": ".80"}},
            })
            self.assertNotIn(slug, trader.candidates)
            self.assertIn('"reason":"session_risk_rule"', trader.log_path.read_text())
            trader.close()

    def test_runtime_update_reprices_unfilled_candidate_and_next_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "stage4-round-history.jsonl").write_text("")
            trader = PaperTrader(data, 50, minimum_edge=.03,
                                 stop_entry_at_remaining=30,
                                 allocation_fraction=.8)
            slug = "btc-updown-5m-100"
            trader.add_signal({
                "slug": slug, "start": 100, "decision_ms": 280_000,
                "direction": "Up", "p_up": .94, "p_down": .06,
                "fee_rate": .07, "quotes": {"Up": {"ask": ".80"}},
            })
            old_price = trader.candidates[slug]["maximum_value_price"]
            trader.update_configuration({
                "minimum_edge": .06, "entry_cutoff_seconds": 50,
                "allocation_fraction": .5,
            })
            candidate = trader.candidates[slug]
            self.assertLess(candidate["maximum_value_price"], old_price)
            self.assertEqual(candidate["deadline_ms"], 350_000)
            self.assertEqual(trader._trade_budget(), 25)
            self.assertIn("configuration_updated", trader.log_path.read_text())
            trader.close()

    def test_session_loss_limit_stops_after_open_trade_resolves(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "stage4-round-history.jsonl").write_text("")
            trader = PaperTrader(data, 50, allocation_fraction=.5, max_loss_usd=5)
            slug = "btc-updown-5m-100"
            trader.add_signal({
                "slug": slug, "start": 100, "decision_ms": 280_000,
                "direction": "Up", "p_up": .94, "p_down": .06,
                "fee_rate": .07, "quotes": {"Up": {"ask": ".80"}},
            })
            trader.consider_quotes(trader.candidates[slug], {
                "received_ms": 281_000,
                "quotes": {"Up": {"ready": True, "ask": ".80",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            self.assertAlmostEqual(trader.positions[slug]["fill"]["cost"], 25)
            trader.resolve({"slug": slug, "actual_result": "Down"})
            status = json.loads(trader.status_path.read_text())
            self.assertFalse(trader.running)
            self.assertEqual(status["status"], "COMPLETED")
            self.assertEqual(status["reason"], "maximum_loss_reached")
            self.assertLessEqual(status["realized_profit"], -5)
            trader.close()

    def test_real_trader_sizes_from_live_cash_and_submits_once(self):
        class Account:
            def __init__(self):
                self.orders = []

            def snapshot(self, force=False):
                return {"connected": True, "balance_usdc": "50"}

            def automatic_buy(self, **order):
                self.orders.append(order)
                return {"ok": True, "order_id": "order-1", "status": "MATCHED",
                        "making_amount": "10", "taking_amount": "12.5",
                        "requested_amount": "10", "estimated_price": ".8"}

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "stage4-round-history.jsonl").write_text("")
            account = Account()
            trader = RealTrader(data, account, 50, allocation_fraction=.2)
            slug = "btc-updown-5m-100"
            trader.add_signal({
                "slug": slug, "start": 100, "decision_ms": 280_000,
                "direction": "Up", "p_up": .94, "p_down": .06,
                "fee_rate": .07, "agreement": {"movement": "Up"},
                "components": {"settlement": {"spot_gap_bps": 8}},
                "quotes": {"Up": {"ask": ".80"}},
            })
            trader.consider_quotes(trader.candidates[slug], {
                "received_ms": 281_000,
                "quotes": {"Up": {"ready": True, "ask": ".80",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            self.assertEqual(len(account.orders), 1)
            self.assertEqual(account.orders[0]["amount_usd"], Decimal("10.0"))
            self.assertAlmostEqual(trader.positions[slug]["fill"]["cost"], 10.14)
            self.assertAlmostEqual(trader.positions[slug]["fill"]["shares"], 12.5)
            self.assertIn("real-trading/sessions", str(trader.log_path))
            trader.close()

    def test_real_fill_uses_confirmed_making_amount_when_balance_snapshot_lags(self):
        class Account:
            def snapshot(self, force=False):
                # Polymarket can briefly expose only a partial collateral debit
                # immediately after returning a matched order.
                return {"connected": True, "balance_usdc": "49.99"}

            def automatic_buy(self, **order):
                return {"ok": True, "order_id": "order-1", "status": "matched",
                        "making_amount": "2.539997", "taking_amount": "3.023806"}

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "stage4-round-history.jsonl").write_text("")
            trader = RealTrader(data, Account(), 50, allocation_fraction=.2)
            slug = "btc-updown-5m-100"
            trader.add_signal({
                "slug": slug, "start": 100, "decision_ms": 280_000,
                "direction": "Up", "p_up": .94, "p_down": .06,
                "fee_rate": .07, "agreement": {"movement": "Up"},
                "components": {"settlement": {"spot_gap_bps": 8}},
                "quotes": {"Up": {"ask": ".84"}},
            })
            trader.consider_quotes(trader.candidates[slug], {
                "received_ms": 281_000,
                "quotes": {"Up": {"ready": True, "ask": ".84",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            fill = trader.positions[slug]["fill"]
            expected_fee = 3.023806 * .07 * (2.539997 / 3.023806) * (
                1 - 2.539997 / 3.023806)
            self.assertAlmostEqual(fill["cost"], 2.539997 + expected_fee)
            self.assertAlmostEqual(fill["price"], 2.539997 / 3.023806)
            self.assertAlmostEqual(fill["fee"], expected_fee)
            self.assertAlmostEqual(trader.bankroll, 50 - 2.539997 - expected_fee)
            trader.resolve({"slug": slug, "actual_result": "Down"})
            self.assertAlmostEqual(trader.realized_profit, -2.539997 - expected_fee)
            trader.close()

    def test_real_trader_applies_v4_adverse_move_guard(self):
        class Account:
            def __init__(self): self.orders = []
            def snapshot(self, force=False):
                return {"connected": True, "balance_usdc": "50"}
            def automatic_buy(self, **order):
                self.orders.append(order)
                return {"ok": True, "status": "matched"}

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "stage4-round-history.jsonl").write_text("")
            account = Account()
            trader = RealTrader(data, account, 50, allocation_fraction=.2,
                                model_version="btc_guard_v4")
            slug = "btc-updown-5m-100"
            trader.add_signal({
                "slug": slug, "start": 100, "decision_ms": 280_000,
                "fee_rate": .07, "quotes": {"Up": {"ask": ".80"}},
                "components": {"settlement": {"spot_gap_bps": 8}},
                "model_outputs": {"btc_guard_v4": {
                    "p_up": .94, "p_down": .06, "side": "Up", "chance": .94,
                    "direction": "Up", "reason": "analytic_unvalidated_signal",
                    "agreement": {"market": "Up", "movement": "Up"},
                    "warnings": [], "model_risk_blocked": False}},
            })
            trader.consider_quotes(trader.candidates[slug], {
                "received_ms": 281_000,
                "quotes": {"Up": {"ready": True, "ask": ".60",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            self.assertEqual(account.orders, [])
            self.assertEqual(
                trader.candidates[slug]["rejections"][
                    "adverse_price_move_requires_confirmation"], 1)
            trader.consider_quotes(trader.candidates[slug], {
                "received_ms": 282_000,
                "quotes": {"Up": {"ready": True, "ask": ".66",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            self.assertEqual(len(account.orders), 1)
            self.assertNotIn(slug, trader.candidates)
            trader.close()

    def test_real_warning_size_is_raised_to_one_dollar_minimum(self):
        class Account:
            def __init__(self): self.orders = []
            def snapshot(self, force=False):
                return {"connected": True, "balance_usdc": "10"}
            def automatic_buy(self, **order):
                self.orders.append(order)
                return {"ok": False, "code": "fok_not_filled", "message": "no fill"}

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "stage4-round-history.jsonl").write_text("")
            account = Account()
            trader = RealTrader(data, account, 10, allocation_fraction=.2,
                                thin_gap_allocation_fraction=.25)
            slug = "btc-updown-5m-100"
            trader.add_signal({
                "slug": slug, "start": 100, "decision_ms": 280_000,
                "direction": "Up", "p_up": .94, "p_down": .06,
                "fee_rate": .07, "warnings": ["thin_gap"],
                "agreement": {"movement": "Up"},
                "components": {"settlement": {"spot_gap_bps": 4}},
                "quotes": {"Up": {"ask": ".80"}},
            })
            trader.consider_quotes(trader.candidates[slug], {
                "received_ms": 281_000,
                "quotes": {"Up": {"ready": True, "ask": ".80",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            self.assertEqual(account.orders[0]["amount_usd"], Decimal("1.0"))
            self.assertIn(slug, trader.candidates)
            trader.consider_quotes(trader.candidates[slug], {
                "received_ms": 282_500,
                "quotes": {"Up": {"ready": True, "ask": ".80",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            self.assertEqual(len(account.orders), 2)
            trader.close()

    def test_delayed_real_order_is_reconciled_before_resolution(self):
        class Account:
            def __init__(self): self.confirmed = False
            def snapshot(self, force=False):
                return {"connected": True,
                        "balance_usdc": "40" if self.confirmed else "50"}
            def automatic_buy(self, **order):
                return {"ok": True, "order_id": "delayed-1", "status": "delayed",
                        "token_id": "up-token", "requested_amount": "10",
                        "estimated_price": ".8"}
            def confirm_automatic_order(self, order_id, token_id):
                self.confirmed = True
                return {"status": "matched", "making_amount": "10",
                        "taking_amount": "12.5", "trade_ids": ["trade-1"],
                        "settlement_status": "confirmed"}

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "stage4-round-history.jsonl").write_text("")
            account = Account()
            trader = RealTrader(data, account, 50, allocation_fraction=.2)
            slug = "btc-updown-5m-100"
            trader.add_signal({
                "slug": slug, "start": 100, "decision_ms": 280_000,
                "direction": "Up", "p_up": .94, "p_down": .06,
                "fee_rate": .07, "agreement": {"movement": "Up"},
                "components": {"settlement": {"spot_gap_bps": 8}},
                "quotes": {"Up": {"ask": ".80"}},
            })
            trader.consider_quotes(trader.candidates[slug], {
                "received_ms": 281_000,
                "quotes": {"Up": {"ready": True, "ask": ".80",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            self.assertIn(slug, trader.pending_orders)
            self.assertEqual(trader.trade_count, 0)
            trader.resolve({"slug": slug, "actual_result": "Up"})
            self.assertNotIn(slug, trader.pending_orders)
            self.assertEqual(trader.trade_count, 1)
            self.assertEqual(trader.wins, 1)
            self.assertAlmostEqual(trader.realized_profit, 2.36)
            trader.close()

    def test_paper_resume_restores_an_open_position(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "stage4-round-history.jsonl").write_text("")
            trader = PaperTrader(data, 20, allocation_fraction=.5, run_id="resume-run")
            slug = "btc-updown-5m-100"
            trader.add_signal({
                "slug": slug, "start": 100, "decision_ms": 280_000,
                "direction": "Up", "p_up": .94, "p_down": .06,
                "fee_rate": .07, "agreement": {"movement": "Up"},
                "components": {"settlement": {"spot_gap_bps": 8}},
                "quotes": {"Up": {"ask": ".80"}},
            })
            trader.consider_quotes(trader.candidates[slug], {
                "received_ms": 281_000,
                "quotes": {"Up": {"ready": True, "ask": ".80",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            trader.close()
            resumed = PaperTrader(data, 10, allocation_fraction=.5,
                                  run_id="resume-run", resume=True)
            self.assertIn(slug, resumed.positions)
            resumed.resolve({"slug": slug, "actual_result": "Up"})
            self.assertEqual(resumed.wins, 1)
            self.assertNotIn(slug, resumed.positions)
            resumed.close()


if __name__ == "__main__":
    unittest.main()
