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
side, fresh data, sufficient visible liquidity, at least 98% confidence,
strength strictly above 2.5, and positive expected value after estimated fees.

## Close-to-line constraints

The strategy uses the smaller distance from the opening line:

```text
thin_gap_distance = min(abs(spot_gap_bps), abs(twap_gap_bps))
thin_gap = thin_gap_distance < 5
```

It skips the round when either a thin gap or movement disagreement appears:

```text
SKIP when thin_gap OR movement_disagreement
```

The `choppy_up` regime remains visible in the record but does not block a trade
by itself.

## Entry monitoring

After the initial decision, the trader watches until 30 seconds remain. A clean
signal enters the core path. A rejected signal enters the recovery path instead
of being discarded. Before each order attempt the trader rebuilds probability,
strength, movement warnings, market agreement, and the value limit from the
latest inputs. It waits while the selected contract is at or below 80 cents or
above 97 cents. A cheaper price is accepted only when that fresh calculation
still passes every rule.

A recovery candidate has its own prediction gate. It passes when the current
fresh side has at least 90% probability, strength strictly above 2.5, and both
the spot and TWAP gaps are at least 5 bps from the opening line. Movement,
market-direction, and choppy-regime disagreements remain visible as warnings
but do not block this path. The side may change while the candidate is being
monitored; execution uses the side that passes the fresh gate.

The passing side must hold those conditions for three consecutive fresh
snapshots over at least two seconds. A direction change restarts that short
confirmation window. This prevents snapshots for opposite sides from being
combined while still allowing a rejected market to become tradable later.

Real orders use bounded fill-or-kill execution. The bot checks the live ask and
the executable price encoded in the signed order before posting it. Fees,
visible liquidity, account cash and the session allocation cap are also checked
before submission. Each order uses 80% of eligible session trading capital.
After a win, only the original trade cost returns to trading capital; profit is
stored in the profit-trading wallet. Core trades continue to use 80% of core
capital. Recovered trades use 50% of the profit-trading wallet and cannot use
core capital or the safe reserve. If that allocation is below the exchange's
$1 principal minimum, it is raised to the minimum only when the profit wallet
can cover the principal and fee. A loss reduces only the wallet that funded the
trade.

The dashboard can disable recovered execution without disabling observation.
The bot still records the first fillable recovered opportunity and settles its
hypothetical result. Core trading, profit trading, and safe reserve are separate
persistent ledger balances. Funds can be transferred between them explicitly
from the Profit trades screen.
