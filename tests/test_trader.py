import json
import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path

from bot.trader import (allocation, edge_per_share, maximum_value_price,
                        risk_adjusted_allocation, session_allocation, settle_fill)
from bot.real import RealTrader


class RealTraderTests(unittest.TestCase):
    @staticmethod
    def production_signal(slug="btc-updown-5m-100", warnings=None,
                          movement="Up"):
        return {
            "slug": slug, "start": 100, "decision_ms": 280_000,
            "direction": "Up", "fee_rate": .07,
            "agreement": {"movement": movement, "market": "Up"},
            "warnings": warnings or [],
            "components": {
                "settlement": {"spot_gap_bps": 6, "twap_gap_bps": 6,
                               "remaining_seconds": 120},
                "movement": {"volatility_120": .01, "regime": "steady_up"},
            },
            "quotes": {"Up": {"ask": ".90"}},
        }

    def test_production_gate_shadow_monitors_each_warning_and_caps_price(self):
        class Account:
            @staticmethod
            def snapshot(force=False):
                return {"connected": True, "balance_usdc": "100"}

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "round-history.jsonl").write_text("")
            trader = RealTrader(
                data, Account(), 100, allocation_fraction=.8,
                model_threshold=.98, minimum_strength=2.5,
                minimum_edge=0, maximum_entry_price=.97, strict_risk_rules=True,
                protect_profits=True)
            trader.add_signal(self.production_signal())
            candidate = trader.candidates["btc-updown-5m-100"]
            self.assertGreater(candidate["strength"], 2.5)
            self.assertEqual(candidate["maximum_value_price"], .97)
            trader.add_signal(self.production_signal(
                "btc-updown-5m-400", ["movement_disagreement"], "Down"))
            movement = trader.candidates["btc-updown-5m-400"]
            self.assertEqual(movement["entry_mode"], "recovered")
            self.assertEqual(
                movement["initial_rejection_reason"], "movement_disagreement")
            trader.add_signal(self.production_signal(
                "btc-updown-5m-700", ["thin_gap"]))
            thin = trader.candidates["btc-updown-5m-700"]
            self.assertEqual(thin["entry_mode"], "recovered")
            self.assertEqual(thin["initial_rejection_reason"], "thin_gap")
            trader.close()

    def test_recovered_gate_uses_fresh_side_and_only_three_prediction_rules(self):
        class Account:
            @staticmethod
            def snapshot(force=False):
                return {"connected": True, "balance_usdc": "100"}

        components = {
            "settlement": {"spot_gap_bps": -6, "twap_gap_bps": -7},
            "movement": {},
        }
        signal = {
            "direction": "SKIP", "reason": "analytic_movement_disagreement",
            "movement_disagreement": True,
            "agreement": {"market": "Up"},
            "warnings": ["movement_disagreement", "choppy_up_regime"],
        }
        probability = {
            "side": "Down", "chance": .90, "z_score": -2.5001,
        }
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "round-history.jsonl").write_text("")
            trader = RealTrader(
                data, Account(), 100, minimum_edge=0,
                recovered_model_threshold=.90,
                recovered_minimum_strength=2.5,
                recovered_minimum_gap_bps=5,
                recovery_confirmation_snapshots=1,
                recovery_confirmation_seconds=0)
            candidate = {
                "slug": "btc-updown-5m-100", "side": "Up",
                "entry_mode": "recovered", "fee_rate": .07,
                "market_end_ms": 400_000, "_fresh_history": [],
                "recovery_pass_count": 0,
                "recovery_pass_started_ms": None,
                "recovery_confirmation_side": None,
            }
            record = {"received_ms": 281_000, "quotes": {}}
            with (patch("bot.real.build_components", return_value=components),
                  patch("bot.real.analytic_signal", return_value=signal),
                  patch("bot.real.probability_snapshot", return_value=probability)):
                valid, reason, snapshot = trader._fresh_validation(candidate, record)
            self.assertTrue(valid)
            self.assertEqual(reason, "fresh_validation_passed")
            self.assertEqual(candidate["side"], "Down")
            self.assertEqual(candidate["chance"], .90)
            self.assertFalse(snapshot["thin_gap"])
            self.assertIn("movement_disagreement", candidate["warnings"])
            trader.close()

    def test_recovered_gate_rejects_spot_or_twap_gap_below_five_bps(self):
        class Account:
            @staticmethod
            def snapshot(force=False):
                return {"connected": True, "balance_usdc": "100"}

        components = {
            "settlement": {"spot_gap_bps": 8, "twap_gap_bps": 4.99},
            "movement": {},
        }
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "round-history.jsonl").write_text("")
            trader = RealTrader(data, Account(), 100)
            candidate = {
                "slug": "btc-updown-5m-100", "side": "Up",
                "entry_mode": "recovered", "fee_rate": .07,
                "market_end_ms": 400_000, "_fresh_history": [],
                "recovery_pass_count": 1,
                "recovery_pass_started_ms": 280_000,
                "recovery_confirmation_side": "Up",
            }
            with (patch("bot.real.build_components", return_value=components),
                  patch("bot.real.analytic_signal", return_value={"warnings": []}),
                  patch("bot.real.probability_snapshot", return_value={
                      "side": "Up", "chance": .98, "z_score": 3.0})):
                valid, reason, snapshot = trader._fresh_validation(
                    candidate, {"received_ms": 281_000, "quotes": {}})
            self.assertFalse(valid)
            self.assertEqual(reason, "fresh_thin_gap")
            self.assertTrue(snapshot["thin_gap"])
            self.assertEqual(candidate["recovery_pass_count"], 0)
            trader.close()

    def test_recovered_quote_switches_to_the_fresh_direction(self):
        class Account:
            def __init__(self):
                self.orders = []

            @staticmethod
            def snapshot(force=False):
                return {"connected": True, "balance_usdc": "100",
                        "positions": []}

            def automatic_buy(self, **order):
                self.orders.append(order)
                return {"ok": True, "status": "matched",
                        "making_amount": "5", "taking_amount": "5.555556"}

        import time
        now_ms = time.time_ns() // 1_000_000
        start = now_ms // 1000 - 180
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "round-history.jsonl").write_text("")
            account = Account()
            trader = RealTrader(
                data, account, 100, minimum_edge=0,
                fresh_revalidation=True, recovered_signals_enabled=True,
                profit_trading_balance=10)
            row = self.production_signal(
                f"btc-updown-5m-{start}", ["movement_disagreement"], "Down")
            row.update(start=start, decision_ms=now_ms)
            trader.add_signal(row)
            candidate = trader.candidates[row["slug"]]

            def fresh_direction(current, record):
                current.update(side="Down", chance=.98, strength=3,
                               maximum_value_price=.97,
                               fresh_snapshot={"side": "Down"})
                return True, "fresh_validation_passed", current["fresh_snapshot"]

            trader._fresh_validation = fresh_direction
            trader.consider_quotes(candidate, {
                "received_ms": now_ms,
                "quotes": {"Down": {"ready": True, "ask": ".90",
                                      "ask_size": "100",
                                      "connection_age_ms": 0}},
            })
            self.assertEqual(len(account.orders), 1)
            self.assertEqual(account.orders[0]["outcome"], "Down")
            trader.close()

    def test_profit_wallet_transfer_is_persisted_and_cannot_overspend(self):
        class Account:
            @staticmethod
            def snapshot(force=False):
                return {"connected": True, "balance_usdc": "100"}

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "round-history.jsonl").write_text("")
            trader = RealTrader(
                data, Account(), 100, protect_profits=True,
                profit_trading_balance=4, safe_reserve=1)
            funds = trader.transfer_funds("profit_trading", "safe_reserve", 1.5)
            self.assertEqual(funds["profit_trading"], 2.5)
            self.assertEqual(funds["safe_reserve"], 2.5)
            saved = json.loads((data / "real-trading" / "funds.json").read_text())
            self.assertEqual(saved["profit_trading"], 2.5)
            with self.assertRaisesRegex(ValueError, "fund_transfer_exceeds_balance"):
                trader.transfer_funds("profit_trading", "core_trading", 3)
            trader.close()

    def test_recovered_opportunity_is_logged_when_execution_is_disabled(self):
        class Account:
            @staticmethod
            def snapshot(force=False):
                return {"connected": True, "balance_usdc": "100"}

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "round-history.jsonl").write_text("")
            trader = RealTrader(
                data, Account(), 100, model_threshold=.98,
                minimum_strength=2.5, strict_risk_rules=True,
                shadow_rejected_signals=True,
                recovered_signals_enabled=False)
            slug = "btc-updown-5m-400"
            trader.add_signal(self.production_signal(
                slug, ["movement_disagreement"], "Down"))
            candidate = trader.candidates[slug]
            trader._fresh_validation = lambda candidate, record: (
                True, "fresh_validation_passed",
                {"probability": .98, "strength": 3, "side": "Up",
                 "spot_gap_bps": 8, "twap_gap_bps": 7, "warnings": []})
            trader.consider_quotes(candidate, {
                "received_ms": 281_000,
                "quotes": {"Up": {"ready": True, "ask": ".90",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            self.assertNotIn(slug, trader.candidates)
            self.assertIn(slug, trader.shadow_positions)
            trader.resolve({"slug": slug, "actual_result": "Up"})
            rows = [json.loads(line) for line in
                    trader.recovery_log_path.read_text().splitlines()]
            opportunity = next(row for row in rows
                               if row["event"] == "recovered_opportunity")
            outcome = next(row for row in rows
                           if row["event"] == "recovered_opportunity_resolved")
            self.assertEqual(opportunity["execution_reason"],
                             "profit_balance_unavailable")
            self.assertTrue(outcome["won"])
            self.assertGreater(outcome["hypothetical_profit"], 0)
            main_rows = [json.loads(line) for line in
                         trader.log_path.read_text().splitlines()]
            self.assertFalse(any(str(row.get("event", "")).startswith(
                "recovered_") for row in main_rows))
            trader.close()

    def test_recovered_fill_uses_only_profit_wallet_and_rounds_up_to_minimum(self):
        class Account:
            def __init__(self):
                self.orders = []

            @staticmethod
            def snapshot(force=False):
                return {"connected": True, "balance_usdc": "100",
                        "positions": []}

            def automatic_buy(self, **order):
                self.orders.append(order)
                return {"ok": True, "status": "matched",
                        "making_amount": "1", "taking_amount": str(1 / .9)}

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "round-history.jsonl").write_text("")
            account = Account()
            trader = RealTrader(
                data, account, 100, model_threshold=.98,
                minimum_strength=2.5, strict_risk_rules=True,
                shadow_rejected_signals=True,
                recovered_signals_enabled=True,
                recovered_allocation_fraction=.5,
                profit_trading_balance=1.57)
            slug = "btc-updown-5m-400"
            trader.add_signal(self.production_signal(
                slug, ["movement_disagreement"], "Down"))
            candidate = trader.candidates[slug]
            trader.consider_quotes(candidate, {
                "received_ms": 281_000,
                "quotes": {"Up": {"ready": True, "ask": ".90",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            self.assertEqual(account.orders[0]["amount_usd"], Decimal("1.0"))
            self.assertEqual(trader.bankroll, 100)
            self.assertLess(trader.protected_profit, .57)
            trader.resolve({"slug": slug, "actual_result": "Up"})
            self.assertEqual(trader.bankroll, 100)
            self.assertGreater(trader.protected_profit, 1.67)
            profit_events = [json.loads(line) for line in
                             trader.recovery_log_path.read_text().splitlines()]
            self.assertIn("trade_filled", {row["event"] for row in profit_events})
            self.assertIn("trade_resolved", {row["event"] for row in profit_events})
            main_events = [json.loads(line) for line in
                           trader.log_path.read_text().splitlines()]
            self.assertNotIn("trade_filled", {row["event"] for row in main_events})
            trader.close()

    def test_protected_profit_is_not_reused_for_the_next_trade(self):
        class Account:
            @staticmethod
            def snapshot(force=False):
                return {"connected": True, "balance_usdc": "110", "positions": []}

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "round-history.jsonl").write_text("")
            trader = RealTrader(
                data, Account(), 100, allocation_fraction=.8,
                protect_profits=True)
            slug = "btc-updown-5m-100"
            trader.bankroll = 20
            trader.positions[slug] = {
                "side": "Up",
                "fill": {"price": .8, "shares": 90, "fee": 0, "cost": 80},
            }
            trader.resolve({"slug": slug, "actual_result": "Up"})
            self.assertEqual(trader.bankroll, 100)
            self.assertEqual(trader.protected_profit, 10)
            self.assertEqual(trader._trade_budget(), 80)
            status = json.loads(trader.status_path.read_text())
            self.assertEqual(status["trading_capital"], 100)
            self.assertEqual(status["protected_profit"], 10)
            run_id = trader.run_id
            trader.close()
            resumed = RealTrader(
                data, Account(), 999, allocation_fraction=.8,
                protect_profits=True, run_id=run_id, resume=True)
            self.assertEqual(resumed.bankroll, 100)
            self.assertEqual(resumed.protected_profit, 10)
            self.assertEqual(resumed._trade_budget(), 80)
            resumed.close()

    def test_allocation_rules(self):
        self.assertEqual(allocation(50), 40)
        self.assertEqual(allocation(20), 20)
        self.assertEqual(session_allocation(50, .6), 30)
        self.assertEqual(session_allocation(50, .8, 25), 25)
        self.assertEqual(risk_adjusted_allocation(2.225, .8, None, .25), 1)
        self.assertEqual(risk_adjusted_allocation(2.225, .8, .75, .5), 0)

    def test_value_price_preserves_required_edge(self):
        maximum = maximum_value_price(.94, .07, .03)
        self.assertGreater(maximum, .89)
        self.assertLess(maximum, .92)
        self.assertAlmostEqual(edge_per_share(.94, maximum, .07), .03, places=8)

    def test_settlement_returns_payout_and_full_loss(self):
        fill = {"shares": 10, "cost": 8.1}
        win = settle_fill(fill, "Up", "Up")
        loss = settle_fill(fill, "Up", "Down")
        self.assertAlmostEqual(win["profit"], 1.9)
        self.assertEqual(loss["profit"], -8.1)
        self.assertEqual(loss["payout"], 0)

    def test_official_resolution_corrects_conflicting_account_position(self):
        class Account:
            @staticmethod
            def snapshot(force=False):
                return {"connected": True, "balance_usdc": "10", "positions": []}

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "round-history.jsonl").write_text("")
            trader = RealTrader(data, Account(), 10)
            slug = "btc-updown-5m-100"
            trader.positions[slug] = {
                "slug": slug, "side": "Up",
                "fill": {"price": .55, "shares": 2, "fee": 0, "cost": 1.1},
            }
            trader.resolve({"slug": slug, "actual_result": "Up",
                            "resolution_source": "account_position"})
            self.assertEqual((trader.wins, trader.losses), (1, 0))
            self.assertAlmostEqual(trader.realized_profit, .9)

            trader.resolve({"slug": slug, "actual_result": "Down",
                            "resolution_source": "collector",
                            "opening_price": "100", "final_price": "99"})
            self.assertEqual((trader.wins, trader.losses), (0, 1))
            self.assertAlmostEqual(trader.realized_profit, -1.1)
            rows = [json.loads(line) for line in trader.log_path.read_text().splitlines()]
            corrected = [row for row in rows if row.get("official_correction")]
            self.assertEqual(corrected[-1]["actual"], "Down")
            self.assertFalse(corrected[-1]["won"])
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
            (data / "round-history.jsonl").write_text("")
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
                "quotes": {"Up": {"ready": True, "ask": ".81",
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
            (data / "round-history.jsonl").write_text("")
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

    def test_real_trader_waits_below_floor_and_buys_after_recovery(self):
        class Account:
            def __init__(self): self.orders = []
            def snapshot(self, force=False):
                return {"connected": True, "balance_usdc": "50"}
            def automatic_buy(self, **order):
                self.orders.append(order)
                return {"ok": True, "status": "matched"}

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "round-history.jsonl").write_text("")
            account = Account()
            trader = RealTrader(data, account, 50, allocation_fraction=.2)
            slug = "btc-updown-5m-100"
            trader.add_signal({
                "slug": slug, "start": 100, "decision_ms": 280_000,
                "fee_rate": .07, "quotes": {"Up": {"ask": ".90"}},
                "components": {"settlement": {"spot_gap_bps": 8}},
                "p_up": .94, "p_down": .06, "direction": "Up",
                "reason": "analytic_unvalidated_signal",
                "agreement": {"market": "Up", "movement": "Up"},
                "warnings": [], "risk_blocked": False,
            })
            trader.consider_quotes(trader.candidates[slug], {
                "received_ms": 281_000,
                "quotes": {"Up": {"ready": True, "ask": ".79",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            self.assertEqual(account.orders, [])
            self.assertEqual(
                trader.candidates[slug]["rejections"][
                    "price_below_entry_floor"], 1)
            trader.consider_quotes(trader.candidates[slug], {
                "received_ms": 282_000,
                "quotes": {"Up": {"ready": True, "ask": ".81",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            self.assertEqual(len(account.orders), 1)
            self.assertNotIn(slug, trader.candidates)
            trader.close()

    def test_real_trader_accepts_lower_price_when_current_rules_pass(self):
        class Account:
            def __init__(self): self.orders = []
            def snapshot(self, force=False):
                return {"connected": True, "balance_usdc": "50"}
            def automatic_buy(self, **order):
                self.orders.append(order)
                return {"ok": True, "status": "matched"}

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "round-history.jsonl").write_text("")
            account = Account()
            trader = RealTrader(data, account, 50, allocation_fraction=.2)
            slug = "btc-updown-5m-100"
            trader.add_signal({
                "slug": slug, "start": 100, "decision_ms": 280_000,
                "fee_rate": .07, "quotes": {"Up": {"ask": ".95"}},
                "components": {"settlement": {"spot_gap_bps": 8}},
                "p_up": .98, "p_down": .02, "direction": "Up",
                "agreement": {"market": "Up", "movement": "Up"},
                "warnings": [], "risk_blocked": False,
            })
            trader.consider_quotes(trader.candidates[slug], {
                "received_ms": 281_000,
                "quotes": {"Up": {"ready": True, "ask": ".84",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            self.assertEqual(len(account.orders), 1)
            self.assertIsNone(account.orders[0]["maximum_adverse_price_move"])
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
            (data / "round-history.jsonl").write_text("")
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
                "quotes": {"Up": {"ask": ".81"}},
            })
            trader.consider_quotes(trader.candidates[slug], {
                "received_ms": 281_000,
                "quotes": {"Up": {"ready": True, "ask": ".81",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            self.assertEqual(account.orders[0]["amount_usd"], Decimal("1.0"))
            self.assertIn(slug, trader.candidates)
            trader.consider_quotes(trader.candidates[slug], {
                "received_ms": 282_500,
                "quotes": {"Up": {"ready": True, "ask": ".81",
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
            (data / "round-history.jsonl").write_text("")
            account = Account()
            trader = RealTrader(data, account, 50, allocation_fraction=.2)
            slug = "btc-updown-5m-100"
            trader.add_signal({
                "slug": slug, "start": 100, "decision_ms": 280_000,
                "direction": "Up", "p_up": .94, "p_down": .06,
                "fee_rate": .07, "agreement": {"movement": "Up"},
                "components": {"settlement": {"spot_gap_bps": 8}},
                "quotes": {"Up": {"ask": ".81"}},
            })
            trader.consider_quotes(trader.candidates[slug], {
                "received_ms": 281_000,
                "quotes": {"Up": {"ready": True, "ask": ".81",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            self.assertIn(slug, trader.pending_orders)
            self.assertEqual(trader.trade_count, 0)
            trader.resolve({"slug": slug, "actual_result": "Up"})
            self.assertNotIn(slug, trader.pending_orders)
            self.assertEqual(trader.trade_count, 1)
            self.assertEqual(trader.wins, 1)
            self.assertAlmostEqual(trader.realized_profit, 2.36)
            self.assertEqual(trader.bankroll, 40)
            trader.close()

    def test_real_position_outcome_requires_market_end_and_final_price(self):
        up = {"outcome": "Up", "current_price": "1", "redeemable": True}
        down = {"outcome": "Up", "current_price": "0", "redeemable": False}
        slug = "btc-updown-5m-100"
        self.assertIsNone(RealTrader._position_outcome(slug, up, 399))
        self.assertEqual(RealTrader._position_outcome(slug, up, 400), "Up")
        self.assertEqual(RealTrader._position_outcome(slug, down, 400), "Down")
        self.assertIsNone(RealTrader._position_outcome(
            slug, {"outcome": "Up", "current_price": ".99"}, 400))

    def test_real_trader_syncs_spendable_cash_from_account(self):
        class Account:
            @staticmethod
            def snapshot(force=False):
                return {"connected": True, "balance_usdc": "12.34", "positions": []}

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "round-history.jsonl").write_text("")
            trader = RealTrader(data, Account(), 50)
            trader._resolve_from_account()
            self.assertEqual(trader.bankroll, 12.34)
            trader.close()



if __name__ == "__main__":
    unittest.main()
