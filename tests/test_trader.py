import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from bot.trader import (allocation, edge_per_share, maximum_value_price,
                        risk_adjusted_allocation, session_allocation, settle_fill)
from bot.real import RealTrader


class RealTraderTests(unittest.TestCase):
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

    def test_real_trader_waits_after_ten_cent_adverse_move(self):
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
            self.assertEqual(account.orders, [])
            self.assertEqual(trader.candidates[slug]["rejections"][
                "adverse_price_move_requires_confirmation"], 1)
            trader.consider_quotes(trader.candidates[slug], {
                "received_ms": 282_000,
                "quotes": {"Up": {"ready": True, "ask": ".85",
                                    "ask_size": "100", "connection_age_ms": 0}},
            })
            self.assertEqual(len(account.orders), 1)
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
