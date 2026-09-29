"""Official BTC price inputs and settlement metadata."""

import json
import os
import shlex
import threading
import time
from collections import deque
from decimal import Decimal
from pathlib import Path

import requests
import websocket

POLYBOLT = "wss://ws-live-v2.polymarket.com/ws"
BINANCE = "wss://data-stream.binance.vision/ws/btcusdt@aggTrade"
CHANNELS = {"price.crypto": "pyth_btc_usd", "price.crypto.twap": "chainlink_twap_60s"}
MAX_AGE_MS = 5000
MAX_FUTURE_MS = 1000


def credentials(env_file=Path('.env')):
    names = {"apiKey": "POLYMARKET_API_KEY", "secret": "POLYMARKET_API_SECRET",
             "passphrase": "POLYMARKET_API_PASSPHRASE"}
    aliases = {"api_key": "apiKey", "api_secret": "secret", "api_passphrase": "passphrase"}
    aliases.update({name: key for key, name in names.items()})
    local = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            name, separator, value = line.strip().removeprefix('export ').partition('=')
            if not separator or name.strip() not in aliases:
                continue  # Never load wallet keys or execute shell contents.
            try:
                parts = shlex.split(value, comments=True)
            except ValueError:
                raise ValueError("Invalid quoting in a CLOB credential entry") from None
            if len(parts) > 1:
                raise ValueError("A CLOB credential entry must contain one value")
            local[aliases[name.strip()]] = parts[0] if parts else ""
    values = {key: os.environ.get(name, local.get(key, "")).strip() for key, name in names.items()}
    if not all(values.values()):
        raise ValueError("Set POLYMARKET_API_KEY, POLYMARKET_API_SECRET and "
                         "POLYMARKET_API_PASSPHRASE locally, or explicitly select --price-source binance")
    return values


def price_point(source, point, snapshot=False):
    value = Decimal(str(point.get("full_accuracy_value", point.get("value"))))
    stamp = int(point["timestamp"])
    if not value.is_finite() or value <= 0 or stamp < 1_000_000_000_000:
        raise ValueError("Invalid price or non-millisecond timestamp")
    return {"source": source, "value": str(value), "source_ms": stamp,
            "snapshot": snapshot, "carried_forward": bool(point.get("is_carried_forward", False))}


def parse_prices(message, source):
    if message.get('op'):
        return []  # Subscription acknowledgements carry a channel but no price.
    if source == "binance":
        if message.get("e") != "aggTrade" or message.get("s") != "BTCUSDT":
            return []
        return [price_point("binance_btc_usdt", {"value": message["p"], "timestamp": message["T"]})]
    channel = message.get("channel")
    if channel not in CHANNELS:
        return []
    payload = message["payload"]
    if payload.get("symbol") != "btcusd":
        raise ValueError("Unexpected reference symbol")
    if channel == "price.crypto.twap" and payload.get("window_seconds") != 60:
        raise ValueError("Expected a 60-second TWAP")
    snapshot = bool(message.get("snapshot"))
    points = payload["data"] if snapshot else [payload]
    return [price_point(CHANNELS[channel], point, snapshot) for point in points]


def fresh(point, now_ms):
    return bool(point and not point["snapshot"] and not point["carried_forward"]
                and -MAX_FUTURE_MS <= now_ms - point["source_ms"] <= MAX_AGE_MS
                and 0 <= now_ms - point["received_ms"] <= MAX_AGE_MS)


def market_reference(event):
    market = event["markets"][0]
    config = market.get("cryptoMarketConfig", {})
    supported = (config.get("asset") == "btc" and config.get("duration") == "5m"
                 and config.get("twapEnabled") is True and config.get("twapLookbackSeconds") == 60
                 and market.get("resolutionSource") == "https://data.chain.link/streams/btc-usd-twap-60s-streams")
    metadata = event.get("eventMetadata") or {}
    opening, final = metadata.get("priceToBeat"), metadata.get("finalPrice")
    winner = None
    if market.get("umaResolutionStatus") == "resolved":
        labels = market["outcomes"]
        prices = market["outcomePrices"]
        labels = json.loads(labels) if isinstance(labels, str) else labels
        prices = json.loads(prices) if isinstance(prices, str) else prices
        winner = next((label for label, price in zip(labels, prices) if Decimal(price) == 1), None)
    return {"supported": supported, "price_to_beat": str(opening) if opening is not None else None,
            "final_price": str(final) if final is not None else None, "winner": winner}


class PriceState:
    def __init__(self):
        self.lock = threading.Lock()
        self.connected = False
        self.latest = {}
        self.history = deque(maxlen=10000)
        self.references = {}

    def reset(self, connected=False):
        with self.lock:
            self.connected = connected
            self.latest.clear()  # Reconnect must not revive an old quote.

    def add(self, point, received_ms):
        point = dict(point, received_ms=received_ms)
        with self.lock:
            previous = self.latest.get(point["source"])
            if previous and point["source_ms"] < previous["source_ms"]:
                raise ValueError("Out-of-order reference price")
            self.history.append(point)
            self.latest[point["source"]] = point

    def snapshot(self, start, now_ms):
        with self.lock:
            prices = {name: dict(point, fresh=self.connected and fresh(point, now_ms),
                                 age_ms=now_ms - point["source_ms"])
                      for name, point in self.latest.items()}
            reference = dict(self.references.get(start, {}))
            boundary = next((p for p in reversed(self.history)
                             if p['source'] == 'chainlink_twap_60s'
                             and p['source_ms'] == start * 1000
                             and p['received_ms'] <= now_ms and not p['carried_forward']), None)
            official = reference.get('price_to_beat')
            mismatch = bool(official and boundary and
                            abs(Decimal(official) - Decimal(boundary['value'])) > Decimal('0.000001'))
            if official:
                reference['opening_source'] = 'gamma_metadata'
            elif boundary and reference.get('supported'):
                # Verified against resolved rounds for this 60s-TWAP contract.
                # Use an actual boundary tick, never a nearby price/interpolation.
                reference.update(price_to_beat=boundary['value'], opening_source='chainlink_boundary',
                                 opening_received_ms=boundary['received_ms'])
            reference['opening_mismatch'] = mismatch
            twap = prices.get("chainlink_twap_60s", {})
            ready = bool(reference.get("supported") and reference.get("price_to_beat")
                         and twap.get("fresh") and not mismatch)
            delta = (str(Decimal(twap["value"]) - Decimal(reference["price_to_beat"]))
                     if ready else None)
            return {"prices": prices, "reference": reference, "reference_ready": ready,
                    "twap_minus_start": delta}

    def alignment(self, start, reference):
        """Diagnostic only: require exact endpoint timestamps; never interpolate."""
        with self.lock:
            points = {p["source_ms"]: p for p in self.history
                      if p["source"] == "chainlink_twap_60s" and not p["carried_forward"]}
        report = {"reference": reference}
        for name, stamp, official in (("opening", start * 1000, reference.get("price_to_beat")),
                                       ("closing", (start + 300) * 1000, reference.get("final_price"))):
            point = points.get(stamp)
            difference = str(Decimal(point["value"]) - Decimal(official)) if point and official else None
            report[name] = {"point": point, "difference": difference}
        if reference.get("price_to_beat") and reference.get("final_price"):
            outcome = "Up" if Decimal(reference["final_price"]) >= Decimal(reference["price_to_beat"]) else "Down"
            report["metadata_outcome_matches"] = outcome == reference.get("winner")
        report["endpoint_match"] = bool(reference.get("supported") and reference.get("winner")
            and report.get("metadata_outcome_matches")
            and all(report[k]["difference"] is not None and abs(Decimal(report[k]["difference"])) <= Decimal("0.000001")
                    for k in ("opening", "closing")))
        return report


def authenticate(connection, auth):
    connection.send(json.dumps({"op": "auth", "rid": "a1", "auth": auth}))
    response = json.loads(connection.recv())
    if response.get("op") != "authed":
        # Never include server responses or credentials in exceptions/logs.
        raise PermissionError("PolyBolt authentication was not accepted")
    connection.send(json.dumps({"op": "subscribe", "rid": "s1", "subscriptions": [
        {"channel": "price.crypto", "filter": {"symbol": "btcusd"}},
        {"channel": "price.crypto.twap", "filter": {"symbol": "btcusd", "window_seconds": 60}}]}))


def receive_price_message(connection):
    # websocket-client answers protocol PINGs automatically. Inspect close codes
    # so an authentication/policy refusal never becomes an endless retry loop.
    opcode, raw = connection.recv_data(control_frame=True)
    if opcode == websocket.ABNF.OPCODE_CLOSE:
        code = int.from_bytes(raw[:2], "big") if len(raw) >= 2 else None
        if code in (4001, 4008):
            raise PermissionError("Reference authentication or policy close")
        raise ConnectionError("Reference socket closed")
    if opcode in (websocket.ABNF.OPCODE_PING, websocket.ABNF.OPCODE_PONG):
        return {}
    return json.loads(raw)


def stream_prices(state, recorder, stop, source, auth, live_store=None):
    backoff = 1
    while not stop.is_set():
        connection = None
        state.reset()
        try:
            connection = websocket.create_connection(POLYBOLT if source == "polybolt" else BINANCE, timeout=10)
            if source == "polybolt":
                authenticate(connection, auth)
            connection.settimeout(1)
            state.reset(connected=True)
            recorder.write("price_connected", source=source)
            sequences = {}
            started = last_received = time.monotonic()
            while not stop.is_set():
                if time.monotonic() - last_received > 20:
                    raise TimeoutError("No reference data for 20 seconds")
                try:
                    message = receive_price_message(connection)
                except websocket.WebSocketTimeoutException:
                    continue
                if message.get("op") == "error":
                    raise PermissionError("Reference subscription rejected")
                points = parse_prices(message, source)
                if not points:
                    continue
                if source == "polybolt":
                    channel, seq = message["channel"], int(message["seq"])
                    if message.get("dropped", 0) or (channel in sequences and seq != sequences[channel] + 1):
                        raise ValueError("Reference sequence gap")
                    sequences[channel] = seq
                received_ms = time.time_ns() // 1_000_000
                for point in points:
                    state.add(point, received_ms)
                    if live_store is not None:
                        live_store.update_price(point, received_ms)
                    recorder.write("btc_price", **point, observed_received_ms=received_ms)
                last_received = time.monotonic()
                if last_received - started > 30:
                    backoff = 1
        except PermissionError:
            recorder.write("price_error", source=source, error="authentication_or_subscription_rejected")
            print("Reference feed rejected authentication/subscription; fix credentials before retrying.", flush=True)
            return
        except (OSError, ValueError, ArithmeticError, KeyError, TypeError, websocket.WebSocketException) as error:
            recorder.write("price_error", source=source, error=type(error).__name__)
            print(f"Reference feed interrupted ({type(error).__name__}); reconnecting.", flush=True)
        finally:
            state.reset()
            if live_store is not None:
                live_store.clear_prices()
            if connection:
                connection.close()
            recorder.write("price_disconnected", source=source)
        stop.wait(backoff)
        backoff = min(backoff * 2, 15)


def watch_references(state, recorder, stop):
    """Poll official metadata separately, so REST cannot stall either WebSocket."""
    from .collector import GAMMA, round_start
    completed = set()
    with requests.Session() as session:
        while not stop.is_set():
            current = round_start(time.time())
            recent = [current, current - 300, current - 600]
            if hasattr(recorder, "pending_raw_rounds"):
                recent.extend(sorted(recorder.pending_raw_rounds()))
            for start in dict.fromkeys(recent):
                if stop.is_set() or start in completed:
                    continue
                try:
                    response = session.get(f"{GAMMA}/events/slug/btc-updown-5m-{start}", timeout=5)
                    response.raise_for_status()
                    event = response.json(parse_float=Decimal)
                    reference = market_reference(event)
                    with state.lock:
                        state.references[start] = dict(reference, observed_ms=time.time_ns() // 1_000_000)
                        state.references = {s: r for s, r in state.references.items() if s >= current - 900}
                    recorder.write("round_reference", start=start, **reference)
                    if reference["winner"]:
                        alignment = state.alignment(start, reference)
                        recorder.write("settlement_alignment", start=start, **alignment)
                        # Polymarket publishes the official winner before Gamma
                        # always exposes finalPrice. Do not keep paper capital
                        # locked while waiting for that later audit field.
                        if reference["price_to_beat"]:
                            closing_point = alignment.get("closing", {}).get("point") or {}
                            final_price = reference["final_price"] or closing_point.get("value")
                            if getattr(recorder, "round_history", False):
                                recorder.append_round_history(
                                    "resolution", f"btc-updown-5m-{start}", start=start,
                                    actual_result=reference["winner"],
                                    opening_price=reference["price_to_beat"],
                                    final_price=final_price,
                                    supported=reference["supported"])
                            completed.add(start)
                    completed = {s for s in completed if s >= current - 900}
                except (requests.RequestException, ValueError, ArithmeticError, KeyError, TypeError):
                    recorder.write("reference_metadata_error", start=start)
            stop.wait(15)


class PriceService:
    def __init__(self, recorder, source, live_store=None):
        self.auth = credentials() if source == "polybolt" else None
        self.state, self.stop = PriceState(), threading.Event()
        self.threads = [threading.Thread(target=stream_prices, args=(self.state, recorder, self.stop, source, self.auth, live_store)),
                        threading.Thread(target=watch_references, args=(self.state, recorder, self.stop))]

    def start(self):
        for thread in self.threads:
            thread.start()

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join()
