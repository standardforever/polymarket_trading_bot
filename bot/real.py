"""Activated autonomous real-money execution for qualified BTC signals."""

import time
from decimal import Decimal, ROUND_FLOOR

from .account import AutomaticOrderSubmissionError
from .prediction import CORE_COMPONENTS, analytic_signal, build_components
from .strategy import probability_snapshot
from .trader import (TradingEngine, maximum_value_price,
                     risk_adjusted_allocation)


class RealTrader(TradingEngine):
    """Submit bounded FOK buys through the real Polymarket account."""

    def __init__(self, data, account, bankroll, **kwargs):
        self.account = account
        self.pending_orders = {}
        super().__init__(data, bankroll, **kwargs)

    def _available_cash(self, force=True):
        snapshot = self.account.snapshot(force=force)
        if not snapshot.get("connected"):
            raise ValueError(snapshot.get("error") or
                             "The Polymarket account is not connected")
        return float(snapshot.get("balance_usdc") or 0)

    def settlement_available_bankroll(self, calculated_bankroll):
        """Refresh the account display without adding protected profit to capital."""
        try:
            self.account_balance = self._available_cash(force=True)
            if not self.protect_profits:
                return self.account_balance
        except (TypeError, ValueError):
            pass
        return calculated_bankroll

    @staticmethod
    def _position_outcome(slug, position, now_seconds):
        """Infer a final binary outcome from an ended account position at 0 or 1."""
        try:
            market_end = int(str(slug).rsplit("-", 1)[1]) + 300
            price = float(position.get("current_price"))
        except (TypeError, ValueError, IndexError):
            return None
        if now_seconds < market_end:
            return None
        outcome = position.get("outcome")
        if outcome not in ("Up", "Down"):
            return None
        if position.get("redeemable") is True or price >= .999:
            return outcome
        if price <= .001:
            return "Down" if outcome == "Up" else "Up"
        return None

    def _resolve_from_account(self):
        snapshot = self.account.snapshot()
        if not snapshot.get("connected"):
            return
        if snapshot.get("balance_usdc") is not None:
            self.account_balance = float(snapshot["balance_usdc"])
            if not self.protect_profits:
                self.bankroll = self.account_balance
        if not self.positions:
            return
        account_positions = snapshot.get("positions") or []
        now_seconds = time.time()
        for slug, position in list(self.positions.items()):
            account_position = next((row for row in account_positions
                                     if row.get("slug") == slug
                                     and row.get("outcome") == position.get("side")), None)
            if account_position is None:
                continue
            actual = self._position_outcome(slug, account_position, now_seconds)
            if actual is not None:
                self.resolve({"slug": slug, "actual_result": actual,
                              "resolution_source": "account_position"})

    def process_history(self, accept_signals=True):
        super().process_history(accept_signals=accept_signals)
        self._resolve_from_account()

    def observe_round_record(self, candidate, record):
        history = candidate.setdefault("_fresh_history", [])
        kind = record.get("kind")
        if kind == "btc_price":
            history.append(record)
            if len(history) > 400:
                del history[:-400]
        elif kind == "btc_context" and record.get("slug") == candidate["slug"]:
            candidate["_fresh_context"] = record

    def _fresh_validation(self, candidate, quote_record):
        """Rebuild the forecast from the latest BTC and market snapshot."""
        context = candidate.get("_fresh_context")
        history = candidate.get("_fresh_history") or []
        observed_ms = int(quote_record.get("received_ms") or 0)
        remaining = (candidate["market_end_ms"] - observed_ms) / 1000
        try:
            components = build_components(
                context, history, quote_record.get("quotes") or {}, None,
                observed_ms, remaining, CORE_COMPONENTS)
            signal = analytic_signal(
                components, quote_record.get("quotes") or {}, candidate["fee_rate"])
            probability = probability_snapshot(components)
        except (ArithmeticError, KeyError, TypeError, ValueError) as error:
            return False, f"fresh_inputs_unavailable:{error}", None
        strength = abs(float(probability["z_score"]))
        side = probability["side"]
        chance = float(probability["chance"])
        settlement = components["settlement"]
        spot_gap = settlement.get("spot_gap_bps")
        twap_gap = settlement.get("twap_gap_bps")
        try:
            gap_values = [abs(float(value)) for value in (spot_gap, twap_gap)
                          if value is not None]
        except (TypeError, ValueError):
            gap_values = []
        if not gap_values:
            return False, "fresh_inputs_unavailable:gap_unavailable", None
        thin_gap_distance = min(gap_values)
        recovered = candidate.get("entry_mode") == "recovered"
        thin_gap = (thin_gap_distance + 1e-12
                    < self.recovered_minimum_gap_bps)
        warnings = list(signal.get("warnings") or [])
        if thin_gap and "thin_gap" not in warnings:
            warnings.append("thin_gap")
        reason = None
        if recovered:
            if chance + 1e-12 < self.recovered_model_threshold:
                reason = "fresh_confidence_below_threshold"
            elif strength <= self.recovered_minimum_strength:
                reason = "fresh_strength_below_threshold"
            elif thin_gap:
                reason = "fresh_thin_gap"
        else:
            if side != candidate["side"]:
                reason = "fresh_direction_changed"
            elif chance + 1e-12 < self.model_threshold:
                reason = "fresh_confidence_below_threshold"
            elif strength <= self.minimum_strength:
                reason = "fresh_strength_below_threshold"
            elif signal.get("thin_gap"):
                reason = "fresh_thin_gap"
            elif signal.get("movement_disagreement"):
                reason = "fresh_movement_disagreement"
            elif (signal.get("agreement") or {}).get("market") != side:
                reason = "fresh_market_disagreement"
            elif signal.get("direction") != side:
                reason = signal.get("reason") or "fresh_signal_rejected"
        snapshot = {
            "probability": chance, "strength": strength, "side": side,
            "spot_gap_bps": spot_gap, "twap_gap_bps": twap_gap,
            "thin_gap": thin_gap,
            "thin_gap_distance_bps": thin_gap_distance,
            "warnings": warnings,
        }
        if reason is not None:
            candidate["recovery_pass_count"] = 0
            candidate["recovery_pass_started_ms"] = None
            candidate["recovery_confirmation_side"] = None
            return False, reason, snapshot
        candidate.update(
            side=side, chance=chance, strength=strength,
            thin_gap=False,
            movement_disagreement=bool(signal.get("movement_disagreement")),
            warnings=warnings,
            maximum_value_price=min(
                float(self.maximum_entry_price or 1),
                maximum_value_price(
                    chance, candidate["fee_rate"], self.minimum_edge)))
        candidate["fresh_snapshot"] = snapshot
        if recovered:
            if candidate.get("recovery_confirmation_side") != side:
                candidate["recovery_confirmation_side"] = side
                candidate["recovery_pass_started_ms"] = observed_ms
                candidate["recovery_pass_count"] = 0
            candidate["recovery_pass_count"] += 1
            elapsed = observed_ms - candidate["recovery_pass_started_ms"]
            if (candidate["recovery_pass_count"]
                    < self.recovery_confirmation_snapshots
                    or elapsed < self.recovery_confirmation_ms):
                return False, "recovery_confirmation_pending", snapshot
        return True, "fresh_validation_passed", snapshot

    def _record_recovered_opportunity(self, candidate, observed_ms, ask,
                                      total_budget, execution_reason):
        """Persist the earliest fully fillable recovery for later evaluation."""
        fee_multiplier = 1 + candidate["fee_rate"] * (1 - ask)
        principal = max(1.0, float(total_budget) / fee_multiplier)
        shares = principal / ask
        fee = shares * candidate["fee_rate"] * ask * (1 - ask)
        cost = principal + fee
        fill = {"price": ask, "shares": shares, "fee": fee, "cost": cost}
        slug = candidate["slug"]
        shadow = {key: value for key, value in candidate.items()
                  if key != "rejections"}
        shadow["fill"] = fill
        self.shadow_positions[slug] = shadow
        self.candidates.pop(slug, None)
        self._close_round_tail(candidate["start"])
        self.handled.add(slug)
        self.write(
            "recovered_opportunity", slug=slug, side=candidate["side"],
            initial_rejection_reason=candidate.get("initial_rejection_reason"),
            recovery_probability=candidate.get("chance"),
            recovery_strength=candidate.get("strength"),
            fresh_snapshot=candidate.get("fresh_snapshot"),
            price=ask, shares=shares, fee=fee, trade_cost=cost,
            hypothetical_principal=principal,
            entry_ms=observed_ms,
            entry_remaining_seconds=(candidate["market_end_ms"] - observed_ms) / 1000,
            execution_enabled=self.recovered_signals_enabled,
            execution_reason=execution_reason,
            profit_trading_balance=self.protected_profit)
        self.write_status("WAITING_FOR_SIGNAL", last_slug=slug,
                          last_result="RECOVERED_OPPORTUNITY_LOGGED")

    def consider_quotes(self, candidate, record):
        observed_ms = int(record.get("received_ms", 0))
        if observed_ms < candidate["entry_start_ms"] or observed_ms >= candidate["deadline_ms"]:
            return
        if self.fresh_revalidation:
            wall_ms = time.time_ns() // 1_000_000
            if wall_ms >= candidate["deadline_ms"]:
                candidate["rejections"]["entry_cutoff_reached"] += 1
                return
            if abs(wall_ms - observed_ms) > 5_000:
                candidate["rejections"]["quote_record_stale"] += 1
                return
        if self.fresh_revalidation:
            valid, validation_reason, snapshot = self._fresh_validation(candidate, record)
            if not valid:
                candidate["rejections"][validation_reason] += 1
                if candidate.get("last_validation_reason") != validation_reason:
                    self.write("entry_validation_changed", slug=candidate["slug"],
                               side=candidate["side"], valid=False,
                               reason=validation_reason, snapshot=snapshot)
                    candidate["last_validation_reason"] = validation_reason
                return
            candidate["last_validation_reason"] = validation_reason
            if (candidate.get("entry_mode") == "recovered"
                    and not candidate.get("recovery_logged")):
                candidate["recovery_logged"] = True
                self.write(
                    "recovered_signal_passed", slug=candidate["slug"],
                    side=candidate["side"],
                    initial_rejection_reason=candidate.get(
                        "initial_rejection_reason"),
                    snapshot=snapshot,
                    confirmation_snapshots=candidate.get("recovery_pass_count"),
                    execution_enabled=self.recovered_signals_enabled)
        quote = (record.get("quotes") or {}).get(candidate["side"], {})
        if not quote.get("ready") or quote.get("ask") is None:
            candidate["rejections"]["ask_unavailable"] += 1
            return
        connection_age = quote.get("connection_age_ms")
        if connection_age is not None and not -1000 <= connection_age <= 25_000:
            candidate["rejections"]["connection_unhealthy"] += 1
            return
        ask = float(quote["ask"])
        candidate["best_ask"] = (ask if candidate["best_ask"] is None
                                  else min(candidate["best_ask"], ask))
        minimum_entry_price = candidate.get("minimum_entry_price")
        if (minimum_entry_price is not None
                and ask <= float(minimum_entry_price) + 1e-12):
            candidate["rejections"]["price_below_entry_floor"] += 1
            return
        if ask > candidate["maximum_value_price"] + 1e-12:
            candidate["rejections"]["edge_below_minimum"] += 1
            return
        # The exchange method performs a lightweight collateral check and clamps
        # the order to the actual available cash. A full account snapshot here
        # also loads positions and open orders; on a live entry that delay can
        # turn the quote which passed the guard into a stale quote.
        recovered = candidate.get("capital_pool") == "profit_trading"
        capital_before = max(
            0.0, self.protected_profit if recovered else self.bankroll)
        account_cash = (max(0.0, float(self.account_balance))
                        if self.account_balance is not None else capital_before)
        cash = min(capital_before, account_cash)
        fee_multiplier = 1 + candidate["fee_rate"] * (1 - ask)
        if recovered:
            desired_total = capital_before * self.recovered_allocation_fraction
            minimum_total = fee_multiplier
            if 0 < desired_total < minimum_total and capital_before >= minimum_total:
                desired_total = minimum_total
            total_budget = min(capital_before, account_cash, desired_total)
            budget = total_budget / fee_multiplier if total_budget > 0 else 0
        else:
            budget = risk_adjusted_allocation(
                cash, self.allocation_fraction, self.maximum_trade_usd,
                candidate["risk_allocation_multiplier"],
                minimum_trade_usd=0 if self.protect_profits else 1)
            total_budget = budget
            if self.protect_profits:
                # Keep principal plus the estimated fee inside the fixed cap.
                budget = total_budget / fee_multiplier
        # Preserve a non-zero warning allocation while meeting the venue's
        # $1 market-buy minimum. Session activation guarantees the normal
        # allocation itself can fund this floor.
        if (not self.protect_profits and 0 < budget < 1 and cash >= 1
                and (self.maximum_trade_usd is None or self.maximum_trade_usd >= 1)):
            budget = 1.0
        budget = float(Decimal(str(budget)).quantize(
            Decimal("0.01"), rounding=ROUND_FLOOR))
        if budget <= 0:
            candidate["rejections"]["no_money"] += 1
            if recovered:
                self._record_recovered_opportunity(
                    candidate, observed_ms, ask, fee_multiplier,
                    "profit_balance_unavailable")
            return
        if budget < 1:
            candidate["rejections"]["trade_budget_below_exchange_minimum"] += 1
            if recovered:
                self._record_recovered_opportunity(
                    candidate, observed_ms, ask, fee_multiplier,
                    "profit_balance_below_exchange_minimum")
            return
        candidate["capital_allocation"] = total_budget
        visible = quote.get("ask_size")
        if visible is None or float(visible) + 1e-9 < budget / ask:
            candidate["rejections"]["insufficient_size"] += 1
            return
        if recovered and not self.recovered_signals_enabled:
            self._record_recovered_opportunity(
                candidate, observed_ms, ask, total_budget,
                "recovered_execution_disabled")
            return
        if observed_ms < candidate.get("next_order_attempt_ms", 0):
            candidate["rejections"]["exchange_retry_cooldown"] += 1
            return
        candidate["next_order_attempt_ms"] = observed_ms + 1_000

        slug = candidate["slug"]
        try:
            result = self.account.automatic_buy(
                run_id=self.run_id, slug=slug, outcome=candidate["side"],
                amount_usd=Decimal(str(budget)),
                maximum_price=Decimal(str(candidate["maximum_value_price"])),
                signal_ask=None,
                maximum_adverse_price_move=None,
                minimum_entry_price=(Decimal(str(minimum_entry_price))
                                     if minimum_entry_price is not None else None))
        except ValueError as error:  # Preflight failed before order submission.
            candidate["rejections"]["exchange_preflight"] += 1
            self.write("order_preflight_rejected", slug=slug, side=candidate["side"],
                       entry_mode=candidate.get("entry_mode", "core"),
                       capital_pool=candidate.get("capital_pool", "core_trading"),
                       message=str(error)[:240], bankroll=cash)
            return
        except AutomaticOrderSubmissionError as error:
            # A timeout can hide an accepted exchange order. Never retry this slug.
            self.candidates.pop(slug, None)
            self._close_round_tail(candidate["start"])
            self.handled.add(slug)
            self.write("order_submission_uncertain", slug=slug,
                       side=candidate["side"], message=str(error)[:240],
                       entry_mode=candidate.get("entry_mode", "core"),
                       capital_pool=candidate.get("capital_pool", "core_trading"),
                       retry_suppressed=True, bankroll=cash)
            self.request_stop("order_submission_uncertain")
            self.write_status("STOPPING", last_slug=slug,
                              last_result="SUBMISSION_UNCERTAIN",
                              reason="manual_reconciliation_required")
            return

        if result.get("ok") is False:
            self.write("order_rejected", slug=slug, side=candidate["side"],
                       entry_mode=candidate.get("entry_mode", "core"),
                       capital_pool=candidate.get("capital_pool", "core_trading"),
                       response=result, rejection_code=result.get("code"),
                       message=result.get("message"), bankroll=cash)
            retryable = result.get("retryable")
            if retryable is None:
                retryable = result.get("code") in {
                    "fok_not_filled", "unmatched", "market_not_ready"
                }
            if retryable:
                candidate["rejections"]["exchange_no_fill"] += 1
                self.write_status("MONITORING_ENTRY", slug=slug,
                                  last_result="ORDER_NOT_FILLED",
                                  maximum_value_price=round(
                                      candidate["maximum_value_price"], 6))
                return
            self.candidates.pop(slug, None)
            self._close_round_tail(candidate["start"])
            self.handled.add(slug)
            self.write_status("WAITING_FOR_SIGNAL", last_slug=slug,
                              last_result="ORDER_REJECTED")
            return
        self.candidates.pop(slug, None)
        self._close_round_tail(candidate["start"])
        self.handled.add(slug)
        if str(result.get("status") or "").lower() != "matched":
            # Delayed/live acknowledgements can execute later. Do not retry or
            # claim a fill until the account stream confirms the resulting position.
            self.write("order_submission_uncertain", slug=slug,
                       side=candidate["side"], response=result,
                       order_id=result.get("order_id"),
                       entry_mode=candidate.get("entry_mode", "core"),
                       capital_pool=candidate.get("capital_pool", "core_trading"),
                       retry_suppressed=True, bankroll=cash)
            self.pending_orders[slug] = {
                "candidate": {key: value for key, value in candidate.items()
                              if key != "rejections"},
                "response": result, "cash_before": capital_before,
                "budget": budget, "ask": ask, "observed_ms": observed_ms,
            }
            self.write_status("ORDER_PENDING_CONFIRMATION", last_slug=slug,
                              last_result="ORDER_PENDING_CONFIRMATION")
            return

        self._record_confirmed_fill(
            candidate, result, observed_ms, capital_before, budget, ask)

    def _record_confirmed_fill(self, candidate, result, observed_ms, cash, budget, ask):
        """Record one exchange-confirmed FOK fill in the session ledger."""
        slug = candidate["slug"]

        principal = float(result.get("making_amount") or result.get("requested_amount") or budget)
        # For a BUY, Polymarket's confirmed making_amount is the USDC principal.
        # The collateral endpoint can lag the matched-order response, so an
        # immediate balance delta is not a reliable fill cost. Using that delta
        # previously recorded a $2.54 fill as $0.01 when only part of the debit
        # was visible in the first post-trade snapshot.
        shares = float(result.get("taking_amount") or (principal / float(
            result.get("estimated_price") or ask)))
        price = principal / shares if shares > 0 else ask
        fee = shares * candidate["fee_rate"] * price * (1 - price)
        cost = principal + fee
        fill = {"price": price, "shares": shares, "fee": fee, "cost": cost,
                "allocation": budget, "assumption": "exchange_fok_response"}
        capital_pool = candidate.get("capital_pool", "core_trading")
        if capital_pool == "profit_trading":
            self.protected_profit = max(0.0, cash - cost)
        else:
            self.bankroll = max(0.0, cash - cost)
        position = {key: value for key, value in candidate.items() if key != "rejections"}
        position.update(fill=fill, entry_ms=observed_ms,
                        entry_delay_seconds=(observed_ms - candidate["decision_ms"]) / 1000,
                        entry_remaining_seconds=(candidate["market_end_ms"] - observed_ms) / 1000,
                        exchange_response=result)
        self.positions[slug] = position
        self.trade_count += 1
        self.write("trade_filled", slug=slug, side=candidate["side"],
                   forecast_probability=candidate["chance"],
                   forecast_strength=candidate.get("strength"),
                   fresh_snapshot=candidate.get("fresh_snapshot"),
                   maximum_value_price=candidate["maximum_value_price"],
                   entry_ms=observed_ms,
                   entry_delay_seconds=position["entry_delay_seconds"],
                   entry_remaining_seconds=position["entry_remaining_seconds"],
                   price=price, shares=shares, fee=fee, trade_cost=cost,
                   capital_allocation=candidate.get("capital_allocation", budget),
                   order_principal=principal,
                   fee_rate=candidate["fee_rate"],
                   bankroll_after_entry=self.bankroll,
                   protected_profit=self.protected_profit,
                   profit_trading_balance=self.protected_profit,
                   entry_mode=candidate.get("entry_mode", "core"),
                   capital_pool=capital_pool,
                   initial_rejection_reason=candidate.get(
                       "initial_rejection_reason"),
                   thin_gap=candidate["thin_gap"],
                   movement_disagreement=candidate["movement_disagreement"],
                   risk_allocation_multiplier=candidate["risk_allocation_multiplier"],
                   automatic=True, exchange_response=result,
                   settlement_status=result.get("settlement_status"),
                   assumption="exchange_fok_response")
        self.write_status("POSITION_OPEN", slug=slug, side=candidate["side"],
                          entry_price=round(price, 6), shares=round(shares, 6),
                          trade_cost=round(cost, 6))
        if self.max_trades is not None and self.trade_count >= self.max_trades:
            self._begin_stop("maximum_trades_reached")

    def _finish_stop(self):
        if self.pending_orders:
            self.write_status("STOPPING", reason=self.stop_reason,
                              pending_orders=len(self.pending_orders))
            return
        super()._finish_stop()

    def resolve(self, row):
        slug = row.get("slug")
        pending = self.pending_orders.pop(slug, None)
        if pending is not None:
            response = pending["response"]
            pending_candidate = pending["candidate"]
            pending_channel = {
                "entry_mode": pending_candidate.get("entry_mode", "core"),
                "capital_pool": pending_candidate.get(
                    "capital_pool", "core_trading"),
            }
            try:
                confirmed = self.account.confirm_automatic_order(
                    str(response.get("order_id") or ""),
                    str(response.get("token_id") or ""))
            except Exception as error:
                confirmed = None
                self.write("order_confirmation_error", slug=slug,
                           order_id=response.get("order_id"),
                           message=str(error)[:240], **pending_channel)
            if confirmed is not None:
                recovered = {**response, **confirmed, "ok": True}
                self._record_confirmed_fill(
                    pending["candidate"], recovered, pending["observed_ms"],
                    pending["cash_before"], pending["budget"], pending["ask"])
                self.write("order_confirmation_recovered", slug=slug,
                           order_id=response.get("order_id"), response=recovered,
                           **pending_channel)
            else:
                self.write("order_unconfirmed_at_resolution", slug=slug,
                           order_id=response.get("order_id"), response=response,
                           retry_suppressed=True, **pending_channel)
                self.stop_reason = self.stop_reason or "order_confirmation_failed"
                self.accepting_entries = False
                self._finish_stop()
                return
        super().resolve(row)
