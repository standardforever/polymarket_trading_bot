"""Runtime manager for user-created real-money trading sessions."""

import json
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from time import time_ns

from .real import RealTrader
from .strategy import (MAXIMUM_ENTRY_PRICE, MINIMUM_STRENGTH,
                       RECOMMENDED_CONFIDENCE, RECOVERED_CONFIDENCE,
                       RECOVERED_MINIMUM_GAP_BPS,
                       RECOVERED_MINIMUM_STRENGTH)
from .trader import session_allocation, settle_fill
from .trading import MINIMUM_EDGE


TERMINAL_STATUSES = {"STOPPED", "COMPLETED", "GAME_OVER"}
RECOVERY_SLUG_EVENTS = {
    "entry_validation_changed", "entry_expired", "entry_cancelled",
    "order_preflight_rejected", "order_rejected",
    "order_submission_uncertain", "order_confirmation_error",
    "order_confirmation_recovered", "order_unconfirmed_at_resolution",
    "trade_filled", "trade_resolved",
}


def production_configuration():
    """One authoritative configuration for autonomous real-money execution."""
    return {
        "allocation_fraction": .80,
        "maximum_trade_usd": None,
        "model_threshold": RECOMMENDED_CONFIDENCE,
        "minimum_edge": MINIMUM_EDGE,
        "entry_cutoff_seconds": 30.0,
        "max_trades": None,
        "target_profit_usd": None,
        "max_loss_usd": None,
        "thin_gap_allocation_fraction": 0.0,
        "movement_disagreement_allocation_fraction": 0.0,
        "minimum_strength": MINIMUM_STRENGTH,
        "maximum_entry_price": MAXIMUM_ENTRY_PRICE,
        "protect_profits": True,
        "fresh_revalidation": True,
        "strict_risk_rules": True,
        "shadow_rejected_signals": True,
        "recovered_signals_enabled": True,
        "recovered_allocation_fraction": .50,
        "recovered_model_threshold": RECOVERED_CONFIDENCE,
        "recovered_minimum_strength": RECOVERED_MINIMUM_STRENGTH,
        "recovered_minimum_gap_bps": RECOVERED_MINIMUM_GAP_BPS,
        "recovery_confirmation_snapshots": 3,
        "recovery_confirmation_seconds": 2.0,
    }


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)


def _recovery_row(row, recovery_slugs):
    event = str(row.get("event") or "")
    if event.startswith("recovered_") or event == "fund_transfer":
        return True
    if (row.get("entry_mode") == "recovered"
            or row.get("capital_pool") == "profit_trading"):
        return True
    return row.get("slug") in recovery_slugs and event in RECOVERY_SLUG_EVENTS


class TradingSessionManager:
    """Own at most one active real session and retain completed sessions."""

    def __init__(self, data, account):
        self.data = Path(data)
        self.folder = self.data / "real-trading" / "sessions"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.profit_folder = self.data / "real-trading" / "profit-signals"
        self.profit_folder.mkdir(parents=True, exist_ok=True)
        self.account = account
        self.lock = threading.RLock()
        self.trader = None
        self.thread = None
        self._split_legacy_recovery_events()
        self._recover_interrupted_sessions()
        self.reconcile()

    def _split_legacy_recovery_events(self):
        """Move recovery/shadow records out of older mixed session logs."""
        for source in self.folder.glob("real-*.jsonl"):
            try:
                rows = [json.loads(line) for line in source.read_text().splitlines()]
            except (OSError, ValueError):
                continue
            recovery_slugs = {
                row.get("slug") for row in rows
                if (str(row.get("event") or "").startswith("recovered_")
                    or row.get("entry_mode") == "recovered"
                    or row.get("capital_pool") == "profit_trading")
                and row.get("slug")
            }
            recovery_rows = [row for row in rows
                             if _recovery_row(row, recovery_slugs)]
            if not recovery_rows:
                continue
            main_rows = [row for row in rows
                         if not _recovery_row(row, recovery_slugs)]
            run_id = source.name.removeprefix("real-").removesuffix(".jsonl")
            target = self.profit_folder / f"profit-{run_id}.jsonl"
            try:
                existing = [json.loads(line) for line in
                            target.read_text().splitlines()]
            except (OSError, ValueError):
                existing = []
            identities = {(row.get("event"), row.get("recorded_ms"), row.get("slug"))
                          for row in existing}
            merged = existing + [row for row in recovery_rows if (
                row.get("event"), row.get("recorded_ms"), row.get("slug"))
                not in identities]
            main_temp = source.with_name(f".{source.name}.split.tmp")
            profit_temp = target.with_name(f".{target.name}.split.tmp")
            main_temp.write_text("".join(
                json.dumps(row, separators=(",", ":")) + "\n" for row in main_rows))
            profit_temp.write_text("".join(
                json.dumps(row, separators=(",", ":")) + "\n" for row in merged))
            profit_temp.replace(target)
            main_temp.replace(source)

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
                    saved_configuration = metadata["configuration"]
                    config = {**saved_configuration,
                              **production_configuration()}
                    # The strategy stays fixed across releases, but this is a
                    # user-controlled execution switch and must survive a
                    # process restart.
                    if "recovered_signals_enabled" in saved_configuration:
                        config["recovered_signals_enabled"] = bool(
                            saved_configuration["recovered_signals_enabled"])
                    metadata["configuration"] = config
                    _write_json(metadata_path, metadata)
                    trader = self._create_trader(
                        run_id, metadata.get("name"), config,
                        float(status.get("trading_capital") or
                              status.get("available_bankroll") or
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
            minimum_edge=config.get("minimum_edge", 0),
            stop_entry_at_remaining=config.get("entry_cutoff_seconds", 30),
            model_threshold=config.get("model_threshold", .98),
            allocation_fraction=config.get("allocation_fraction", .8),
            maximum_trade_usd=config.get("maximum_trade_usd"),
            max_trades=config.get("max_trades"),
            target_profit_usd=config.get("target_profit_usd"),
            max_loss_usd=config.get("max_loss_usd"),
            thin_gap_allocation_fraction=config.get("thin_gap_allocation_fraction", 0),
            movement_disagreement_allocation_fraction=(
                config.get("movement_disagreement_allocation_fraction", 0)),
            minimum_strength=config.get("minimum_strength", 0),
            maximum_entry_price=config.get("maximum_entry_price"),
            protect_profits=config.get("protect_profits", False),
            fresh_revalidation=config.get("fresh_revalidation", False),
            strict_risk_rules=config.get("strict_risk_rules", False),
            shadow_rejected_signals=config.get("shadow_rejected_signals", True),
            recovered_signals_enabled=config.get(
                "recovered_signals_enabled", False),
            recovered_allocation_fraction=config.get(
                "recovered_allocation_fraction", .5),
            recovered_model_threshold=config.get(
                "recovered_model_threshold", RECOVERED_CONFIDENCE),
            recovered_minimum_strength=config.get(
                "recovered_minimum_strength", RECOVERED_MINIMUM_STRENGTH),
            recovered_minimum_gap_bps=config.get(
                "recovered_minimum_gap_bps", RECOVERED_MINIMUM_GAP_BPS),
            recovery_confirmation_snapshots=config.get(
                "recovery_confirmation_snapshots", 3),
            recovery_confirmation_seconds=config.get(
                "recovery_confirmation_seconds", 2),
            safe_reserve=config.get("safe_reserve", 0),
            profit_trading_balance=config.get("profit_trading_balance", 0),
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
            log_paths = list(self.folder.glob("real-*.jsonl"))
            log_paths += list(self.profit_folder.glob("profit-*.jsonl"))
            for log_path in log_paths:
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
                bankroll = float(status.get(
                    "trading_capital", status.get("available_bankroll") or 0))
                protected = float(status.get("protected_profit") or 0)
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
                    capital_pool = fill.get("capital_pool", "core_trading")
                    protected_ledger = "protected_profit" in status
                    if capital_pool == "profit_trading":
                        principal_return = result["payout"]
                        protected_delta = result["profit"]
                        protected += result["payout"]
                    else:
                        principal_return = (min(result["payout"], float(fill["trade_cost"]))
                                            if protected_ledger else 0)
                        protected_delta = (max(result["profit"], 0)
                                           if protected_ledger else 0)
                        bankroll += principal_return
                        protected += protected_delta
                    wins += int(result["won"])
                    losses += int(not result["won"])
                    new_rows.append({
                        "event": "trade_resolved", "recorded_ms": time_ns() // 1_000_000,
                        "run_id": run_id, "slug": fill["slug"], "predicted": fill["side"],
                        "actual": actual, "won": result["won"],
                        "entry_price": fill["price"], "shares": fill["shares"],
                        "fee": fill.get("fee", 0), "trade_cost": fill["trade_cost"],
                        "payout": result["payout"], "profit": result["profit"],
                        "bankroll": bankroll, "principal_return": principal_return,
                        "protected_profit_delta": protected_delta,
                        "protected_profit": protected,
                        "entry_mode": fill.get("entry_mode", "core"),
                        "capital_pool": capital_pool,
                        "opening_price": resolution.get("opening_price"),
                        "final_price": resolution.get("final_price"),
                        "official_recorded_ms": resolution.get("recorded_ms"),
                        "reconciled_after_restart": True,
                    })
                if not new_rows:
                    continue
                with log_path.open("a", buffering=1) as output:
                    for row in new_rows:
                        output.write(json.dumps(row, separators=(",", ":")) + "\n")
                status.update(
                    status=status.get("status") if status.get("status") in TERMINAL_STATUSES
                    else "STOPPED",
                    reason=status.get("reason") or "runtime_restarted",
                    accepting_entries=False, available_bankroll=round(bankroll, 6),
                    trading_capital=round(bankroll, 6),
                    protected_profit=round(protected, 6),
                    profit_trading_balance=round(protected, 6),
                    safe_reserve=round(float(status.get("safe_reserve") or 0), 6),
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
            liquid_cash = float(snapshot.get("balance_usdc") or 0)
            try:
                saved_funds = json.loads(
                    (self.data / "real-trading" / "funds.json").read_text())
            except (OSError, ValueError):
                saved_funds = {}
            if saved_funds:
                config["starting_bankroll"] = max(
                    0.0, float(saved_funds.get("core_trading") or 0))
                config["safe_reserve"] = max(
                    0.0, float(saved_funds.get("safe_reserve") or 0))
                config["profit_trading_balance"] = max(
                    0.0, float(saved_funds.get("profit_trading") or 0))
            else:
                config["starting_bankroll"] = liquid_cash
                config["safe_reserve"] = 0.0
                config["profit_trading_balance"] = 0.0
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
            if config["planned_first_trade_budget"] < 1:
                raise ValueError("real_trade_budget_below_minimum")
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

    def funds(self, activity_limit=20, activity_offset=0):
        activity_limit = max(1, int(activity_limit))
        activity_offset = max(0, int(activity_offset))
        with self.lock:
            if self._active_locked():
                payload = self.trader.funds_snapshot()
                log_path = self.trader.recovery_log_path
            else:
                try:
                    payload = json.loads(
                        (self.data / "real-trading" / "funds.json").read_text())
                except (OSError, ValueError):
                    payload = {"core_trading": 0.0, "profit_trading": 0.0,
                               "safe_reserve": 0.0,
                               "recovered_signals_enabled": False,
                               "recovered_candidates": []}
                run_id = payload.get("run_id")
                log_path = (self.profit_folder / f"profit-{run_id}.jsonl"
                            if run_id else None)
            try:
                snapshot = self.account.snapshot()
            except Exception:
                snapshot = {}
            payload = dict(payload)
            payload["active"] = self._active_locked()
            payload["account_balance"] = snapshot.get(
                "balance_usdc", payload.get("account_balance"))
            payload.setdefault("recovered_candidates", [])
            activity_window = deque(maxlen=activity_offset + activity_limit)
            activity_total = 0
            watched = set()
            opportunities = 0
            hypothetical_wins = 0
            hypothetical_losses = 0
            hypothetical_profit = 0.0
            recovered_trades = 0
            real_wins = 0
            real_losses = 0
            realized_profit = 0.0
            if log_path is not None and log_path.exists():
                try:
                    with log_path.open() as source:
                        for line in source:
                            try:
                                row = json.loads(line)
                            except (TypeError, ValueError):
                                continue
                            activity_total += 1
                            activity_window.append(row)
                            event = row.get("event")
                            if event == "recovered_signal_watching":
                                watched.add(row.get("slug"))
                            elif event == "recovered_opportunity":
                                opportunities += 1
                            elif event == "recovered_opportunity_resolved":
                                hypothetical_wins += int(row.get("won") is True)
                                hypothetical_losses += int(row.get("won") is False)
                                hypothetical_profit += float(
                                    row.get("hypothetical_profit") or 0)
                            elif (event == "trade_resolved"
                                  and row.get("entry_mode") == "recovered"):
                                recovered_trades += 1
                                real_wins += int(row.get("won") is True)
                                real_losses += int(row.get("won") is False)
                                realized_profit += float(row.get("profit") or 0)
                except OSError:
                    pass
            newest_first = list(reversed(activity_window))
            payload["statistics"] = {
                "watched": len(watched),
                "opportunities": opportunities,
                "hypothetical_wins": hypothetical_wins,
                "hypothetical_losses": hypothetical_losses,
                "hypothetical_profit": hypothetical_profit,
                "real_trades": recovered_trades,
                "real_wins": real_wins,
                "real_losses": real_losses,
                "realized_profit": realized_profit,
            }
            payload["activity"] = newest_first[
                activity_offset:activity_offset + activity_limit]
            payload["activity_page"] = {
                "total": activity_total,
                "offset": activity_offset,
                "limit": activity_limit,
            }
            return payload

    def set_recovered_signals_enabled(self, session_id, enabled):
        with self.lock:
            if not self._active_locked() or self.trader.run_id != session_id:
                raise ValueError("trading_session_not_active")
            result = self.trader.set_recovered_signals_enabled(enabled)
            metadata_path = self.folder / f"real-{session_id}-session.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["configuration"]["recovered_signals_enabled"] = bool(enabled)
            metadata["updated_ms"] = time_ns() // 1_000_000
            _write_json(metadata_path, metadata)
            return result

    def transfer_funds(self, session_id, source, destination, amount):
        with self.lock:
            if not self._active_locked() or self.trader.run_id != session_id:
                raise ValueError("trading_session_not_active")
            return self.trader.transfer_funds(source, destination, amount)

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
