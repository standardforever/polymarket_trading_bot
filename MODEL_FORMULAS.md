# BTC model formulas (V2-V4)

This file describes the formulas currently implemented in `bot/models.py`,
`bot/prediction.py`, and `bot/paper.py`. These are analytic estimates, not a
trained or statistically calibrated model.

## 1. Inputs and units

- **Opening price:** official BTC price to beat for the five-minute market.
- **Spot price:** current Pyth BTC/USD price.
- **Current TWAP:** current Chainlink 60-second TWAP.
- **Remaining time (`R`):** seconds until the five-minute market ends.
- **Polymarket book:** live Up/Down bids, asks, sizes, and fee schedule.
- **bps:** basis points. `1 bps = 0.01%`.

Price distances use log returns:

```text
spot_gap_bps = ln(current_spot / opening_price) * 10,000
twap_gap_bps = ln(current_twap / opening_price) * 10,000
```

A positive gap supports **Up**; a negative gap supports **Down**. Dollar gap
and bps are different: a $10 move near $84,000 is about 1.19 bps.

## 2. Shared probability calculation

The bot measures 120-second spot volatility from consecutive price updates:

```text
variance_per_second = sum(ln(price_i / price_i-1)^2) / elapsed_seconds
volatility_120_bps  = sqrt(variance_per_second) * 10,000
```

It estimates how much uncertainty remains before the final 60-second
settlement TWAP is complete:

```text
effective_seconds = max(R - 60, 0) + 20
raw_sigma_bps     = max(volatility_120_bps * sqrt(effective_seconds), 0.1)
```

The extra `20` is `60 / 3`, the variance contribution of averaging prices over
the final 60-second window. Around 120 seconds remaining,
`effective_seconds = 80`.

All current models then calculate:

```text
z      = clamp(spot_gap_bps / applied_sigma_bps, -8, +8)
p_up   = standard_normal_CDF(z)
p_down = 1 - p_up
side   = Up if p_up >= 0.50, otherwise Down
chance = max(p_up, p_down)
```

The probability uses the **spot gap**. The current TWAP gap is recorded and is
also used by V4's close-to-line guard, but it is not inserted into the
probability equation.

The engine takes the first complete settlement, movement, and Polymarket-book
snapshot between 120 and 110 seconds remaining. If no complete snapshot exists
by the end of that window, the round is recorded as a skip.

Related fields shown in logs and the UI are:

```text
spot_twap_gap_bps = ln(current_spot / current_twap) * 10,000
gap_over_expected_move = twap_gap_bps /
                         max(volatility_120_bps * sqrt(R), 0.1)
crossing_probability = min(p_up, p_down)
```

`crossing_probability` is the model's probability of the less likely final
side under this approximation. It is a risk display field and is not a
separate trade gate.

## 3. Model versions

| Version | Probability | Default confidence | Hard close-to-line rule |
|---|---|---:|---|
| **V2 — BTC TWAP v2** | Sigma floor of 2 bps; probability capped at 2%-98% | 90% | Thin spot gap **and** movement disagreement |
| **V3 — BTC Regime Guard v3** | Same as V2 | 87.5% | Thin spot gap **and** `choppy_up` regime |
| **V4 — BTC Dual Guard v4** | Same as V2 | 90% | Thin conservative gap **and** (movement disagreement **or** `choppy_up`) |

### V2

```text
applied_sigma_bps = max(raw_sigma_bps, 2.0)
p_up_raw = normal_CDF(spot_gap_bps / applied_sigma_bps)
p_up     = clamp(p_up_raw, 0.02, 0.98)
```

The 2 bps floor prevents quiet short samples from creating unrealistically
large z-scores. The 98% cap prevents the model from reporting certainty.

### V3

V3 uses V2's exact probability. Its difference is the risk rule and its lower
recommended threshold of 87.5%:

```text
SKIP when abs(spot_gap_bps) < 5
          AND market_regime == choppy_up
```

Thin gap plus movement disagreement is only a warning in V3 unless the path is
also classified as `choppy_up`.

### V4

V4 also uses V2's exact probability. It uses the smaller distance from the
opening line, making the thin-gap test more conservative:

```text
thin_gap_distance = min(abs(spot_gap_bps), abs(twap_gap_bps))
thin_gap = thin_gap_distance < 5

SKIP when thin_gap
          AND (movement_disagreement OR market_regime == choppy_up)
```

V4 also refuses an entry while the selected share ask has fallen more than
15 cents below its ask at signal time. It keeps monitoring until the entry
cutoff in case the price returns within that limit.

## 4. Movement and regime rules

Movement direction is a majority vote over:

```text
momentum_10_bps
momentum_30_bps
momentum_60_bps
twap_slope_30_bps
```

Each value above `+0.01` votes Up, below `-0.01` votes Down, and otherwise is
neutral. A tied total is neutral. Movement disagreement is true when this
result does not equal the model side; neutral therefore counts as disagreement.

The regime uses the 120-second spot path:

```text
path_efficiency = abs(net_move_bps) / sum(abs(each_tick_move_bps))
reversal_rate   = direction_changes / possible_direction_changes

steady: efficiency >= 0.65 and reversal_rate <= 0.35
choppy: efficiency <= 0.35 or  reversal_rate >= 0.60
mixed:  everything between those boundaries
```

Direction is Flat when `abs(net_120_bps) < 0.1`, otherwise Up or Down. The bot
combines shape and direction into labels such as `steady_down`, `choppy_flat`,
or `choppy_up`.

## 5. Polymarket agreement, liquidity, and edge

Polymarket probability is derived from executable complementary bounds:

```text
lower = max(up_bid, 1 - down_ask)
upper = min(up_ask, 1 - down_bid)
polymarket_p_up = (lower + upper) / 2
```

All versions require Polymarket's direction to agree with the model side.
Polymarket probability does **not** change the model probability.

For the displayed signal, the checks run in this order: model confidence,
model-specific close-to-line risk, Polymarket direction agreement, executable
liquidity, and minimum edge. A trading session then applies its own selected
model, user confidence threshold, risk sizing, and entry settings.

The signal also requires a healthy book, a direct executable ask, and at least
5 shares displayed at the top ask. Value is calculated per share:

```text
fee_per_share  = fee_rate * ask * (1 - ask)
estimated_edge = chance - ask - fee_per_share
```

The standard signal requires `estimated_edge >= 0.03` (3 cents per share).
The trader's configured minimum edge can differ. It calculates the highest
acceptable entry price by solving:

```text
chance - price - fee_rate * price * (1 - price) >= minimum_edge
```

The live trader monitors from the decision until the configured cutoff,
30 seconds remaining by default. It buys only when the current executable ask
is at or below that maximum price and the complete order can fill. Real orders
are bounded fill-or-kill orders.

## 6. Trade sizing and warnings

The normal budget is:

```text
base_budget = bankroll * user_allocation_percentage
base_budget = min(base_budget, optional_maximum_trade_dollars)
```

Legacy sessions without a chosen percentage use 80% above a $20 bankroll and
100% at or below $20. The configured risk multiplier is then applied:

```text
thin gap warning            -> base_budget * thin_gap_percentage
movement disagreement only -> base_budget * movement_warning_percentage
no warning                  -> base_budget
```

If the selected model's combined risk rule fires, the trade is skipped before
sizing. A warning percentage of `0%` also means skip. A non-zero reduced live
budget is raised to Polymarket's $1 market-buy minimum when the bankroll and
the user's dollar cap allow it.

After a fill:

```text
paper shares = budget / (entry_price + fee_per_share)
win payout = shares * $1
loss payout = $0
profit = payout - total_trade_cost
```

For real trades, the exchange decides the final shares and execution price;
the bot records the confirmed principal and share amounts returned by
Polymarket and then calculates the effective price and fee.

Session limits—maximum trades, profit target, loss limit, available bankroll,
and stop request—can stop new entries independently of the model.

## 7. Features recorded but not used in probability

The bot records 5/10/30/60/120-second momentum, acceleration, 30/120-second
volatility, stability, order-book depth imbalance, order additions and
cancellations, executed-trade imbalance, and Polymarket price momentum.

Currently:

- movement votes and regime can warn, block, or reduce size;
- Polymarket direction, ask, liquidity, and fees gate the trade;
- order-flow features are diagnostic only and do not alter probability,
  direction, or trade eligibility;
- V2, V3, and V4 always produce the same probability from the same snapshot.
