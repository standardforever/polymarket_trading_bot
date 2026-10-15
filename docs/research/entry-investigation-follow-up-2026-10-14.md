# Follow-up: investigate a selective BTC entry policy

Date: 14 October 2026, Africa/Lagos. Research and report artifacts only. No production code, account orders, active session settings, or services were changed.

## Finding and recommended next candidate

The balanced threshold of absolute gap-to-uncertainty score at least 2.5 remains a reasonable candidate for further validation. The threshold of 3.0 is materially less likely to provide two entries daily.

Refine the proposed confirmation rule: confirm the **BTC signal** over three elapsed seconds using genuinely new reference reports; separately check the market direction, executable price, and complete-order liquidity at the entry instant. Recheck all current conditions after order preparation and immediately before posting. If they fail, keep watching within the cutoff. Do not require a tradable quote to persist continuously throughout the confirmation interval.

This is a proposed policy, not an established profitable or zero-risk strategy. One external execution day and part of one fresh local day cannot validate daily reliability.

## 1. New local observations after the initial investigation

The earlier report was written at approximately 00:29 Lagos time. To avoid counting rounds inspected before the proposal as forward evidence, this follow-up uses rounds starting at or after **00:40**. The fixed snapshot ends at the round starting **15:50**, with its later supported settlement recorded.

There are 180 resolved rounds with the necessary features in this snapshot. Outcomes are the supported collector resolutions, not provisional account-position results. The candidate thresholds are the same 2.5 and 3.0 thresholds proposed earlier; they were not fitted to these new outcomes.

| Decision-time selection | Predictions | Wins | Losses | Price/edge qualified in recorded snapshot | Enough displayed top-ask shares for $100 principal |
| --- | ---: | ---: | ---: | ---: | ---: |
| Confidence at least 90% | 104 | 99 | 5 | 33 | 23 |
| Confidence at the 98% cap | 69 | 69 | 0 | 11 | 8 |
| Clean capped 98% | 36 | 36 | 0 | 3 | 3 |
| Clean, score at least 2.5 | 34 | 34 | 0 | 2 | 2 |
| Clean, score at least 3.0 | 27 | 27 | 0 | 1 | 1 |

The price check requires ask above 80 cents and at least a 3-cent expected edge after the recorded fee schedule. These table counts are static snapshot checks. They do not establish three-second persistence, maintained execution liquidity, or an actual exchange fill. The first two general rows also include warnings/risk blocks; their price-qualified counts are not final trader eligibility.

Both candidate thresholds avoided the five sub-98% losing predictions. However, **every capped prediction won in this new window**, so this window alone does not establish that adding cleanliness or score filtering improves accuracy over the cap-only policy. It supports the candidates' consistency, not their superiority.

The fresh balanced group includes 22 Down and 12 Up predictions. A favourable directional regime could contribute to the clean results; do not infer that future mixed or stressed regimes will behave identically.

### Two concrete balanced opportunities

| Round | Round start, Lagos | Side | Score | Recorded ask | Top-ask shares | Official outcome |
| --- | --- | --- | ---: | ---: | ---: | --- |
| 1791954900 | 06:15 | Down | 3.150 | 94 cents | 1,010.81 | Down |
| 1791962100 | 08:15 | Down | 2.589 | 94 cents | 1,375.67 | Down |

These are round start times, not claimed purchase times. The normal decision occurs roughly three minutes later.

At a hypothetical $100 principal purchase at 94 cents, the estimated crypto taker fee is $0.42 and a win earns approximately $5.96 after that fee. Two such wins would total **+$11.93**. The bot did not make those hypothetical $100 trades. This is not an available-balance or compounding calculation; actual cash for principal and fees is required, and resolved proceeds may remain unavailable until redeemed.

The score≥3.0 policy qualifies only the first of these two snapshot opportunities. The clean cap-only policy has a third winning quoted opportunity at 89 cents, but its score is approximately 2.081 and it is excluded by both proposed strength thresholds. Stronger filtering therefore has a real opportunity cost even in this fresh window.

### Statistical limits

The fresh 34/34 result is a small sample. Under a fixed-rule independent-binomial assumption, zero failures in 34 still permits a one-sided 95% upper failure rate of about 8.43%. Temporal dependence can weaken that interpretation further.

Pooling earlier complete-warning data and these new rounds gives 238/238 for score≥2.5 and 201/201 for score≥3.0. Those pooled zero-loss counts are descriptive only: the earlier data were used to discover the filters. They are not prospective statistical guarantees.

## 2. What the filters select among actual historical bot fills

Using the original decision-time components for 82 recorded real-session fills with supported outcomes:

| Retrospective selection | Actual historical fills in selection | Wins | Losses |
| --- | ---: | ---: | ---: |
| All recorded bot fills | 82 | 70 | 12 |
| Fills with recorded confidence capped at 98% | 46 | 44 | 2 |
| Clean score≥2.5, actual fill price above 80 cents | 8 | 8 | 0 |
| Clean score≥3.0, actual fill price above 80 cents | 4 | 4 | 0 |

This does not reconstruct whether those fills would still qualify after live revalidation or a persistence rule. It also does not predict how many previously skipped rounds a revised policy could enter. Historical filled trades are a selected subset of all rounds, and the hypothetical selection is influenced by development history.

## 3. A stricter execution replay

The independent September 8 sample was replayed with the same two strength thresholds. This follow-up tightened several assumptions from the first report:

- Only markets whose exact TWAP endpoints independently reproduced the supplied outcome are used: 253 markets.
- Source age is included in the remaining forecast horizon, rather than treating an older price as observed at the current instant.
- Momentum uses wall-clock lookbacks, matching the current component method more closely.
- The first complete reference/book snapshot in the 120–110-second window defines the original candidate side. Dynamic candidates normally retain that side.
- The entire hypothetical $100 principal must fit within visible depth at an acceptable price **before submission**, as well as in the delayed matching state.
- Both books and the unthrottled quote stream must agree on top ask.
- Signed-price limits are rounded down to the one-cent bound used by the current execution code.
- The original signal-price adverse-move constraint is retained.
- A three-second confirmation requires elapsed time and at least three distinct source reports from each reference feed. Repeated snapshots of one report do not count as new evidence.
- Order attempts do not overlap: a retry cannot start before the previous simulated submission delay has elapsed.
- A fill below the entry floor is retained as a breach, not removed to flatter the results.

The instantaneous reference in the external dataset is Chainlink; the local instantaneous input is named `pyth_btc_usd`. This remains a structural/execution research replay, not an exact reproduction of the running bot's feed or user settings.

The frozen-confidence comparisons approximate the existing candidate flow with equal $100 principal per trade and its price/risk safeguards. They do not replicate warning allocation fractions, account balances, or every authenticated SDK request. They use full-depth availability rather than reproducing the running trader's stricter top-size-only trigger. Therefore they are comparison policies, not claimed counterfactual real fills.

### One-second submission delay; entries before the final averaging window

| Comparison policy | Simulated entries | Wins | Losses |
| --- | ---: | ---: | ---: |
| Frozen initial confidence≥90%; existing combined-risk policy | 47 | 44 | 3 |
| Frozen initial confidence capped at 98%; existing combined-risk policy | 28 | 28 | 0 |
| Fresh clean capped confidence, original side | 8 | 8 | 0 |
| Fresh clean score≥2.5, no persistence | 6 | 6 | 0 |
| Score≥2.5; signal, market and book remain qualified for 3 seconds | 2 | 2 | 0 |
| Score≥3.0; same full confirmation | 0 | 0 | 0 |

The revised two-entry result is lower than the earlier report's three-entry estimate because the protocol is stricter. The earlier result should not be treated as a validated fill count.

Importantly, the cap-only frozen comparison also had no losses on this day. The replay does not demonstrate that the balanced candidate is more profitable or safer than capped-confidence trading. It shows the cost of selecting and confirming fewer opportunities under the proposed constraints.

### Latency sensitivity of the balanced full-confirmation policy

| Total assumed submission delay | Simulated entries | Wins | Losses |
| --- | ---: | ---: | ---: |
| 0 ms | 3 | 3 | 0 |
| 1,000 ms | 2 | 2 | 0 |
| 2,500 ms | 1 | 1 | 0 |
| 5,000 ms | 0 | 0 | 0 |

Longer delay changes which events are available and which prices match. Counts need not be monotonic under every tested policy. A better price caused by a decline during latency is not evidence that slower execution is desirable.

## 4. Refine what gets confirmed

There are two distinct confirmation ideas:

1. **Confirm the BTC state:** probability, strength, direction, and warning status remain qualified while new reference reports arrive.
2. **Confirm an entry:** current market direction, price, and enough liquidity qualify at the moment of purchase.

Requiring every temporary quote/book condition to qualify continuously can reset confirmation even when the BTC signal remains strong. Price and depth can legitimately change during the waiting period. This may unnecessarily shrink the opportunity set.

In the tighter one-second replay:

| Three-second confirmation method | Simulated $100 entries | Wins | Losses |
| --- | ---: | ---: | ---: |
| Entire signal/market/book qualification | 2 | 2 | 0 |
| BTC qualification, then fresh market/book check | 5 | 5 | 0 |

This is a promising architecture distinction, not a statistically established superiority. Both methods use the same strength threshold and keep all purchase-time requirements.

A one-second full confirmation was also tested; it gave two simulated entries under the one-second delay. That is not enough evidence to choose one versus three seconds based on accuracy.

## 5. Revalidate after order preparation

A prepared order can consume time before it is posted. A BTC forecast checked at the initial trigger can deteriorate during account checks and signing, even when it passed a persistence rule.

One balanced full-confirmation simulated entry had a score of about 2.90 at the trigger, then approximately 2.27 one second later. The confidence still displayed 98%. The round ultimately won; this example demonstrates score deterioration, not an observed losing mechanism.

An additional research variant used BTC-only three-second confirmation, an 800 ms preparation period, a fresh full qualification check before posting, then the remaining assumed submission delay. This is a hypothetical split, not a measured breakdown of all current requests.

| Total assumed delay | Simulated entries | Wins | Losses |
| --- | ---: | ---: | ---: |
| 1,000 ms | 5 | 5 | 0 |
| 2,500 ms | 2 | 2 | 0 |
| 5,000 ms | 0 | 0 | 0 |

This is the preferred next candidate to observe prospectively. It preserves the signal confirmation while checking the final entry against the newest locally available state. It cannot guarantee the signal remains qualified during exchange matching.

In its one-second replay, the first two hypothetical $100 entries produced approximately +$13.00 after modeled fees. Other confirmation variants picked different first two entries. These are sample-specific outcomes, not reliable daily profit forecasts.

## 6. The 80-cent floor remains an execution limitation

The frozen 98%-confidence comparison produced a simulated below-80-cent fill at a five-second delay, even though the initial quote passed the floor. This is consistent with the limitation already seen in the real 79-cent fill.

A BUY bound controls the maximum accepted price. It cannot impose a minimum paid price. FOK controls whether the order fills completely. Narrowing the maximum BUY price cannot stop a cheaper fill. [Polymarket order documentation](https://docs.polymarket.com/trading/place-orders#market-orders).

No below-floor fill was observed for the confirmed balanced cases in this replay, but that does not establish protection against a faster collapse or a different day. Entry buffers and local revalidation can reduce exposure; they cannot create an exchange-supported lower BUY bound.

The replay rejects inconsistent or unavailable delayed book states. Such a rejection is missing execution evidence, not proof that the venue would reject the order. In live execution, an ambiguous submission must retain duplicate suppression and reconciliation. Archived snapshots cannot guarantee which order would have matched ours or whether visible liquidity would survive competing orders.

## 7. Entering after 60 seconds remains a separate experiment

A research-only partial-window model was evaluated down to 31 seconds remaining. It integrates the observed portion of the last 60 seconds from the received instantaneous tape and forecasts the unknown remainder with zero drift. Its variance shrinks with the cube of the remaining unobserved interval; the 2 bps uncertainty floor is retained.

This is an approximation. Chainlink's exact TWAP uses information not fully present in a one-second instantaneous tape. It is not an exact reconstruction or a proposed production replacement.

The tested extension did not add entries to the balanced or stricter policies at one-second or 2.5-second delays. Many late predictions reached the probability cap, but either lacked a complete affordable quote state or failed other qualification. The experiment therefore gives no reason to rush a partial-window model into the existing 30-second cutoff.

## 8. Decision for the next research stage

Prefer the **balanced threshold of 2.5** for prospective observation. Keep the threshold of 3.0 as a more selective comparison; it has poor daily-flow evidence.

The candidate specification should be:

1. Use the official opening and 60-second settlement configuration, with synchronized fresh reference inputs.
2. Require current capped confidence of 98%, absolute score≥2.5, and complete warning information with no active thin-gap, movement-disagreement, or choppy-Up flag.
3. Confirm the same BTC side for three elapsed seconds with new reference reports; do not count stale repeats.
4. At entry, require market direction agreement, price above 80 cents, the retained adverse-move bound, minimum edge after fees, and complete-order liquidity.
5. After account checks/order preparation, revalidate the current BTC and market state locally immediately before posting. A failed check returns to monitoring.
6. Preserve the original candidate side, immutable initial decision, and duplicate-order protection.
7. Use a maximum of two fills per Lagos calendar day. A lack of qualified entries is a valid result.
8. Apply a small, explicit dollar loss budget independently of probability. A high success rate does not make an all-in stake safe.

At 94 cents, one approximately $100.42 losing purchase consumes about seventeen $5.96 winning profits. At $100 principal per trade, two winning daily trades can meet the $10 aspiration, but the associated dollar loss must fit a much larger bankroll or a deliberately accepted risk budget. The historical outcomes do not establish a guaranteed minimum daily income. Fee calculations follow the market schedule described in [Polymarket fees](https://docs.polymarket.com/trading/fees).

To evaluate the full policy on new local data, we need compact second-by-second reference/forecast, quote, and relevant depth records, plus attempted/not-filled/ambiguous/filled execution states. The app currently removes resolved raw round tapes. The 180 new compact decisions cannot establish persistence or recreate later entry prices.

Continue testing across several complete days and different volatility regimes. Count unique rounds for signal accuracy and exchange-confirmed fills for execution accuracy. Compare zero/one/two-entry days and the first two eligible entries, not the retrospectively best two. Keep the specification frozen while collecting those results.

The probability formula was not changed. The strongest actionable research finding remains **fresh validity at purchase time**, with the 2.5 selector as a candidate rather than a promise.
