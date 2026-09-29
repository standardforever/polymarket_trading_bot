"""Activated autonomous real-money execution for qualified BTC signals."""

from decimal import Decimal, ROUND_FLOOR

from .account import AutomaticOrderSubmissionError
from .paper import PaperTrader, risk_adjusted_allocation


class RealTrader(PaperTrader):
    """Use the paper strategy lifecycle while submitting bounded FOK buys."""

    def __init__(self, data, account, bankroll, **kwargs):
        self.account = account
        self.pending_orders = {}
        super().__init__(data, bankroll, execution_mode="real",
                         folder_name="real-trading/sessions",
                         file_prefix="real", **kwargs)

    def _available_cash(self, force=True):
        snapshot = self.account.snapshot(force=force)
        if not snapshot.get("connected"):
            raise ValueError(snapshot.get("error") or
                             "The Polymarket account is not connected")
        return float(snapshot.get("balance_usdc") or 0)

    def consider_quotes(self, candidate, record):
        observed_ms = int(record.get("received_ms", 0))
        if observed_ms < candidate["entry_start_ms"] or observed_ms >= candidate["deadline_ms"]:
            return
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
        signal_ask = candidate.get("signal_ask")
        adverse_limit = candidate.get("maximum_adverse_price_move")
        if (adverse_limit is not None and signal_ask is not None
                and float(signal_ask) - ask > float(adverse_limit) + 1e-12):
            candidate["rejections"]["adverse_price_move_requires_confirmation"] += 1
            return
        if ask > candidate["maximum_value_price"] + 1e-12:
            candidate["rejections"]["edge_below_minimum"] += 1
            return
        # The exchange method performs a lightweight collateral check and clamps
        # the order to the actual available cash. A full account snapshot here
        # also loads positions and open orders; on a live entry that delay can
        # turn the quote which passed the guard into a stale quote.
        cash = max(0.0, self.bankroll)
        budget = risk_adjusted_allocation(
            cash, self.allocation_fraction, self.maximum_trade_usd,
            candidate["risk_allocation_multiplier"])
        # Preserve a non-zero warning allocation while meeting the venue's
        # $1 market-buy minimum. Session activation guarantees the normal
        # allocation itself can fund this floor.
        if (0 < budget < 1 and cash >= 1
                and (self.maximum_trade_usd is None or self.maximum_trade_usd >= 1)):
            budget = 1.0
        budget = float(Decimal(str(budget)).quantize(
            Decimal("0.01"), rounding=ROUND_FLOOR))
        if budget <= 0:
            candidate["rejections"]["no_money"] += 1
            return
        visible = quote.get("ask_size")
        if visible is None or float(visible) + 1e-9 < budget / ask:
            candidate["rejections"]["insufficient_size"] += 1
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
                signal_ask=(Decimal(str(signal_ask))
                            if signal_ask is not None else None),
                maximum_adverse_price_move=(Decimal(str(adverse_limit))
                                             if adverse_limit is not None else None))
        except ValueError as error:  # Preflight failed before order submission.
            candidate["rejections"]["exchange_preflight"] += 1
            self.write("order_preflight_rejected", slug=slug, side=candidate["side"],
                       message=str(error)[:240], bankroll=cash)
            return
        except AutomaticOrderSubmissionError as error:
            # A timeout can hide an accepted exchange order. Never retry this slug.
            self.candidates.pop(slug, None)
            self._close_round_tail(candidate["start"])
            self.handled.add(slug)
            self.write("order_submission_uncertain", slug=slug,
                       side=candidate["side"], message=str(error)[:240],
                       retry_suppressed=True, bankroll=cash)
            self.request_stop("order_submission_uncertain")
            self.write_status("STOPPING", last_slug=slug,
                              last_result="SUBMISSION_UNCERTAIN",
                              reason="manual_reconciliation_required")
            return

        if result.get("ok") is False:
            self.write("order_rejected", slug=slug, side=candidate["side"],
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
                       retry_suppressed=True, bankroll=cash)
            self.pending_orders[slug] = {
                "candidate": {key: value for key, value in candidate.items()
                              if key != "rejections"},
                "response": result, "cash_before": cash,
                "budget": budget, "ask": ask, "observed_ms": observed_ms,
            }
            self.write_status("ORDER_PENDING_CONFIRMATION", last_slug=slug,
                              last_result="ORDER_PENDING_CONFIRMATION")
            return

        self._record_confirmed_fill(candidate, result, observed_ms, cash, budget, ask)

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
        self.bankroll = max(0.0, cash - cost)
        position = {key: value for key, value in candidate.items() if key != "rejections"}
        position.update(fill=fill, entry_ms=observed_ms,
                        entry_delay_seconds=(observed_ms - candidate["decision_ms"]) / 1000,
                        entry_remaining_seconds=(candidate["market_end_ms"] - observed_ms) / 1000,
                        exchange_response=result)
        self.positions[slug] = position
        self.trade_count += 1
        self.write("trade_filled", slug=slug, side=candidate["side"],
                   model_version=self.model_version,
                   forecast_probability=candidate["chance"],
                   maximum_value_price=candidate["maximum_value_price"],
                   entry_ms=observed_ms,
                   entry_delay_seconds=position["entry_delay_seconds"],
                   entry_remaining_seconds=position["entry_remaining_seconds"],
                   price=price, shares=shares, fee=fee, trade_cost=cost,
                   fee_rate=candidate["fee_rate"],
                   bankroll_after_entry=self.bankroll,
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
            try:
                confirmed = self.account.confirm_automatic_order(
                    str(response.get("order_id") or ""),
                    str(response.get("token_id") or ""))
            except Exception as error:
                confirmed = None
                self.write("order_confirmation_error", slug=slug,
                           order_id=response.get("order_id"),
                           message=str(error)[:240])
            if confirmed is not None:
                recovered = {**response, **confirmed, "ok": True}
                self._record_confirmed_fill(
                    pending["candidate"], recovered, pending["observed_ms"],
                    pending["cash_before"], pending["budget"], pending["ask"])
                self.write("order_confirmation_recovered", slug=slug,
                           order_id=response.get("order_id"), response=recovered)
            else:
                self.write("order_unconfirmed_at_resolution", slug=slug,
                           order_id=response.get("order_id"), response=response,
                           retry_suppressed=True)
                self.stop_reason = self.stop_reason or "order_confirmation_failed"
                self.accepting_entries = False
                self._finish_stop()
                return
        super().resolve(row)
