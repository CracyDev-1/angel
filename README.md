# Angel One SmartAPI trading pipeline

Server-side flow: **session → market data → features → strategy → optional LLM filter → risk → execution → state**.

## Setup

1. Copy `.env.example` to `.env` and fill broker credentials (`ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_PIN`, client IP/MAC, etc.). **You can leave `ANGEL_TOTP` / `ANGEL_TOTP_SECRET` blank** and use the dashboard for TOTP each time you connect.
2. `python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`
3. Start the dashboard: `python -m angel_bot.main dashboard` → open the printed URL → enter the **current 6-digit code from your authenticator** → **Connect** → **Start bot**.

## CLI (`python -m angel_bot.main …` or `angel-trader …`)

| Command | Purpose |
|--------|---------|
| `dashboard` | **Recommended:** local web UI — enter **TOTP from your authenticator** at runtime, connect to Angel One, then start/stop the bot (no TOTP in `.env`). |
| `profile` | Login + `getProfile` (requires `ANGEL_TOTP` or `ANGEL_TOTP_SECRET` in `.env`, or use `dashboard`). |
| `poll-ltp` | REST `getLtpData` using `LTP_EXCHANGE_TOKENS_JSON` (same TOTP rules as `profile`). |
| `ws-feed` | Smart Stream v2 WebSocket ticks from `WS_SUBSCRIPTIONS` (Ctrl+C to stop). |
| `orders-sync` | Pull order book, upsert lifecycle rows in `STATE_SQLITE_PATH`, print risk snapshot. |

## Environment

All variables are documented in **`.env.example`**. Use **`LOG_FORMAT=json`** for machine-readable logs.

See `pyproject.toml` for the `angel-trader` console script.
