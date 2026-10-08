"""Independent model paper races for forward performance comparison."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from time import time_ns

from .models import model_releases
from .paper import PaperTrader, maximum_value_price, session_allocation


TERMINAL = {"STOPPED", "COMPLETED", "GAME_OVER"}


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _read_jsonl(path):
    try:
        return [json.loads(line) for line in Path(path).read_text().splitlines()]
    except (OSError, ValueError):
        return []


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)


class ModelRaceManager:
    """Run every immutable model with equal independent paper bankrolls."""

    def __init__(self, data):
        self.data = Path(data)
        self.folder = self.data / "model-races"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.race_id = None
        self.traders = {}
        self.threads = {}
        self._recover_interrupted()
        self._resume_latest()

    def _recover_interrupted(self):
        for path in self.folder.glob("*/*/race-*-status.json"):
            status = _read_json(path)
            if not status or status.get("status") in TERMINAL:
                continue
            status.update(status="STOPPED", reason="runtime_restarted",
                          accepting_entries=False, updated_ms=time_ns() // 1_000_000)
            _write_json(path, status)

    def _resume_latest(self):
        sessions = sorted(self.folder.glob("*/race-session.json"), reverse=True)
        if not sessions:
            return
        metadata = _read_json(sessions[0])
        if not metadata:
            return
        race_id = metadata["race_id"]
        statuses = {}
        for release in model_releases():
            model = release["id"]
            path = self.folder / race_id / model / f"race-{race_id}-status.json"
            status = _read_json(path) or {}
            if (status.get("status") != "STOPPED"
                    or status.get("reason") not in ("runtime_shutdown", "runtime_restarted")):
                return
            statuses[model] = status
        config = metadata["configuration"]
        self.race_id, self.traders, self.threads = race_id, {}, {}
        for release in model_releases():
            model = release["id"]
            trader = PaperTrader(
                self.data, float(statuses[model].get("available_bankroll") or 0),
                model_version=model,
                model_threshold=release["recommended_confidence"],
                minimum_edge=config["minimum_edge"],
                stop_entry_at_remaining=config["entry_cutoff_seconds"],
                allocation_fraction=config["allocation_fraction"],
                maximum_trade_usd=config.get("maximum_trade_usd"),
                thin_gap_allocation_fraction=config["thin_gap_allocation_fraction"],
                movement_disagreement_allocation_fraction=(
                    config["movement_disagreement_allocation_fraction"]),
                run_id=race_id, session_name=metadata["name"], resume=True,
                folder_name=f"model-races/{race_id}/{model}", file_prefix="race")
            thread = threading.Thread(name=f"model-race-{model}", daemon=True,
                                      target=self._run_one, args=(model, trader))
            self.traders[model], self.threads[model] = trader, thread
        for thread in self.threads.values():
            thread.start()

    def is_active(self):
        with self.lock:
            return bool(self.race_id and any(thread.is_alive()
                        for thread in self.threads.values()))

    def start(self, configuration):
        with self.lock:
            if self.is_active():
                raise ValueError("model_race_already_active")
            race_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            config = dict(configuration)
            metadata = {
                "race_id": race_id,
                "created_ms": time_ns() // 1_000_000,
                "name": config.pop("name", None) or f"Model race {race_id[:15]}",
                "configuration": config,
                "models": [{"model_version": release["id"],
                            "name": release["name"],
                            "confidence": release["recommended_confidence"],
                            "risk_rule": release["risk_rule"]}
                           for release in model_releases()],
            }
            race_folder = self.folder / race_id
            _write_json(race_folder / "race-session.json", metadata)
            self.race_id, self.traders, self.threads = race_id, {}, {}
            for release in model_releases():
                model = release["id"]
                trader = PaperTrader(
                    self.data, config["starting_bankroll"],
                    model_version=model,
                    model_threshold=release["recommended_confidence"],
                    minimum_edge=config["minimum_edge"],
                    stop_entry_at_remaining=config["entry_cutoff_seconds"],
                    allocation_fraction=config["allocation_fraction"],
                    maximum_trade_usd=config.get("maximum_trade_usd"),
                    thin_gap_allocation_fraction=config["thin_gap_allocation_fraction"],
                    movement_disagreement_allocation_fraction=(
                        config["movement_disagreement_allocation_fraction"]),
                    run_id=race_id, session_name=metadata["name"],
                    folder_name=f"model-races/{race_id}/{model}", file_prefix="race")
                thread = threading.Thread(
                    name=f"model-race-{model}", daemon=True,
                    target=self._run_one, args=(model, trader))
                self.traders[model], self.threads[model] = trader, thread
            for thread in self.threads.values():
                thread.start()
            return self.detail(race_id)

    def _run_one(self, model, trader):
        try:
            trader.run()
        finally:
            with self.lock:
                if self.traders.get(model) is trader:
                    self.traders.pop(model, None)

    def stop(self, race_id):
        with self.lock:
            if race_id != self.race_id or not self.is_active():
                raise ValueError("model_race_not_active")
            for trader in self.traders.values():
                trader.request_stop("stopped_by_user")

    def list(self):
        rows = []
        for path in self.folder.glob("*/race-session.json"):
            metadata = _read_json(path)
            if metadata:
                detail = self.detail(metadata["race_id"], include_rounds=False)
                rows.append({key: detail[key] for key in (
                    "race_id", "created_ms", "name", "status", "configuration", "models")})
        return sorted(rows, key=lambda row: row["created_ms"], reverse=True)

    def _history(self, created_ms):
        decisions, resolutions = {}, {}
        path = self.data / "stage4-round-history.jsonl"
        for row in _read_jsonl(path):
            if int(row.get("recorded_ms") or 0) < int(created_ms):
                continue
            if row.get("record_type") == "decision":
                decisions[row.get("slug")] = row
            elif row.get("record_type") == "resolution":
                resolutions[row.get("slug")] = row
        return decisions, resolutions

    def detail(self, race_id, include_rounds=True):
        root = self.folder / race_id
        metadata = _read_json(root / "race-session.json")
        if metadata is None:
            return None
        decisions, resolutions = self._history(metadata["created_ms"])
        by_model, all_fills = {}, {}
        statuses = []
        for release in model_releases():
            model = release["id"]
            folder = root / model
            events = _read_jsonl(folder / f"race-{race_id}.jsonl")
            status = _read_json(folder / f"race-{race_id}-status.json") or {}
            statuses.append(status.get("status", "UNKNOWN"))
            fills = {row["slug"]: row for row in events
                     if row.get("event") == "trade_filled" and row.get("slug")}
            resolved = {row["slug"]: row for row in events
                        if row.get("event") == "trade_resolved" and row.get("slug")}
            signals = {row["slug"]: row for row in events
                       if row.get("event") in ("signal_seen", "signal_rejected")
                       and row.get("slug")}
            for slug, fill in fills.items():
                all_fills.setdefault(slug, []).append(fill)
            by_model[model] = {"release": release, "events": events, "status": status,
                               "fills": fills, "resolved": resolved, "signals": signals}
        model_rows = []
        for model, data in by_model.items():
            rounds = []
            for slug, decision in decisions.items():
                output = (decision.get("model_outputs") or {}).get(model) or {}
                signal, fill, result = (data["signals"].get(slug),
                                        data["fills"].get(slug),
                                        data["resolved"].get(slug))
                actual = (resolutions.get(slug) or {}).get("actual_result")
                side = output.get("side")
                chance = output.get("chance")
                state = "PENDING"
                if result:
                    state = "WIN" if result.get("won") else "LOSS"
                elif fill:
                    state = "OPEN"
                elif signal and signal.get("event") == "signal_seen":
                    state = "NO_FILL"
                elif actual:
                    state = "SKIP"
                opportunity = None
                if not fill and actual and side in ("Up", "Down") and chance is not None:
                    fee = float(decision.get("fee_rate") or .07)
                    maximum = maximum_value_price(
                        float(chance), fee, metadata["configuration"]["minimum_edge"])
                    shared = [item for item in all_fills.get(slug, [])
                              if item.get("side") == side
                              and float(item.get("price") or 2) <= maximum]
                    if shared:
                        opportunity = "MISSED_WIN" if side == actual else "AVOIDED_LOSS"
                rounds.append({
                    "slug": slug, "start": decision.get("start"), "state": state,
                    "opportunity": opportunity, "side": side, "probability": chance,
                    "decision": output.get("direction"), "reason": output.get("reason"),
                    "warnings": output.get("warnings") or [],
                    "regime": (decision.get("market_stability") or {}).get("regime"),
                    "spot_gap_bps": ((decision.get("components") or {}).get("settlement") or {}).get("spot_gap_bps"),
                    "twap_gap_bps": ((decision.get("components") or {}).get("settlement") or {}).get("twap_gap_bps"),
                    "actual": actual, "entry_price": fill.get("price") if fill else None,
                    "cost": fill.get("trade_cost") if fill else None,
                    "profit": result.get("profit") if result else None,
                    "signal_event": signal.get("event") if signal else None,
                    "execution_reason": signal.get("reason") if signal else None,
                })
            summary = {
                "model_version": model, "name": data["release"]["name"],
                "confidence": data["release"]["recommended_confidence"],
                "risk_rule": data["release"]["risk_rule"],
                "status": data["status"],
                "trades": len(data["fills"]), "wins": len([r for r in data["resolved"].values() if r.get("won")]),
                "losses": len([r for r in data["resolved"].values() if r.get("won") is False]),
                "missed_wins": sum(row["opportunity"] == "MISSED_WIN" for row in rounds),
                "avoided_losses": sum(row["opportunity"] == "AVOIDED_LOSS" for row in rounds),
                "rounds": sorted(rounds, key=lambda row: row.get("start") or 0, reverse=True),
            }
            model_rows.append(summary)
        status = ("RUNNING" if any(value not in TERMINAL for value in statuses)
                  else "STOPPED")
        payload = {**metadata, "status": status, "models": model_rows}
        if not include_rounds:
            payload["models"] = [{key: value for key, value in row.items() if key != "rounds"}
                                 for row in model_rows]
        return payload

    def shutdown(self):
        with self.lock:
            traders, threads = list(self.traders.values()), list(self.threads.values())
            for trader in traders:
                trader.request_stop("runtime_shutdown")
        for thread in threads:
            thread.join(timeout=5)
