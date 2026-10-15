"""Authenticated Polymarket account reads and bounded order execution."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any, Literal

from eth_account import Account

try:
    from polymarket import SecureClient
    from polymarket.clients._transport import SyncTransport
    from polymarket.environments import PRODUCTION
    from polymarket._internal.actions.relayer.deployed import fetch_deployed_sync
    from polymarket._internal.wallet import (
        derive_beacon_deposit_wallet_address,
        derive_uups_deposit_wallet_address,
    )
    from polymarket.models.clob import ApiKeyCreds
    from polymarket.models.clob.relayer import RelayerTransactionType
    from polymarket.errors import TimeoutError as PolymarketTimeoutError
    from polymarket.errors import (
        InsufficientAllowanceError,
        InsufficientLiquidityError,
        RateLimitError,
        RequestRejectedError,
        SigningError,
        TransactionFailedError,
        UserInputError,
    )
except ImportError:  # Keeps the read-only API usable until dependencies are installed.
    SecureClient = None
    ApiKeyCreds = None
    PolymarketTimeoutError = TimeoutError
    TransactionFailedError = RuntimeError
    InsufficientAllowanceError = RuntimeError
    InsufficientLiquidityError = RuntimeError
    RateLimitError = RuntimeError
    RequestRejectedError = RuntimeError
    SigningError = RuntimeError
    UserInputError = RuntimeError


MICRO_USDC = Decimal(1_000_000)
PREVIEW_SECONDS = 12
MINIMUM_MARKET_BUY_USD = Decimal("1")


class AutomaticOrderSubmissionError(RuntimeError):
    """The exchange submission was attempted, so callers must not retry it."""


def _load_local_env(path: Path = Path(".env")) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def _first(env: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = os.environ.get(name) or env.get(name)
        if value:
            return value.strip()
    return None


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


@dataclass(frozen=True)
class AccountConfiguration:
    private_key: str | None
    wallet: str | None
    api_key: str | None
    api_secret: str | None
    api_passphrase: str | None

    @classmethod
    def read(cls) -> "AccountConfiguration":
        env = _load_local_env()
        private_key = _first(env, "POLYMARKET_PRIVATE_KEY", "PK")
        wallet = _first(env, "POLYMARKET_WALLET", "POLYMARKET_DEPOSIT_WALLET", "FUNDER")
        return cls(
            private_key=private_key,
            wallet=wallet,
            api_key=_first(env, "POLYMARKET_API_KEY", "CLOB_API_KEY", "api_key"),
            api_secret=_first(env, "POLYMARKET_API_SECRET", "CLOB_SECRET", "api_secret"),
            api_passphrase=_first(
                env, "POLYMARKET_API_PASSPHRASE", "CLOB_PASS_PHRASE", "api_passphrase"
            ),
        )

    @property
    def complete(self) -> bool:
        return all((self.private_key, self.api_key, self.api_secret, self.api_passphrase))


class PolymarketAccount:
    """Thread-safe account facade for manual and activated automated orders."""

    def __init__(self, data: Path, live_store, client_factory=None, clock=time.time,
                 configuration: AccountConfiguration | None = None):
        self.data = data
        self.live_store = live_store
        self.clock = clock
        self.configuration = configuration or AccountConfiguration.read()
        self.client_factory = client_factory
        self.client = None
        self.lock = threading.RLock()
        self.previews: dict[str, dict[str, Any]] = {}
        self.cached_account: dict[str, Any] | None = None
        self.cached_at = 0.0
        self.error: str | None = None
        self.audit_path = data / "real-trading" / "manual-orders.jsonl"

    def _connect(self):
        if self.client is not None:
            return self.client
        if not self.configuration.complete:
            raise RuntimeError("Real account is not fully configured in .env")
        if self.client_factory is not None:
            self.client = self.client_factory(self.configuration)
            return self.client
        if SecureClient is None or ApiKeyCreds is None:
            raise RuntimeError("Install polymarket-client before connecting a real account")
        credentials = ApiKeyCreds(
            apiKey=self.configuration.api_key,
            secret=self.configuration.api_secret,
            passphrase=self.configuration.api_passphrase,
        )
        wallet = self.configuration.wallet or self._existing_default_wallet()
        # Supplying the wallet explicitly prevents the SDK from selecting or
        # deploying a different deposit wallet implicitly.
        self.client = SecureClient.create(
            private_key=self.configuration.private_key,
            wallet=wallet,
            credentials=credentials,
        )
        return self.client

    def _existing_default_wallet(self):
        """Use an existing SDK deposit wallet; never deploy one during a read."""
        signer = Account.from_key(self.configuration.private_key).address
        legacy = derive_uups_deposit_wallet_address(signer, PRODUCTION.wallet_derivation)
        current = derive_beacon_deposit_wallet_address(signer, PRODUCTION.wallet_derivation)
        relayer = SyncTransport(base_url=PRODUCTION.relayer_url)
        try:
            if fetch_deployed_sync(relayer, address=legacy,
                                   type=RelayerTransactionType.WALLET):
                return legacy
            if fetch_deployed_sync(relayer, address=current,
                                   type=RelayerTransactionType.WALLET):
                return current
        finally:
            relayer.close()
        # An externally owned wallet needs no deployment and is the safe
        # fallback for accounts created outside the Polymarket web app.
        return signer

    def close(self):
        with self.lock:
            if self.client is not None and hasattr(self.client, "close"):
                self.client.close()
            self.client = None

    def _audit(self, event: str, **fields):
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"event": event, "recorded_ms": int(self.clock() * 1000), **fields}
        with self.audit_path.open("a", buffering=1) as handle:
            handle.write(json.dumps(_plain(record), separators=(",", ":")) + "\n")

    def _remote_snapshot(self):
        client = self._connect()
        collateral = client.get_balance_allowance(asset_type="COLLATERAL")
        positions = [item for item in client.list_positions(page_size=100).iter_items()
                     if (Decimal(str(item.current_value or 0)) > 0
                         or item.redeemable is not True)]
        orders = list(client.list_open_orders().iter_items())
        allowances = {str(key): str(Decimal(value) / MICRO_USDC)
                      for key, value in collateral.allowances.items()}
        return {
            "connected": True,
            "wallet": str(client.wallet),
            "signer": str(client.signer),
            "wallet_type": str(client.wallet_type),
            "balance_usdc": str(Decimal(collateral.balance) / MICRO_USDC),
            "allowance_ready": any(value > 0 for value in collateral.allowances.values()),
            "collateral_allowances_usdc": allowances,
            "positions": [self._position(item) for item in positions],
            "open_orders": [_plain(item) for item in orders],
        }

    @staticmethod
    def _position(position):
        row = _plain(position)
        return {
            "condition_id": row.get("condition_id"),
            "token_id": (row.get("token_id") or row.get("asset_id")
                         or row.get("position_id")),
            "slug": row.get("slug"),
            "title": row.get("title"),
            "outcome": row.get("outcome"),
            "size": row.get("size") or row.get("current_size"),
            "avg_price": row.get("avg_price"),
            "initial_value": row.get("initial_value"),
            "current_value": row.get("current_value"),
            "current_price": row.get("cur_price") or row.get("current_price"),
            "cash_pnl": row.get("cash_pnl"),
            "percent_pnl": row.get("percent_pnl"),
            "realized_pnl": row.get("realized_pnl"),
            "redeemable": row.get("redeemable"),
            "end_date": row.get("end_date"),
        }

    def snapshot(self, *, force=False):
        with self.lock:
            configured = self.configuration.complete
            if configured and (force or self.cached_account is None
                               or self.clock() - self.cached_at >= 2):
                try:
                    self.cached_account = self._remote_snapshot()
                    self.cached_at = self.clock()
                    self.error = None
                except Exception as error:  # API returns a useful state instead of crashing.
                    self.error = f"{type(error).__name__}: {str(error)[:240]}"
            base = dict(self.cached_account or {
                "connected": False,
                "wallet": self.configuration.wallet,
                "signer": None,
                "wallet_type": None,
                "balance_usdc": None,
                "allowance_ready": False,
                "positions": [],
                "open_orders": [],
            })
            if self.error:
                base["connected"] = False
            base.update({
                "configured": configured,
                "error": self.error,
                "updated_ms": int(self.cached_at * 1000) if self.cached_at else None,
            })
            base["positions"] = self._mark_positions(base["positions"])
            portfolio = sum((Decimal(str(row.get("current_value") or 0))
                             for row in base["positions"]), Decimal(0))
            cash = Decimal(str(base.get("balance_usdc") or 0))
            base["portfolio_value_usdc"] = str(portfolio)
            base["account_value_usdc"] = str(cash + portfolio)
            base["current_market"] = self._market_state()
            return base

    def _market_state(self):
        market = self.live_store.market_snapshot() if self.live_store is not None else None
        if not market:
            return {"tradable": False, "reason": "Waiting for the live market"}
        remaining = max(0.0, float(market.get("remaining_seconds") or 0))
        age = int(self.clock() * 1000) - int(market.get("received_ms") or 0)
        tradable = (remaining > 0 and age <= 3_000 and market.get("accepting_orders", True)
                    and not market.get("closed", False))
        reason = None
        if remaining <= 0 or market.get("closed"):
            reason = "This market has closed"
        elif not market.get("accepting_orders", True):
            reason = "Polymarket is no longer accepting orders"
        elif age > 3_000:
            reason = "The live order book is stale"
        return {
            "slug": market.get("slug"), "remaining_seconds": remaining,
            "tradable": tradable, "reason": reason,
        }

    def _mark_positions(self, positions):
        market = self.live_store.market_snapshot() if self.live_store is not None else None
        if not market:
            return positions
        labels = market.get("tokens") or {}
        quotes = market.get("quotes") or {}
        for original in positions:
            row = dict(original)
            token = row.get("token_id")
            label = labels.get(str(token))
            quote = quotes.get(label, {}) if label else {}
            bid = quote.get("bid")
            if market.get("slug") == row.get("slug") and bid is not None:
                size = Decimal(str(row.get("size") or 0))
                average = Decimal(str(row.get("avg_price") or 0))
                value = size * Decimal(str(bid))
                pnl = value - size * average
                row.update(current_price=str(bid), current_value=str(value),
                           cash_pnl=str(pnl), live_mark=True,
                           tradable=self._market_state()["tradable"])
            else:
                row["live_mark"] = False
                row["tradable"] = False
            original.clear()
            original.update(row)
        return positions

    def _assert_market(self, slug: str):
        market = self._assert_live_market(slug)
        remote = self._connect().get_market(slug=slug)
        if remote.state.closed or remote.state.accepting_orders is False:
            raise ValueError("Polymarket is no longer accepting orders for this market")
        if remote.state.end_date and remote.state.end_date.timestamp() <= self.clock():
            raise ValueError("This market has closed")
        return market, remote

    def _assert_live_market(self, slug: str):
        """Validate the millisecond live state without a duplicate Gamma request."""
        market = self.live_store.market_snapshot() if self.live_store is not None else None
        state = self._market_state()
        if not market or market.get("slug") != slug:
            raise ValueError("The requested round is no longer current")
        if not state["tradable"]:
            raise ValueError(state.get("reason") or "The market is not tradable")
        return market

    def preview(self, *, action: Literal["BUY", "SELL"], slug: str,
                outcome: str | None = None, token_id: str | None = None,
                amount_usd: Decimal | None = None, shares: Decimal | None = None,
                slippage_bps: int = 100):
        with self.lock:
            market, remote = self._assert_market(slug)
            labels = market.get("tokens") or {}
            if action == "BUY":
                token_id = next((token for token, label in labels.items()
                                 if label.lower() == str(outcome).lower()), None)
                if token_id is None or amount_usd is None or amount_usd <= 0:
                    raise ValueError("Choose Up or Down and enter a positive dollar amount")
                if amount_usd < MINIMUM_MARKET_BUY_USD:
                    raise ValueError("Polymarket market buys require at least $1")
                account = self.snapshot(force=True)
                if not account.get("connected"):
                    raise ValueError("The Polymarket account is not connected")
                if amount_usd > Decimal(str(account.get("balance_usdc") or 0)):
                    raise ValueError("Buy amount exceeds the available USDC balance")
                if not account.get("allowance_ready"):
                    raise ValueError("The account does not have a trading allowance")
                estimated = self._connect().estimate_market_price(
                    token_id=token_id, side="BUY", amount=amount_usd, order_type="FOK")
                raw_bound = estimated * (Decimal(1) + Decimal(slippage_bps) /
                                         Decimal(10_000))
                estimated_shares = amount_usd / estimated
                requested = str(amount_usd)
                outcome = labels[token_id]
            else:
                if not token_id or token_id not in labels or shares is None or shares <= 0:
                    raise ValueError("Choose a current position and enter positive shares")
                account = self.snapshot(force=True)
                position = next((row for row in account["positions"]
                                 if str(row.get("token_id")) == token_id), None)
                if position is None or shares > Decimal(str(position.get("size") or 0)):
                    raise ValueError("Sell shares exceed the current position")
                estimated = self._connect().estimate_market_price(
                    token_id=token_id, side="SELL", shares=shares, order_type="FOK")
                raw_bound = estimated * (Decimal(1) - Decimal(slippage_bps) /
                                         Decimal(10_000))
                estimated_shares = shares
                requested = str(shares)
                outcome = labels[token_id]
            tick = remote.trading.minimum_tick_size or Decimal("0.01")
            rounding = ROUND_CEILING if action == "BUY" else ROUND_FLOOR
            price_bound = (raw_bound / tick).to_integral_value(rounding=rounding) * tick
            price_bound = (min(Decimal(1) - tick, price_bound) if action == "BUY"
                           else max(tick, price_bound))
            now = self.clock()
            self.previews = {key: item for key, item in self.previews.items()
                             if not item["used"] and item["expires_at"] >= now}
            preview_id = uuid.uuid4().hex
            record = {
                "preview_id": preview_id, "action": action, "slug": slug,
                "outcome": outcome, "token_id": str(token_id), "requested": requested,
                "estimated_price": str(estimated), "estimated_shares": str(estimated_shares),
                "price_bound": str(price_bound),
                "slippage_bps": slippage_bps, "created_at": now,
                "expires_at": now + PREVIEW_SECONDS, "used": False,
            }
            self.previews[preview_id] = record
            return {key: value for key, value in record.items()
                    if key not in ("token_id", "created_at", "used")}

    def execute(self, preview_id: str):
        with self.lock:
            preview = self.previews.get(preview_id)
            if preview is None:
                raise ValueError("Order preview was not found")
            if preview["used"]:
                raise ValueError("This order preview has already been submitted")
            if self.clock() > preview["expires_at"]:
                raise ValueError("Order preview expired; review the current price again")
            self._assert_market(preview["slug"])
            preview["used"] = True  # one attempt only, including exchange rejections
            self._audit("order_submitted", preview_id=preview_id,
                        action=preview["action"], slug=preview["slug"],
                        outcome=preview["outcome"], requested=preview["requested"],
                        price_bound=preview["price_bound"])
            try:
                if preview["action"] == "BUY":
                    response = self._connect().place_market_order(
                        token_id=preview["token_id"], side="BUY",
                        amount=preview["requested"],
                        max_price=preview["price_bound"], order_type="FOK")
                else:
                    response = self._connect().place_market_order(
                        token_id=preview["token_id"], side="SELL",
                        shares=preview["requested"], min_price=preview["price_bound"],
                        order_type="FOK")
                result = _plain(response)
                self._audit("order_result", preview_id=preview_id, response=result)
                self.cached_at = 0
                return result
            except Exception as error:
                self._audit("order_error", preview_id=preview_id,
                            error_type=type(error).__name__, message=str(error)[:240])
                raise

    def automatic_buy(self, *, run_id: str, slug: str, outcome: str,
                      amount_usd: Decimal, maximum_price: Decimal,
                      signal_ask: Decimal | None = None,
                      maximum_adverse_price_move: Decimal | None = None,
                      minimum_entry_price: Decimal | None = None):
        """Submit one bounded FOK buy for an already-activated real session."""
        with self.lock:
            started = time.monotonic()
            market = self._assert_live_market(slug)
            labels = market.get("tokens") or {}
            token_id = next((token for token, label in labels.items()
                             if label.lower() == outcome.lower()), None)
            if token_id is None:
                raise ValueError("The predicted outcome token is unavailable")
            client = self._connect()
            balance_started = time.monotonic()
            collateral = client.get_balance_allowance(asset_type="COLLATERAL")
            balance_latency_ms = round((time.monotonic() - balance_started) * 1000, 3)
            available = Decimal(collateral.balance) / MICRO_USDC
            amount = min(available, Decimal(amount_usd)).quantize(
                Decimal("0.01"), rounding=ROUND_FLOOR)
            if amount <= 0:
                raise ValueError("No available USDC remains for this order")
            if amount < MINIMUM_MARKET_BUY_USD:
                raise ValueError("Polymarket market buys require at least $1")
            # Re-read the shared live book after the balance request. This is
            # the final local check before signing/submitting, so a contract
            # which has fallen too far from the original signal remains a
            # candidate instead of being bought from a stale trigger quote.
            fresh_market = self._assert_live_market(slug)
            fresh_quote = (fresh_market.get("quotes") or {}).get(outcome, {})
            if not fresh_quote.get("ready") or fresh_quote.get("ask") is None:
                raise ValueError("The latest executable price is unavailable")
            fresh_ask = Decimal(str(fresh_quote["ask"]))
            if minimum_entry_price is not None and fresh_ask <= minimum_entry_price:
                raise ValueError(
                    f"Current executable price must be above the {minimum_entry_price} entry floor")
            if (signal_ask is not None and maximum_adverse_price_move is not None
                    and signal_ask - fresh_ask > maximum_adverse_price_move):
                limit_cents = (maximum_adverse_price_move * 100).normalize()
                raise ValueError(
                    "Current executable price is more than "
                    f"{limit_cents} cents below the signal price")
            if fresh_ask > maximum_price:
                raise ValueError("Current executable price exceeds the strategy price limit")
            # One cent is always on the BTC Up/Down tick grid (including the
            # finer 0.001 grid used near the price extremes). The SDK verifies
            # the current tick again while constructing the signed order.
            tick = Decimal("0.01")
            price_bound = (Decimal(maximum_price) / tick).to_integral_value(
                rounding=ROUND_FLOOR) * tick
            price_bound = min(Decimal(1) - tick, max(tick, price_bound))
            order_id = uuid.uuid4().hex
            pre_submit_latency_ms = round((time.monotonic() - started) * 1000, 3)
            order = {"token_id": token_id, "side": "BUY", "amount": str(amount),
                     "max_price": str(price_bound), "order_type": "FOK"}
            # At the $1 venue minimum, max_spend=$1 would cause the SDK to
            # reserve fees inside the cap and submit less than $1.
            if amount > Decimal("1.10"):
                order["max_spend"] = str(amount)

            signed_order = None
            signed_price = None
            build_latency_ms = None
            if (minimum_entry_price is not None or
                    (signal_ask is not None and maximum_adverse_price_move is not None)):
                # Building the market order asks the SDK for its authoritative
                # executable book and can take seconds. Check the price encoded
                # in that signed order before posting it; the shared stream may
                # still show the older quote during a fast collapse.
                build_started = time.monotonic()
                signed_order = client.create_market_order(**order)
                build_latency_ms = round((time.monotonic() - build_started) * 1000, 3)
                maker_amount = Decimal(str(signed_order.maker_amount))
                taker_amount = Decimal(str(signed_order.taker_amount))
                if taker_amount <= 0:
                    raise ValueError("The signed order has no executable shares")
                signed_price = maker_amount / taker_amount
                if minimum_entry_price is not None and signed_price <= minimum_entry_price:
                    raise ValueError(
                        f"Signed executable price must be above the {minimum_entry_price} entry floor")
                if (signal_ask is not None and maximum_adverse_price_move is not None
                        and signal_ask - signed_price > maximum_adverse_price_move):
                    limit_cents = (maximum_adverse_price_move * 100).normalize()
                    raise ValueError(
                        "Signed executable price is more than "
                        f"{limit_cents} cents below the signal price")
                if signed_price > maximum_price:
                    raise ValueError("Signed executable price exceeds the strategy price limit")

            self._audit("automatic_order_submitted", order_id=order_id,
                        run_id=run_id, slug=slug, outcome=outcome,
                        amount_usd=str(amount), maximum_price=str(price_bound),
                        signal_ask=(str(signal_ask) if signal_ask is not None else None),
                        preflight_ask=str(fresh_ask),
                        maximum_adverse_price_move=(
                            str(maximum_adverse_price_move)
                            if maximum_adverse_price_move is not None else None),
                        minimum_entry_price=(str(minimum_entry_price)
                                             if minimum_entry_price is not None else None),
                        signed_price=(str(signed_price)
                                      if signed_price is not None else None),
                        balance_latency_ms=balance_latency_ms,
                        order_build_latency_ms=build_latency_ms,
                        pre_submit_latency_ms=pre_submit_latency_ms)
            try:
                submission_started = time.monotonic()
                response = (client.post_order(signed_order) if signed_order is not None
                            else client.place_market_order(**order))
                result = _plain(response)
                result["submission_latency_ms"] = round(
                    (time.monotonic() - submission_started) * 1000, 3)
                result["total_latency_ms"] = round((time.monotonic() - started) * 1000, 3)
                if result.get("ok") is False:
                    result["retryable"] = result.get("code") in {
                        "fok_not_filled", "unmatched", "market_not_ready"
                    }
                initial_status = str(result.get("status") or "").lower()
                if result.get("ok") and str(result.get("status") or "").lower() == "delayed":
                    confirmed = self._wait_for_delayed_fill(
                        client, result.get("order_id"), str(token_id), timeout_seconds=10)
                    if confirmed is not None:
                        result.update(confirmed)
                if (result.get("ok") and initial_status == "matched"
                        and hasattr(client, "wait_for_order_fill_settlement")):
                    try:
                        hashes = client.wait_for_order_fill_settlement(response, timeout_s=8)
                        result["settlement_status"] = "confirmed"
                        result["settlement_hashes"] = [str(value) for value in hashes]
                    except PolymarketTimeoutError:
                        # The FOK fill is accepted; on-chain settlement can finish later.
                        result["settlement_status"] = "pending"
                    except TransactionFailedError as error:
                        result = {**result, "ok": False,
                                  "code": "fill_settlement_failed",
                                  "message": str(error)[:240],
                                  "settlement_status": "failed"}
                self._audit("automatic_order_result", order_id=order_id,
                            run_id=run_id, slug=slug, response=result)
                self.cached_at = 0
                return {"automatic_order_id": order_id, "token_id": str(token_id),
                        "requested_amount": str(amount),
                        "maximum_price": str(price_bound),
                        "preflight_ask": str(fresh_ask), **result}
            except (InsufficientLiquidityError, RateLimitError,
                    RequestRejectedError) as error:
                result = {
                    "ok": False,
                    "code": type(error).__name__,
                    "message": str(error)[:240],
                    "retryable": True,
                    "submission_latency_ms": round(
                        (time.monotonic() - submission_started) * 1000, 3),
                    "total_latency_ms": round((time.monotonic() - started) * 1000, 3),
                }
                self._audit("automatic_order_result", order_id=order_id,
                            run_id=run_id, slug=slug, response=result)
                self.cached_at = 0
                return {"automatic_order_id": order_id, "token_id": str(token_id),
                        "requested_amount": str(amount),
                        "maximum_price": str(price_bound),
                        "preflight_ask": str(fresh_ask), **result}
            except (InsufficientAllowanceError, SigningError, UserInputError) as error:
                result = {
                    "ok": False,
                    "code": type(error).__name__,
                    "message": str(error)[:240],
                    "retryable": False,
                    "submission_latency_ms": round(
                        (time.monotonic() - submission_started) * 1000, 3),
                    "total_latency_ms": round((time.monotonic() - started) * 1000, 3),
                }
                self._audit("automatic_order_result", order_id=order_id,
                            run_id=run_id, slug=slug, response=result)
                self.cached_at = 0
                return {"automatic_order_id": order_id, "token_id": str(token_id),
                        "requested_amount": str(amount),
                        "maximum_price": str(price_bound),
                        "preflight_ask": str(fresh_ask), **result}
            except Exception as error:
                self._audit("automatic_order_error", order_id=order_id,
                            run_id=run_id, slug=slug,
                            error_type=type(error).__name__, message=str(error)[:240])
                self.cached_at = 0
                raise AutomaticOrderSubmissionError(str(error)) from error

    def _wait_for_delayed_fill(self, client, order_id, token_id, timeout_seconds=10):
        """Resolve an accepted delayed FOK from authenticated account trades."""
        if not order_id or not hasattr(client, "list_account_trades"):
            return None
        deadline = time.monotonic() + timeout_seconds
        first_attempt = True
        while first_attempt or time.monotonic() < deadline:
            first_attempt = False
            try:
                trades = list(client.list_account_trades(
                    token_id=token_id).iter_items())
            except Exception:
                trades = []
            matched = []
            for trade in trades:
                row = _plain(trade)
                maker_ids = [str(item.get("order_id") or item.get("orderId") or "")
                             for item in row.get("maker_orders") or []]
                if (str(row.get("taker_order_id") or "") == str(order_id)
                        or str(order_id) in maker_ids):
                    matched.append(row)
            valid = [row for row in matched
                     if str(row.get("status") or "").upper() != "FAILED"]
            if valid:
                shares = sum((Decimal(str(row.get("size") or 0)) for row in valid), Decimal(0))
                principal = sum((Decimal(str(row.get("size") or 0))
                                 * Decimal(str(row.get("price") or 0))
                                 for row in valid), Decimal(0))
                return {
                    "status": "matched", "making_amount": str(principal),
                    "taking_amount": str(shares),
                    "trade_ids": [str(row.get("id")) for row in valid if row.get("id")],
                    "delayed_confirmation": True,
                    "settlement_status": ("confirmed" if all(
                        str(row.get("status") or "").upper() == "CONFIRMED"
                        for row in valid) else "pending"),
                }
            time.sleep(.25)
        return None

    def confirm_automatic_order(self, order_id: str, token_id: str):
        """Read authenticated trades once to reconcile a delayed FOK order."""
        with self.lock:
            return self._wait_for_delayed_fill(
                self._connect(), order_id, token_id, timeout_seconds=0)
