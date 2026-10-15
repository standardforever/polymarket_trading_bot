import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from bot.api.routes import create_app
from bot.live import LiveStore
from bot.sessions import TradingSessionManager


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name)
        (self.data / "live-status.json").write_text(json.dumps({
            "updated_ms": 1, "status": "WAITING", "slug": "btc-updown-5m-100"}))
        rows = [
            {"record_type": "decision", "slug": "btc-updown-5m-100", "start": 100,
             "decision_ms": 280_000, "direction": "Up", "p_up": .94, "p_down": .06,
             "reason": "analytic_unvalidated_signal", "warnings": ["movement_disagreement"],
             "estimated_edge": .08, "agreement": {"settlement": "Up", "movement": "Down"},
             "components": {"market": {"p_up": .885}},
             "quotes": {"Up": {"ask": ".89"}, "Down": {"ask": ".12"}}},
            {"record_type": "resolution", "slug": "btc-updown-5m-100", "start": 100,
             "actual_result": "Up", "opening_price": "10", "final_price": "11"},
            {"record_type": "decision", "slug": "btc-updown-5m-400", "start": 400,
             "decision_ms": 580_000, "direction": "SKIP", "p_up": .6, "p_down": .4,
             "reason": "analytic_probability_below_90pct", "agreement": {}},
        ]
        (self.data / "round-history.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows))
        real = self.data / "real-trading" / "sessions"
        real.mkdir(parents=True)
        (real / "latest-status.json").write_text(json.dumps({"status": "WAITING_FOR_SIGNAL"}))
        events = [
            {"event": "trade_filled", "run_id": "run", "slug": "btc-updown-5m-100",
             "side": "Up", "forecast_probability": .94, "entry_ms": 281_000,
             "price": .8, "shares": 10, "fee": .1, "trade_cost": 8.1},
            {"event": "trade_resolved", "run_id": "run", "slug": "btc-updown-5m-100",
             "recorded_ms": 300_000, "won": False, "actual": "Down",
             "profit": -8.1, "payout": 0,
             "resolution_source": "account_position"},
        ]
        (real / "real-run.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
        self.client = TestClient(create_app(self.data))

    def tearDown(self):
        self.temporary.cleanup()

    def test_dashboard_round_filters_and_trade_statistics(self):
        dashboard = self.client.get("/api/v1/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotIn("models", dashboard.json())
        self.assertNotIn("default_model", dashboard.json())
        self.assertEqual(self.client.get("/api/v1/models").status_code, 404)
        self.assertAlmostEqual(dashboard.json()["statistics"]["realized_profit"], 1.9)
        wins = self.client.get("/api/v1/rounds", params={"result": "WIN"}).json()
        self.assertEqual(wins["total"], 1)
        self.assertEqual(wins["items"][0]["warnings"], ["movement_disagreement"])
        self.assertEqual(wins["items"][0]["market_p_up"], .885)
        self.assertEqual(wins["items"][0]["market_up_ask"], ".89")
        self.assertEqual(wins["items"][0]["market_down_ask"], ".12")
        combined = self.client.get("/api/v1/rounds", params={
            "result": "WIN", "prediction": "Up", "traded": True,
        }).json()
        self.assertEqual(combined["total"], 1)
        excluded = self.client.get("/api/v1/rounds", params={
            "result": "WIN", "prediction": "Down", "traded": True,
        }).json()
        self.assertEqual(excluded["total"], 0)

    def test_real_trade_history_repairs_lagging_balance_delta_from_exchange_fill(self):
        folder = self.data / "real-trading" / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        run_id = "real-run"
        (folder / f"real-{run_id}-session.json").write_text(json.dumps({
            "session_id": run_id, "created_ms": 100, "name": "Real run",
            "configuration": {"starting_bankroll": 12.003471},
        }))
        events = [
            {"event": "trade_filled", "run_id": run_id,
             "slug": "btc-updown-5m-100", "side": "Down", "entry_ms": 200,
             "price": .84, "shares": 3.023806, "fee": 0,
             "trade_cost": .01401, "fee_rate": .07,
             "exchange_response": {"making_amount": "2.539997"}},
            {"event": "trade_resolved", "run_id": run_id,
             "slug": "btc-updown-5m-100", "recorded_ms": 300,
             "won": False, "actual": "Up", "payout": 0,
             "trade_cost": .01401, "profit": -.01401},
        ]
        (folder / f"real-{run_id}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in events))
        (folder / f"real-{run_id}-status.json").write_text(json.dumps({
            "run_id": run_id, "status": "WAITING_FOR_SIGNAL",
            "available_bankroll": 11.989461, "realized_profit": -.01401,
        }))

        session = self.client.get(
            f"/api/v1/trading/sessions/{run_id}").json()
        fee = 3.023806 * .07 * (2.539997 / 3.023806) * (
            1 - 2.539997 / 3.023806)
        corrected_cost = 2.539997 + fee
        self.assertAlmostEqual(session["trades"][0]["cost"], corrected_cost)
        self.assertAlmostEqual(session["trades"][0]["profit"], -corrected_cost)
        self.assertAlmostEqual(session["realized_profit"], -corrected_cost)
        self.assertAlmostEqual(
            session["status_detail"]["available_bankroll"],
            12.003471 - corrected_cost)
        real_rounds = self.client.get("/api/v1/rounds", params={
            "traded": True,
        }).json()
        self.assertEqual(real_rounds["total"], 1)
        skipped = self.client.get("/api/v1/rounds", params={"result": "SKIP"}).json()
        self.assertEqual(skipped["total"], 1)
        page = self.client.get("/api/v1/rounds", params={
            "limit": 1, "offset": 1,
        }).json()
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["offset"], 1)
        self.assertEqual(len(page["items"]), 1)
        trades = self.client.get("/api/v1/trades", params={"status": "WIN"}).json()
        self.assertAlmostEqual(trades["items"][0]["profit"], 1.9)

    def test_trading_session_list_is_paginated_newest_first(self):
        real = self.data / "real-trading" / "sessions"
        for index in range(3):
            session_id = f"session-{index}"
            (real / f"real-{session_id}-session.json").write_text(json.dumps({
                "session_id": session_id, "created_ms": 1000 + index,
                "name": f"Session {index}",
                "configuration": {"starting_bankroll": 10},
            }))
            (real / f"real-{session_id}-status.json").write_text(json.dumps({
                "status": "STOPPED", "available_bankroll": 10,
            }))
            (real / f"real-{session_id}.jsonl").write_text("")

        first = self.client.get("/api/v1/trading/sessions", params={
            "limit": 2, "offset": 0,
        }).json()
        second = self.client.get("/api/v1/trading/sessions", params={
            "limit": 2, "offset": 2,
        }).json()

        self.assertEqual(first["total"], 3)
        self.assertEqual(first["limit"], 2)
        self.assertEqual([row["name"] for row in first["items"]],
                         ["Session 2", "Session 1"])
        self.assertEqual(second["items"][0]["name"], "Session 0")

    def test_round_detail_preserves_full_prediction_evidence(self):
        response = self.client.get("/api/v1/rounds/btc-updown-5m-100")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["decision"]["p_up"], .94)
        self.assertEqual(len(response.json()["trades"]), 1)




    def test_real_session_uses_connected_account_balance(self):
        class Account:
            configuration = SimpleNamespace(complete=True)

            @staticmethod
            def snapshot(force=False):
                return {"connected": True, "allowance_ready": True,
                        "balance_usdc": "37.50", "positions": [],
                        "open_orders": []}

        account = Account()
        manager = TradingSessionManager(self.data, account=account)
        client = TestClient(create_app(self.data, sessions=manager,
                                       account=account))
        response = client.post("/api/v1/trading/sessions", json={
            "allocation_percent": 20, "maximum_trade_usd": 5,
            "max_loss_usd": 10,
        })
        self.assertEqual(response.status_code, 201)
        session = response.json()
        self.assertNotIn("mode", session)
        self.assertEqual(session["configuration"]["starting_bankroll"], 37.5)
        self.assertEqual(session["configuration"]["planned_first_trade_budget"], 5)
        detail = client.get(
            f"/api/v1/trading/sessions/{session['session_id']}").json()
        self.assertEqual(detail["status_detail"]["available_bankroll"], 37.5)
        self.assertEqual(detail["status_detail"]["balance_source"], "account_api")
        client.post(f"/api/v1/trading/sessions/{session['session_id']}/stop")
        manager.shutdown()

    def test_real_session_relies_on_sdk_allowance_recovery(self):
        class Account:
            configuration = SimpleNamespace(complete=True)

            @staticmethod
            def snapshot(force=False):
                return {"connected": True, "allowance_ready": False,
                        "balance_usdc": "20", "positions": [], "open_orders": []}

        account = Account()
        manager = TradingSessionManager(self.data, account=account)
        client = TestClient(create_app(self.data, sessions=manager,
                                       account=account))
        response = client.post("/api/v1/trading/sessions", json={
            "allocation_percent": 20, "maximum_trade_usd": 4,
        })
        self.assertEqual(response.status_code, 201)
        client.post(f"/api/v1/trading/sessions/{response.json()['session_id']}/stop")
        manager.shutdown()

    def test_real_session_allows_dynamic_one_dollar_floor(self):
        class Account:
            configuration = SimpleNamespace(complete=True)

            @staticmethod
            def snapshot(force=False):
                return {"connected": True, "allowance_ready": True,
                        "balance_usdc": "2", "positions": [], "open_orders": []}

        account = Account()
        manager = TradingSessionManager(self.data, account=account)
        client = TestClient(create_app(self.data, sessions=manager,
                                       account=account))
        response = client.post("/api/v1/trading/sessions", json={
            "allocation_percent": 20,
        })
        self.assertEqual(response.status_code, 201)
        client.post(f"/api/v1/trading/sessions/{response.json()['session_id']}/stop")
        manager.shutdown()

    def test_active_real_session_resumes_after_runtime_restart(self):
        real = self.data / "real-trading" / "sessions"
        run_id = "restartable"
        configuration = {
            "starting_bankroll": 25,
            "allocation_fraction": .2,
            "maximum_trade_usd": 5,
            "model_threshold": .9,
            "minimum_edge": .03,
            "entry_cutoff_seconds": 30,
            "max_trades": None,
            "target_profit_usd": None,
            "max_loss_usd": None,
            "thin_gap_allocation_fraction": .25,
            "movement_disagreement_allocation_fraction": .5,
        }
        (real / f"real-{run_id}-session.json").write_text(json.dumps({
            "session_id": run_id, "created_ms": 100,
            "name": "Restartable session", "configuration": configuration,
        }))
        (real / f"real-{run_id}.jsonl").write_text(json.dumps({
            "event": "run_started", "run_id": run_id, "recorded_ms": 100,
        }) + "\n")
        (real / f"real-{run_id}-status.json").write_text(json.dumps({
            "run_id": run_id, "status": "WAITING_FOR_SIGNAL",
            "updated_ms": 100, "starting_bankroll": 25,
            "available_bankroll": 25, "realized_profit": 0,
            "trades": 0, "wins": 0, "losses": 0,
            "open_positions": 0, "accepting_entries": True,
        }))

        class Account:
            @staticmethod
            def snapshot(force=False):
                return {"connected": True, "balance_usdc": "25",
                        "positions": [], "open_orders": []}

        first = TradingSessionManager(self.data, account=Account())
        self.assertTrue(first.is_active())
        self.assertEqual(first.trader.run_id, run_id)
        first.shutdown()
        suspended = json.loads(
            (real / f"real-{run_id}-status.json").read_text())
        self.assertEqual(suspended["status"], "RESTARTING")
        self.assertTrue(suspended["accepting_entries"])

        second = TradingSessionManager(self.data, account=Account())
        self.assertTrue(second.is_active())
        self.assertEqual(second.trader.run_id, run_id)
        events = [json.loads(line) for line in
                  (real / f"real-{run_id}.jsonl").read_text().splitlines()]
        self.assertGreaterEqual(sum(
            event.get("event") == "run_resumed" for event in events), 2)
        second.shutdown()

    def test_interrupted_open_trade_is_reconciled_from_official_history(self):
        real = self.data / "real-trading" / "sessions"
        run_id = "orphan"
        (real / f"real-{run_id}.jsonl").write_text(json.dumps({
            "event": "trade_filled", "run_id": run_id,
            "slug": "btc-updown-5m-100", "side": "Up", "price": .8,
            "shares": 10, "fee": .1, "trade_cost": 8.1,
        }) + "\n")
        (real / f"real-{run_id}-status.json").write_text(json.dumps({
            "run_id": run_id, "status": "POSITION_OPEN",
            "starting_bankroll": 50, "available_bankroll": 41.9,
            "realized_profit": 0, "trades": 1, "wins": 0, "losses": 0,
            "open_positions": 1, "accepting_entries": True,
        }))
        class Account:
            @staticmethod
            def snapshot(force=False):
                return {"connected": False}

        manager = TradingSessionManager(self.data, account=Account())
        status = json.loads((real / f"real-{run_id}-status.json").read_text())
        self.assertEqual(status["status"], "STOPPED")
        self.assertEqual(status["open_positions"], 0)
        self.assertEqual(status["wins"], 1)
        # Real winnings remain in the position until redeemed and therefore do
        # not appear in the available USDC balance during reconciliation.
        self.assertAlmostEqual(status["available_bankroll"], 41.9)
        rows = [json.loads(line) for line in
                (real / f"real-{run_id}.jsonl").read_text().splitlines()]
        resolution = next(row for row in rows if row["event"] == "trade_resolved")
        self.assertTrue(resolution["reconciled_after_restart"])
        self.assertEqual(resolution["actual"], "Up")
        manager.shutdown()

    def test_live_socket_emits_snapshot(self):
        with self.client.websocket_connect("/api/v1/ws/live") as socket:
            payload = socket.receive_json()
        self.assertEqual(payload["prediction"]["slug"], "btc-updown-5m-100")

    def test_chart_socket_emits_chart_shape(self):
        active = self.data / "active-rounds"
        active.mkdir()
        (active / "session-100.jsonl").write_text(json.dumps({
            "kind": "quotes", "slug": "btc-updown-5m-100", "received_ms": 250_000,
            "remaining": 150,
            "quotes": {"Up": {"bid": ".62", "ask": ".63"},
                       "Down": {"bid": ".37", "ask": ".38"}}}) + "\n")
        with self.client.websocket_connect("/api/v1/ws/chart") as socket:
            payload = socket.receive_json()
        self.assertEqual(payload["slug"], "btc-updown-5m-100")
        self.assertEqual(payload["market"][-1]["up"]["ask"], ".63")
        self.assertEqual(payload["market"][-1]["down"]["ask"], ".38")

    def test_quote_socket_emits_direct_collector_update(self):
        live = LiveStore()
        live.update_market(
            slug="btc-updown-5m-100", received_ms=250_123,
            remaining_seconds=149.877,
            quotes={"Up": {"bid": ".61", "ask": ".62"},
                    "Down": {"bid": ".38", "ask": ".39"}})
        client = TestClient(create_app(self.data, live))
        with client.websocket_connect("/api/v1/ws/quotes") as socket:
            payload = socket.receive_json()
        self.assertEqual(payload["revision"], 1)
        self.assertEqual(payload["received_ms"], 250_123)
        self.assertEqual(payload["quotes"]["Up"]["ask"], ".62")

    def test_reference_socket_emits_exact_open_and_live_sources(self):
        live = LiveStore()
        live.update_reference_context(
            slug="btc-updown-5m-100", opening_price="84843.280699183213838336",
            opening_source="chainlink_boundary")
        live.update_price({"source": "pyth_btc_usd", "value": "84857.7",
                           "source_ms": 250_000}, 250_012)
        live.update_price({"source": "chainlink_twap_60s", "value": "84900.817769787130118144",
                           "source_ms": 249_000}, 250_015)
        client = TestClient(create_app(self.data, live))
        with client.websocket_connect("/api/v1/ws/reference") as socket:
            payload = socket.receive_json()
        self.assertEqual(payload["opening_price"], "84843.280699183213838336")
        self.assertEqual(payload["opening_source"], "chainlink_boundary")
        self.assertEqual(payload["prices"]["pyth_btc_usd"]["value"], "84857.7")
        self.assertEqual(payload["prices"]["chainlink_twap_60s"]["value"],
                         "84900.817769787130118144")
        self.assertEqual(payload["current_price"], "84900.817769787130118144")
        self.assertEqual(payload["current_source"], "chainlink_twap_60s")
        self.assertEqual(payload["margin"], "57.537070603916279808")

    def test_manual_order_api_requires_preview_then_executes(self):
        class Account:
            configuration = SimpleNamespace(complete=True)

            def snapshot(self):
                return {"configured": True, "connected": True, "positions": []}

            def preview(self, **values):
                self.preview_values = values
                return {"preview_id": "a" * 32, "action": values["action"]}

            def execute(self, preview_id):
                return {"ok": True, "order_id": "order-1", "status": "MATCHED"}

        account = Account()
        client = TestClient(create_app(self.data, account=account))
        preview = client.post("/api/v1/account/orders/preview", json={
            "action": "BUY", "slug": "btc-updown-5m-100",
            "outcome": "Up", "amount_usd": 5,
        })
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(account.preview_values["amount_usd"], 5)
        placed = client.post("/api/v1/account/orders", json={
            "preview_id": "a" * 32,
        })
        self.assertEqual(placed.status_code, 200)
        self.assertEqual(placed.json()["status"], "MATCHED")


if __name__ == "__main__":
    unittest.main()
