"""Read API views over the collector and trading append-only files."""

import json
import time
from collections import deque
from pathlib import Path


TERMINAL_STATUSES = {"STOPPED", "COMPLETED", "GAME_OVER"}

def _clean_event(row):
    if not isinstance(row, dict):
        return row
    cleaned = dict(row)
    cleaned.pop("model_version", None)
    cleaned.pop("execution_mode", None)
    return cleaned


def _json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _jsonl(path: Path):
    if not path.exists():
        return
    try:
        with path.open() as source:
            for line in source:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


class BotRepository:
    def __init__(self, data: Path, account=None):
        self.data = data
        self.account = account
        self.history_path = data / "round-history.jsonl"
        self.real_folder = data / "real-trading" / "sessions"

    def prediction_status(self):
        return _json(self.data / "live-status.json")

    def trading_status(self):
        real = _json(self.real_folder / "latest-status.json")
        if real and real.get("run_id"):
            session = next((row for row in self._sessions()
                if row.get("session_id") == real["run_id"]), None)
            if session:
                real = session["status_detail"]
        return real

    def capabilities(self):
        return {
            "asset": "BTC",
            "market_duration_seconds": 300,
            "collector_hot_reload": False,
            "live_trading": False,
            "manual_orders": False,
            "live_websocket": True,
        }

    def health(self):
        now_ms = time.time_ns() // 1_000_000
        prediction = self.prediction_status()
        age = now_ms - prediction["updated_ms"] if prediction and prediction.get("updated_ms") else None
        return {
            "status": "ok" if age is not None and age <= 15_000 else "degraded",
            "server_time_ms": now_ms,
            "collector": {"available": prediction is not None, "age_ms": age,
                          "fresh": age is not None and age <= 15_000},
            "trader": {"available": self.trading_status() is not None},
        }

    def _round_map(self):
        rounds = {}
        for row in _jsonl(self.history_path) or ():
            slug = row.get("slug")
            kind = row.get("record_type")
            if not slug or kind not in ("decision", "entry", "resolution"):
                continue
            item = rounds.setdefault(slug, {"slug": slug, "entries": {}})
            if kind == "entry":
                item["entries"][str(row.get("delay_seconds"))] = row
            else:
                item[kind] = row
        return rounds

    def _trade_map(self):
        trades = {}
        paths = sorted(self.real_folder.glob("real-*.jsonl"), key=str)
        for path in paths:
            for row in _jsonl(path) or ():
                if row.get("event") not in ("trade_filled", "trade_resolved"):
                    continue
                if (row.get("entry_mode") == "recovered"
                        or row.get("capital_pool") == "profit_trading"):
                    continue
                key = (row.get("run_id"), row.get("slug"))
                item = trades.setdefault(key, {"run_id": key[0], "slug": key[1]})
                item["fill" if row["event"] == "trade_filled" else "resolution"] = row
        return trades

    def trades(self):
        rows = []
        round_map = self._round_map()
        for item in self._trade_map().values():
            fill = dict(item.get("fill", {}))
            resolution = dict(item.get("resolution", {}))
            if not fill:
                continue
            # A matched BUY's making_amount is the confirmed USDC principal.
            # Immediate collateral snapshots can lag and old real-session rows
            # therefore sometimes contain an incorrect balance-delta cost.
            making_amount = (fill.get("exchange_response") or {}).get("making_amount")
            if making_amount is not None:
                principal = float(making_amount)
                shares = float(fill.get("shares") or 0)
                price = principal / shares if shares > 0 else float(fill.get("price") or 0)
                decision = (round_map.get(item.get("slug")) or {}).get("decision") or {}
                fee_rate = float(fill.get("fee_rate") if fill.get("fee_rate") is not None
                                 else decision.get("fee_rate") or 0)
                fee = shares * fee_rate * price * (1 - price)
                cost = principal + fee
                fill["trade_cost"] = cost
                fill["fee"] = fee
                if shares > 0:
                    fill["price"] = price
                if resolution:
                    resolution["trade_cost"] = cost
                    resolution["fee"] = fee
                    resolution["profit"] = float(resolution.get("payout") or 0) - cost
            # The collector's settled winner is authoritative. Account position
            # marks can briefly report 0/1 or redeemable before the final BTC
            # five-minute result is available, so override any conflicting
            # provisional session result in every API view.
            official = (round_map.get(item.get("slug")) or {}).get("resolution") or {}
            official_actual = official.get("actual_result")
            if official_actual in ("Up", "Down"):
                shares = float(fill.get("shares") or 0)
                cost = float(fill.get("trade_cost") or 0)
                won = fill.get("side") == official_actual
                payout = shares if won else 0.0
                resolution.update(
                    actual=official_actual, won=won, payout=payout,
                    profit=payout - cost,
                    recorded_ms=official.get("recorded_ms"),
                    resolution_source="official_round_history",
                )
            status = ("WIN" if resolution.get("won") is True else
                      "LOSS" if resolution.get("won") is False else "OPEN")
            rows.append({
                "run_id": item["run_id"], "slug": item["slug"], "status": status,
                "side": fill.get("side"), "forecast_probability": fill.get("forecast_probability"),
                "entry_ms": fill.get("entry_ms"), "entry_price": fill.get("price"),
                "shares": fill.get("shares"), "fee": fill.get("fee"),
                "cost": fill.get("trade_cost"), "maximum_value_price": fill.get("maximum_value_price"),
                "entry_mode": fill.get("entry_mode", "core"),
                "capital_pool": fill.get("capital_pool", "core_trading"),
                "initial_rejection_reason": fill.get("initial_rejection_reason"),
                "actual": resolution.get("actual"), "profit": resolution.get("profit"),
                "payout": resolution.get("payout"), "resolved_ms": resolution.get("recorded_ms"),
            })
        return sorted(rows, key=lambda row: row.get("entry_ms") or 0, reverse=True)

    def _sessions(self):
        sessions = []
        folder, prefix = self.real_folder, "real"
        if not folder.exists():
            return sessions
        trades = self.trades()
        metadata_by_id = {}
        for path in folder.glob(f"{prefix}-*-session.json"):
            metadata = _json(path)
            if not metadata or not metadata.get("session_id"):
                continue
            metadata_by_id[metadata["session_id"]] = metadata
        live_cash = None
        if self.account is not None:
            snapshot = self.account.snapshot()
            if snapshot.get("connected") and snapshot.get("balance_usdc") is not None:
                live_cash = float(snapshot["balance_usdc"])
        for metadata in metadata_by_id.values():
            metadata = dict(metadata)
            metadata.pop("mode", None)
            metadata["configuration"] = dict(metadata.get("configuration") or {})
            metadata["configuration"].pop("model_version", None)
            session_id = metadata["session_id"]
            status = _clean_event(
                _json(folder / f"{prefix}-{session_id}-status.json") or {})
            session_trades = [row for row in trades if row.get("run_id") == session_id]
            realized_profit = sum(float(row.get("profit") or 0)
                                  for row in session_trades)
            starting = float((metadata.get("configuration") or {}).get(
                "starting_bankroll") or 0)
            open_cost = sum(float(row.get("cost") or 0) for row in session_trades
                            if row.get("status") == "OPEN")
            status = dict(status)
            reconstructed_cash = max(0.0, starting + realized_profit - open_cost)
            if metadata["configuration"].get("protect_profits"):
                protected = sum(max(float(row.get("profit") or 0), 0)
                                for row in session_trades if row.get("status") != "OPEN")
                lost = sum(float(row.get("cost") or 0) for row in session_trades
                           if row.get("status") == "LOSS")
                reconstructed_capital = max(0.0, starting - lost - open_cost)
                status["trading_capital"] = float(status.get(
                    "trading_capital", reconstructed_capital))
                status["protected_profit"] = float(status.get(
                    "protected_profit", protected))
                status["available_bankroll"] = status["trading_capital"]
                status["balance_source"] = "protected_session_ledger"
                if live_cash is not None and status.get("status") not in TERMINAL_STATUSES:
                    status["account_balance"] = live_cash
            elif live_cash is not None and status.get("status") not in TERMINAL_STATUSES:
                status["available_bankroll"] = live_cash
                status["account_balance"] = live_cash
                status["balance_source"] = "account_api"
            else:
                status["available_bankroll"] = reconstructed_cash
                status["balance_source"] = "session_ledger"
            status["realized_profit"] = realized_profit
            sessions.append({
                **metadata,
                "status": status.get("status", "UNKNOWN"),
                "status_detail": status,
                "trade_count": len(session_trades),
                "wins": sum(row["status"] == "WIN" for row in session_trades),
                "losses": sum(row["status"] == "LOSS" for row in session_trades),
                "open_positions": sum(row["status"] == "OPEN" for row in session_trades),
                "realized_profit": realized_profit,
            })
        return sorted(sessions, key=lambda row: row.get("created_ms") or 0, reverse=True)

    def trading_sessions(self):
        return self._sessions()

    def trading_session(self, session_id):
        session = next((row for row in self.trading_sessions()
                        if row["session_id"] == session_id), None)
        if session is None:
            return None
        session["trades"] = [row for row in self.trades()
                             if row.get("run_id") == session_id]
        session["events"] = [
            _clean_event(row)
            for row in (_jsonl(self.real_folder / f"real-{session_id}.jsonl") or ())
            if not (str(row.get("event") or "").startswith("recovered_")
                    or row.get("event") == "fund_transfer"
                    or row.get("entry_mode") == "recovered"
                    or row.get("capital_pool") == "profit_trading")
        ]
        return session

    def rounds(self):
        trade_rows = self.trades()
        trades_by_slug = {}
        for trade in trade_rows:
            trades_by_slug.setdefault(trade["slug"], []).append(trade)
        traded = set(trades_by_slug)
        rows = []
        for item in self._round_map().values():
            decision = item.get("decision")
            resolution = item.get("resolution", {})
            if not decision:
                continue
            prediction, actual = decision.get("direction"), resolution.get("actual_result")
            result = ("WIN" if prediction == actual else "LOSS") if (
                prediction in ("Up", "Down") and actual) else (
                "SKIP" if prediction == "SKIP" else "PENDING")
            probability = (decision.get("p_up") if prediction == "Up" else
                           decision.get("p_down") if prediction == "Down" else
                           max((decision.get("p_up") or 0), (decision.get("p_down") or 0)))
            round_trades = trades_by_slug.get(decision["slug"], [])
            components = decision.get("components") or {}
            market_component = components.get("market") or {}
            quotes = decision.get("quotes") or {}
            up_quote, down_quote = quotes.get("Up") or {}, quotes.get("Down") or {}
            rows.append({
                "slug": decision["slug"], "start": decision.get("start"),
                "decision_ms": decision.get("decision_ms"), "prediction": prediction,
                "probability": probability, "p_up": decision.get("p_up"),
                "p_down": decision.get("p_down"), "reason": decision.get("reason"),
                "market_p_up": market_component.get("p_up"),
                "market_up_ask": up_quote.get("ask"),
                "market_down_ask": down_quote.get("ask"),
                "warnings": decision.get("warnings") or [], "actual": actual,
                "result": result, "traded": decision["slug"] in traded,
                "estimated_edge": decision.get("estimated_edge"),
                "distance_to_beat": decision.get("distance_to_beat"),
                "distance_source": decision.get("distance_source"),
                "crossing_probability": decision.get("crossing_probability"),
                "agreement": decision.get("agreement") or {},
                "market_stability": decision.get("market_stability") or {},
            })
        return sorted(rows, key=lambda row: row.get("start") or 0, reverse=True)

    def round_detail(self, slug: str):
        item = self._round_map().get(slug)
        if item is None:
            return None
        item = dict(item)
        item["entries"] = {
            key: _clean_event(value) for key, value in item.get("entries", {}).items()
        }
        item["trades"] = [row for row in self.trades() if row["slug"] == slug]
        return item

    def statistics(self):
        rounds, trades = self.rounds(), self.trades()
        resolved_signals = [r for r in rounds if r["result"] in ("WIN", "LOSS")]
        resolved_trades = [t for t in trades if t["status"] in ("WIN", "LOSS")]
        return {
            "rounds": len(rounds), "signals": sum(r["prediction"] in ("Up", "Down") for r in rounds),
            "skips": sum(r["prediction"] == "SKIP" for r in rounds),
            "resolved_signals": len(resolved_signals),
            "signal_wins": sum(r["result"] == "WIN" for r in resolved_signals),
            "signal_accuracy": (sum(r["result"] == "WIN" for r in resolved_signals) /
                                len(resolved_signals) if resolved_signals else None),
            "trades": len(trades), "open_trades": sum(t["status"] == "OPEN" for t in trades),
            "trade_wins": sum(t["status"] == "WIN" for t in trades),
            "trade_losses": sum(t["status"] == "LOSS" for t in trades),
            "realized_profit": sum(t.get("profit") or 0 for t in resolved_trades),
            "total_trade_cost": sum(t.get("cost") or 0 for t in trades),
        }

    def _active_file(self):
        folder = self.data / "active-rounds"
        if not folder.exists():
            return None
        prediction = self.prediction_status() or {}
        slug = prediction.get("slug", "")
        start = slug.rsplit("-", 1)[-1] if slug else None
        candidates = list(folder.glob(f"*-{start}.jsonl")) if start else []
        candidates = candidates or list(folder.glob("*.jsonl"))
        return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None

    def chart(self, limit=600):
        btc, market = deque(maxlen=limit), deque(maxlen=limit)
        path = self._active_file()
        if path is None:
            return {"slug": None, "btc": [], "market": []}
        slug = None
        for row in _jsonl(path) or ():
            kind = row.get("kind")
            slug = row.get("slug", slug)
            if kind == "btc_context":
                prices = row.get("prices") or {}
                spot = prices.get("pyth_btc_usd") or prices.get("binance_btc_usdt") or {}
                twap = prices.get("chainlink_twap_60s") or {}
                opening = (row.get("reference") or {}).get("price_to_beat")
                spot_value = float(spot["value"]) if spot.get("value") is not None else None
                opening_value = float(opening) if opening is not None else None
                btc.append({"timestamp_ms": row.get("received_ms"), "remaining_seconds": row.get("remaining"),
                            "spot": spot_value,
                            "twap": float(twap["value"]) if twap.get("value") is not None else None,
                            "opening_price": opening_value,
                            "distance_to_open": (spot_value - opening_value
                                                 if None not in (spot_value, opening_value) else None)})
            elif kind == "quotes":
                quotes = row.get("quotes") or {}
                market.append({"timestamp_ms": row.get("received_ms"),
                               "remaining_seconds": row.get("remaining"),
                               "up": quotes.get("Up") or {}, "down": quotes.get("Down") or {}})
        return {"slug": slug, "btc": list(btc), "market": list(market)}

    def live(self):
        rounds = self.rounds()
        return {"prediction": self.prediction_status(), "trading": self.trading_status(),
                "latest_decision": rounds[0] if rounds else None}
