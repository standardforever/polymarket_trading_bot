"""Validated, persisted strategy controls shared with the future runtime."""

import json
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrategySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset: Literal["BTC"] = "BTC"
    market_duration_seconds: Literal[300] = 300
    model_confidence_threshold: float = Field(.90, ge=.5, le=1)
    minimum_edge: float = Field(.03, ge=0, lt=1)
    decision_window_open_seconds: float = Field(120, gt=0, le=300)
    decision_window_close_seconds: float = Field(110, gt=0, le=300)
    entry_stop_remaining_seconds: float = Field(30, gt=0, lt=120)
    minimum_liquidity_shares: float = Field(5, gt=0)
    maximum_entry_price: float = Field(.99, gt=0, lt=1)
    maximum_slippage: float = Field(.02, ge=0, le=.25)
    automatic_trading_enabled: bool = False
    trading_mode: Literal["disabled", "paper"] = "paper"
    max_trades_per_day: int = Field(10, ge=0, le=288)
    max_trade_usd: float = Field(50, gt=0)
    max_daily_loss_usd: float = Field(25, gt=0)
    max_open_positions: int = Field(2, ge=1, le=20)
    allocation_bankroll_threshold: float = Field(20, gt=0)
    allocation_fraction_above_threshold: float = Field(.80, gt=0, le=1)
    allocation_fraction_at_or_below_threshold: float = Field(1, gt=0, le=1)

    @model_validator(mode="after")
    def validate_relationships(self):
        if self.decision_window_open_seconds <= self.decision_window_close_seconds:
            raise ValueError("decision window open must be greater than close")
        if self.entry_stop_remaining_seconds >= self.decision_window_close_seconds:
            raise ValueError("entry cutoff must be below the decision window")
        if self.automatic_trading_enabled and self.trading_mode == "disabled":
            raise ValueError("automatic trading requires paper mode")
        return self


class StrategySettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_confidence_threshold: float | None = Field(None, ge=.5, le=1)
    minimum_edge: float | None = Field(None, ge=0, lt=1)
    decision_window_open_seconds: float | None = Field(None, gt=0, le=300)
    decision_window_close_seconds: float | None = Field(None, gt=0, le=300)
    entry_stop_remaining_seconds: float | None = Field(None, gt=0, lt=120)
    minimum_liquidity_shares: float | None = Field(None, gt=0)
    maximum_entry_price: float | None = Field(None, gt=0, lt=1)
    maximum_slippage: float | None = Field(None, ge=0, le=.25)
    automatic_trading_enabled: bool | None = None
    trading_mode: Literal["disabled", "paper"] | None = None
    max_trades_per_day: int | None = Field(None, ge=0, le=288)
    max_trade_usd: float | None = Field(None, gt=0)
    max_daily_loss_usd: float | None = Field(None, gt=0)
    max_open_positions: int | None = Field(None, ge=1, le=20)
    allocation_bankroll_threshold: float | None = Field(None, gt=0)
    allocation_fraction_above_threshold: float | None = Field(None, gt=0, le=1)
    allocation_fraction_at_or_below_threshold: float | None = Field(None, gt=0, le=1)


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def read(self) -> StrategySettings:
        with self.lock:
            if not self.path.exists():
                return StrategySettings()
            try:
                return StrategySettings.model_validate_json(self.path.read_text())
            except (OSError, ValueError, json.JSONDecodeError):
                return StrategySettings()

    def update(self, patch: StrategySettingsPatch) -> StrategySettings:
        with self.lock:
            current = (StrategySettings.model_validate_json(self.path.read_text())
                       if self.path.exists() else StrategySettings())
            values = current.model_dump()
            values.update(patch.model_dump(exclude_none=True))
            updated = StrategySettings.model_validate(values)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.tmp")
            temporary.write_text(updated.model_dump_json(indent=2) + "\n")
            temporary.replace(self.path)
            return updated
