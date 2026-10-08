"""Application lifecycle for the collector, API, and paper-session runtime."""

import threading
from pathlib import Path

import uvicorn

from .api import create_app
from .api.config import SettingsStore
from .collector import CollectorConfig, run as run_collector
from .live import LiveStore
from .paper_sessions import PaperSessionManager
from .account import PolymarketAccount


def serve(data: Path, host: str, port: int, api_only: bool = False) -> None:
    stop = threading.Event()
    live_store = LiveStore()
    settings_store = SettingsStore(data / "config" / "strategy.json")
    workers = []
    account = PolymarketAccount(data, live_store)
    paper_sessions = None if api_only else PaperSessionManager(data, account=account)

    if not api_only:
        collector = threading.Thread(
            name="market-collector", daemon=True,
            target=run_collector, args=(CollectorConfig(data=data), stop, live_store))
        collector.start()
        workers.append(collector)

    try:
        uvicorn.run(create_app(data, live_store, settings_store,
                               paper_sessions=paper_sessions, account=account),
                    host=host, port=port, log_level="info")
    finally:
        stop.set()
        if paper_sessions is not None:
            paper_sessions.shutdown()
        account.close()
        for worker in workers:
            worker.join(timeout=5)
