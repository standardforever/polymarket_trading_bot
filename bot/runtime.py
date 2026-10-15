"""Application lifecycle for the collector, API, and real trading runtime."""

import threading
from pathlib import Path

import uvicorn

from .api import create_app
from .collector import CollectorConfig, run as run_collector
from .live import LiveStore
from .sessions import TradingSessionManager
from .account import PolymarketAccount


def serve(data: Path, host: str, port: int, api_only: bool = False) -> None:
    stop = threading.Event()
    live_store = LiveStore()
    workers = []
    account = PolymarketAccount(data, live_store)
    sessions = None if api_only else TradingSessionManager(data, account=account)

    if not api_only:
        collector = threading.Thread(
            name="market-collector", daemon=True,
            target=run_collector, args=(CollectorConfig(data=data), stop, live_store))
        collector.start()
        workers.append(collector)

    try:
        uvicorn.run(create_app(data, live_store, sessions=sessions, account=account),
                    host=host, port=port, log_level="info")
    finally:
        stop.set()
        if sessions is not None:
            sessions.shutdown()
        account.close()
        for worker in workers:
            worker.join(timeout=5)
