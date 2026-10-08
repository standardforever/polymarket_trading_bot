"""Probability calculation and fixed safeguards for the BTC strategy."""

import math
from statistics import NormalDist


RECOMMENDED_CONFIDENCE = .90
MAXIMUM_ADVERSE_PRICE_MOVE = .15
RISK_RULE = "thin_gap_and_movement_or_choppy_up"


def probability_snapshot(component_rows):
    """Calculate the strategy's UP probability from one component snapshot."""
    settlement = component_rows["settlement"]
    movement = component_rows["movement"]
    gap = float(settlement["spot_gap_bps"])
    remaining = float(settlement["remaining_seconds"])
    volatility = float(movement["volatility_120"])
    effective_seconds = max(remaining - 60.0, 0.0) + 20.0
    raw_sigma = max(volatility * math.sqrt(effective_seconds), .1)
    applied_sigma = max(raw_sigma, 2.0)
    z_score = max(-8.0, min(8.0, gap / applied_sigma))
    probability_before_cap = NormalDist().cdf(z_score)
    probability_cap = .98
    p_up = max(1 - probability_cap, min(probability_cap, probability_before_cap))
    side = "Up" if p_up >= .5 else "Down"
    return {
        "p_up": p_up,
        "p_down": 1 - p_up,
        "side": side,
        "chance": p_up if side == "Up" else 1 - p_up,
        "raw_sigma_bps": raw_sigma,
        "applied_sigma_bps": applied_sigma,
        "z_score": z_score,
        "probability_before_cap": probability_before_cap,
        "probability_cap": probability_cap,
    }


def probability_from_decision(row):
    """Read a recorded probability, or reproduce it from stored components."""
    components = row.get("components") or {}
    if "settlement" in components and "movement" in components:
        return probability_snapshot(components)
    p_up = row.get("p_up")
    if p_up is None:
        raise ValueError("probability_unavailable")
    p_up = float(p_up)
    side = "Up" if p_up >= .5 else "Down"
    return {
        "p_up": p_up,
        "p_down": 1 - p_up,
        "side": side,
        "chance": max(p_up, 1 - p_up),
    }
