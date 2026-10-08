"""Live BTC five-minute prediction components and decision engine."""

import math
from collections import deque
from statistics import NormalDist

from .strategy import RECOMMENDED_CONFIDENCE, probability_snapshot
from .trading import (MINIMUM_EDGE, MINIMUM_SHARES, healthy_book,
                      market_fee_rate, points_before, prior, valid_book)


COMPONENT_FEATURES = {
    "settlement": (
        "twap_gap_bps", "spot_gap_bps", "gap_over_expected_move",
        "spot_twap_gap_bps", "remaining_seconds",
    ),
    "movement": (
        "momentum_5", "momentum_10", "momentum_30", "momentum_60",
        "momentum_120", "acceleration_10_30", "volatility_30",
        "volatility_120", "volatility_ratio", "twap_slope_30",
    ),
    "market": (
        "p_up", "up_spread", "down_spread", "midpoint_sum", "ask_sum",
    ),
    "order_flow": (
        "depth_imbalance_1", "depth_imbalance_3", "order_flow_5",
        "order_flow_30", "trade_imbalance_10", "trade_imbalance_30",
        "trade_count_30", "market_momentum_10",
    ),
}
ALL_COMPONENTS = tuple(COMPONENT_FEATURES)
CORE_COMPONENTS = ("settlement", "movement", "market")
ANALYTIC_MAX_PROBABILITY = 0.98
THIN_GAP_BPS = 5.0
FINAL_TWAP_SIGMA_FLOOR_BPS = 2.0
DECISION_WINDOW_OPEN = 120
DECISION_WINDOW_CLOSE = 110


def component_names(enabled=None):
    names = tuple(enabled or ALL_COMPONENTS)
    unknown = set(names) - set(ALL_COMPONENTS)
    if unknown:
        raise ValueError("unknown_components:" + ",".join(sorted(unknown)))
    return names


def validate_components(rows, enabled=None):
    for component in component_names(enabled):
        values = rows.get(component)
        if values is None:
            raise ValueError(f"missing_component:{component}")
        for name in COMPONENT_FEATURES[component]:
            value = values.get(name)
            if value is None or not math.isfinite(float(value)):
                raise ValueError(f"invalid_feature:{component}.{name}")


def _vote(values, deadband=1e-12):
    votes = [1 if value > deadband else -1 if value < -deadband else 0 for value in values]
    total = sum(votes)
    return (1 if total > 0 else -1 if total < 0 else 0), votes


def analytic_signal(component_rows, quotes, taker_fee_rate):
    """Conservative no-training paper signal based on component agreement.

    Its normal-distribution probability is an engineering approximation, not a
    calibrated forecast.  It must remain distinguishable from a trained model.
    """
    required = CORE_COMPONENTS
    missing = [name for name in required if name not in component_rows]
    if missing:
        return {"direction": "SKIP", "reason": "analytic_missing_components:" + ",".join(missing),
                "p_up": None, "calibrated": False, "method": "analytic_consensus",
                "warnings": []}

    settlement = component_rows["settlement"]
    movement = component_rows["movement"]
    market = component_rows["market"]
    order_flow = component_rows.get("order_flow")
    if (movement.get("volatility_120", 0) > 0
            and settlement.get("remaining_seconds", 0) >= 60):
        probability = probability_snapshot(component_rows)
        p_up = probability["p_up"]
        z_score = probability["z_score"]
        probability_before_cap = probability["probability_before_cap"]
        probability_cap = probability["probability_cap"]
    else:  # Compatibility for incomplete historical and unit-test snapshots.
        z_score = max(-8.0, min(8.0, settlement["gap_over_expected_move"]))
        probability_before_cap = NormalDist().cdf(z_score)
        probability_cap = ANALYTIC_MAX_PROBABILITY
        p_up = max(1 - probability_cap, min(probability_cap, probability_before_cap))
    side_sign = 1 if p_up >= .5 else -1
    side = "Up" if side_sign > 0 else "Down"
    chance = p_up if side == "Up" else 1 - p_up
    minimum_probability = RECOMMENDED_CONFIDENCE

    momentum_sign, momentum_votes = _vote([
        movement["momentum_10"], movement["momentum_30"],
        movement["momentum_60"], movement["twap_slope_30"],
    ], deadband=.01)
    flow_sign, flow_votes = (0, []) if order_flow is None else _vote([
        order_flow["depth_imbalance_1"], order_flow["depth_imbalance_3"],
        order_flow["order_flow_5"], order_flow["order_flow_30"],
        order_flow["trade_imbalance_10"], order_flow["trade_imbalance_30"],
        order_flow["market_momentum_10"],
    ], deadband=.001)
    market_sign = 1 if market["p_up"] >= .5 else -1
    agreement = {"settlement": side, "movement": "Up" if momentum_sign > 0 else
                 "Down" if momentum_sign < 0 else "Neutral",
                 "market": "Up" if market_sign > 0 else "Down",
                 "order_flow": ("Unavailable" if order_flow is None else
                                "Up" if flow_sign > 0 else "Down" if flow_sign < 0 else "Neutral"),
                 "movement_votes": momentum_votes, "order_flow_votes": flow_votes}
    result = {"direction": "SKIP", "reason": None, "side": side,
              "chance": chance, "p_up": p_up, "p_down": 1 - p_up,
              "calibrated": False, "method": "analytic_final_twap_core_consensus",
              "minimum_probability": minimum_probability,
              "z_score": z_score, "agreement": agreement,
              "warnings": [],
              "probability_before_cap": probability_before_cap,
              "probability_cap": probability_cap,
              "estimated_edge": None,
              "crossing_probability": min(p_up, 1 - p_up),
              "distance_to_beat": settlement.get("twap_minus_open"),
              "distance_source": "chainlink_twap_60s"}

    # Show the value estimate whenever there is a trustworthy executable ask,
    # even when an earlier confidence or agreement rule ultimately says SKIP.
    quote = quotes.get(side, {})
    if valid_book(quote):
        ask = float(quote["ask"])
        result["estimated_edge"] = chance - ask - taker_fee_rate * ask * (1 - ask)

    if chance < minimum_probability:
        result["reason"] = "analytic_probability_below_90pct"
        return result
    movement_disagreement = momentum_sign != side_sign
    spot_gap_bps = abs(float(settlement["spot_gap_bps"]))
    twap_gap_value = settlement.get("twap_gap_bps")
    twap_gap_bps = (abs(float(twap_gap_value))
                    if twap_gap_value is not None else spot_gap_bps)
    thin_gap_distance = min(spot_gap_bps, twap_gap_bps)
    thin_gap = thin_gap_distance < THIN_GAP_BPS
    combined_risk = thin_gap and movement_disagreement
    regime_risk = thin_gap and movement.get("regime") == "choppy_up"
    risk_blocked = combined_risk or regime_risk
    result.update(thin_gap=thin_gap, thin_gap_threshold_bps=THIN_GAP_BPS,
                  thin_gap_distance_bps=thin_gap_distance,
                  thin_gap_source="spot_or_twap",
                  movement_disagreement=movement_disagreement,
                  combined_risk=combined_risk, regime_risk=regime_risk,
                  risk_blocked=risk_blocked,
                  risk_rule="thin_gap_and_movement_or_choppy_up")
    if movement_disagreement:
        result["warnings"].append("movement_disagreement")
    if thin_gap:
        result["warnings"].append("thin_gap")
    if movement.get("regime") == "choppy_up":
        result["warnings"].append("choppy_up_regime")
    if risk_blocked:
        result["reason"] = "analytic_combined_risk"
        return result
    if market_sign != side_sign:
        result["reason"] = "analytic_disagreement:market"
        return result
    if (not valid_book(quote) or quote.get("ask_size") is None
            or float(quote["ask_size"]) < MINIMUM_SHARES):
        result["reason"] = "analytic_liquidity_unavailable"
        return result
    edge = result["estimated_edge"]
    if edge < MINIMUM_EDGE:
        result["reason"] = "analytic_edge_below_threshold"
        return result
    result.update(direction=side, reason="analytic_unvalidated_signal")
    return result


def _volatility(points, now_ms, seconds):
    selected = [(stamp, value) for stamp, value in points if stamp >= now_ms - seconds * 1000]
    if len(selected) < 2 or selected[-1][0] - selected[0][0] < (seconds - 5) * 1000:
        raise ValueError("insufficient_price_history")
    elapsed = (selected[-1][0] - selected[0][0]) / 1000
    variance = sum(math.log(b[1] / a[1]) ** 2 for a, b in zip(selected, selected[1:])) / elapsed
    return math.sqrt(variance) * 10000


def _path_shape(points, now_ms, seconds):
    """Describe whether a price path was gradual, jumpy, or reversing."""
    selected = [(stamp, value) for stamp, value in points if stamp >= now_ms - seconds * 1000]
    if len(selected) < 3 or selected[-1][0] - selected[0][0] < (seconds - 5) * 1000:
        raise ValueError("insufficient_price_history")
    returns = [math.log(b[1] / a[1]) * 10000 for a, b in zip(selected, selected[1:])]
    total_path = sum(abs(value) for value in returns)
    net = math.log(selected[-1][1] / selected[0][1]) * 10000
    efficiency = abs(net) / total_path if total_path else 1.0
    meaningful = [1 if value > .001 else -1 for value in returns if abs(value) > .001]
    reversals = sum(a != b for a, b in zip(meaningful, meaningful[1:]))
    reversal_rate = reversals / (len(meaningful) - 1) if len(meaningful) > 1 else 0.0

    times = [(stamp - selected[0][0]) / 1000 for stamp, _ in selected]
    logs = [math.log(value) for _, value in selected]
    mean_t, mean_y = sum(times) / len(times), sum(logs) / len(logs)
    covariance = sum((t - mean_t) * (y - mean_y) for t, y in zip(times, logs))
    time_variance = sum((t - mean_t) ** 2 for t in times)
    price_variance = sum((y - mean_y) ** 2 for y in logs)
    trend_r2 = (covariance ** 2 / (time_variance * price_variance)
                if time_variance and price_variance else 1.0)
    range_bps = math.log(max(value for _, value in selected)
                         / min(value for _, value in selected)) * 10000
    max_jump = max((abs(value) for value in returns), default=0.0)
    jump_share = max_jump / total_path if total_path else 0.0
    stability = max(0.0, min(1.0, (efficiency + trend_r2 + (1 - reversal_rate)) / 3))
    return {"net_bps": net, "path_bps": total_path, "range_bps": range_bps,
            "efficiency": efficiency, "trend_r2": trend_r2,
            "reversal_count": float(reversals), "reversal_rate": reversal_rate,
            "max_tick_bps": max_jump, "largest_jump_share": jump_share,
            "stability_score": stability}


def movement_stability(points, now_ms):
    short, long = _path_shape(points, now_ms, 30), _path_shape(points, now_ms, 120)
    net = long["net_bps"]
    direction = "Flat" if abs(net) < .1 else "Up" if net > 0 else "Down"
    if long["efficiency"] >= .65 and long["reversal_rate"] <= .35:
        shape = "steady"
    elif long["efficiency"] <= .35 or long["reversal_rate"] >= .60:
        shape = "choppy"
    else:
        shape = "mixed"
    return {
        "regime": f"{shape}_{direction.lower()}",
        "direction": direction,
        "stability_score": long["stability_score"],
        "net_30_bps": short["net_bps"], "net_120_bps": long["net_bps"],
        "range_30_bps": short["range_bps"], "range_120_bps": long["range_bps"],
        "path_efficiency_30": short["efficiency"],
        "path_efficiency_120": long["efficiency"],
        "trend_r2_30": short["trend_r2"], "trend_r2_120": long["trend_r2"],
        "reversal_rate_30": short["reversal_rate"],
        "reversal_rate_120": long["reversal_rate"],
        "largest_jump_share_30": short["largest_jump_share"],
        "largest_jump_share_120": long["largest_jump_share"],
        "max_tick_30_bps": short["max_tick_bps"],
        "max_tick_120_bps": long["max_tick_bps"],
    }


def market_book_view(quotes):
    """Estimate probability bounds without inventing executable liquidity."""
    if not all(healthy_book(quotes.get(side)) for side in ("Up", "Down")):
        raise ValueError("book_unavailable_or_stale")
    up, down = quotes["Up"], quotes["Down"]
    up_bid = float(up["bid"]) if up.get("bid") is not None else None
    up_ask = float(up["ask"]) if up.get("ask") is not None else None
    down_bid = float(down["bid"]) if down.get("bid") is not None else None
    down_ask = float(down["ask"]) if down.get("ask") is not None else None
    observed = [value for value in (up_bid, up_ask, down_bid, down_ask) if value is not None]
    if not observed or any(not 0 < value < 1 for value in observed):
        raise ValueError("book_unavailable_or_stale")

    lower_candidates = [value for value in (up_bid, 1 - down_ask if down_ask is not None else None)
                        if value is not None]
    upper_candidates = [value for value in (up_ask, 1 - down_bid if down_bid is not None else None)
                        if value is not None]
    lower = max(lower_candidates, default=0.0)
    upper = min(upper_candidates, default=1.0)
    if lower > upper + 1e-9:
        raise ValueError("inconsistent_complementary_book")
    spread = max(0.0, upper - lower)
    direct_two_sided = all(value is not None for value in (up_bid, up_ask, down_bid, down_ask))
    return {
        "p_up": (lower + upper) / 2,
        "up_spread": spread,
        "down_spread": spread,
        "midpoint_sum": 1.0,
        "ask_sum": 1.0 + spread,
        "probability_lower": lower,
        "probability_upper": upper,
        "book_mode": "direct_two_sided" if direct_two_sided else "complementary_bounds",
        "direct_up_ask_available": up_ask is not None,
        "direct_down_ask_available": down_ask is not None,
    }


def _price_inputs(context, history, now_ms):
    prices = context.get("prices", {})
    if not all(prices.get(source, {}).get("fresh")
               for source in ("pyth_btc_usd", "chainlink_twap_60s")):
        raise ValueError("reference_feed_stale")
    spot = points_before(history, "pyth_btc_usd", now_ms)
    twap = points_before(history, "chainlink_twap_60s", now_ms)
    if not spot or not twap:
        raise ValueError("insufficient_price_history")
    if now_ms - spot[-1][0] > 5000 or now_ms - twap[-1][0] > 5000:
        raise ValueError("reference_feed_stale")
    prior(spot, now_ms - 120_000)
    if any(b[0] - a[0] > 5000 for a, b in zip(spot, spot[1:])):
        raise ValueError("price_history_gap")
    return spot, twap


def build_components(context, history, quotes, microstructure, now_ms, remaining,
                     enabled=None):
    """Build only the requested families from information available at now_ms."""
    enabled = component_names(enabled)
    result = {}

    if "settlement" in enabled:
        if not context.get("reference_ready"):
            raise ValueError("opening_or_twap_unavailable")
        spot, twap = _price_inputs(context, history, now_ms)
        opening = float(context["reference"]["price_to_beat"])
        if opening <= 0:
            raise ValueError("opening_or_twap_unavailable")
        current_spot, current_twap = spot[-1][1], twap[-1][1]
        volatility120 = _volatility(spot, now_ms, 120)
        gap = math.log(current_twap / opening) * 10000
        spot_gap = math.log(current_spot / opening) * 10000
        # At decision time the final 60-second settlement TWAP is still in the
        # future. For zero-drift diffusion, the variance of that average is
        # sigma^2 * (wait_to_window + window/3).
        final_window_seconds = 60.0
        effective_seconds = max(float(remaining) - final_window_seconds, 0.0) + final_window_seconds / 3
        raw_final_twap_sigma_bps = max(volatility120 * math.sqrt(effective_seconds), .1)
        final_twap_sigma_bps = max(raw_final_twap_sigma_bps,
                                   FINAL_TWAP_SIGMA_FLOOR_BPS)
        final_twap_z = max(-8.0, min(8.0, spot_gap / final_twap_sigma_bps))
        final_twap_p_up = NormalDist().cdf(final_twap_z)
        result["settlement"] = {
            "twap_gap_bps": gap,
            "spot_gap_bps": spot_gap,
            "gap_over_expected_move": gap / max(volatility120 * math.sqrt(remaining), .1),
            "spot_twap_gap_bps": math.log(current_spot / current_twap) * 10000,
            "remaining_seconds": float(remaining),
            "spot_minus_open": current_spot - opening,
            "twap_minus_open": current_twap - opening,
            "raw_final_twap_sigma_bps": raw_final_twap_sigma_bps,
            "final_twap_sigma_bps": final_twap_sigma_bps,
            "final_twap_sigma_floor_bps": FINAL_TWAP_SIGMA_FLOOR_BPS,
            "final_twap_z_score": final_twap_z,
            "final_twap_p_up": final_twap_p_up,
            "crossing_probability": min(final_twap_p_up, 1 - final_twap_p_up),
        }

    if "movement" in enabled:
        spot, twap = _price_inputs(context, history, now_ms)
        current_spot = spot[-1][1]
        momentum = {seconds: math.log(current_spot / prior(spot, now_ms - seconds * 1000)) * 10000
                    for seconds in (5, 10, 30, 60, 120)}
        vol30 = _volatility(spot, now_ms, 30)
        vol120 = _volatility(spot, now_ms, 120)
        result["movement"] = {
            **{f"momentum_{seconds}": momentum[seconds] for seconds in (5, 10, 30, 60, 120)},
            "acceleration_10_30": momentum[10] - momentum[30] / 3,
            "volatility_30": vol30,
            "volatility_120": vol120,
            "volatility_ratio": vol30 / max(vol120, 1e-8),
            "twap_slope_30": math.log(twap[-1][1] / prior(twap, now_ms - 30_000)) * 10000,
            **movement_stability(spot, now_ms),
        }

    if "market" in enabled:
        result["market"] = market_book_view(quotes)

    if "order_flow" in enabled:
        if microstructure is None:
            raise ValueError("missing_component:order_flow")
        result["order_flow"] = microstructure.features(now_ms)

    validate_components(result, enabled)
    return result


class MicrostructureState:
    """Reconstruct the UP book and rolling UP-token order/trade pressure."""

    def __init__(self, tokens):
        self.up_token = next(token for token, outcome in tokens.items() if outcome == "Up")
        self.reset()

    def reset(self, received_ms=None):
        self.bids, self.asks = {}, {}
        self.ready = False
        self.connected_ms = received_ms
        self.last_received_ms = None
        self.last_connection_ms = received_ms
        self.last_source_ms = None
        self.flows, self.trades, self.mids = deque(), deque(), deque()

    @staticmethod
    def _trim(queue, now_ms, horizon=35_000):
        while queue and queue[0][0] < now_ms - horizon:
            queue.popleft()

    def observe(self, event, received_ms):
        kind = event.get("event_type")
        relevant_book_event = (kind == "book" and event.get("asset_id") == self.up_token)
        relevant_change_event = (kind == "price_change" and any(
            change.get("asset_id") == self.up_token for change in event.get("price_changes", [])))
        if (relevant_book_event or relevant_change_event) and event.get("timestamp") is not None:
            source_ms = int(event["timestamp"])
            if self.last_source_ms is not None and source_ms < self.last_source_ms:
                self.last_connection_ms = received_ms
                return
            self.last_source_ms = source_ms
        if kind == "book" and event.get("asset_id") == self.up_token:
            self.bids = {float(x["price"]): float(x["size"]) for x in event["bids"] if float(x["size"]) > 0}
            self.asks = {float(x["price"]): float(x["size"]) for x in event["asks"] if float(x["size"]) > 0}
            self.ready = bool(self.bids and self.asks)
        elif kind == "price_change":
            for change in event.get("price_changes", []):
                if change.get("asset_id") != self.up_token or change.get("side") not in ("BUY", "SELL"):
                    continue
                levels = self.bids if change["side"] == "BUY" else self.asks
                price, size = float(change["price"]), float(change["size"])
                old = levels.get(price, 0.0)
                if size > 0:
                    levels[price] = size
                else:
                    levels.pop(price, None)
                best = float(change["best_bid"] if change["side"] == "BUY" else change["best_ask"])
                near_top = abs(price - best) <= 0.031
                if near_top:
                    sign = 1 if change["side"] == "BUY" else -1
                    self.flows.append((received_ms, sign * (size - old)))
            self.ready = bool(self.bids and self.asks)
        elif kind == "last_trade_price" and event.get("asset_id") == self.up_token:
            side = event.get("side")
            if side in ("BUY", "SELL"):
                self.trades.append((received_ms, (1 if side == "BUY" else -1) * float(event["size"])))

        if self.bids and self.asks:
            self.mids.append((received_ms, (max(self.bids) + min(self.asks)) / 2))
        self.last_received_ms = received_ms
        self.last_connection_ms = received_ms
        for queue in (self.flows, self.trades, self.mids):
            self._trim(queue, received_ms)

    def connection_activity(self, received_ms):
        self.last_connection_ms = received_ms

    @staticmethod
    def _imbalance(values, now_ms, seconds):
        recent = [value for stamp, value in values if stamp >= now_ms - seconds * 1000]
        denominator = sum(abs(value) for value in recent)
        return sum(recent) / denominator if denominator else 0.0

    def _depth(self, levels):
        bid_prices = sorted(self.bids, reverse=True)[:levels]
        ask_prices = sorted(self.asks)[:levels]
        bid = sum(self.bids[p] for p in bid_prices)
        ask = sum(self.asks[p] for p in ask_prices)
        return (bid - ask) / (bid + ask) if bid + ask else 0.0

    def features(self, now_ms):
        if not self.ready or self.connected_ms is None or now_ms - self.connected_ms < 30_000:
            raise ValueError("order_flow_history_unavailable")
        if self.last_connection_ms is None or not -1000 <= now_ms - self.last_connection_ms <= 25000:
            raise ValueError("order_flow_stale")
        current_mid = (max(self.bids) + min(self.asks)) / 2 if self.bids and self.asks else None
        old_mid = next((value for stamp, value in reversed(self.mids)
                        if stamp <= now_ms - 10_000),
                       self.mids[0][1] if self.mids else current_mid)
        if not old_mid or not current_mid:
            raise ValueError("order_flow_history_unavailable")
        recent_trades = [value for stamp, value in self.trades if stamp >= now_ms - 30_000]
        return {
            "depth_imbalance_1": self._depth(1),
            "depth_imbalance_3": self._depth(3),
            "order_flow_5": self._imbalance(self.flows, now_ms, 5),
            "order_flow_30": self._imbalance(self.flows, now_ms, 30),
            "trade_imbalance_10": self._imbalance(self.trades, now_ms, 10),
            "trade_imbalance_30": self._imbalance(self.trades, now_ms, 30),
            "trade_count_30": float(len(recent_trades)),
            "market_momentum_10": (current_mid - old_mid) * 100,
        }


class PredictionEngine:
    """Build one live prediction for each BTC five-minute market."""

    def __init__(self, recorder):
        self.recorder = recorder
        self.decisions, self.microstructures = {}, {}
        self.window_seen = set()

    def start_connection(self, market, now_ms):
        state = self.microstructures.setdefault(market["slug"], MicrostructureState(market["tokens"]))
        state.reset(now_ms)

    def on_market_event(self, market, event, received_ms):
        state = self.microstructures.setdefault(market["slug"], MicrostructureState(market["tokens"]))
        state.observe(event, received_ms)

    def on_connection_activity(self, market, received_ms):
        state = self.microstructures.get(market["slug"])
        if state is not None:
            state.connection_activity(received_ms)

    def _publish_status(self, market, remaining, row=None):
        if not hasattr(self.recorder, "write_status"):
            return
        if row is None:
            self.recorder.write_status(
                "live-status.json", status="WAITING", slug=market["slug"],
                remaining_seconds=remaining,
                decision_window_seconds=[DECISION_WINDOW_CLOSE, DECISION_WINDOW_OPEN],
                message="Collecting inputs for the first complete snapshot from 120 to 110 seconds")
            return
        p_up = row.get("p_up")
        direction = row.get("direction", "SKIP")
        self.recorder.write_status(
            "live-status.json",
            status="SIGNAL" if direction in ("Up", "Down") else "SKIP",
            slug=row["slug"], decision_ms=row["decision_ms"],
            remaining_seconds=row["remaining"], direction=direction,
            reason=row.get("reason"), signal_method=row.get("signal_method"),
            probability_calibrated=row.get("probability_calibrated"),
            p_up=p_up, p_down=(1 - p_up) if p_up is not None else None,
            estimated_edge=row.get("estimated_edge"), agreement=row.get("agreement"),
            probability_before_cap=row.get("probability_before_cap"),
            probability_cap=row.get("probability_cap"),
            crossing_probability=row.get("crossing_probability"),
            distance_to_beat=row.get("distance_to_beat"),
            distance_source=row.get("distance_source"),
            market_stability=row.get("market_stability"),
            opening_price=row.get("opening_price"), component_errors=row.get("component_errors"),
            warnings=row.get("warnings", []),
            entry_asks={side: row.get("quotes", {}).get(side, {}).get("ask")
                        for side in ("Up", "Down")},
            message=("Prediction passed every filter" if direction in ("Up", "Down")
                     else "No signal: the prediction did not pass every filter"))

    def _record_round(self, row):
        if not hasattr(self.recorder, "append_round_history"):
            return
        p_up = row.get("p_up")
        self.recorder.append_round_history(
            "decision", row["slug"], start=row["start"], decision_ms=row["decision_ms"],
            remaining_seconds=row["remaining"], direction=row.get("direction"),
            reason=row.get("reason"), signal_method=row.get("signal_method"),
            probability_calibrated=row.get("probability_calibrated"),
            p_up=p_up, p_down=(1 - p_up) if p_up is not None else None,
            estimated_edge=row.get("estimated_edge"), agreement=row.get("agreement"),
            crossing_probability=row.get("crossing_probability"),
            distance_to_beat=row.get("distance_to_beat"),
            distance_source=row.get("distance_source"),
            market_stability=row.get("market_stability"),
            opening_price=row.get("opening_price"), opening_source=row.get("opening_source"),
            fee_rate=row.get("fee_rate"), quotes=row.get("quotes"),
            components=row.get("components"), component_errors=row.get("component_errors"),
            warnings=row.get("warnings", []), thin_gap=row.get("thin_gap"),
            thin_gap_threshold_bps=row.get("thin_gap_threshold_bps"),
            movement_disagreement=row.get("movement_disagreement"),
            combined_risk=row.get("combined_risk"))

    def observe(self, market, context, history, quotes, now_ms):
        slug = market["slug"]
        remaining = market["end"] - now_ms / 1000
        if remaining > DECISION_WINDOW_OPEN and slug not in self.decisions:
            self._publish_status(market, remaining)
        if DECISION_WINDOW_CLOSE <= remaining <= DECISION_WINDOW_OPEN:
            self.window_seen.add(slug)
        if remaining <= DECISION_WINDOW_OPEN and slug not in self.decisions:
            row = {"slug": slug, "start": market["start"], "decision_ms": now_ms,
                   "remaining": remaining, "components": {}, "component_errors": {},
                   "direction": "SKIP", "reason": None, "p_up": None,
                   "warnings": [],
                   "opening_price": context.get("reference", {}).get("price_to_beat"),
                   "opening_source": context.get("reference", {}).get("opening_source"),
                   "quotes": quotes, "fee_rate": None, "baselines": {}}
            if remaining < DECISION_WINDOW_CLOSE and slug not in self.window_seen:
                row["reason"] = "decision_window_missed"
            else:
                try:
                    row["fee_rate"] = market_fee_rate(market)
                except (ValueError, KeyError, TypeError) as error:
                    row["reason"] = str(error)
                for component in ALL_COMPONENTS:
                    try:
                        made = build_components(context, history, quotes,
                                                self.microstructures.get(slug), now_ms,
                                                remaining, (component,))
                        row["components"][component] = made[component]
                    except (ValueError, KeyError, TypeError, ArithmeticError) as error:
                        row["component_errors"][component] = str(error)
                if "settlement" in row["components"]:
                    row["baselines"]["ahead"] = ("Up" if row["components"]["settlement"]["twap_gap_bps"] >= 0
                                                   else "Down")
                if "market" in row["components"]:
                    row["baselines"]["market"] = ("Up" if row["components"]["market"]["p_up"] >= .5
                                                  else "Down")
                if "movement" in row["components"]:
                    movement = row["components"]["movement"]
                    row["market_stability"] = {key: movement[key] for key in (
                        "regime", "direction", "stability_score", "net_30_bps", "net_120_bps",
                        "range_30_bps", "range_120_bps", "path_efficiency_30",
                        "path_efficiency_120", "trend_r2_30", "trend_r2_120",
                        "reversal_rate_30", "reversal_rate_120", "largest_jump_share_30",
                        "largest_jump_share_120", "max_tick_30_bps", "max_tick_120_bps")}
                if not row["components"] and row["reason"] is None:
                    row["reason"] = "no_components_available"
                if row["reason"] is None:
                    try:
                        analytic = analytic_signal(
                            row["components"], quotes, row["fee_rate"])
                        row.update(direction=analytic["direction"], reason=analytic["reason"],
                                   p_up=analytic["p_up"], signal_method=analytic["method"],
                                   probability_calibrated=False,
                                   warnings=analytic.get("warnings", []))
                        for key in ("z_score", "agreement", "estimated_edge",
                                    "crossing_probability", "distance_to_beat",
                                    "distance_source", "probability_before_cap",
                                    "probability_cap", "thin_gap",
                                    "thin_gap_threshold_bps",
                                    "movement_disagreement", "combined_risk",
                                    "regime_risk", "risk_blocked", "risk_rule"):
                            if key in analytic:
                                row[key] = analytic[key]
                    except (ValueError, KeyError, TypeError) as error:
                        row["reason"] = str(error)
            required = CORE_COMPONENTS
            complete = (row["fee_rate"] is not None
                        and all(name in row["components"] for name in required))
            if not complete and remaining > DECISION_WINDOW_CLOSE:
                self._publish_status(market, remaining)
                return
            self.decisions[slug] = row
            self.recorder.write("strategy_decision", **row)
            self._record_round(row)
            self._publish_status(market, remaining, row)
            print(f"PREDICTION {slug}: {row['direction']} ({row['reason']})", flush=True)
