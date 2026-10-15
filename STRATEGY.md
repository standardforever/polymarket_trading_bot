# BTC prediction and trading strategy

The bot uses one analytic probability calculation and one fixed set of safety
constraints. It is an engineering estimate rather than a statistically trained
or calibrated forecast.

## Probability

The inputs are the official BTC opening price, current Pyth BTC/USD spot price,
Chainlink 60-second TWAP, seconds remaining, and recent realized volatility.
Price gaps use log returns in basis points:

```text
spot_gap_bps = ln(current_spot / opening_price) * 10,000
twap_gap_bps = ln(current_twap / opening_price) * 10,000
```

The remaining uncertainty estimate is:

```text
effective_seconds = max(remaining_seconds - 60, 0) + 20
raw_sigma_bps = max(volatility_120_bps * sqrt(effective_seconds), 0.1)
applied_sigma_bps = max(raw_sigma_bps, 2.0)
z = clamp(spot_gap_bps / applied_sigma_bps, -8, 8)
p_up = clamp(standard_normal_CDF(z), 0.02, 0.98)
p_down = 1 - p_up
```

The 2 bps sigma floor limits overconfidence during unusually quiet samples. The
2%-98% probability cap prevents the result from claiming certainty. The side
with the larger probability is the predicted side.

The collector takes the first complete settlement, movement, and Polymarket
snapshot between 120 and 110 seconds remaining. If no complete snapshot exists,
the round is skipped.

## Direction agreement and value

Movement direction is a majority vote over 10, 30 and 60-second momentum plus
the 30-second TWAP slope. Polymarket direction comes from its current implied
probability. A trade requires Polymarket direction to agree with the predicted
side, fresh data, sufficient visible liquidity, the session confidence
threshold, and the session minimum edge after estimated fees.

## Close-to-line constraints

The strategy uses the smaller distance from the opening line:

```text
thin_gap_distance = min(abs(spot_gap_bps), abs(twap_gap_bps))
thin_gap = thin_gap_distance < 5
```

It skips the round when a thin gap appears together with either movement
disagreement or a `choppy_up` regime:

```text
SKIP when thin_gap AND (movement_disagreement OR choppy_up)
```

A thin gap or movement disagreement by itself remains a warning. The trading
session controls the percentage of normal position size used for either
warning; setting a percentage to zero skips that warning type.

## Entry monitoring

After a qualified signal, the trader watches for an executable ask below the
maximum price allowed by the configured edge. It stops opening new entries at
the session cutoff. It pauses entry while the selected contract is at or below 80
cents or more than 10 cents below its signal-time ask. The candidate remains
active and may trade if the price recovers to both limits before the cutoff.

Real orders use bounded fill-or-kill execution. The bot checks the live ask and
the executable price encoded in the signed order before posting it. Fees,
visible liquidity, account cash and the session allocation cap are also checked
before submission.
