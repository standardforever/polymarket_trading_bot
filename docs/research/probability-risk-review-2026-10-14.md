# BTC five-minute bot: probability, entry quality, and risk review

Research date: 14 October 2026, Africa/Lagos. Application code, settings, and active trading sessions were not changed by this research.

## Recommendation

Keep the current settlement probability as a baseline. The most useful next experiment is a selective strategy that refreshes the forecast while waiting for an entry, retains the uncapped gap-to-uncertainty score, and only buys when forecast quality, current BTC conditions, executable price, and order depth qualify together.

Test two candidate policies before selecting one:

- **Balanced selective:** current probability reaches its 98% cap, full warning inputs are available, no thin-gap/movement-disagreement/choppy-Up warning, absolute gap-to-uncertainty score at least 2.5, and the same side stays qualified for three elapsed seconds.
- **More selective:** the same conditions with a score of at least 3.0. This trades fewer opportunities and may produce zero or one fill on a day.

These thresholds are research candidates discovered in data already inspected. They are not validated production settings or evidence of zero risk. Keep the confidence cap; a large score is a ranking/selection feature, not permission to display a 99.9% probability without calibration.

For daily flow, make two trades a **maximum**, not a compulsory minimum. A safe-looking outcome prediction can still be unavailable at an economically sensible price.

## 1. Data and audit boundaries

The local audit joined current round history to archived histories, deduplicated by market slug, and used the supported collector resolution. Account-position provisional results were not used as outcome labels. It examined all probability levels, not only traded rounds or signals above 90%.

At the research snapshot:

| Item | Count |
| --- | ---: |
| Unique decision rounds | 2,237 |
| Unique supported resolutions | 2,228 |
| Resolved rounds with required price/volatility/time features | 2,123 |
| Excluded for missing features or settlement prices | 105 |
| Decisions without supported resolution | 9 |
| Earlier evaluation slice, September 24–29 | 1,461 |
| Later evaluation slice, October 7–14 | 662 |

Coverage has substantial gaps. These counts do not represent three continuous weeks of observation. Some listed dates contain only a partial day or a few rounds.

The current formula was recalculated from the stored decision-time components. In 397 rounds it differed from the recorded probability because older formulas/caps were used historically. This is a retrospective current-formula comparison, not a claim that the current bot ran unchanged across the entire archive. All comparable opening prices in decision and resolution records agreed; no mismatched opening-price case was detected.

The later slice provides a chronological check, but it is not a genuinely untouched future holdout: those historical dates have already been discussed and examined during development. Reported filter results also have selection bias because we inspected multiple filters.

### Warning completeness

The signal code assigns warnings only after probability reaches 90%. Below that, an empty warnings array does not prove there was no risky condition. Earlier records also lack a market regime field.

This audit recomputed thin gap as `min(abs(spot_gap_bps), abs(twap_gap_bps)) < 5`, and recreated agreement/risk features. Any analysis described as having complete warning information additionally requires a recorded movement regime. Earlier records without this information are not counted as genuinely clean in the recommendation tables.

### Independent downloaded data

Downloaded a free, separate BTC five-minute day dated September 8, 2026 from the data publisher's public release. It includes official market metadata/outcome snapshots, Chainlink instantaneous prices, Chainlink 60-second TWAP prices, full order-book snapshots, and the unthrottled best-bid/ask stream. All five compressed files passed the publisher's SHA-256 checksums.

The market file contains 292 rows. There were 274 resolved markets with a usable strike; 253 had exact TWAP reports at both boundary seconds and independently reproduced the published outcome. At 120 seconds remaining, the replay had 255 usable price/history snapshots, of which 239 had that independent endpoint verification. Markets without both endpoint ticks were scored using their supplied resolved outcome, not interpolated into an alleged verified outcome.

For the strict dynamic execution results, every selected market had independently verified endpoints.

The external instantaneous stream is Chainlink, while the local formula's instantaneous source is named `pyth_btc_usd`. The external tests therefore validate the settlement-model structure under another reference feed; they are not exact reproductions of the running bot's inputs. The archive is one public sample day, and its representativeness is unknown.

Source: [publisher's dataset and release documentation](https://github.com/Ligengxin96/polymarket-data-samples), [sample release](https://github.com/Ligengxin96/polymarket-data-samples/releases/tag/samples-2026-09-08).

## 2. What the current probability actually assumes

For decisions before the final averaging window starts:

```
gap_bps = 10,000 × log(current instantaneous BTC price / official opening price)
effective_seconds = remaining_seconds − 60 + 20
raw_uncertainty = measured_120_second_volatility × sqrt(effective_seconds)
uncertainty = max(raw_uncertainty, 2 bps)
z = gap_bps / uncertainty
P(UP) = NormalCDF(z), clipped to 2%–98%
P(DOWN) = 1 − P(UP)
```

The time expression has a coherent mathematical motivation. Under zero-drift Brownian motion with constant volatility, the variance of a future average over a window of length W is proportional to `wait_until_window + W/3`. At 120 seconds remaining with W=60, that gives 60+20=80 effective seconds. This is an approximation in log-price space to a price TWAP.

It assumes recent volatility predicts future volatility, increments behave sufficiently like a Gaussian diffusion, drift is negligible, and the instantaneous feed is a suitable conditional predictor of the final settlement feed. Momentum, order-flow votes, and warnings mainly affect eligibility; they do not all independently contribute information to this probability formula.

The formula for a completely future averaging window should not simply be extended unchanged after less than 60 seconds remain. A later-time model must include the observed part of the final averaging window and model only its remaining contribution. Under the same Brownian approximation, the future contribution's variance is proportional to `remaining_seconds³ / (3 × W²)` once the final window has begun. Oracle TWAP computation and source differences still need an uncertainty allowance. The exact Chainlink TWAP cannot be assumed reconstructible from a one-second spot tape.

Official documentation describes the Chainlink-computed 60-second stream and its timestamps: [Polymarket realtime data](https://docs.polymarket.com/market-data/realtime-data#twap-prices).

## 3. Calibration across the full local dataset

Direction is selected from the recalculated UP/DOWN probability. The displayed confidence is the probability of that selected side.

| Stated confidence band | Rounds | Wins | Losses | Mean stated confidence | Actual accuracy |
| --- | ---: | ---: | ---: | ---: | ---: |
| 50–59.9% | 263 | 148 | 115 | 54.60% | 56.27% |
| 60–69.9% | 262 | 177 | 85 | 64.73% | 67.56% |
| 70–79.9% | 254 | 193 | 61 | 75.00% | 75.98% |
| 80–89.9% | 261 | 214 | 47 | 85.05% | 81.99% |
| 90–94.9% | 232 | 205 | 27 | 92.80% | 88.36% |
| 95–97.9% | 190 | 174 | 16 | 96.50% | 91.58% |
| Capped 98% | 661 | 652 | 9 | 98.00% | 98.64% |

The important problem is not uniform overconfidence everywhere. The cap bucket performed well, while the 90–97.9% range overstated observed reliability. Pooling all signals above 90% conceals this.

The 98% bucket contains varying signal strengths. For example, a normal z-score just over 2.05 and a score of 4 can both be displayed as 98%. The cap conceals their ranking. The three apparently clean capped losses in the broad archive had scores approximately 2.15, 2.16, and 2.09. One predates complete regime logging. The two with complete warning records were:

- `1790616600`: Up, z≈2.162, spot gap≈9.93 bps, TWAP gap≈6.63 bps; eventual Down.
- `1791780300`: Up, z≈2.091, spot gap≈9.24 bps, TWAP gap≈7.23 bps; eventual Down.

An apparently wide dollar gap can still be only about twice the remaining volatility uncertainty. Neither loss was caused by the 2 bps floor being active: their applied uncertainty was about 4.59 and 4.42 bps.

## 4. Selective risk/volume comparison

These are predictions from snapshots, not presumed filled trades. Complete warning information is required for the clean rows.

| Selection | Earlier slice W/L | Later slice W/L | All W/L | Observed accuracy |
| --- | ---: | ---: | ---: | ---: |
| Every capped 98% round | 486/7 | 166/2 | 652/9 | 98.64% |
| Clean capped 98% | 159/1 | 62/1 | 221/2 | 99.10% |
| Clean, score ≥2.5 | 145/0 | 59/0 | 204/0 | 100% observed |
| Clean, score ≥3.0 | 127/0 | 47/0 | 174/0 | 100% observed |

The later slice matters. Adding score ≥3 retained 47 clean predictions there, with no observed losses. However, a fixed-rule independent-binomial 95% two-sided Wilson interval for 47/47 still has a lower accuracy bound of only about 92.44%. Temporal dependence and choosing filters after inspecting outcomes weaken that interpretation further.

Even 174/174 does not imply failure is impossible. Under a fixed-rule independent-binomial assumption, zero failures in 174 permits a one-sided 95% upper failure rate of roughly 1.71%. About 299 independent, prospective qualifying outcomes with no failures would be needed for that bound to fall below 1%. The historical selection process does not satisfy those prospective conditions.

Restricting to Down also looks unusually good historically, but there is too little diverse out-of-sample evidence to treat a permanent Down-only restriction as a causal advantage. Similarly, a stability cutoff trained to remove historical losses may miss a later clean reversal; the recent clean loss had stability around 0.64.

## 5. Accuracy does not mean an affordable opportunity

Among complete-warning clean capped snapshots:

- 223 candidate predictions existed.
- Only 128 had a usable selected-side ask in the stored snapshot.
- Only 6 passed market-direction agreement, the >80-cent entry floor, and 3-cent expected edge at that exact snapshot; 5 won and 1 lost.

Among the 174 complete-warning clean score≥3 predictions, 85 had a usable ask, and **none** met those same immediate price/edge conditions. Most quotes were already above 95 cents.

This does not mean such games can never become tradable later. Real fills occur after the decision snapshot. It means the current summaries cannot establish that two economically qualifying fills are available every day. A probability can remain correct while the available price removes the advantage.

The actual 98%-forecast real-fill cohort remains 46 trades, 44 wins, and 2 losses. It is much smaller and differently selected from the full prediction cohort. Do not use the overall snapshot success rate as the true success rate of fills chosen by waiting for a cheaper price.

## 6. The most important execution issue: the forecast stays frozen

The current trader saves probability and maximum value price in a candidate created around 120–110 seconds remaining. Quote monitoring then checks price, visible quantity, available money, and preflight conditions, but does not continuously rebuild the BTC forecast/risk features while waiting for entry.

This creates an important failure mode:

1. BTC supports Up with high confidence at decision time.
2. The order is initially too expensive.
3. BTC reverses or volatility increases.
4. The Up contract becomes cheaper and passes the *old* probability's edge limit.
5. The bot purchases using a stale assessment of settlement risk.

The same fall in contract price that creates the apparent bargain may also be evidence that the original probability has expired. This is an inference from the code and trade trail, consistent with research on adverse selection; it is not proof that every price decline predicts a losing trade.

Recommendation: retain the original decision for history, but use a separate live entry forecast. Immediately before attempting an order, recalculate the score/probability, warning conditions, uncertainty, and current edge. If conditions fail, continue watching until the existing cutoff. Do not carry a strong probability forward unchanged for up to 90 seconds.

Recorded order attempts have median submission latency around 1,046 ms and median total latency around 1,350 ms. Among 31 attempts with measured order-build time, median build time was about 588 ms. These timing distributions cover mixed earlier execution versions. They justify testing at least 1-second and roughly 3-second price-movement stress cases.

A BUY maximum price does not provide a minimum fill-price guarantee. FOK controls completeness of execution, not a lower price bound. A final quote above 80 cents can still be followed by a cheaper fill during submission. [Polymarket order documentation](https://docs.polymarket.com/trading/place-orders#market-orders). Research on limit-order adverse selection and latency provides a relevant mechanism, but its results are not direct Polymarket-specific parameter estimates: [Lehalle and Mounjid](https://arxiv.org/abs/1610.00261).

## 7. Independent replay: dynamic checks versus fixed decisions

Replayed the independent day using only price/quote information received by each observation time. Forecasts were recalculated every second between 120 and 60 seconds remaining. Earlier observations up to 123 seconds remaining seeded any three-second persistence check. A strategy selected the first qualifying entry, not the best later price. Only one purchase per market was allowed.

A hypothetical $100 principal was walked through the available ask depth; crypto taker fees were added using the market's 0.07 schedule. Initial edge had to qualify before submission, and delayed liquidity was evaluated one second later. The strict replay required the full-book best ask to agree with the unthrottled best ask at both observation and delayed execution. Missing/mismatched book states were skipped.

| Strict replay selection | Simulated $100 entries | Wins | Losses | Net simulated P&L across every selected entry |
| --- | ---: | ---: | ---: | ---: |
| Dynamic clean capped 98% | 11 | 11 | 0 | +$96.26 |
| Clean, score ≥2.5 | 7 | 7 | 0 | +$55.23 |
| Clean, score ≥2.5, qualified for 3 seconds | 3 | 3 | 0 | +$20.66 |
| Clean, score ≥3.0 | 1 | 1 | 0 | +$5.96 |
| Clean, score ≥3.0, qualified for 3 seconds | 1 | 1 | 0 | +$7.04 |

This is one day of book-based simulation. It is not a forecast of daily returns, a guarantee of FOK execution, or a compounding backtest. It assumes sufficient principal/fee cash for every entry, ignores competing orders and market impact from our own trades, and does not prove that the archived quote was actually executable at the exchange. A real fill/settlement audit would be required.

The first two entries under the score≥2.5 plus persistence rule yielded approximately +$14.10 in this simulation. The corresponding score≥3 rule could enter only one game. That is useful evidence of the risk-versus-volume tradeoff, not proof of a $10 daily minimum.

The less strict depth replay admitted 15 clean $100 entries, with 14 wins and 1 loss. That difference demonstrates sensitivity to quote reconstruction/latency assumptions. It prevents treating a coarse perfect-looking backtest as a guaranteed execution advantage.

At fixed observation times, capped prediction counts increased from 89 at 120 seconds remaining to 144 at 60 seconds remaining. Clean capped forecasts increased from 33 to 46. But many became too expensive to enter with the retained minimum edge. Waiting until a particular later second is not a universal improvement.

## 8. Formula alternatives actually tested

The earlier local slice was used to fit parameters or the normalized residual distribution; the later local slice evaluated them. The independent September 8 sample was kept outside local fitting. Its timing precedes the local training interval, so this is external validation, not a claim of a tradable forward chronological experiment.

| Formula | Later local Brier score | External Brier score | Later local capped predictions W/L |
| --- | ---: | ---: | ---: |
| Current normal + 2 bps floor | 0.13143 | 0.14700 | 166/2 |
| 1.5× current applied uncertainty | 0.13454 | 0.13893 | 82/1 |
| 2× current applied uncertainty | 0.14113 | 0.13901 | 33/0 |
| Wider Student-t with 3 degrees of freedom | 0.13162 | 0.14247 | 60/1 |
| Earlier-slice fitted scale/floor | 0.12766 | 0.14260 | 142/1 |
| Earlier-slice empirical forecast-error CDF | 0.13191 | 0.14303 | 77/1 |

Lower Brier score means lower mean squared probability error, but it is not calibration alone: discrimination also matters. Log loss was measured too. Calibration reliability bins, opportunity coverage, execution costs, and downside must be examined alongside either score. [Official calibration guidance](https://scikit-learn.org/stable/modules/calibration.html).

The fitted normal variant selected 1.5× raw volatility uncertainty with a 1 bps floor, from a 30-combination early-data grid. That improves later local probability scores, but its 98% bucket still loses in the external day (51 wins, 1 loss). Lowering the floor therefore should not be adopted just because one score improved.

A Student-t with equal variance also was tested and performed worse than the wider Student-t. A heavier-tailed distribution is not automatically more conservative at every score; its scale must be specified.

The empirical residual model widens the treatment of rare forecast errors but did not consistently outperform the baseline. Blindly doubling uncertainty sacrifices many opportunities. Using the maximum of 30-second and 120-second measured volatility did not improve later local scoring either.

No replacement is established as the overall winner. Recalibration of the current signal is justified, especially in the 90–97.9% band. Deployment of a wholesale new distribution is not yet justified.

## 9. What a better probability layer should learn

A practical next research model would retain a settlement-based score and learn uncertainty from actual final-settlement forecast errors:

```
forecast error = final settlement gap − current instantaneous gap
normalized error = forecast error / estimated remaining uncertainty
```

Estimate the error distribution from past completed rounds only. Condition conservatively on a small number of meaningful factors: remaining time, measured volatility, short-term volatility change, gap direction, and whether the recent path is reversing. Include reference-feed/oracle basis uncertainty. Test empirical quantiles or a small mixture model against the baseline; do not assume a complex classifier must be better.

The local normalized forecast errors exceeded absolute value 3 in approximately 4.52% of early rounds and 3.17% of later rounds. A standard normal predicts about 0.27%. Errors exceeding absolute value 2 were approximately 12.73% and 10.57%, against a standard normal's roughly 4.55%. These residuals mix dynamics and forecast/source approximation error; the figures do not isolate a causal volatility-estimation defect. They support investigating wider tails and state-dependent uncertainty.

Bitcoin literature documents frequent jumps and clustering, but those papers do not validate a specific five-minute Polymarket parameter. [Scaillet, Treccani, and Trevisan](https://arxiv.org/abs/1704.08175); [Hu, Härdle, and Kuo](https://arxiv.org/abs/1912.05228).

A calibrated model should explicitly allow SKIP when a plausible downside interval crosses the opening line. Use rolling, chronology-respecting calibration, limit the number of conditioning groups, and pool sparse groups instead of assigning 100% probability from zero historical losses. Conformal risk-control methods are relevant later, but their assumptions and temporal distribution drift need explicit treatment; they do not remove BTC tail risk. [Conformal risk control](https://arxiv.org/abs/2208.02814), [non-exchangeable risk control](https://arxiv.org/abs/2310.01262).

There is no need to make Polymarket the probability anchor or add an automatic confidence penalty based solely on being more confident than the market. The executable ask belongs in the economics and order checks.

## 10. Proposed entry policy and economics

For the balanced candidate, qualify a round only when:

1. The supported official opening/TWAP inputs and instantaneous reference are available and synchronized.
2. Current capped confidence reaches 98%, and current absolute score is at least 2.5. The more selective candidate uses 3.0.
3. Full warning information exists. No thin gap, movement disagreement, or choppy-Up flag is active; selected side agrees with the market direction.
4. The same direction and qualification persist for three elapsed seconds with fresh inputs. Repeated stale observations must not count as confirmation.
5. The available ask is above the existing 80-cent floor and within the existing adverse-price-move bound; the actual available depth funds the complete order.
6. A freshly assessed conservative probability gives sufficient expected value after fees and an execution allowance. Do not count a lower quote as better value unless the BTC forecast is revalidated.
7. At most two fills are made per Lagos calendar day. Stop new entries after one losing trade for this low-volume policy, subject to official outcome confirmation. Filled positions remain tracked.

Three seconds of confirmation is a test parameter, not a proven optimum. Its effect on missed fills must be included. The replay does not model after the final averaging window starts; extending entries down to the current 30-second cutoff requires the appropriate partial-window forecast.

For a share price a and fee rate k, cost per share is approximately:

```
c = a + k × a × (1 − a)
expected net dollars per share = conservative_win_probability − c
```

At 92 cents and k=0.07, c≈0.925152. Break-even accuracy is therefore about 92.52%. With a 3-cent expected edge requirement, conservative win probability must be at least 95.52% at that price. This edge is cents per $1-payout share, not the same quantity as percentage return on capital.

A historical accuracy of 96% gives about 3.79% expected return on $100 principal at 92 cents under this fee model. A historical accuracy of 94% gives only about 1.61%. Small probability-estimation errors therefore have a material economic effect.

A late-entry strategy with virtually certain-looking outcomes can still be unattractive at 98–99 cents. Price limits should follow defensible probability and fees, rather than chase a daily profit requirement. Maker orders can lower fees, but add non-fill, queue, cancellation, and adverse-selection risks; they are a separate experiment, not a free fix. [Official fees](https://docs.polymarket.com/trading/fees).

## 11. Bankroll risk is separate from signal risk

Even if a future strategy genuinely wins 98% of independent trades, 14 trades have about a 24.64% probability of at least one loss. At 99%, that is still about 13.13%. Correlated volatility shocks can make the independence assumption optimistic.

Do not size from an unvalidated 98% display using full Kelly or most of the account. A provisional maximum loss budget of 1–2% of deployable capital per entry is a safer research starting point. Set trade and daily loss caps in dollars, not just a target percentage of account balance. Keep a reserve outside the deployable balance, and do not increase stake automatically after a loss.

A $100 stake represents 1% exposure only for an approximately $10,000 bankroll. On a $100 bankroll, a $100 stake can wipe out the account. A fewer-trades policy does not by itself make a large stake safe.

A win payout may remain a resolved position until redeemed. The current app does not automatically redeem. Daily reuse of proceeds must therefore be based on actual available collateral, not unrealized/resolved position value. This review does not propose changing the earlier no-redemption instruction.

Risk-constrained Kelly research explicitly separates growth from drawdown constraints; use conservative probabilities and hard loss limits if pursuing that method. [Busseti, Ryu, and Boyd](https://web.stanford.edu/~boyd/papers/pdf/kelly.pdf).

## 12. Validation sequence before a production change

1. Preserve the baseline and freeze two candidate specifications: score 2.5 and 3.0, both with complete warning qualification and three-second persistence.
2. Retain compact per-second forecasts and book/depth summaries from 120 seconds to the entry cutoff. Keep live eligibility and actual fills distinguishable. Current resolved raw tapes are largely deleted, so the local history cannot reconstruct all later entry states.
3. Collect at least several continuous weeks covering different volatility regimes. Count unique rounds, not correlated one-second predictions as independent observations.
4. Evaluate in rolling chronological blocks with a gap that covers overlapping feature/settlement windows. Fit calibration only on earlier data and score later data.
5. Test the after-60-second model separately. Check oracle/report availability by receive time, not only source timestamps, and require the correct market settlement configuration.
6. Replay actual $1/$10/$100 depth, fees, realistic latency, missing quotes, no-fills, and the first two eligible fills per day. Record every skipped opportunity and why.
7. Compare probability reliability bins, score calibration, loss count, worst drawdown, net P&L, fill rate, and days with zero/one/two fills. Use day/block resampling to assess dependence and avoid declaring zero-risk accuracy from a small cohort.
8. Promote a candidate only after unseen forward data supports its economics and risk. Do not repeatedly retune the supposedly held-out period after a failure.

The study includes multiple exploratory filters, eight probability methods, a scale/floor grid, persistence variants, and execution assumptions. A winning historical setting may be overfit. [Bailey et al., The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf). Official [TimeSeriesSplit documentation](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html) explains chronology and gap mechanics; missing round coverage means custom day-based splits may be needed rather than treating irregular observations as equally spaced.

The next priority is live forecast validity at the moment of purchase. The score≥2.5 candidate is a reasonable balance to investigate; score≥3 is a more selective alternative with clearly weaker daily-flow evidence. Neither establishes a guaranteed daily income or a zero-failure signal.
