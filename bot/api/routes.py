"""FastAPI application serving live bot state and production controls."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator

from .repository import BotRepository
from ..live import LiveStore
from ..models import DEFAULT_MODEL, model_releases, validate_model_version
from ..paper_sessions import PaperSessionManager
from ..account import PolymarketAccount


class PaperSessionCreate(BaseModel):
    mode: Literal["paper", "real"] = "paper"
    model_version: str = DEFAULT_MODEL
    name: str | None = Field(None, max_length=60)
    starting_bankroll: float | None = Field(None, gt=0, le=1_000_000)
    allocation_percent: float = Field(80, gt=0, le=100)
    maximum_trade_usd: float | None = Field(None, gt=0, le=1_000_000)
    confidence_percent: float = Field(90, ge=50, le=98)
    minimum_edge_percent: float = Field(3, ge=0, lt=100)
    entry_cutoff_seconds: float = Field(30, ge=5, lt=110)
    max_trades: int | None = Field(None, ge=1, le=288)
    target_profit_usd: float | None = Field(None, gt=0, le=1_000_000)
    max_loss_usd: float | None = Field(None, gt=0, le=1_000_000)
    thin_gap_allocation_percent: float = Field(25, ge=0, le=100)
    movement_disagreement_allocation_percent: float = Field(50, ge=0, le=100)

    @model_validator(mode="after")
    def validate_session_limits(self):
        try:
            validate_model_version(self.model_version)
        except ValueError as error:
            raise ValueError("Unknown probability model release") from error
        if self.mode == "paper" and self.starting_bankroll is None:
            raise ValueError("paper mode requires a starting bankroll")
        if (self.mode == "real" and self.maximum_trade_usd is not None
                and self.maximum_trade_usd < 1):
            raise ValueError("real maximum trade must be at least $1")
        if (self.starting_bankroll is not None and self.maximum_trade_usd is not None
                and self.maximum_trade_usd > self.starting_bankroll):
            raise ValueError("maximum trade value cannot exceed starting bankroll")
        if (self.starting_bankroll is not None and self.max_loss_usd is not None
                and self.max_loss_usd > self.starting_bankroll):
            raise ValueError("maximum loss cannot exceed starting bankroll")
        if self.name is not None:
            self.name = self.name.strip() or None
        return self

    def runtime_configuration(self):
        return {
            "mode": self.mode,
            "model_version": self.model_version,
            "name": self.name,
            "starting_bankroll": self.starting_bankroll,
            "allocation_fraction": self.allocation_percent / 100,
            "maximum_trade_usd": self.maximum_trade_usd,
            "model_threshold": self.confidence_percent / 100,
            "minimum_edge": self.minimum_edge_percent / 100,
            "entry_cutoff_seconds": self.entry_cutoff_seconds,
            "max_trades": self.max_trades,
            "target_profit_usd": self.target_profit_usd,
            "max_loss_usd": self.max_loss_usd,
            "thin_gap_allocation_fraction": self.thin_gap_allocation_percent / 100,
            "movement_disagreement_allocation_fraction": (
                self.movement_disagreement_allocation_percent / 100),
        }


class PaperSessionUpdate(BaseModel):
    name: str | None = Field(None, max_length=60)
    allocation_percent: float | None = Field(None, gt=0, le=100)
    maximum_trade_usd: float | None = Field(None, gt=0, le=1_000_000)
    confidence_percent: float | None = Field(None, ge=50, le=98)
    minimum_edge_percent: float | None = Field(None, ge=0, lt=100)
    entry_cutoff_seconds: float | None = Field(None, ge=5, lt=110)
    max_trades: int | None = Field(None, ge=1, le=288)
    target_profit_usd: float | None = Field(None, gt=0, le=1_000_000)
    max_loss_usd: float | None = Field(None, gt=0, le=1_000_000)
    thin_gap_allocation_percent: float | None = Field(None, ge=0, le=100)
    movement_disagreement_allocation_percent: float | None = Field(None, ge=0, le=100)

    @model_validator(mode="after")
    def validate_required_values(self):
        required = {"allocation_percent", "confidence_percent",
                    "minimum_edge_percent", "entry_cutoff_seconds",
                    "thin_gap_allocation_percent",
                    "movement_disagreement_allocation_percent"}
        for field in required & self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be cleared")
        if "name" in self.model_fields_set and self.name is not None:
            self.name = self.name.strip() or None
        return self

    def runtime_changes(self):
        source = self.model_dump(exclude_unset=True)
        mapped = {
            "allocation_percent": "allocation_fraction",
            "confidence_percent": "model_threshold",
            "minimum_edge_percent": "minimum_edge",
            "thin_gap_allocation_percent": "thin_gap_allocation_fraction",
            "movement_disagreement_allocation_percent": (
                "movement_disagreement_allocation_fraction"),
        }
        changes = {}
        for key, value in source.items():
            if key == "name":
                changes[key] = value
            elif key in ("allocation_percent", "confidence_percent",
                          "minimum_edge_percent", "thin_gap_allocation_percent",
                          "movement_disagreement_allocation_percent"):
                changes[mapped[key]] = value / 100
            else:
                changes[key] = value
        return changes


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
               paper_sessions: PaperSessionManager | None = None,
               account: PolymarketAccount | None = None) -> FastAPI:
    data = Path(data_dir)
    repository = BotRepository(data)
    app = FastAPI(title="BTC 5-Minute Agent API", version="1.0.0")
    app.add_middleware(CORSMiddleware, allow_origins=[
                           "http://localhost:3000", "http://127.0.0.1:3000",
                           "http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    app.state.repository = repository
    app.state.live_store = live_store
    app.state.paper_sessions = paper_sessions
    app.state.account = account

    def capabilities_payload():
        payload = repository.capabilities()
        payload["paper_session_runtime"] = paper_sessions is not None
        payload["paper_session_active"] = bool(paper_sessions and paper_sessions.is_active())
        payload["live_trading"] = account is not None and account.configuration.complete
        payload["manual_orders"] = payload["live_trading"]
        payload["automatic_real_trading"] = bool(
            paper_sessions is not None and payload["live_trading"])
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
                "capabilities": capabilities_payload(),
                "models": model_releases(), "default_model": DEFAULT_MODEL}

    @app.get("/api/v1/models")
    def models():
        return {"items": model_releases(), "default": DEFAULT_MODEL}

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

    @app.get("/api/v1/paper/sessions")
    def paper_session_list(
            limit: int = Query(10, ge=1, le=100),
            offset: int = Query(0, ge=0)):
        if paper_sessions is not None:
            paper_sessions.reconcile()
        rows = repository.paper_sessions()
        active_session = next((row for row in rows if row.get("status") not in {
            "STOPPED", "COMPLETED", "GAME_OVER"}), None)
        return {"items": rows[offset:offset + limit], "total": len(rows),
                "offset": offset, "limit": limit,
                "active": bool(paper_sessions and paper_sessions.is_active()),
                "active_session": active_session}

    @app.post("/api/v1/paper/sessions", status_code=201)
    def paper_session_start(request: PaperSessionCreate):
        if paper_sessions is None:
            raise HTTPException(503, "Paper-session runtime is unavailable")
        if request.starting_bankroll is None:
            raise HTTPException(422, "Paper mode requires a starting bankroll")
        try:
            configuration = request.runtime_configuration()
            configuration["mode"] = "paper"
            metadata = paper_sessions.start(configuration)
        except ValueError as error:
            if str(error) == "paper_session_already_active":
                raise HTTPException(409, "Another paper session is already active") from error
            raise HTTPException(422, str(error)) from error
        return repository.paper_session(metadata["session_id"])

    @app.get("/api/v1/trading/sessions")
    def trading_session_list(
            limit: int = Query(10, ge=1, le=100),
            offset: int = Query(0, ge=0)):
        if paper_sessions is not None:
            paper_sessions.reconcile()
        rows = repository.trading_sessions()
        active_session = next((row for row in rows if row.get("status") not in {
            "STOPPED", "COMPLETED", "GAME_OVER"}), None)
        return {"items": rows[offset:offset + limit], "total": len(rows),
                "offset": offset, "limit": limit,
                "active": bool(paper_sessions and paper_sessions.is_active()),
                "active_session": active_session}

    @app.post("/api/v1/trading/sessions", status_code=201)
    def trading_session_start(request: PaperSessionCreate):
        if paper_sessions is None:
            raise HTTPException(503, "Trading-session runtime is unavailable")
        try:
            metadata = paper_sessions.start(request.runtime_configuration())
        except ValueError as error:
            messages = {
                "paper_session_already_active": "Another trading session is already active",
                "real_account_unavailable": "The real Polymarket account is unavailable",
                "real_account_allowance_unavailable": "The real account has no trading allowance",
                "real_account_balance_empty": "The real account has no available USDC",
                "real_account_balance_below_minimum": "The real account needs at least $1 to trade",
                "real_maximum_trade_below_minimum": "Maximum trade must be at least $1 for real trading",
                "real_trade_budget_below_minimum": "Allocation produces less than Polymarket's $1 minimum trade",
                "maximum_trade_usd_exceeds_balance": "Maximum trade exceeds the available real balance",
                "max_loss_usd_exceeds_balance": "Loss limit exceeds the available real balance",
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

    @app.patch("/api/v1/trading/sessions/{session_id}")
    def trading_session_update(session_id: str, request: PaperSessionUpdate):
        if paper_sessions is None:
            raise HTTPException(503, "Trading-session runtime is unavailable")
        try:
            paper_sessions.update(session_id, request.runtime_changes())
        except ValueError as error:
            raise HTTPException(409, str(error).replace("_", " ")) from error
        return repository.trading_session(session_id)

    @app.post("/api/v1/trading/sessions/{session_id}/stop")
    def trading_session_stop(session_id: str):
        if paper_sessions is None:
            raise HTTPException(503, "Trading-session runtime is unavailable")
        try:
            paper_sessions.stop(session_id)
        except ValueError as error:
            raise HTTPException(409, "Trading session is not active") from error
        return {"session_id": session_id, "status": "STOPPING"}

    @app.get("/api/v1/paper/sessions/{session_id}")
    def paper_session_detail(
            session_id: str,
            trade_limit: int = Query(25, ge=1, le=250),
            trade_offset: int = Query(0, ge=0),
            event_limit: int = Query(25, ge=1, le=250),
            event_offset: int = Query(0, ge=0),
            trade_status: Literal["WIN", "LOSS", "OPEN"] | None = None):
        session = repository.paper_session(session_id)
        if session is None:
            raise HTTPException(404, "Paper session not found")
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

    @app.patch("/api/v1/paper/sessions/{session_id}")
    def paper_session_update(session_id: str, request: PaperSessionUpdate):
        if paper_sessions is None:
            raise HTTPException(503, "Paper-session runtime is unavailable")
        try:
            paper_sessions.update(session_id, request.runtime_changes())
        except ValueError as error:
            messages = {
                "paper_session_not_active": "Only the active paper session can be edited",
                "paper_session_stopping": "A stopping paper session cannot be edited",
                "maximum_trade_exceeds_bankroll": "Maximum trade cannot exceed the starting bankroll",
                "maximum_loss_exceeds_starting_bankroll": "Maximum loss cannot exceed the starting bankroll",
            }
            raise HTTPException(409, messages.get(str(error), str(error))) from error
        return repository.paper_session(session_id)

    @app.post("/api/v1/paper/sessions/{session_id}/stop")
    def paper_session_stop(session_id: str):
        if paper_sessions is None:
            raise HTTPException(503, "Paper-session runtime is unavailable")
        try:
            paper_sessions.stop(session_id)
        except ValueError as error:
            raise HTTPException(409, "Paper session is not active") from error
        return {"session_id": session_id, "status": "STOPPING"}

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
