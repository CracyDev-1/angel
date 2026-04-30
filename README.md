# Angel One SmartAPI auto-trader

Server-side trader (FastAPI) + React dashboard. Flow: **TOTP login → live snapshot (funds, positions, scanner, decisions, orders) → optional auto trading**.

> **No system can guarantee profits or zero losses.** This bot defaults to **dry-run** (`TRADING_ENABLED=false`); it logs every intended trade in the dashboard so you can review behavior before sending real orders.

## Setup

1. Copy `.env.example` to `.env` and fill broker credentials. Leave `ANGEL_TOTP` / `ANGEL_TOTP_SECRET` blank — you enter the TOTP in the dashboard at runtime.
2. Backend: `python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`
3. Frontend (production build, served by FastAPI):
   ```bash
   cd frontend && yarn install && yarn build
   ```
4. Start: `python -m angel_bot.main dashboard` → open `http://127.0.0.1:9812/`.

### Frontend dev mode (hot reload)

```bash
cd frontend && yarn dev
# open http://localhost:5173/
# vite proxies /api to http://127.0.0.1:9812 (override with VITE_API_TARGET)
```

## Dashboard tour

- **Login screen**: paste current 6-digit TOTP from your authenticator → Connect.
- **Dashboard**:
  - **Available cash, capital used (CE/PE), open P&L, realized today** at the top.
  - **Scanner**: ranks watchlist instruments by intraday change% + short momentum, shows lot size, notional/lot, and **affordable lots** vs your available cash.
  - **Open positions**: per-symbol qty, avg buy, LTP, capital, P&L (broker `getPosition` response).
  - **Recent orders**: lifecycle from local SQLite (broker `getOrderBook` reconciliation).
  - **Bot decisions (live)**: every iteration the bot logs an entry — instrument, signal (BUY_CALL / BUY_PUT / NO_TRADE), side (CE/PE), qty/lots, capital used, reason, dry-run vs live.
  - **Daily summary**: today and last 30 days realized P&L.
- **Header buttons**: Start bot, Stop bot, Disconnect. The badge shows **DRY RUN** vs **LIVE TRADING**.

## Configuring the bot

In `.env`:

| Variable | Meaning |
|----------|---------|
| `TRADING_ENABLED` | `true` to send real orders (else dry-run only). Keep `false` until you trust the behavior. |
| `BOT_LOOP_INTERVAL_S` | Seconds between scanner / decision iterations. |
| `BOT_USE_CAPITAL_PCT` | Cap on the % of available cash that can be deployed in one iteration. |
| `BOT_MAX_CONCURRENT_POSITIONS` | Hard cap on simultaneous broker positions. |
| `BOT_MIN_SIGNAL_STRENGTH` | Skip scanner hits whose composite score is below this threshold. |
| `BOT_DEFAULT_PRODUCT` / `BOT_DEFAULT_VARIETY` | Order enums for live placement (broker enums, e.g. `INTRADAY`, `NORMAL`). |
| `SCANNER_WATCHLIST_JSON` | The instruments the scanner / bot tracks. Tokens come from your Angel **instrument master** CSV. |

## Honest scope

- The bot’s strategy is a **trend / momentum heuristic**, not a guaranteed-profit model.
- The current scanner picks underlyings; **placing index option orders requires you to also resolve the specific strike token** via the instrument master. To stay safe, when an INDEX is the top hit and `TRADING_ENABLED=true`, the bot logs a `live_index_options_require_strike_resolution` skip rather than guessing a strike.
- For equity / commodity underlyings, when `TRADING_ENABLED=true` the bot can place a market order (size capped by your **risk caps and available funds**).
- Always run with risk caps (`RISK_*`) you can afford to lose, and keep `BOT_MAX_CONCURRENT_POSITIONS` low.

## Tests

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest -q
```
