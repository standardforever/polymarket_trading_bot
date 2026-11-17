"""FastAPI application serving live bot state and production controls."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .repository import BotRepository
from ..live import LiveStore
from ..sessions import TradingSessionManager, production_configuration
from ..account import PolymarketAccount


class TradingSessionCreate(BaseModel):
    """The production strategy is fixed; activation accepts no risk overrides."""
    model_config = ConfigDict(extra="forbid")

    def runtime_configuration(self):
        return {"name": None, **production_configuration()}


class RecoveredExecutionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class FundTransfer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["core_trading", "profit_trading", "safe_reserve"]
    destination: Literal["core_trading", "profit_trading", "safe_reserve"]
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=6)

    @model_validator(mode="after")
    def different_wallets(self):
        if self.source == self.destination:
            raise ValueError("Source and destination must be different")
        return self


class ManualOrderPreview(BaseModel):
    action: Literal["BUY", "SELL"]
    slug: str = Field(pattern=r"^btc-updown-5m-\d+$")
    outcome: Literal["Up", "Down"] | None = None
    token_id: str | None = None
    amount_usd: Decimal | None = Field(None, gt=0, max_digits=12, decimal_places=2)
    shares: Decimal | None = Field(None, gt=0, max_digits=18, decimal_places=6)
    slippage_bps: int = Field(100, ge=0, le=500)

    @model_validator(mode="after")
    def validate_sides(self):
        if self.action == "BUY" and (self.outcome is None or self.amount_usd is None):
            raise ValueError("BUY requires outcome and amount_usd")
        if self.action == "SELL" and (self.token_id is None or self.shares is None):
            raise ValueError("SELL requires token_id and shares")
        return self


class ManualOrderExecute(BaseModel):
    preview_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]+$")


def create_app(data_dir: Path | str = Path("data"), live_store: LiveStore | None = None,
               sessions: TradingSessionManager | None = None,
               account: PolymarketAccount | None = None) -> FastAPI:
    data = Path(data_dir)
    repository = BotRepository(data, account=account)
    app = FastAPI(title="BTC 5-Minute Agent API", version="1.0.0")
    app.add_middleware(CORSMiddleware, allow_origins=[
                           "http://localhost:3000", "http://127.0.0.1:3000",
                           "http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    app.state.repository = repository
    app.state.live_store = live_store
    app.state.sessions = sessions
    app.state.account = account

    def capabilities_payload():
        payload = repository.capabilities()
        payload["trading_session_runtime"] = sessions is not None
        payload["trading_session_active"] = bool(sessions and sessions.is_active())
        payload["live_trading"] = account is not None and account.configuration.complete
        payload["manual_orders"] = payload["live_trading"]
        payload["automatic_real_trading"] = bool(
            sessions is not None and payload["live_trading"])
        return payload

    @app.get("/health")
    def health():
        return repository.health()

    @app.get("/api/v1/capabilities")
    def capabilities():
        return capabilities_payload()

    @app.get("/api/v1/dashboard")
    def dashboard():
        return {"live": repository.live(), "statistics": repository.statistics(),
                "capabilities": capabilities_payload()}

    @app.get("/api/v1/live")
    def live():
        return repository.live()

    @app.get("/api/v1/live/chart")
    def chart(limit: int = Query(600, ge=10, le=5000)):
        return repository.chart(limit)

    @app.get("/api/v1/rounds")
    def rounds(limit: int = Query(50, ge=1, le=1000), offset: int = Query(0, ge=0),
               result: Literal["WIN", "LOSS", "SKIP", "PENDING"] | None = None,
               prediction: Literal["Up", "Down", "SKIP"] | None = None,
               traded: bool | None = None):
        rows = repository.rounds()
        if result is not None:
            rows = [row for row in rows if row["result"] == result]
        if prediction is not None:
            rows = [row for row in rows if row["prediction"] == prediction]
        if traded is not None:
            rows = [row for row in rows if row["traded"] is traded]
        return {"total": len(rows), "offset": offset, "limit": limit,
                "items": rows[offset:offset + limit]}

    @app.get("/api/v1/rounds/{slug}")
    def round_detail(slug: str):
        if not slug.startswith("btc-updown-5m-"):
            raise HTTPException(400, "Invalid BTC five-minute round slug")
        row = repository.round_detail(slug)
        if row is None:
            raise HTTPException(404, "Round not found")
        return row

    @app.get("/api/v1/trades")
    def trades(limit: int = Query(50, ge=1, le=1000), offset: int = Query(0, ge=0),
               status: Literal["WIN", "LOSS", "OPEN"] | None = None):
        rows = repository.trades()
        if status is not None:
            rows = [row for row in rows if row["status"] == status]
        return {"total": len(rows), "offset": offset, "limit": limit,
                "items": rows[offset:offset + limit]}

    @app.get("/api/v1/statistics")
    def statistics():
        return repository.statistics()

    @app.get("/api/v1/trading/sessions")
    def trading_session_list(
            limit: int = Query(10, ge=1, le=100),
            offset: int = Query(0, ge=0)):
        if sessions is not None:
            sessions.reconcile()
        rows = repository.trading_sessions()
        active_session = next((row for row in rows if row.get("status") not in {
            "STOPPED", "COMPLETED", "GAME_OVER"}), None)
        return {"items": rows[offset:offset + limit], "total": len(rows),
                "offset": offset, "limit": limit,
                "active": bool(sessions and sessions.is_active()),
                "active_session": active_session}

    @app.post("/api/v1/trading/sessions", status_code=201)
    def trading_session_start(request: TradingSessionCreate):
        if sessions is None:
            raise HTTPException(503, "Trading-session runtime is unavailable")
        try:
            metadata = sessions.start(request.runtime_configuration())
        except ValueError as error:
            messages = {
                "trading_session_already_active": "Another trading session is already active",
                "real_account_unavailable": "The real Polymarket account is unavailable",
                "real_account_allowance_unavailable": "The real account has no trading allowance",
                "real_account_balance_empty": "The real account has no available USDC",
                "real_account_balance_below_minimum": "The real account needs at least $1 to trade",
                "real_maximum_trade_below_minimum": "Maximum trade must be at least $1 for real trading",
                "real_trade_budget_below_minimum": "Allocation produces less than Polymarket's $1 minimum trade",
                "maximum_trade_usd_exceeds_balance": "Maximum trade exceeds the available real balance",
                "max_loss_usd_exceeds_balance": "Loss limit exceeds the available real balance",
                "maximum_trade_exceeds_bankroll": "Maximum trade exceeds the available real balance",
                "maximum_loss_exceeds_available_cash": "Loss limit exceeds the available real balance",
            }
            raise HTTPException(409, messages.get(str(error), str(error))) from error
        return repository.trading_session(metadata["session_id"])

    @app.get("/api/v1/trading/sessions/{session_id}")
    def trading_session_detail(
            session_id: str,
            trade_limit: int = Query(25, ge=1, le=250),
            trade_offset: int = Query(0, ge=0),
            event_limit: int = Query(25, ge=1, le=250),
            event_offset: int = Query(0, ge=0),
            trade_status: Literal["WIN", "LOSS", "OPEN"] | None = None):
        session = repository.trading_session(session_id)
        if session is None:
            raise HTTPException(404, "Trading session not found")
        trades = session.get("trades", [])
        if trade_status is not None:
            trades = [row for row in trades if row.get("status") == trade_status]
        trades = sorted(trades, key=lambda row: row.get("entry_ms") or 0, reverse=True)
        events = list(reversed(session.get("events", [])))
        session["trades"] = trades[trade_offset:trade_offset + trade_limit]
        session["events"] = events[event_offset:event_offset + event_limit]
        session["trades_page"] = {
            "total": len(trades), "offset": trade_offset, "limit": trade_limit,
        }
        session["events_page"] = {
            "total": len(events), "offset": event_offset, "limit": event_limit,
        }
        return session

    @app.post("/api/v1/trading/sessions/{session_id}/stop")
    def trading_session_stop(session_id: str):
        if sessions is None:
            raise HTTPException(503, "Trading-session runtime is unavailable")
        try:
            sessions.stop(session_id)
        except ValueError as error:
            raise HTTPException(409, "Trading session is not active") from error
        return {"session_id": session_id, "status": "STOPPING"}

    @app.get("/api/v1/trading/funds")
    def trading_funds(
            activity_limit: int = Query(20, ge=1, le=100),
            activity_offset: int = Query(0, ge=0)):
        if sessions is None:
            raise HTTPException(503, "Trading-session runtime is unavailable")
        return sessions.funds(
            activity_limit=activity_limit,
            activity_offset=activity_offset)

    @app.patch("/api/v1/trading/sessions/{session_id}/recovered-signals")
    def recovered_signals_update(session_id: str,
                                 request: RecoveredExecutionUpdate):
        if sessions is None:
            raise HTTPException(503, "Trading-session runtime is unavailable")
        try:
            return sessions.set_recovered_signals_enabled(
                session_id, request.enabled)
        except ValueError as error:
            raise HTTPException(409, "Trading session is not active") from error

    @app.post("/api/v1/trading/sessions/{session_id}/fund-transfers")
    def fund_transfer(session_id: str, request: FundTransfer):
        if sessions is None:
            raise HTTPException(503, "Trading-session runtime is unavailable")
        try:
            return sessions.transfer_funds(
                session_id, request.source, request.destination,
                float(request.amount))
        except ValueError as error:
            messages = {
                "trading_session_not_active": "Trading session is not active",
                "invalid_fund_transfer": "Choose two different valid wallets",
                "invalid_fund_transfer_amount": "Transfer amount must be positive",
                "fund_transfer_exceeds_balance": "Transfer exceeds the source wallet balance",
            }
            raise HTTPException(409, messages.get(str(error), str(error))) from error

    @app.get("/api/v1/account")
    def account_snapshot():
        if account is None:
            return {"configured": False, "connected": False,
                    "error": "Real-account runtime is unavailable", "positions": [],
                    "open_orders": []}
        return account.snapshot()

    @app.post("/api/v1/account/orders/preview")
    def manual_order_preview(request: ManualOrderPreview):
        if account is None:
            raise HTTPException(503, "Real-account runtime is unavailable")
        try:
            return account.preview(**request.model_dump())
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        except Exception as error:
            raise HTTPException(502, f"Polymarket rejected the preview: {str(error)[:240]}") from error

    @app.post("/api/v1/account/orders")
    def manual_order_execute(request: ManualOrderExecute):
        if account is None:
            raise HTTPException(503, "Real-account runtime is unavailable")
        try:
            result = account.execute(request.preview_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        except Exception as error:
            raise HTTPException(502, f"Polymarket rejected the order: {str(error)[:240]}") from error
        if result.get("ok") is False:
            raise HTTPException(409, result.get("message") or "Order was rejected")
        return result

    @app.websocket("/api/v1/ws/live")
    async def live_socket(socket: WebSocket):
        await socket.accept()
        previous = None
        try:
            while True:
                payload = repository.live()
                encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                if encoded != previous:
                    await socket.send_json(payload)
                    previous = encoded
                await asyncio.sleep(.5)
        except WebSocketDisconnect:
            return

    @app.websocket("/api/v1/ws/chart")
    async def chart_socket(socket: WebSocket):
        await socket.accept()
        previous = None
        try:
            while True:
                payload = repository.chart(600)
                encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                if encoded != previous:
                    await socket.send_json(payload)
                    previous = encoded
                await asyncio.sleep(.5)
        except WebSocketDisconnect:
            return

    @app.websocket("/api/v1/ws/quotes")
    async def quote_socket(socket: WebSocket):
        await socket.accept()
        revision = None
        try:
            while True:
                payload = live_store.market_snapshot() if live_store is not None else None
                if payload is not None and payload["revision"] != revision:
                    await socket.send_json(payload)
                    revision = payload["revision"]
                await asyncio.sleep(.05)
        except WebSocketDisconnect:
            return

    @app.websocket("/api/v1/ws/reference")
    async def reference_socket(socket: WebSocket):
        await socket.accept()
        revision = None
        try:
            while True:
                payload = live_store.reference_snapshot() if live_store is not None else None
                if payload is not None and payload["revision"] != revision:
                    await socket.send_json(payload)
                    revision = payload["revision"]
                await asyncio.sleep(.05)
        except WebSocketDisconnect:
            return

    @app.websocket("/api/v1/ws/account")
    async def account_socket(socket: WebSocket):
        await socket.accept()
        previous = None
        try:
            while True:
                payload = (await asyncio.to_thread(account.snapshot)
                           if account is not None else {"configured": False,
                           "connected": False, "positions": [], "open_orders": []})
                encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                if encoded != previous:
                    await socket.send_json(payload)
                    previous = encoded
                await asyncio.sleep(.5)
        except WebSocketDisconnect:
            return

    return app


app = create_app()
