"""Collect the live market, reference prices, and one prediction per round."""

import json
import time
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests
import websocket

GAMMA = "https://gamma-api.polymarket.com"
MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
ROUND_SECONDS = 300


def round_start(now):
    return int(now) // ROUND_SECONDS * ROUND_SECONDS


def json_list(value):
    return json.loads(value) if isinstance(value, str) else value


def validate_market(event, start):
    """Use outcome labels, never assume token ordering means UP then DOWN."""
    slug = f"btc-updown-5m-{start}"
    market = next(m for m in event["markets"] if m["slug"] == slug)
    labels = json_list(market["outcomes"])
    tokens = json_list(market["clobTokenIds"])
    if len(tokens) != 2 or len(labels) != 2 or set(labels) != {"Up", "Down"}:
        raise ValueError("Expected exactly two outcomes: Up and Down")
    if len(set(tokens)) != 2:
        raise ValueError("Outcome token IDs must be distinct")
    begin = datetime.fromisoformat(market["eventStartTime"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(market["endDate"].replace("Z", "+00:00"))
    if begin.timestamp() != start or end.timestamp() != start + ROUND_SECONDS:
        raise ValueError("Market times do not match the requested five-minute round")
    if market.get("closed") or not market.get("acceptingOrders"):
        raise ValueError("Current market is closed or is not accepting orders")
    return {"slug": slug, "start": start, "end": start + ROUND_SECONDS,
            "tokens": dict(zip(tokens, labels)), "event": event}


def discover(session, start):
    response = session.get(f"{GAMMA}/events/slug/btc-updown-5m-{start}", timeout=10)
    response.raise_for_status()
    return validate_market(response.json(), start)


class Book:
    """Track the feed's best prices; never guess size at a newly revealed level."""

    def __init__(self):
        self.bid = self.ask = None
        self.bid_size = self.ask_size = None
        self.ready = False
        self.source_ms = None

    def snapshot(self, event):
        bids = {Decimal(x["price"]): Decimal(x["size"])
                for x in event["bids"] if Decimal(x["size"]) > 0}
        asks = {Decimal(x["price"]): Decimal(x["size"])
                for x in event["asks"] if Decimal(x["size"]) > 0}
        self.bid, self.ask = max(bids, default=None), min(asks, default=None)
        self.bid_size, self.ask_size = bids.get(self.bid), asks.get(self.ask)
        self.source_ms = int(event["timestamp"])
        self.ready = True

    def change(self, change, timestamp):
        if not self.ready:
            return False
        stamp = int(timestamp)
        if stamp < self.source_ms:
            # A delayed duplicate must not overwrite a newer best quote, but it
            # also does not prove that the healthy socket needs reconnecting.
            return False
        if change["side"] not in ("BUY", "SELL"):
            raise ValueError("Unknown book side")
        # The feed uses 0/1 for absent bids/asks. Neither is an executable quote.
        bid, ask = Decimal(change["best_bid"]), Decimal(change["best_ask"])
        bid = bid if bid > 0 else None
        ask = ask if ask < 1 else None
        if bid != self.bid:
            self.bid_size = None
        if ask != self.ask:
            self.ask_size = None
        self.bid, self.ask = bid, ask
        price, size = Decimal(change["price"]), Decimal(change["size"])
        if change["side"] == "BUY" and price == bid:
            self.bid_size = size if size > 0 else None
        if change["side"] == "SELL" and price == ask:
            self.ask_size = size if size > 0 else None
        self.source_ms = stamp
        return True

    def quote(self, now_ms, connection_age_ms=None):
        return {"ready": self.ready,
                "bid": str(self.bid) if self.bid is not None else None,
                "ask": str(self.ask) if self.ask is not None else None,
                "bid_size": str(self.bid_size) if self.bid_size is not None else None,
                "ask_size": str(self.ask_size) if self.ask_size is not None else None,
                "source_age_ms": now_ms - self.source_ms if self.source_ms else None,
                "connection_age_ms": connection_age_ms}


def apply_event(books, event):
    kind = event.get("event_type")
    if kind == "book" and event.get("asset_id") in books:
        books[event["asset_id"]].snapshot(event)
    elif kind == "price_change":
        for change in event["price_changes"]:
            if change["asset_id"] in books:
                books[change["asset_id"]].change(change, event["timestamp"])


class Recorder:
    def __init__(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        self.folder = folder
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self.session_stamp = stamp
        self.path = folder / f"session-{stamp}.jsonl"
        self.file = self.path.open("x", buffering=1)
        self.lock = threading.Lock()
        self.history_keys = {}
        self.round_history = False
        self.round_files = {}

    def _round_start_for(self, record):
        slug = record.get("slug")
        if isinstance(slug, str) and slug.startswith("btc-updown-5m-"):
            try:
                return int(slug.rsplit("-", 1)[1])
            except ValueError:
                return None
        if isinstance(record.get("start"), int):
            return record["start"]
        # Reference prices are shared live inputs. Assign each tick to the
        # wall-clock round that may consume it so the temporary file can later
        # be removed with that round.
        if record.get("kind") == "btc_price":
            return round_start(record["received_ms"] / 1000)
        return None

    def _write_round_locked(self, start, payload):
        active = self.folder / "active-rounds"
        active.mkdir(exist_ok=True)
        if start not in self.round_files:
            path = active / f"{self.session_stamp}-{start}.jsonl"
            self.round_files[start] = path.open("a", buffering=1)
        self.round_files[start].write(json.dumps(payload, separators=(",", ":")) + "\n")

    def _discard_round_locked(self, start):
        handle = self.round_files.pop(start, None)
        if handle:
            handle.close()
        active = self.folder / "active-rounds"
        if active.exists():
            for path in active.glob(f"*-{start}.jsonl"):
                path.unlink(missing_ok=True)
            try:
                active.rmdir()
            except OSError:
                pass

    def pending_raw_rounds(self):
        """Return unresolved starts whose temporary evidence still exists."""
        with self.lock:
            starts = set(self.round_files)
            active = self.folder / "active-rounds"
            if active.exists():
                for path in active.glob("*.jsonl"):
                    try:
                        starts.add(int(path.stem.rsplit("-", 1)[1]))
                    except ValueError:
                        continue
            return starts

    def write(self, kind, **fields):
        payload = {"kind": kind, "received_ms": time.time_ns() // 1_000_000, **fields}
        with self.lock:
            start = self._round_start_for(payload) if self.round_history else None
            if start is not None:
                self._write_round_locked(start, payload)
            else:
                self.file.write(json.dumps(payload, separators=(",", ":")) + "\n")

    def write_status(self, filename, **fields):
        """Atomically replace a small human-readable status file."""
        if Path(filename).name != filename or not filename.endswith(".json"):
            raise ValueError("Status filename must be a local JSON filename")
        target = self.folder / filename
        temporary = self.folder / f".{filename}.tmp"
        payload = {"updated_ms": time.time_ns() // 1_000_000, **fields}
        with self.lock:
            temporary.write_text(json.dumps(payload, indent=2) + "\n")
            temporary.replace(target)

    def append_round_history(self, record_type, slug, **fields):
        """Append compact decisions, entries and outcomes, even across restarts."""
        filename = "round-history.jsonl"
        target = self.folder / filename
        suffix = f":{fields.get('delay_seconds')}" if record_type == "entry" else ""
        key = f"{record_type}:{slug}{suffix}"
        with self.lock:
            if filename not in self.history_keys:
                keys = set()
                if target.exists():
                    for line in target.read_text().splitlines():
                        try:
                            row = json.loads(line)
                            row_suffix = (f":{row.get('delay_seconds')}"
                                          if row.get("record_type") == "entry" else "")
                            keys.add(f"{row.get('record_type')}:{row.get('slug')}{row_suffix}")
                        except json.JSONDecodeError:
                            continue
                self.history_keys[filename] = keys
            if record_type in ("entry", "resolution") and f"decision:{slug}" not in self.history_keys[filename]:
                if record_type == "resolution":
                    self._discard_round_locked(int(slug.rsplit("-", 1)[1]))
                return False  # Do not create outcome-only rows for rounds we never evaluated.
            if key in self.history_keys[filename]:
                if record_type == "resolution":
                    self._discard_round_locked(int(slug.rsplit("-", 1)[1]))
                return False
            row = {"record_type": record_type, "slug": slug,
                   "recorded_ms": time.time_ns() // 1_000_000, **fields}
            with target.open("a", buffering=1) as history:
                history.write(json.dumps(row, separators=(",", ":")) + "\n")
            self.history_keys[filename].add(key)
            if record_type == "resolution":
                self._discard_round_locked(int(slug.rsplit("-", 1)[1]))
            return True

    def close(self):
        with self.lock:
            for handle in self.round_files.values():
                handle.close()
            self.round_files.clear()
            self.file.close()


def stream_round(market, recorder, deadline, stats, price_state, predictor, stop=None,
                 live_store=None):
    """Reconnects always create empty books; old quotes are never reused."""
    books = {token: Book() for token in market["tokens"]}
    print(f"Watching {market['slug']}", flush=True)
    connection = websocket.create_connection(MARKET_WS, timeout=10)
    try:
        predictor.start_connection(market, time.time_ns() // 1_000_000)
        connection.settimeout(1)
        connection.send(json.dumps({"assets_ids": list(books), "type": "market",
                                    "custom_feature_enabled": True}))
        recorder.write("connected", slug=market["slug"])
        stats["connections"] += 1
        connected_at = last_ping = last_received = time.monotonic()
        next_display = time.monotonic()
        while (time.monotonic() < deadline and time.time() < market["end"]
               and not (stop and stop.is_set())):
            now = time.monotonic()
            if now - last_ping >= 10:
                connection.send("PING")
                last_ping = now
            if now - last_received > 25:
                raise TimeoutError("No messages or PONG for 25 seconds")
            if not all(book.ready for book in books.values()) and now - connected_at > 10:
                raise TimeoutError("Missing initial book snapshot")
            if now >= next_display:
                now_ms = time.time_ns() // 1_000_000
                connection_age_ms = int((now - last_received) * 1000)
                quotes = {market["tokens"][token]: book.quote(now_ms, connection_age_ms)
                          for token, book in books.items()}
                remaining = max(0, int(market["end"] - time.time()))
                recorder.write("quotes", slug=market["slug"], remaining=remaining, quotes=quotes)
                parts = [f"{name}: bid={q['bid']} ask={q['ask']} age={q['source_age_ms']}ms"
                         for name, q in quotes.items()]
                print(f"{remaining:3}s left | " + " | ".join(parts), flush=True)
                if price_state is not None:
                    context = price_state.snapshot(market["start"], now_ms)
                    if live_store is not None:
                        reference = context.get("reference", {})
                        live_store.update_reference_context(
                            slug=market["slug"],
                            opening_price=reference.get("price_to_beat"),
                            opening_source=reference.get("opening_source"))
                    recorder.write("btc_context", slug=market["slug"], remaining=remaining, **context)
                    live = [f"{name}={p['value']} ({'fresh' if p['fresh'] else 'stale'})"
                            for name, p in context["prices"].items()]
                    print("  BTC | " + " | ".join(live or ["waiting for prices"]) +
                          f" | start={context['reference'].get('price_to_beat')}" +
                          f" | reference_ready={context['reference_ready']}", flush=True)
                    stats["reference_ready"] += int(context["reference_ready"])
                    stats["btc_observations"] += int(any(p["fresh"] for p in context["prices"].values()))
                    with price_state.lock:
                        history = list(price_state.history)
                    predictor.observe(market, context, history, quotes, now_ms)
                if all(q["ready"] for q in quotes.values()):
                    stats["paired_quotes"] += 1
                next_display = now + 1
            try:
                raw = connection.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                raise ConnectionError("WebSocket closed")
            last_received = time.monotonic()
            received_ms = time.time_ns() // 1_000_000
            predictor.on_connection_activity(market, received_ms)
            if raw == "PONG":
                recorder.write("heartbeat", slug=market["slug"])
                continue
            payload = json.loads(raw)
            events = payload if isinstance(payload, list) else [payload]
            for event in events:
                received_ms = time.time_ns() // 1_000_000
                recorder.write("market_event", received_ms=received_ms,
                               slug=market["slug"], event=event)
                stats["events"] += 1
                apply_event(books, event)
                if live_store is not None:
                    quotes = {market["tokens"][token]: book.quote(received_ms, 0)
                              for token, book in books.items()}
                    live_store.update_market(
                        slug=market["slug"], received_ms=received_ms,
                        remaining_seconds=max(0, market["end"] - received_ms / 1000),
                        quotes=quotes, tokens=market["tokens"],
                        market_end_ms=market["end"] * 1000,
                        accepting_orders=True, closed=False)
                predictor.on_market_event(market, event, received_ms)
    finally:
        connection.close()
        recorder.write("disconnected", slug=market["slug"])


@dataclass(frozen=True)
class CollectorConfig:
    data: Path = Path("data")
    seconds: int = 0


def run(config=None, stop=None, live_store=None):
    from .market_data import PriceService, credentials
    from .prediction import PredictionEngine

    config = config or CollectorConfig()
    credentials()  # Fail before creating output files when credentials are absent.
    recorder = Recorder(config.data)
    recorder.round_history = True
    predictor = PredictionEngine(recorder)
    session = requests.Session()
    deadline = time.monotonic() + config.seconds if config.seconds else float("inf")
    stats = {"events": 0, "paired_quotes": 0, "connections": 0, "errors": 0, "rounds": 0}
    service = PriceService(recorder, "polybolt", live_store)
    stats.update(reference_ready=0, btc_observations=0)
    service.start()
    last_start = None
    backoff = 1
    print(f"Saving data to {recorder.path}", flush=True)
    try:
        while time.monotonic() < deadline and not (stop and stop.is_set()):
            try:
                start = round_start(time.time())
                market = discover(session, start)
                # HTTP may have completed after the boundary. Never subscribe to the old round.
                if round_start(time.time()) != start:
                    continue
                recorder.write("market", slug=market["slug"], event=market["event"])
                if start != last_start:
                    stats["rounds"] += 1
                    last_start = start
                stream_round(market, recorder, deadline, stats, service.state, predictor, stop,
                             live_store)
                backoff = 1
            except (requests.RequestException, websocket.WebSocketException,
                    OSError, ValueError, KeyError, StopIteration, TypeError) as error:
                stats["errors"] += 1
                print(f"Stream unavailable: {error}. Retrying in {backoff}s.", flush=True)
                recorder.write("error", message=str(error))
                wait = min(backoff, max(0, deadline - time.monotonic()))
                if stop:
                    stop.wait(wait)
                else:
                    time.sleep(wait)
                backoff = min(backoff * 2, 15)
    except KeyboardInterrupt:
        print("Stopped by user.", flush=True)
    finally:
        service.close()
        stats["predictions"] = len(predictor.decisions)
        recorder.write("summary", **stats)
        recorder.close()
        session.close()
        print(f"Summary: {json.dumps(stats)}", flush=True)
    usable = stats["paired_quotes"] and stats["btc_observations"] and stats["reference_ready"]
    return 0 if usable else 1
