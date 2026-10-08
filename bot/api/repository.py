"""Read API views over the collector and trading append-only files."""

import json
import time
from collections import deque
from pathlib import Path

def _clean_event(row):
    if not isinstance(row, dict):
        return row
    cleaned = dict(row)
    cleaned.pop("model_version", None)
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
    def __init__(self, data: Path):
        self.data = data
        self.history_path = data / "round-history.jsonl"
        self.paper_folder = data / "paper-trading"
        self.real_folder = data / "real-trading" / "sessions"

    def prediction_status(self):
        return _json(self.data / "live-status.json")

    def paper_status(self):
        return _clean_event(_json(self.paper_folder / "latest-status.json"))

    def trading_status(self):
        paper = self.paper_status()
        real = _json(self.real_folder / "latest-status.json")
        if real and real.get("run_id"):
            session = next((row for row in self._sessions(
                self.real_folder, "real", "real")
                if row.get("session_id") == real["run_id"]), None)
            if session:
                real = session["status_detail"]
        choices = [row for row in (paper, real) if row]
        return max(choices, key=lambda row: row.get("updated_ms") or 0) if choices else None

    def capabilities(self):
        return {
            "asset": "BTC",
            "market_duration_seconds": 300,
            "collector_hot_reload": False,
            "paper_trading": True,
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
            "paper_trader": {"available": self.paper_status() is not None},
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
        paths = [(path, "paper") for path in self.paper_folder.glob("paper-*.jsonl")]
        paths += [(path, "real") for path in self.real_folder.glob("real-*.jsonl")]
        for path, mode in sorted(paths, key=lambda item: str(item[0])):
            for row in _jsonl(path) or ():
                if row.get("event") not in ("trade_filled", "trade_resolved"):
                    continue
                key = (row.get("run_id"), row.get("slug"))
                item = trades.setdefault(key, {"run_id": key[0], "slug": key[1],
                                               "mode": mode})
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
            if item.get("mode") == "real":
                making_amount = (fill.get("exchange_response") or {}).get(
                    "making_amount")
                if making_amount is not None:
                    principal = float(making_amount)
                    shares = float(fill.get("shares") or 0)
                    price = principal / shares if shares > 0 else float(
                        fill.get("price") or 0)
                    decision = (round_map.get(item.get("slug")) or {}).get(
                        "decision") or {}
                    fee_rate = float(fill.get("fee_rate")
                                     if fill.get("fee_rate") is not None
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
                        resolution["profit"] = float(
                            resolution.get("payout") or 0) - cost
            status = ("WIN" if resolution.get("won") is True else
                      "LOSS" if resolution.get("won") is False else "OPEN")
            rows.append({
                "run_id": item["run_id"], "slug": item["slug"], "status": status,
                "mode": item.get("mode", "paper"),
                "side": fill.get("side"), "forecast_probability": fill.get("forecast_probability"),
                "entry_ms": fill.get("entry_ms"), "entry_price": fill.get("price"),
                "shares": fill.get("shares"), "fee": fill.get("fee"),
                "cost": fill.get("trade_cost"), "maximum_value_price": fill.get("maximum_value_price"),
                "actual": resolution.get("actual"), "profit": resolution.get("profit"),
                "payout": resolution.get("payout"), "resolved_ms": resolution.get("recorded_ms"),
            })
        return sorted(rows, key=lambda row: row.get("entry_ms") or 0, reverse=True)

    def _sessions(self, folder, prefix, mode, include_legacy=False):
        sessions = []
        if not folder.exists():
            return sessions
        trades = self.trades()
        metadata_by_id = {}
        for path in folder.glob(f"{prefix}-*-session.json"):
            metadata = _json(path)
            if not metadata or not metadata.get("session_id"):
                continue
            metadata_by_id[metadata["session_id"]] = metadata
        if include_legacy:
            for path in folder.glob(f"{prefix}-*.jsonl"):
                first = next(iter(_jsonl(path) or ()), None)
                if (not first or not first.get("run_id")
                        or first["run_id"] in metadata_by_id):
                    continue
                session_id = first["run_id"]
                starting = float(first.get("starting_bankroll") or 0)
                metadata_by_id[session_id] = {
                    "session_id": session_id,
                    "created_ms": first.get("recorded_ms"),
                    "name": f"Legacy paper session {session_id[:15]}",
                    "mode": mode,
                    "paper_only": True,
                    "legacy": True,
                    "configuration": {
                        "starting_bankroll": starting,
                        "allocation_fraction": .8,
                        "maximum_trade_usd": None,
                        "model_threshold": first.get("model_threshold", .9),
                        "minimum_edge": first.get("minimum_edge", .03),
                        "entry_cutoff_seconds": first.get(
                            "stop_entry_at_remaining_seconds", 50),
                        "max_trades": None, "target_profit_usd": None,
                        "max_loss_usd": None,
                        "planned_first_trade_budget": (
                            starting * .8 if starting > 20 else starting),
                    },
                }
        for metadata in metadata_by_id.values():
            metadata = dict(metadata)
            metadata["configuration"] = dict(metadata.get("configuration") or {})
            metadata["configuration"].pop("model_version", None)
            session_id = metadata["session_id"]
            status = _clean_event(
                _json(folder / f"{prefix}-{session_id}-status.json") or {})
            session_trades = [row for row in trades if row.get("run_id") == session_id]
            realized_profit = sum(float(row.get("profit") or 0)
                                  for row in session_trades)
            if mode == "real":
                starting = float((metadata.get("configuration") or {}).get(
                    "starting_bankroll") or 0)
                open_cost = sum(float(row.get("cost") or 0) for row in session_trades
                                if row.get("status") == "OPEN")
                status = dict(status)
                status["available_bankroll"] = max(
                    0.0, starting + realized_profit - open_cost)
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

    def paper_sessions(self):
        return self._sessions(self.paper_folder, "paper", "paper", include_legacy=True)

    def trading_sessions(self):
        rows = self.paper_sessions()
        rows += self._sessions(self.real_folder, "real", "real")
        return sorted(rows, key=lambda row: row.get("created_ms") or 0, reverse=True)

    def paper_session(self, session_id):
        session = next((row for row in self.paper_sessions()
                        if row["session_id"] == session_id), None)
        if session is None:
            return None
        log_path = self.paper_folder / f"paper-{session_id}.jsonl"
        session["trades"] = [row for row in self.trades()
                             if row.get("run_id") == session_id]
        session["events"] = [_clean_event(row) for row in (_jsonl(log_path) or ())]
        return session

    def trading_session(self, session_id):
        session = next((row for row in self.trading_sessions()
                        if row["session_id"] == session_id), None)
        if session is None:
            return None
        mode = session.get("mode", "paper")
        folder = self.paper_folder if mode == "paper" else self.real_folder
        prefix = "paper" if mode == "paper" else "real"
        session["trades"] = [row for row in self.trades()
                             if row.get("run_id") == session_id]
        session["events"] = [
            _clean_event(row)
            for row in (_jsonl(folder / f"{prefix}-{session_id}.jsonl") or ())
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
        item["paper_trades"] = [row for row in self.trades() if row["slug"] == slug]
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
            "paper_trades": len(trades), "open_trades": sum(t["status"] == "OPEN" for t in trades),
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
        return {"prediction": self.prediction_status(), "paper": self.paper_status(),
                "trading": self.trading_status(),
                "latest_decision": rounds[0] if rounds else None}
