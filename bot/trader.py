"""Shared strategy, risk, ledger, and lifecycle for real-money execution."""

import json
import math
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .strategy import (MAXIMUM_ADVERSE_PRICE_MOVE, MINIMUM_ENTRY_PRICE, RISK_RULE,
                       probability_from_decision)


def allocation(bankroll):
    """Use 80% above $20 and all available money at or below $20."""
    return bankroll * .8 if bankroll > 20 else bankroll


def session_allocation(bankroll, fraction=None, maximum_trade_usd=None):
    """Calculate a trade budget from the session rules on the server."""
    budget = allocation(bankroll) if fraction is None else bankroll * fraction
    if maximum_trade_usd is not None:
        budget = min(budget, maximum_trade_usd)
    return max(0.0, min(bankroll, budget))


def risk_adjusted_allocation(bankroll, fraction=None, maximum_trade_usd=None,
                             multiplier=1.0, minimum_trade_usd=1.0):
    """Size a risk-reduced trade while preserving the venue's cash minimum."""
    base = session_allocation(bankroll, fraction, maximum_trade_usd)
    if multiplier <= 0 or base <= 0:
        return 0.0
    adjusted = base * multiplier
    if 0 < adjusted < minimum_trade_usd:
        cap_allows = (maximum_trade_usd is None
                      or maximum_trade_usd + 1e-9 >= minimum_trade_usd)
        if bankroll + 1e-9 >= minimum_trade_usd and cap_allows:
            return float(minimum_trade_usd)
        return 0.0
    return max(0.0, min(bankroll, adjusted))


def fee_per_share(rate, price):
    return rate * price * (1 - price)


def edge_per_share(chance, price, fee_rate):
    return chance - price - fee_per_share(fee_rate, price)


def maximum_value_price(chance, fee_rate, minimum_edge):
    """Highest price whose forecast edge still clears the safety margin."""
    low, high = 0.0, min(.999999, chance)
    for _ in range(60):
        middle = (low + high) / 2
        if edge_per_share(chance, middle, fee_rate) >= minimum_edge:
            low = middle
        else:
            high = middle
    return low


def settle_fill(fill, predicted, actual):
    won = predicted == actual
    payout = fill["shares"] if won else 0.0
    return {"won": won, "payout": payout, "profit": payout - fill["cost"]}


class JsonlTail:
    def __init__(self, path, start_at_end=False):
        self.path = Path(path)
        self.file = None
        self.start_at_end = start_at_end
        self._open()

    def _open(self):
        if self.file is not None or not self.path.exists():
            return
        self.file = self.path.open()
        if self.start_at_end:
            self.file.seek(0, 2)

    def read(self):
        self._open()
        if self.file is None:
            return []
        records = []
        while True:
            position = self.file.tell()
            line = self.file.readline()
            if not line:
                break
            if not line.endswith("\n"):
                self.file.seek(position)
                break
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def close(self):
        if self.file:
            self.file.close()


class TradingEngine:
    def __init__(self, data, bankroll, minimum_edge=.03, stop_entry_at_remaining=30.0,
                 poll=.25, model_threshold=.90,
                 allocation_fraction=None, maximum_trade_usd=None, max_trades=None,
                 target_profit_usd=None, max_loss_usd=None, run_id=None,
                 session_name=None, thin_gap_allocation_fraction=.25,
                 movement_disagreement_allocation_fraction=.5,
                 resume=False):
        self.data = Path(data)
        self.starting_bankroll = self.bankroll = float(bankroll)
        self.minimum_edge = float(minimum_edge)
        self.stop_entry_at_remaining_ms = int(stop_entry_at_remaining * 1000)
        self.poll = float(poll)
        self.model_threshold = float(model_threshold)
        self.allocation_fraction = allocation_fraction
        self.maximum_trade_usd = maximum_trade_usd
        self.max_trades = max_trades
        self.target_profit_usd = target_profit_usd
        self.max_loss_usd = max_loss_usd
        self.thin_gap_allocation_fraction = float(thin_gap_allocation_fraction)
        self.movement_disagreement_allocation_fraction = float(
            movement_disagreement_allocation_fraction)
        self.session_name = session_name
        stamp = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self.run_id = stamp
        self.folder = self.data / "real-trading" / "sessions"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.log_path = self.folder / f"real-{stamp}.jsonl"
        self.status_path = self.folder / f"real-{stamp}-status.json"
        self.session_started_ms = (time.time_ns() // 1_000_000) if resume else 0
        prior_status = {}
        if resume and self.status_path.exists():
            try:
                prior_status = json.loads(self.status_path.read_text())
            except (OSError, ValueError):
                prior_status = {}
        self.log_file = self.log_path.open("a" if resume else "x", buffering=1)
        # A resumed process must replay persisted decisions and resolutions that
        # may have arrived while the API was offline. The handled ledger below
        # prevents old completed rounds from being acted on again.
        self.history = JsonlTail(
            self.data / "round-history.jsonl", start_at_end=not resume)
        self.candidates, self.positions, self.resolved_trades, self.handled = {}, {}, {}, set()
        if resume and self.log_path.exists():
            try:
                prior_events = [json.loads(line) for line in
                                self.log_path.read_text().splitlines()]
            except (OSError, ValueError):
                prior_events = []
            for event in prior_events:
                if (event.get("event") == "run_started"
                        and event.get("recorded_ms") is not None):
                    self.session_started_ms = min(
                        self.session_started_ms, int(event["recorded_ms"]))
                slug = event.get("slug")
                if not slug:
                    continue
                if event.get("event") == "trade_filled":
                    self.positions[slug] = {
                        "slug": slug, "side": event.get("side"),
                        "fill": {"price": float(event.get("price") or 0),
                                 "shares": float(event.get("shares") or 0),
                                 "fee": float(event.get("fee") or 0),
                                 "cost": float(event.get("trade_cost") or 0)},
                    }
                    self.handled.add(slug)
                elif event.get("event") == "trade_resolved":
                    self.positions.pop(slug, None)
                    self.resolved_trades[slug] = event
                    self.handled.add(slug)
                elif event.get("event") in {
                        "signal_rejected", "entry_expired", "entry_cancelled",
                        "order_rejected", "order_submission_uncertain"}:
                    self.handled.add(slug)
            # If the process exited after posting an order but before its
            # session ledger was updated, retrying the same round could buy
            # twice. Treat every previously submitted slug as handled. A
            # confirmed fill remains recoverable through account/history
            # reconciliation, while future rounds continue normally.
            audit_path = self.data / "real-trading" / "manual-orders.jsonl"
            try:
                audit_events = [json.loads(line) for line in
                                audit_path.read_text().splitlines()]
            except (OSError, ValueError):
                audit_events = []
            for event in audit_events:
                if (event.get("event") == "automatic_order_submitted"
                        and event.get("run_id") == self.run_id
                        and event.get("slug")):
                    self.handled.add(event["slug"])
        self.quote_tails = {}
        self.starting_bankroll = float(prior_status.get(
            "starting_bankroll", self.starting_bankroll))
        self.bankroll = float(prior_status.get("available_bankroll", self.bankroll))
        self.trade_count = int(prior_status.get("trades", 0))
        self.wins = int(prior_status.get("wins", 0))
        self.losses = int(prior_status.get("losses", 0))
        self.realized_profit = float(prior_status.get("realized_profit", 0))
        self.running = True
        self.accepting_entries = True
        self.stop_requested_reason = None
        self.stop_reason = None
        self.suspend_requested = False
        self.write("run_resumed" if resume else "run_started",
                   starting_bankroll=self.starting_bankroll,
                   resumed_bankroll=self.bankroll if resume else None,
                   minimum_edge=self.minimum_edge,
                   model_threshold=self.model_threshold,
                   stop_entry_at_remaining_seconds=self.stop_entry_at_remaining_ms / 1000,
                   allocation_fraction=self.allocation_fraction,
                   maximum_trade_usd=self.maximum_trade_usd,
                   max_trades=self.max_trades,
                   target_profit_usd=self.target_profit_usd,
                   max_loss_usd=self.max_loss_usd,
                   thin_gap_allocation_fraction=self.thin_gap_allocation_fraction,
                   movement_disagreement_allocation_fraction=(
                       self.movement_disagreement_allocation_fraction),
                   risk_rule=RISK_RULE,
                   risk_action="skip",
                   session_name=self.session_name)
        self.write_status("POSITION_OPEN" if self.positions else "WAITING_FOR_SIGNAL",
                          resumed=bool(resume))

    def _cancel_candidates(self, reason):
        for slug, candidate in list(self.candidates.items()):
            self.candidates.pop(slug)
            self._close_round_tail(candidate["start"])
            self.handled.add(slug)
            self.write("entry_cancelled", slug=slug, side=candidate["side"],
                       reason=reason,
                       rejections=dict(candidate["rejections"]), bankroll=self.bankroll)

    def write(self, event, **fields):
        row = {"event": event, "recorded_ms": time.time_ns() // 1_000_000,
               "run_id": self.run_id, **fields}
        self.log_file.write(json.dumps(row, separators=(",", ":")) + "\n")

    def write_status(self, status, **fields):
        payload = {
            "updated_ms": time.time_ns() // 1_000_000,
            "run_id": self.run_id,
            "status": status,
            "starting_bankroll": round(self.starting_bankroll, 6),
            "available_bankroll": round(self.bankroll, 6),
            "realized_profit": round(self.realized_profit, 6),
            "trades": self.trade_count,
            "wins": self.wins,
            "losses": self.losses,
            "open_positions": len(self.positions),
            "candidates": len(self.candidates),
            "accepting_entries": self.accepting_entries,
            "stop_reason": self.stop_reason,
            **fields,
        }
        temporary = self.folder / f".{self.status_path.name}.tmp"
        temporary.write_text(json.dumps(payload, indent=2) + "\n")
        temporary.replace(self.status_path)
        latest = self.folder / "latest-status.json"
        latest_temporary = self.folder / ".latest-status.json.tmp"
        latest_temporary.write_text(json.dumps(payload, indent=2) + "\n")
        latest_temporary.replace(latest)

    def _round_tail(self, start):
        if start in self.quote_tails:
            return self.quote_tails[start]
        active = self.data / "active-rounds"
        paths = sorted(active.glob(f"*-{start}.jsonl")) if active.exists() else []
        if not paths:
            return None
        tail = JsonlTail(paths[-1], start_at_end=False)
        self.quote_tails[start] = tail
        return tail

    def _close_round_tail(self, start):
        tail = self.quote_tails.pop(start, None)
        if tail:
            tail.close()

    def request_stop(self, reason="stopped_by_user"):
        """Request a safe stop; open positions remain tracked until resolution."""
        self.stop_requested_reason = reason

    def request_suspend(self):
        """Pause this process without ending the persisted trading session."""
        self.suspend_requested = True

    def update_configuration(self, changes):
        """Apply validated rules while the real trader is running."""
        attribute_names = {
            "allocation_fraction": "allocation_fraction",
            "maximum_trade_usd": "maximum_trade_usd",
            "model_threshold": "model_threshold",
            "minimum_edge": "minimum_edge",
            "entry_cutoff_seconds": "stop_entry_at_remaining_ms",
            "max_trades": "max_trades",
            "target_profit_usd": "target_profit_usd",
            "max_loss_usd": "max_loss_usd",
            "thin_gap_allocation_fraction": "thin_gap_allocation_fraction",
            "movement_disagreement_allocation_fraction": (
                "movement_disagreement_allocation_fraction"),
        }
        for key, value in changes.items():
            attribute = attribute_names.get(key)
            if attribute is None:
                continue
            if key == "entry_cutoff_seconds":
                value = int(value * 1000)
            setattr(self, attribute, value)
        for candidate in self.candidates.values():
            candidate["deadline_ms"] = (
                candidate["market_end_ms"] - self.stop_entry_at_remaining_ms)
            candidate["maximum_value_price"] = maximum_value_price(
                candidate["chance"], candidate["fee_rate"], self.minimum_edge)
            candidate["risk_allocation_multiplier"] = (
                self.thin_gap_allocation_fraction if candidate["thin_gap"] else
                self.movement_disagreement_allocation_fraction
                if candidate["movement_disagreement"] else 1.0)
        self.write("configuration_updated", changes=changes,
                   planned_allocation=self._trade_budget(), bankroll=self.bankroll)
        if self.max_trades is not None and self.trade_count >= self.max_trades:
            self.request_stop("maximum_trades_reached")
        elif (self.target_profit_usd is not None
              and self.realized_profit >= self.target_profit_usd):
            self.request_stop("target_profit_reached")
        elif (self.max_loss_usd is not None
              and self.realized_profit <= -self.max_loss_usd):
            self.request_stop("maximum_loss_reached")

    def _trade_budget(self, multiplier=1.0):
        return risk_adjusted_allocation(
            self.bankroll, self.allocation_fraction, self.maximum_trade_usd,
            multiplier)

    def _finish_stop(self):
        if self.positions:
            return
        self.running = False
        status = "STOPPED" if self.stop_reason in ("stopped_by_user", "runtime_shutdown") else "COMPLETED"
        self.write("session_stopped", reason=self.stop_reason,
                   bankroll=self.bankroll, realized_profit=self.realized_profit,
                   trades=self.trade_count)
        self.write_status(status, reason=self.stop_reason)

    def _begin_stop(self, reason):
        if self.stop_reason is None:
            self.stop_reason = reason
        self.accepting_entries = False
        self._cancel_candidates(self.stop_reason)
        if self.positions:
            self.write_status("STOPPING", reason=self.stop_reason)
        else:
            self._finish_stop()

    def add_signal(self, row):
        slug = row.get("slug")
        if slug in self.handled or slug in self.candidates or slug in self.positions:
            return
        try:
            decision_ms = int(row["decision_ms"])
        except (KeyError, TypeError, ValueError):
            return
        if decision_ms < self.session_started_ms:
            self.handled.add(slug)
            return
        try:
            probability = probability_from_decision(row)
            p_up = float(probability["p_up"])
            p_down = float(probability["p_down"])
        except (TypeError, ValueError):
            return
        if not (math.isfinite(p_up) and math.isfinite(p_down)
                and 0 <= p_up <= 1 and 0 <= p_down <= 1):
            return
        side, chance = ("Up", p_up) if p_up >= p_down else ("Down", p_down)
        if chance + 1e-12 < self.model_threshold:
            self.write("signal_rejected", slug=slug, side=side,
                       model_probability=chance,
                       reason="confidence_below_threshold",
                       threshold=self.model_threshold, bankroll=self.bankroll)
            return
        agreement = row.get("agreement") or {}
        warnings = set(row.get("warnings") or [])
        settlement = (row.get("components") or {}).get("settlement") or {}
        spot_gap = settlement.get("spot_gap_bps")
        thin_gap = ("thin_gap" in warnings or
                    (spot_gap is not None and abs(float(spot_gap)) < 5.0))
        movement_side = agreement.get("movement")
        movement_disagreement = ("movement_disagreement" in warnings or
                                 (movement_side is not None and
                                  movement_side != side))
        combined_risk = thin_gap and movement_disagreement
        regime_risk = thin_gap and ((row.get("components") or {}).get(
            "movement") or {}).get("regime") == "choppy_up"
        risk_blocked = bool(row.get("risk_blocked", combined_risk or regime_risk))
        market_side = agreement.get("market")
        if market_side is not None and market_side != side:
            self.handled.add(slug)
            self.write("signal_rejected", slug=slug, side=side,
                       model_probability=chance,
                       reason="market_disagreement",
                       market_direction=market_side, warnings=sorted(warnings),
                       bankroll=self.bankroll)
            return
        if risk_blocked:
            self.handled.add(slug)
            risk_reason = row.get("reason") or "combined_risk"
            self.write("signal_rejected", slug=slug, side=side,
                       model_probability=chance,
                       reason="combined_risk", signal_reason=risk_reason,
                       thin_gap=thin_gap,
                       movement_disagreement=movement_disagreement,
                       warnings=sorted(warnings),
                       regime=((row.get("components") or {}).get("movement") or {}).get("regime"),
                       bankroll=self.bankroll)
            return
        risk_multiplier = (self.thin_gap_allocation_fraction if thin_gap else
                           self.movement_disagreement_allocation_fraction
                           if movement_disagreement else 1.0)
        if risk_multiplier <= 0:
            self.handled.add(slug)
            self.write("signal_rejected", slug=slug, side=side,
                       reason="session_risk_rule", thin_gap=thin_gap,
                       movement_disagreement=movement_disagreement,
                       bankroll=self.bankroll)
            return
        fee_rate = row.get("fee_rate")
        if fee_rate is None or not 0 <= fee_rate <= 1:
            self.handled.add(slug)
            self.write("signal_rejected", slug=slug, reason="invalid_fee_rate")
            return
        market_end_ms = (int(row["start"]) + 300) * 1000
        candidate = {
            "slug": slug, "start": int(row["start"]), "side": side,
            "chance": float(chance), "fee_rate": float(fee_rate),
            "signal_direction": row.get("direction"),
            "signal_reason": row.get("reason"),
            "decision_ms": decision_ms,
            "entry_start_ms": decision_ms,
            "market_end_ms": market_end_ms,
            "deadline_ms": market_end_ms - self.stop_entry_at_remaining_ms,
            "signal_ask": (row.get("quotes") or {}).get(side, {}).get("ask"),
            "maximum_value_price": maximum_value_price(chance, fee_rate, self.minimum_edge),
            "thin_gap": thin_gap,
            "movement_disagreement": movement_disagreement,
            "warnings": sorted(warnings),
            "regime": ((row.get("components") or {}).get("movement") or {}).get("regime"),
            "spot_gap_bps": settlement.get("spot_gap_bps"),
            "twap_gap_bps": settlement.get("twap_gap_bps"),
            "maximum_adverse_price_move": MAXIMUM_ADVERSE_PRICE_MOVE,
            "minimum_entry_price": MINIMUM_ENTRY_PRICE,
            "risk_allocation_multiplier": risk_multiplier,
            "rejections": Counter(), "best_ask": None,
        }
        self.candidates[slug] = candidate
        self.write("signal_seen", **{key: value for key, value in candidate.items()
                                     if key != "rejections"},
                   bankroll=self.bankroll,
                   planned_allocation=self._trade_budget(risk_multiplier))
        self.write_status("MONITORING_ENTRY", slug=slug, side=side,
                          model_probability=round(chance, 6),
                          stop_entry_at_remaining_seconds=self.stop_entry_at_remaining_ms / 1000,
                          maximum_value_price=round(candidate["maximum_value_price"], 6))
        remaining = self.stop_entry_at_remaining_ms / 1000
        print(f"Signal candidate {slug}: {side} {chance:.1%}; monitoring now until {remaining:g}s "
              f"remaining, up to ${candidate['maximum_value_price']:.3f}", flush=True)

    def consider_quotes(self, candidate, record):
        raise NotImplementedError

    def expire_candidates(self, now_ms):
        for slug, candidate in list(self.candidates.items()):
            if now_ms < candidate["deadline_ms"]:
                continue
            self.candidates.pop(slug)
            self._close_round_tail(candidate["start"])
            self.handled.add(slug)
            self.write("entry_expired", slug=slug, side=candidate["side"],
                       signal_ask=candidate["signal_ask"], best_ask=candidate["best_ask"],
                       maximum_value_price=candidate["maximum_value_price"],
                       rejections=dict(candidate["rejections"]), bankroll=self.bankroll)
            self.write_status("WAITING_FOR_SIGNAL", last_slug=slug,
                              last_result="NO_FILL")
            print(f"NO FILL {slug}: value/liquidity conditions did not pass", flush=True)

    def resolve(self, row):
        slug = row.get("slug")
        position = self.positions.pop(slug, None)
        if position is None:
            prior = self.resolved_trades.get(slug)
            actual = row.get("actual_result")
            source = row.get("resolution_source", "collector")
            if (prior is not None and actual in ("Up", "Down")
                    and source != "account_position"
                    and prior.get("actual") != actual):
                predicted = prior.get("predicted")
                fill = {
                    "shares": float(prior.get("shares") or 0),
                    "cost": float(prior.get("trade_cost") or 0),
                }
                corrected = settle_fill(fill, predicted, actual)
                old_profit = float(prior.get("profit") or 0)
                old_won = prior.get("won") is True
                self.realized_profit += corrected["profit"] - old_profit
                self.wins += int(corrected["won"]) - int(old_won)
                self.losses += int(not corrected["won"]) - int(not old_won)
                self.bankroll = self.settlement_available_bankroll(self.bankroll)
                fields = {
                    "slug": slug, "predicted": predicted, "actual": actual,
                    "won": corrected["won"],
                    "entry_price": prior.get("entry_price"),
                    "shares": fill["shares"], "fee": prior.get("fee", 0),
                    "trade_cost": fill["cost"], "payout": corrected["payout"],
                    "profit": corrected["profit"], "bankroll": self.bankroll,
                    "resolution_source": source,
                    "opening_price": row.get("opening_price"),
                    "final_price": row.get("final_price"),
                    "official_correction": True,
                    "previous_actual": prior.get("actual"),
                    "previous_profit": old_profit,
                }
                self.write("trade_resolved", **fields)
                self.resolved_trades[slug] = fields
                self.write_status(
                    "WAITING_FOR_SIGNAL" if self.accepting_entries else "STOPPING",
                    last_slug=slug,
                    last_result="WIN" if corrected["won"] else "LOSS",
                    last_trade_profit=round(corrected["profit"], 6),
                    official_correction=True)
            candidate = self.candidates.pop(slug, None)
            if candidate is not None:
                self._close_round_tail(candidate["start"])
                self.handled.add(slug)
                self.write("entry_expired", slug=slug, side=candidate["side"],
                           reason="market_resolved_without_fill",
                           signal_ask=candidate["signal_ask"], best_ask=candidate["best_ask"],
                           maximum_value_price=candidate["maximum_value_price"],
                           rejections=dict(candidate["rejections"]), bankroll=self.bankroll)
                self.write_status("WAITING_FOR_SIGNAL", last_slug=slug,
                                  last_result="NO_FILL")
            return
        actual = row.get("actual_result")
        if actual not in ("Up", "Down"):
            return
        result = settle_fill(position["fill"], position["side"], actual)
        self.bankroll += result["payout"]
        self.bankroll = self.settlement_available_bankroll(self.bankroll)
        self.realized_profit += result["profit"]
        self.wins += int(result["won"])
        self.losses += int(not result["won"])
        self.handled.add(slug)
        self.write("trade_resolved", slug=slug, predicted=position["side"], actual=actual,
                   won=result["won"], entry_price=position["fill"]["price"],
                   shares=position["fill"]["shares"], fee=position["fill"]["fee"],
                   trade_cost=position["fill"]["cost"], payout=result["payout"],
                   profit=result["profit"], bankroll=self.bankroll,
                   resolution_source=row.get("resolution_source", "collector"),
                   opening_price=row.get("opening_price"), final_price=row.get("final_price"))
        self.resolved_trades[slug] = {
            "slug": slug, "predicted": position["side"], "actual": actual,
            "won": result["won"], "entry_price": position["fill"]["price"],
            "shares": position["fill"]["shares"], "fee": position["fill"]["fee"],
            "trade_cost": position["fill"]["cost"], "payout": result["payout"],
            "profit": result["profit"],
            "resolution_source": row.get("resolution_source", "collector"),
        }
        print(f"REAL {'WIN' if result['won'] else 'LOSS'} {slug}: "
              f"profit ${result['profit']:.2f}; bankroll ${self.bankroll:.2f}", flush=True)
        if self.bankroll <= 1e-9 and not self.positions:
            self.bankroll = 0.0
            self.running = False
            self.write("game_over", reason="bankroll_depleted", bankroll=0.0,
                       realized_profit=self.realized_profit)
            self.write_status("GAME_OVER", last_slug=slug)
        else:
            if (self.target_profit_usd is not None
                    and self.realized_profit >= self.target_profit_usd):
                self._begin_stop("target_profit_reached")
            elif (self.max_loss_usd is not None
                  and self.realized_profit <= -self.max_loss_usd):
                self._begin_stop("maximum_loss_reached")
            elif self.stop_reason is not None:
                self._finish_stop()
            if not self.running or self.stop_reason is not None:
                return
            self.write_status("WAITING_FOR_SIGNAL", last_slug=slug,
                              last_result="WIN" if result["won"] else "LOSS",
                              last_trade_profit=round(result["profit"], 6))

    def settlement_available_bankroll(self, calculated_bankroll):
        """Return spendable cash after settlement."""
        return calculated_bankroll

    def process_history(self, accept_signals=True):
        for row in self.history.read():
            if row.get("record_type") == "decision":
                if accept_signals:
                    self.add_signal(row)
            elif row.get("record_type") == "resolution":
                self.resolve(row)

    def process_quotes(self):
        for candidate in list(self.candidates.values()):
            tail = self._round_tail(candidate["start"])
            if tail is None:
                candidate["rejections"]["round_log_unavailable"] += 1
                continue
            for record in tail.read():
                if record.get("kind") == "quotes" and record.get("slug") == candidate["slug"]:
                    self.consider_quotes(candidate, record)
                    if candidate["slug"] not in self.candidates:
                        break

    def close(self, interrupted=False):
        if interrupted and self.running:
            self.write("run_stopped", reason="user_interrupt", bankroll=self.bankroll,
                       realized_profit=self.realized_profit)
            self.write_status("STOPPED")
        self.history.close()
        for tail in self.quote_tails.values():
            tail.close()
        self.log_file.close()

    def run(self):
        label = "Real"
        print(f"{label} trader log: {self.log_path}", flush=True)
        print(f"{label} trader status: {self.status_path}", flush=True)
        print(f"Starting real bankroll: ${self.bankroll:.2f}", flush=True)
        interrupted = False
        try:
            while self.running:
                if self.suspend_requested:
                    self.write("runtime_suspended", bankroll=self.bankroll,
                               realized_profit=self.realized_profit,
                               trades=self.trade_count)
                    self.write_status("RESTARTING", reason="runtime_restart")
                    break
                if self.stop_requested_reason is not None and self.stop_reason is None:
                    self._begin_stop(self.stop_requested_reason)
                    if not self.running:
                        break
                self.process_history(accept_signals=self.accepting_entries)
                if self.accepting_entries:
                    self.process_quotes()
                    self.expire_candidates(time.time_ns() // 1_000_000)
                time.sleep(self.poll)
        except KeyboardInterrupt:
            interrupted = True
            print("Real trader stopped by user; live collection is unaffected.", flush=True)
        finally:
            self.close(interrupted)
