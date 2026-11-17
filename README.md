# BTC 5-Minute Prediction Bot

A single-user service that watches Polymarket BTC Up/Down five-minute markets, streams the official BTC reference inputs, makes one prediction near 120 seconds remaining, records every round, and exposes live data and real-money execution through FastAPI.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Add the CLOB credentials and signing key to `.env`. Existing lowercase names (`PK`, `api_key`, `api_secret`, `api_passphrase`) remain supported. You may set `POLYMARKET_WALLET` to the exact wallet shown by Polymarket. Keep `.env` private because the API process can sign real orders.

## Start

```bash
.venv/bin/python main.py
```

The API is available at `http://127.0.0.1:8000`; interactive documentation is at `/docs`.

Run both services with Docker Compose:

```bash
cp .env.example .env
# Add the Polymarket credentials to .env, then run:
docker compose up --build
```

The dashboard is available at `http://127.0.0.1:5173` and the API at
`http://127.0.0.1:8000`. Runtime records are persisted in the local `data/`
directory. Use `docker compose up -d --build` to run in the background and
`docker compose down` to stop the services.

The dashboard starts and stops the real-money bot with one button. Each session uses fixed production rules: 98% confidence, strength above 2.5, Polymarket direction agreement, no thin gap, no movement disagreement, fresh inputs, and an executable entry strictly above 80 cents and no higher than 97 cents. Qualified entries use bounded fill-or-kill orders without confirmation for each trade.

A signal that fails at the initial decision is retained as a recovery candidate until 30 seconds remain. The bot rebuilds its probability, strength, warnings, direction agreement, value limit, and executable quote on every fresh update. A recovered candidate must pass all normal rules for three consecutive snapshots spanning at least two seconds. Recovered execution has its own dashboard switch. Switching execution off does not stop observation: fillable opportunities and their eventual hypothetical result continue to be logged.

Recovered trades use only the profit-trading wallet and allocate 50% of that wallet. When 50% is below the exchange's $1 principal minimum, the bot raises the order to the minimum only when the profit wallet can cover the principal and fee. It never draws the missing amount from core trading capital or the safe reserve.

Turning a session off cancels unfilled candidates. Filled positions remain tracked until resolution. The bot uses 80% of the current session trading capital for each entry. Explicit FOK no-fills remain candidates and retry at most once per second until 30 seconds remain. An ambiguous submission stops the session for reconciliation to prevent a duplicate real order.

An active session survives application restarts. The runtime restores its configuration, counters, realized P&L, and open positions, then continues accepting future signals. Decisions and resolutions written while the API was offline are replayed. A round that had already reached exchange submission is never submitted a second time during recovery.

After a market ends, an account position at its final 0 or 1 price can update the result before the slower collector settlement arrives. Redeemable winnings remain position value until Polymarket reports them in available USDC. The bot does not redeem positions automatically.

The bot uses one probability calculation with a 2 bps uncertainty floor and a 98% probability cap. It blocks either a thin gap or movement disagreement on its own. A `choppy_up` label alone does not block a trade. The forecast and warnings are rebuilt from fresh BTC and order-book data before every submission attempt, so a cheaper price can trade only when the current forecast still passes. Profits are recorded as protected profit and excluded from all later sizing in that session; losses reduce trading capital.

Serve existing history without live collection:

```bash
.venv/bin/python main.py --api-only
```

Options: `--data PATH`, `--host ADDRESS`, and `--port NUMBER`.

## Code layout

```text
main.py                 single entry point
bot/collector.py        Polymarket order book and round persistence
bot/market_data.py      official BTC/reference streams
bot/prediction.py       components, probability, warnings, and decision
bot/strategy.py         probability formula and fixed safeguards
bot/trading.py          shared fee and order-book rules
bot/trader.py           real-session state, sizing, and monitoring
bot/real.py             authenticated exchange execution
bot/sessions.py         API-managed real-session lifecycle
bot/account.py          account reads and manual order execution
bot/api/                FastAPI routes and history repository
tests/                  maintained behavior tests
ui/                     separate browser dashboard
```

The prediction uses the official opening price and settlement estimate, recent BTC movement and volatility, Polymarket prices, and order-book flow when available. Settlement, movement, market, and order-flow components remain separate. Market disagreement, weak probability, low liquidity, stale inputs, insufficient edge, or combined risk can produce `SKIP`.

## Main API routes

- `GET /health`
- `GET /api/v1/dashboard`
- `GET /api/v1/live`
- `GET /api/v1/live/chart`
- `GET /api/v1/rounds`
- `GET /api/v1/rounds/{slug}`
- `GET /api/v1/trades`
- `GET /api/v1/statistics`
- `GET|POST /api/v1/trading/sessions`
- `GET /api/v1/trading/sessions/{session_id}`
- `POST /api/v1/trading/sessions/{session_id}/stop`
- `GET /api/v1/trading/funds`
- `PATCH /api/v1/trading/sessions/{session_id}/recovered-signals`
- `POST /api/v1/trading/sessions/{session_id}/fund-transfers`
- `GET /api/v1/account`
- `POST /api/v1/account/orders/preview`
- `POST /api/v1/account/orders`
- `WS /api/v1/ws/live`
- `WS /api/v1/ws/chart`
- `WS /api/v1/ws/quotes`
- `WS /api/v1/ws/reference`
- `WS /api/v1/ws/account`

Manual orders use a one-time 12-second preview. Buys are capped by cash, allowance, reviewed maximum price, and maximum spend. Sells are capped by the current position and reviewed minimum price. Both use fill-or-kill execution.

## UI

The UI is separate and reads the bot only through HTTP and WebSocket APIs.

```bash
cd ui
npm start
```

Open `http://127.0.0.1:5173`. Configure the API address in `ui/config.js`.

## Records

- `data/live-status.json` — current prediction
- `data/round-history.jsonl` — decisions and resolved outcomes
- `data/active-rounds/` — temporary unresolved-round evidence
- `data/archive/legacy-history/` — history from retired strategies
- `data/real-trading/sessions/` — automated real-session logs and status
- `data/real-trading/profit-signals/` — recovered-signal, shadow, and profit-trade logs
- `data/real-trading/funds.json` — persistent core, profit-trading, and safe-reserve balances
- `data/real-trading/manual-orders.jsonl` — manual-order audit trail

## Test

```bash
.venv/bin/python -m unittest discover -s tests -v
```
