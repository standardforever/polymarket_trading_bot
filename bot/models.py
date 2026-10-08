"""Immutable probability-model releases used by trading sessions."""

import math
from statistics import NormalDist


CORRECTED_MODEL = "btc_twap_v2"
REGIME_MODEL = "btc_regime_v3"
COMBINED_MODEL = "btc_guard_v4"
DEFAULT_MODEL = CORRECTED_MODEL

MODEL_RELEASES = (
    {
        "id": CORRECTED_MODEL,
        "name": "BTC TWAP v2",
        "description": "Corrected formula with a 2 bps uncertainty floor and 98% probability cap.",
        "default": True,
        "experimental": False,
        "recommended_confidence": .90,
        "risk_rule": "thin_gap_and_movement_disagreement",
    },
    {
        "id": REGIME_MODEL,
        "name": "BTC Regime Guard v3",
        "description": ("Experimental V2 probability with a regime-aware close-to-line guard; "
                        "recommended paper threshold 87.5%."),
        "default": False,
        "experimental": True,
        "recommended_confidence": .875,
        "risk_rule": "thin_gap_and_choppy_up",
    },
    {
        "id": COMBINED_MODEL,
        "name": "BTC Dual Guard v4",
        "description": ("Experimental V2 probability with both close-to-line guards, "
                        "a conservative spot-or-TWAP gap, and a 90% threshold."),
        "default": False,
        "experimental": True,
        "recommended_confidence": .90,
        "risk_rule": "thin_gap_and_movement_or_choppy_up",
        "maximum_adverse_price_move": .15,
    },
)

_RELEASES_BY_ID = {release["id"]: release for release in MODEL_RELEASES}


def model_releases():
    return [dict(release) for release in MODEL_RELEASES]


def model_release(model_version):
    validate_model_version(model_version)
    return dict(_RELEASES_BY_ID[model_version])


def validate_model_version(model_version):
    if model_version not in _RELEASES_BY_ID:
        raise ValueError("unknown_model_version")
    return model_version


def probability_snapshot(component_rows, model_version=DEFAULT_MODEL):
    """Calculate one versioned UP probability from a component snapshot."""
    validate_model_version(model_version)
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
    chance = p_up if side == "Up" else 1 - p_up
    return {
        "model_version": model_version,
        "p_up": p_up,
        "p_down": 1 - p_up,
        "side": side,
        "chance": chance,
        "raw_sigma_bps": raw_sigma,
        "applied_sigma_bps": applied_sigma,
        "z_score": z_score,
        "probability_before_cap": probability_before_cap,
        "probability_cap": probability_cap,
    }


def probability_releases(component_rows):
    return {release["id"]: probability_snapshot(component_rows, release["id"])
            for release in MODEL_RELEASES}


def probability_from_decision(row, model_version=DEFAULT_MODEL):
    """Read a recorded release, or reproduce it from stored components."""
    validate_model_version(model_version)
    recorded = (row.get("model_probabilities") or {}).get(model_version)
    if recorded and recorded.get("p_up") is not None:
        return dict(recorded)
    components = row.get("components") or {}
    if "settlement" in components and "movement" in components:
        return probability_snapshot(components, model_version)
    p_up = row.get("p_up")
    if p_up is None:
        raise ValueError("model_probability_unavailable")
    p_up = float(p_up)
    side = "Up" if p_up >= .5 else "Down"
    return {
        "model_version": row.get("model_version") or "unversioned",
        "p_up": p_up,
        "p_down": 1 - p_up,
        "side": side,
        "chance": max(p_up, 1 - p_up),
    }
