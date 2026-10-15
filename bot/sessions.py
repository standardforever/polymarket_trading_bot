"""Runtime manager for user-created real-money trading sessions."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from time import time_ns

from .real import RealTrader
from .trader import session_allocation, settle_fill


TERMINAL_STATUSES = {"STOPPED", "COMPLETED", "GAME_OVER"}


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)


class TradingSessionManager:
    """Own at most one active real session and retain completed sessions."""

    def __init__(self, data, account):
        self.data = Path(data)
        self.folder = self.data / "real-trading" / "sessions"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.account = account
        self.lock = threading.RLock()
        self.trader = None
        self.thread = None
        self._recover_interrupted_sessions()
        self.reconcile()

    def _recover_interrupted_sessions(self):
        interrupted = []
        for path in self.folder.glob("real-*-status.json"):
            try:
                status = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if status.get("status") in TERMINAL_STATUSES:
                continue
            interrupted.append((int(status.get("updated_ms") or 0), path, status))
        interrupted.sort(key=lambda item: item[0], reverse=True)

        resumed = False
        resumable_states = {
            "WAITING_FOR_SIGNAL", "MONITORING_ENTRY", "POSITION_OPEN", "RESTARTING"
        }
        for _, path, status in interrupted:
            run_id = status.get("run_id") or path.name.removeprefix(
                "real-").removesuffix("-status.json")
            metadata_path = self.folder / f"real-{run_id}-session.json"
            log_path = self.folder / f"real-{run_id}.jsonl"
            can_resume = (not resumed and status.get("status") in resumable_states
                          and status.get("accepting_entries") is not False
                          and metadata_path.exists() and log_path.exists())
            if can_resume:
                try:
                    metadata = json.loads(metadata_path.read_text())
                    config = metadata["configuration"]
                    trader = self._create_trader(
                        run_id, metadata.get("name"), config,
                        float(status.get("available_bankroll") or
                              config.get("starting_bankroll") or 0),
                        resume=True)
                except (KeyError, OSError, TypeError, ValueError):
                    can_resume = False
                else:
                    self._launch(trader)
                    resumed = True
                    continue

            # A user-requested stop, uncertain submission, corrupt record, or
            # an older stray active file must never become a second live trader.
            status.update(status="STOPPED",
                          reason=status.get("reason") or "restart_not_resumable",
                          accepting_entries=False,
                          updated_ms=time_ns() // 1_000_000)
            _write_json(path, status)
            if not resumed:
                _write_json(self.folder / "latest-status.json", status)

    def _create_trader(self, run_id, name, config, bankroll, resume=False):
        return RealTrader(
            self.data, self.account, bankroll,
            minimum_edge=config["minimum_edge"],
            stop_entry_at_remaining=config["entry_cutoff_seconds"],
            model_threshold=config["model_threshold"],
            allocation_fraction=config["allocation_fraction"],
            maximum_trade_usd=config.get("maximum_trade_usd"),
            max_trades=config.get("max_trades"),
            target_profit_usd=config.get("target_profit_usd"),
            max_loss_usd=config.get("max_loss_usd"),
            thin_gap_allocation_fraction=config["thin_gap_allocation_fraction"],
            movement_disagreement_allocation_fraction=(
                config["movement_disagreement_allocation_fraction"]),
            run_id=run_id, session_name=name, resume=resume)

    def _launch(self, trader):
        self.trader = trader
        self.thread = threading.Thread(
            name=f"real-session-{trader.run_id}", daemon=True,
            target=self._run, args=(trader,))
        self.thread.start()

    def _active_locked(self):
        return (self.trader is not None and self.thread is not None
                and self.thread.is_alive())

    def _resolution_map(self):
        resolutions = {}
        try:
            lines = (self.data / "round-history.jsonl").read_text().splitlines()
        except OSError:
            return resolutions
        for line in lines:
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if row.get("record_type") != "resolution" or not row.get("slug"):
                continue
            previous = resolutions.get(row["slug"])
            if previous is None or row.get("recorded_ms", 0) > previous.get("recorded_ms", 0):
                resolutions[row["slug"]] = row
        return resolutions

    def reconcile(self):
        """Settle interrupted real fills once their official outcome exists."""
        with self.lock:
            active_id = self.trader.run_id if self._active_locked() else None
            resolutions = self._resolution_map()
            reconciled = 0
            for log_path in self.folder.glob("real-*.jsonl"):
                try:
                    rows = [json.loads(line) for line in log_path.read_text().splitlines()]
                except (OSError, ValueError):
                    continue
                run_id = next((row.get("run_id") for row in rows if row.get("run_id")), None)
                if not run_id or run_id == active_id:
                    continue
                open_fills = {}
                for row in rows:
                    if row.get("event") == "trade_filled" and row.get("slug"):
                        open_fills[row["slug"]] = row
                    elif row.get("event") == "trade_resolved":
                        open_fills.pop(row.get("slug"), None)
                matches = [(fill, resolutions.get(slug))
                           for slug, fill in open_fills.items() if resolutions.get(slug)]
                if not matches:
                    continue
                status_path = self.folder / f"real-{run_id}-status.json"
                try:
                    status = json.loads(status_path.read_text())
                except (OSError, ValueError):
                    status = {"run_id": run_id, "status": "STOPPED"}
                bankroll = float(status.get("available_bankroll") or 0)
                realized = float(status.get("realized_profit") or 0)
                wins, losses = int(status.get("wins") or 0), int(status.get("losses") or 0)
                new_rows = []
                for fill, resolution in matches:
                    actual = resolution.get("actual_result")
                    if actual not in ("Up", "Down"):
                        continue
                    result = settle_fill(
                        {"shares": float(fill["shares"]), "cost": float(fill["trade_cost"])},
                        fill["side"], actual)
                    realized += result["profit"]
                    wins += int(result["won"])
                    losses += int(not result["won"])
                    new_rows.append({
                        "event": "trade_resolved", "recorded_ms": time_ns() // 1_000_000,
                        "run_id": run_id, "slug": fill["slug"], "predicted": fill["side"],
                        "actual": actual, "won": result["won"],
                        "entry_price": fill["price"], "shares": fill["shares"],
                        "fee": fill.get("fee", 0), "trade_cost": fill["trade_cost"],
                        "payout": result["payout"], "profit": result["profit"],
                        "bankroll": bankroll, "opening_price": resolution.get("opening_price"),
                        "final_price": resolution.get("final_price"),
                        "official_recorded_ms": resolution.get("recorded_ms"),
                        "reconciled_after_restart": True,
                    })
                if not new_rows:
                    continue
                with log_path.open("a", buffering=1) as output:
                    for row in new_rows:
                        output.write(json.dumps(row, separators=(",", ":")) + "\n")
                try:
                    account_state = self.account.snapshot(force=True)
                    if account_state.get("connected"):
                        bankroll = float(account_state.get("balance_usdc") or 0)
                except Exception:
                    pass
                status.update(
                    status=status.get("status") if status.get("status") in TERMINAL_STATUSES
                    else "STOPPED",
                    reason=status.get("reason") or "runtime_restarted",
                    accepting_entries=False, available_bankroll=round(bankroll, 6),
                    realized_profit=round(realized, 6), wins=wins, losses=losses,
                    open_positions=max(0, int(status.get("open_positions") or 0)
                                       - len(new_rows)),
                    updated_ms=time_ns() // 1_000_000,
                )
                _write_json(status_path, status)
                latest_path = self.folder / "latest-status.json"
                try:
                    latest = json.loads(latest_path.read_text())
                except (OSError, ValueError):
                    latest = {}
                if latest.get("run_id") == run_id:
                    _write_json(latest_path, status)
                reconciled += len(new_rows)
            return reconciled

    def is_active(self):
        with self.lock:
            return self._active_locked()

    def start(self, configuration):
        with self.lock:
            if self._active_locked():
                raise ValueError("trading_session_already_active")
            snapshot = self.account.snapshot(force=True)
            if not snapshot.get("connected"):
                raise ValueError(snapshot.get("error") or "real_account_unavailable")
            config = dict(configuration)
            config["starting_bankroll"] = float(snapshot.get("balance_usdc") or 0)
            if config["starting_bankroll"] < 1:
                raise ValueError("real_account_balance_below_minimum")
            maximum = config.get("maximum_trade_usd")
            if maximum is not None and maximum < 1:
                raise ValueError("real_maximum_trade_below_minimum")
            for field in ("maximum_trade_usd", "max_loss_usd"):
                if config.get(field) is not None and config[field] > config["starting_bankroll"]:
                    raise ValueError(f"{field}_exceeds_balance")
            config["planned_first_trade_budget"] = session_allocation(
                config["starting_bankroll"], config["allocation_fraction"],
                config.get("maximum_trade_usd"))
            session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            metadata = {
                "session_id": session_id, "created_ms": time_ns() // 1_000_000,
                "name": config.pop("name", None) or f"Real session {session_id[:15]}",
                "configuration": config,
            }
            _write_json(self.folder / f"real-{session_id}-session.json", metadata)
            trader = self._create_trader(
                session_id, metadata["name"], config,
                config["starting_bankroll"])
            self._launch(trader)
            return metadata

    def _run(self, trader):
        try:
            trader.run()
        except Exception as error:
            try:
                status = json.loads(trader.status_path.read_text())
            except (OSError, ValueError):
                status = {}
            status.update(status="STOPPED", reason="runtime_error",
                          error=type(error).__name__, accepting_entries=False,
                          updated_ms=time_ns() // 1_000_000)
            _write_json(trader.status_path, status)
            _write_json(trader.folder / "latest-status.json", status)
        finally:
            with self.lock:
                if self.trader is trader:
                    self.trader = None
                    self.thread = None

    def stop(self, session_id):
        with self.lock:
            if not self._active_locked() or self.trader.run_id != session_id:
                raise ValueError("trading_session_not_active")
            self.trader.request_stop("stopped_by_user")

    def update(self, session_id, changes):
        with self.lock:
            if not self._active_locked() or self.trader.run_id != session_id:
                raise ValueError("trading_session_not_active")
            if not self.trader.accepting_entries or self.trader.stop_requested_reason:
                raise ValueError("trading_session_stopping")
            changes = dict(changes)
            name_was_set = "name" in changes
            name = changes.pop("name", None)
            snapshot = self.account.snapshot(force=True)
            if not snapshot.get("connected"):
                raise ValueError("real_account_unavailable")
            available = float(snapshot.get("balance_usdc") or 0)
            self.trader.bankroll = available
            maximum = changes.get("maximum_trade_usd")
            if maximum is not None and maximum < 1:
                raise ValueError("real_maximum_trade_below_minimum")
            if maximum is not None and maximum > available:
                raise ValueError("maximum_trade_exceeds_bankroll")
            maximum_loss = changes.get("max_loss_usd")
            if maximum_loss is not None and maximum_loss > available:
                raise ValueError("maximum_loss_exceeds_available_cash")
            self.trader.update_configuration(changes)
            if name_was_set:
                self.trader.session_name = name or f"Real session {session_id[:15]}"
            metadata_path = self.folder / f"real-{session_id}-session.json"
            metadata = json.loads(metadata_path.read_text())
            if name_was_set:
                metadata["name"] = self.trader.session_name
            metadata["configuration"].update(changes)
            metadata["configuration"]["planned_first_trade_budget"] = session_allocation(
                self.trader.bankroll, self.trader.allocation_fraction,
                self.trader.maximum_trade_usd)
            metadata["updated_ms"] = time_ns() // 1_000_000
            _write_json(metadata_path, metadata)
            return metadata

    def shutdown(self):
        with self.lock:
            trader, thread = self.trader, self.thread
            if trader is not None and thread is not None and thread.is_alive():
                trader.request_suspend()
        if thread is not None:
            thread.join(timeout=5)
