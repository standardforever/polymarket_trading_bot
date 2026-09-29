import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from bot.account import AccountConfiguration, PolymarketAccount
from bot.live import LiveStore


class Item:
    def __init__(self, **values):
        self.values = values

    def model_dump(self, mode="python"):
        return dict(self.values)

    def __getattr__(self, name):
        return self.values.get(name)


class Pages:
    def __init__(self, items):
        self.items = items

    def iter_items(self):
        return iter(self.items)


class FakeClient:
    wallet = "0x1111111111111111111111111111111111111111"
    signer = wallet
    wallet_type = "EOA"

    def __init__(self):
        self.placed = []
        self.position = Item(condition_id="0x" + "2" * 64, token_id="up-token",
                             slug="btc-updown-5m-100", title="BTC Up or Down",
                             outcome="Up", size="10", avg_price="0.6",
                             initial_value="6", current_value="7", cur_price="0.7",
                             cash_pnl="1", percent_pnl=16.6, realized_pnl="0",
                             redeemable=False, end_date=None)

    def get_balance_allowance(self, **kwargs):
        return SimpleNamespace(balance=50_000_000, allowances={"exchange": 50_000_000})

    def list_positions(self, **kwargs):
        return Pages([self.position])

    def list_open_orders(self, **kwargs):
        return Pages([])

    def get_market(self, **kwargs):
        return SimpleNamespace(
            state=SimpleNamespace(closed=False, accepting_orders=True, end_date=None),
            trading=SimpleNamespace(minimum_order_size=Decimal("1"),
                                    minimum_tick_size=Decimal("0.01")))

    def estimate_market_price(self, **kwargs):
        return Decimal("0.8") if kwargs["side"] == "BUY" else Decimal("0.7")

    def place_market_order(self, **kwargs):
        self.placed.append(kwargs)
        return Item(ok=True, order_id="order-1", status="MATCHED",
                    making_amount="8", taking_amount="10", trade_ids=["trade-1"],
                    transactions_hashes=[])


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [150.0]
        self.live = LiveStore()
        self.live.update_market(
            slug="btc-updown-5m-100", received_ms=150_000, remaining_seconds=50,
            quotes={"Up": {"ready": True, "bid": ".7", "ask": ".8"},
                    "Down": {"ready": True, "bid": ".19", "ask": ".2"}},
            tokens={"up-token": "Up", "down-token": "Down"},
            market_end_ms=200_000)
        self.client = FakeClient()
        config = AccountConfiguration("0x" + "1" * 64, self.client.wallet,
                                      "key", "secret", "passphrase")
        self.account = PolymarketAccount(
            Path(self.temp.name), self.live, client_factory=lambda _: self.client,
            clock=lambda: self.now[0], configuration=config)

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_uses_live_bid_for_position_pnl(self):
        snapshot = self.account.snapshot()
        self.assertEqual(snapshot["balance_usdc"], "50")
        self.assertEqual(snapshot["positions"][0]["current_value"], "7.0")
        self.assertEqual(snapshot["positions"][0]["cash_pnl"], "1.0")
        self.assertTrue(snapshot["positions"][0]["tradable"])

    def test_current_sdk_position_field_names_are_normalized(self):
        row = self.account._position(Item(
            condition_id="condition", asset_id="asset", current_size="7",
            current_price="0.6", outcome="Up"))
        self.assertEqual(row["token_id"], "asset")
        self.assertEqual(row["size"], "7")
        self.assertEqual(row["current_price"], "0.6")

    def test_buy_requires_one_time_short_lived_preview(self):
        preview = self.account.preview(action="BUY", slug="btc-updown-5m-100",
                                       outcome="Up", amount_usd=Decimal("8"))
        self.assertEqual(preview["estimated_shares"], "1E+1")
        result = self.account.execute(preview["preview_id"])
        self.assertTrue(result["ok"])
        self.assertEqual(self.client.placed[0]["order_type"], "FOK")
        self.assertEqual(self.client.placed[0]["amount"], "8")
        self.assertNotIn("max_spend", self.client.placed[0])
        with self.assertRaisesRegex(ValueError, "already"):
            self.account.execute(preview["preview_id"])

    def test_expired_preview_cannot_trade(self):
        preview = self.account.preview(action="BUY", slug="btc-updown-5m-100",
                                       outcome="Down", amount_usd=Decimal("5"))
        self.now[0] += 13
        with self.assertRaisesRegex(ValueError, "expired"):
            self.account.execute(preview["preview_id"])
        self.assertEqual(self.client.placed, [])

    def test_one_dollar_buy_is_not_blocked_by_share_minimum(self):
        self.client.get_market = lambda **kwargs: SimpleNamespace(
            state=SimpleNamespace(closed=False, accepting_orders=True, end_date=None),
            trading=SimpleNamespace(minimum_order_size=Decimal("5"),
                                    minimum_tick_size=Decimal("0.01")))
        preview = self.account.preview(action="BUY", slug="btc-updown-5m-100",
                                       outcome="Up", amount_usd=Decimal("1"))
        self.assertEqual(preview["requested"], "1")
        self.assertEqual(preview["estimated_shares"], "1.25")

        self.account.execute(preview["preview_id"])
        self.assertEqual(self.client.placed[-1]["amount"], "1")
        self.assertNotIn("max_spend", self.client.placed[-1])

    def test_buy_below_one_dollar_is_rejected(self):
        with self.assertRaisesRegex(ValueError, r"at least \$1"):
            self.account.preview(action="BUY", slug="btc-updown-5m-100",
                                 outcome="Up", amount_usd=Decimal("0.99"))

    def test_sell_cannot_exceed_live_position(self):
        with self.assertRaisesRegex(ValueError, "exceed"):
            self.account.preview(action="SELL", slug="btc-updown-5m-100",
                                 token_id="up-token", shares=Decimal("11"))

    def test_small_position_can_be_previewed_for_market_sell(self):
        self.client.position.values["size"] = "1.4705"
        preview = self.account.preview(action="SELL", slug="btc-updown-5m-100",
                                       token_id="up-token", shares=Decimal("1.4705"))
        self.assertEqual(preview["requested"], "1.4705")

    def test_activated_automatic_buy_skips_manual_preview(self):
        self.client.get_market = lambda **kwargs: self.fail(
            "automatic orders must not repeat the remote market lookup")
        self.client.estimate_market_price = lambda **kwargs: self.fail(
            "automatic orders must not perform a redundant price estimate")
        result = self.account.automatic_buy(
            run_id="real-run", slug="btc-updown-5m-100", outcome="Up",
            amount_usd=Decimal("8"), maximum_price=Decimal("0.90"))
        self.assertTrue(result["ok"])
        self.assertEqual(self.client.placed[-1]["order_type"], "FOK")
        self.assertEqual(self.client.placed[-1]["max_spend"], "8.00")
        self.assertEqual(self.client.placed[-1]["max_price"], "0.90")
        self.assertIn("total_latency_ms", result)
        audit = (Path(self.temp.name) / "real-trading" /
                 "manual-orders.jsonl").read_text()
        self.assertIn("automatic_order_submitted", audit)
        self.assertIn("automatic_order_result", audit)

    def test_one_dollar_automatic_buy_keeps_full_venue_minimum(self):
        self.account.automatic_buy(
            run_id="real-run", slug="btc-updown-5m-100", outcome="Up",
            amount_usd=Decimal("1"), maximum_price=Decimal("0.90"))
        order = self.client.placed[-1]
        self.assertEqual(order["amount"], "1.00")
        self.assertNotIn("max_spend", order)

    def test_automatic_buy_rechecks_adverse_move_after_balance_lookup(self):
        original_balance = self.client.get_balance_allowance

        def balance_then_move(**kwargs):
            self.live.update_market(
                slug="btc-updown-5m-100", received_ms=150_000,
                remaining_seconds=50,
                quotes={"Up": {"ready": True, "bid": ".59", "ask": ".60"},
                        "Down": {"ready": True, "bid": ".39", "ask": ".40"}},
                tokens={"up-token": "Up", "down-token": "Down"},
                market_end_ms=200_000)
            return original_balance(**kwargs)

        self.client.get_balance_allowance = balance_then_move
        with self.assertRaisesRegex(ValueError, "15 cents below"):
            self.account.automatic_buy(
                run_id="real-run", slug="btc-updown-5m-100", outcome="Up",
                amount_usd=Decimal("1"), maximum_price=Decimal("0.90"),
                signal_ask=Decimal("0.80"),
                maximum_adverse_price_move=Decimal("0.15"))
        self.assertEqual(self.client.placed, [])

    def test_automatic_buy_allows_sdk_allowance_recovery(self):
        self.client.get_balance_allowance = lambda **kwargs: SimpleNamespace(
            balance=50_000_000, allowances={"exchange": 0})

        result = self.account.automatic_buy(
            run_id="real-run", slug="btc-updown-5m-100", outcome="Up",
            amount_usd=Decimal("1"), maximum_price=Decimal("0.90"))

        self.assertTrue(result["ok"])
        self.assertEqual(self.client.placed[-1]["amount"], "1.00")

    def test_delayed_fok_is_confirmed_from_authenticated_trade(self):
        self.client.place_market_order = lambda **kwargs: Item(
            ok=True, order_id="delayed-1", status="DELAYED",
            making_amount="0", taking_amount="0", trade_ids=[],
            transactions_hashes=[])
        trade = Item(id="trade-2", taker_order_id="delayed-1",
                     maker_orders=[], status="CONFIRMED", size="1.25",
                     price="0.8")
        self.client.list_account_trades = lambda **kwargs: Pages([trade])
        result = self.account.automatic_buy(
            run_id="real-run", slug="btc-updown-5m-100", outcome="Up",
            amount_usd=Decimal("1"), maximum_price=Decimal("0.90"))
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["making_amount"], "1.000")
        self.assertEqual(result["taking_amount"], "1.25")
        self.assertTrue(result["delayed_confirmation"])
        self.assertEqual(result["settlement_status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
