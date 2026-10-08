# BTC 5-Minute Prediction Bot

A single-user service that watches Polymarket BTC Up/Down five-minute markets, streams the official BTC reference inputs, makes one prediction near 120 seconds remaining, records every round, and exposes the data through FastAPI.

Automatic execution supports paper sessions and explicitly activated real-money
sessions. Manual real orders still use the separate review flow.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Add the CLOB credentials and signing key to `.env`. Existing lowercase names
(`PK`, `api_key`, `api_secret`, `api_passphrase`) remain supported. You may set
`POLYMARKET_WALLET` to the exact wallet shown by Polymarket. If it is omitted,
the API selects an already-deployed official deposit wallet for the signer and
falls back to the signer address; it never deploys a wallet while connecting.
Keep `.env` private because the API process can sign real orders.

## Start

One command starts the collector and API:

```bash
.venv/bin/python main.py
```

The API is available at `http://127.0.0.1:8000`; interactive documentation is at `/docs`.

Trading sessions are started from the dashboard. Choose paper or real mode, set
the session balance, allocation, trade cap, confidence, edge, entry cutoff,
and optional trade-count, profit, and loss limits. Each session keeps its own
configuration, status, trades, and event log. Turning a session off cancels
unfilled candidates; filled positions stay open until the market resolves.
Real mode reads the connected account's current USDC before activation and
again before every order. Once activated, qualified entries use bounded
fill-or-kill orders without a per-trade confirmation. Paper mode never submits
an exchange order. Non-zero warning allocations are raised to Polymarket's $1
market-buy minimum when the account and per-trade cap can fund it. The official
SDK handles missing exchange approval, matched fills are checked for settlement,
and delayed FOK acknowledgements are reconciled against authenticated account
trades before they are recorded. An ambiguous network submission is never
retried automatically; that session stops for reconciliation to prevent a
duplicate real order. The automatic-order hot path uses the live CLOB trigger,
a lightweight collateral check, and an immediate exchange-enforced FOK order at
the strategy's maximum price. It does not repeat the full account snapshot or make
a separate price-estimate request. Explicit FOK no-fills remain candidates and
retry at most once per second until the configured entry cutoff.

The bot uses one probability calculation with a 2 bps uncertainty floor and a
98% probability cap. Its fixed safeguards use the smaller of the spot and TWAP
gap for close-to-line detection, block a thin gap combined with either movement
disagreement or `choppy_up`, and pause a delayed fill after the selected
contract moves more than 15 cents against the original signal.

Each paper session also sets the trade-size percentage for a thin-gap warning
and for a movement-disagreement warning. Set either percentage to `0` to skip
that type of single-warning trade. The combined close-to-line constraints are
always enforced. These settings can be edited while the session is running.

Serve existing history without live collection:

```bash
.venv/bin/python main.py --api-only
```

Other options:

```text
--data PATH       Data directory (default: data)
--host ADDRESS    API bind address (default: 127.0.0.1)
--port NUMBER     API port (default: 8000)
```

## Code layout

```text
main.py                 single entry point
bot/collector.py        Polymarket order book and round persistence
bot/market_data.py      official BTC/reference streams
bot/prediction.py       components, probability, warnings, and signal decision
bot/strategy.py         probability formula and fixed trading safeguards
bot/trading.py          shared fee and order-book rules
bot/paper.py            paper execution and bankroll calculations
bot/paper_sessions.py   API-managed session lifecycle
bot/account.py          real-account reads and manual order execution
bot/api/                FastAPI routes and history repository
tests/                  maintained behavior tests
ui/                     separate browser dashboard
```

The prediction uses the official opening price and settlement estimate, recent BTC movement and volatility, Polymarket prices, and order-book flow when available. Settlement, movement, market, and order-flow components remain separate so their effect can be measured independently. A movement disagreement or thin gap is recorded as a warning; the pair together produces `SKIP`. Market disagreement, weak probability, low liquidity, stale inputs, or insufficient edge can also produce `SKIP`. The analytic probability is capped at 98% and uses a minimum uncertainty floor so an unusually quiet sample cannot create false near-100% confidence. It does not apply a confidence penalty based on disagreement with Polymarket's probability.

## Main API routes

- `GET /health`
- `GET /api/v1/dashboard`
- `GET /api/v1/live`
- `GET /api/v1/live/chart`
- `GET /api/v1/rounds`
- `GET /api/v1/rounds/{slug}`
- `GET /api/v1/trades`
- `GET /api/v1/statistics`
- `GET /api/v1/paper/sessions`
- `POST /api/v1/paper/sessions`
- `GET /api/v1/paper/sessions/{session_id}`
- `PATCH /api/v1/paper/sessions/{session_id}` — edit the active session's runtime rules
- `POST /api/v1/paper/sessions/{session_id}/stop`
- `GET /api/v1/trading/sessions`
- `POST /api/v1/trading/sessions`
- `GET /api/v1/trading/sessions/{session_id}`
- `PATCH /api/v1/trading/sessions/{session_id}`
- `POST /api/v1/trading/sessions/{session_id}/stop`
- `GET /api/v1/account`
- `POST /api/v1/account/orders/preview`
- `POST /api/v1/account/orders`
- `WS /api/v1/ws/live`
- `WS /api/v1/ws/chart`
- `WS /api/v1/ws/quotes` — direct order-book updates, up to 20 per second
- `WS /api/v1/ws/reference` — live BTC, TWAP, and exact opening reference
- `WS /api/v1/ws/account` — cash, positions, live mark, and unrealized P&L

Manual orders use a one-time 12-second preview. Buys are capped by cash,
allowance, a reviewed maximum price, and maximum spend. Sells are capped by the
current position and a reviewed minimum price. Both use fill-or-kill execution,
so a partial fill is not left behind. The API checks the live feed and the
authoritative Polymarket market state again immediately before submission.

## UI

The UI is a separate application and reads the bot only through the HTTP and
WebSocket API. Its main prediction card and round history show the single bot
decision, probability, warnings and execution outcome for review after
settlement.

Start it in another terminal:

```bash
cd ui
npm start
```

Open `http://127.0.0.1:5173`. Its API address is configured in `ui/config.js`
and defaults to `http://127.0.0.1:8000`.

## Records

Live files remain under `data/` for compatibility with existing history:

- `live-status.json` — current prediction status
- `round-history.jsonl` — retained-strategy decisions and resolved outcomes
- `active-rounds/` — temporary evidence for unresolved rounds
- `archive/legacy-history/` — unchanged history from retired strategies
- `session-*.jsonl` — session events that are not assigned to a round
- `paper-trading/` — separate paper-trading logs and status
- `real-trading/sessions/` — autonomous real-session logs and status
- `real-trading/manual-orders.jsonl` — manual real-order audit trail; no secrets

Resolved raw round evidence is discarded after the compact decision and outcome are retained.
When the service starts, it also reconciles interrupted paper positions against
official outcomes already stored in the round history and repairs their profit,
bankroll, win/loss, and open-position records.

## Test

```bash
.venv/bin/python -m unittest discover -s tests -v
```
