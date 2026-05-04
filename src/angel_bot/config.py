from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    angel_api_key: SecretStr = Field(validation_alias="ANGEL_API_KEY")
    angel_client_code: str = Field(validation_alias="ANGEL_CLIENT_CODE")
    angel_pin: SecretStr = Field(validation_alias="ANGEL_PIN")
    angel_totp: SecretStr | None = Field(default=None, validation_alias="ANGEL_TOTP")
    angel_totp_secret: SecretStr | None = Field(default=None, validation_alias="ANGEL_TOTP_SECRET")

    angel_client_local_ip: str = Field(default="127.0.0.1", validation_alias="ANGEL_CLIENT_LOCAL_IP")
    angel_client_public_ip: str = Field(default="127.0.0.1", validation_alias="ANGEL_CLIENT_PUBLIC_IP")
    angel_mac_address: str = Field(default="00:00:00:00:00:00", validation_alias="ANGEL_MAC_ADDRESS")

    angel_base_url: str = Field(
        default="https://apiconnect.angelone.in",
        validation_alias="ANGEL_BASE_URL",
    )

    ltp_poll_interval_s: float = Field(default=7.5, validation_alias="LTP_POLL_INTERVAL_S")
    ltp_exchange_tokens_json: str = Field(
        default='{"NSE":["99926000"]}',
        validation_alias="LTP_EXCHANGE_TOKENS_JSON",
    )

    ws_subscriptions: str = Field(default="", validation_alias="WS_SUBSCRIPTIONS")
    ws_feed_mode: int = Field(default=1, validation_alias="WS_FEED_MODE")

    state_sqlite_path: str = Field(
        default="./data/angel_bot_state.sqlite3",
        validation_alias="STATE_SQLITE_PATH",
    )
    order_reconcile_interval_s: float = Field(default=15.0, validation_alias="ORDER_RECONCILE_INTERVAL_S")

    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", validation_alias="OPENAI_MODEL")
    # The LLM acts as a probabilistic *classifier* over each candidate the
    # rule-based brain produces. It returns {decision, confidence, type} and
    # is consulted ONLY after all cheap gates pass (signal, market, funds).
    # It cannot create trades — it can only allow or skip the brain's choice.
    llm_filter_enabled: bool = Field(default=True, validation_alias="LLM_FILTER_ENABLED")
    # If OpenAI is unreachable / returns garbage / times out:
    #   true  → skip the trade (safe, default)
    #   false → fall through and let the trade go (faster, riskier)
    llm_filter_fail_closed: bool = Field(default=True, validation_alias="LLM_FILTER_FAIL_CLOSED")
    llm_filter_timeout_s: float = Field(default=8.0, validation_alias="LLM_FILTER_TIMEOUT_S")
    # Minimum classifier confidence (0..1) required to actually take the
    # trade. Lower = more trades + lower quality; higher = fewer + higher.
    llm_decision_threshold: float = Field(
        default=0.65, validation_alias="LLM_DECISION_THRESHOLD"
    )
    # How many top-scoring candidates per loop the LLM may classify. The
    # bot will iterate them in score order and stop at max-concurrent.
    llm_top_n_candidates: int = Field(
        default=3, validation_alias="LLM_TOP_N_CANDIDATES"
    )
    # When True (and OPENAI_API_KEY set), the classifier may return stop_loss_pct,
    # take_profit_pct, max_hold_minutes for each TAKE — clamped to the ranges
    # below and applied to live + paper exits (overrides PAPER_* defaults).
    llm_exit_params_enabled: bool = Field(
        default=True, validation_alias="LLM_EXIT_PARAMS_ENABLED"
    )
    # Clamps for LLM-suggested exits (fractions of option premium). Aligned
    # with intraday index options: wide enough to avoid normal premium noise.
    llm_exit_sl_pct_min: float = Field(default=0.08, validation_alias="LLM_EXIT_SL_PCT_MIN")
    llm_exit_sl_pct_max: float = Field(default=0.15, validation_alias="LLM_EXIT_SL_PCT_MAX")
    llm_exit_tp_pct_min: float = Field(default=0.15, validation_alias="LLM_EXIT_TP_PCT_MIN")
    llm_exit_tp_pct_max: float = Field(default=0.30, validation_alias="LLM_EXIT_TP_PCT_MAX")
    llm_exit_hold_min: int = Field(default=15, validation_alias="LLM_EXIT_HOLD_MIN")
    llm_exit_hold_max: int = Field(default=30, validation_alias="LLM_EXIT_HOLD_MAX")

    # 0 = auto: use live broker available cash. Any positive value overrides.
    risk_capital_rupees: float = Field(default=0.0, validation_alias="RISK_CAPITAL_RUPEES")
    risk_per_trade_pct: float = Field(default=1.0, validation_alias="RISK_PER_TRADE_PCT")
    risk_max_daily_loss_pct: float = Field(default=2.5, validation_alias="RISK_MAX_DAILY_LOSS_PCT")
    # Daily trade-count cap. 0 disables (the default) — count-based
    # throttles can stop a strategy mid-streak; rely on the daily-loss
    # kill switch + concurrent-position cap for safety instead.
    risk_max_trades_per_day: int = Field(default=0, validation_alias="RISK_MAX_TRADES_PER_DAY")
    risk_one_position_at_a_time: bool = Field(
        default=False, validation_alias="RISK_ONE_POSITION_AT_A_TIME"
    )
    # Trades-per-hour cap (rolling 60-minute window). 0 disables (the
    # default) — same rationale as the daily count cap.
    risk_max_trades_per_hour: int = Field(
        default=0, validation_alias="RISK_MAX_TRADES_PER_HOUR"
    )
    # Legacy name — still respected for backwards-compat. Prefer
    # INSTRUMENT_MASTER_PATH below (works for JSON + CSV).
    instrument_master_csv: str | None = Field(default=None, validation_alias="INSTRUMENT_MASTER_CSV")
    instrument_master_path: str | None = Field(
        default="./data/angel_scrip_master.json",
        validation_alias="INSTRUMENT_MASTER_PATH",
    )
    instrument_master_url: str = Field(
        default="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
        validation_alias="INSTRUMENT_MASTER_URL",
    )
    instrument_master_auto_download: bool = Field(
        default=True, validation_alias="INSTRUMENT_MASTER_AUTO_DOWNLOAD"
    )
    # Re-download if the local cache is older than this many hours.
    instrument_master_max_age_hours: float = Field(
        default=20.0, validation_alias="INSTRUMENT_MASTER_MAX_AGE_HOURS"
    )

    # Dynamic universe — overrides SCANNER_WATCHLIST_JSON when set, and
    # ATM option contracts are recomputed every ATM_REFRESH_INTERVAL_S seconds
    # using the latest spot price from the scanner.
    universe_spec_json: str = Field(
        default="",
        validation_alias="UNIVERSE_SPEC_JSON",
    )
    atm_refresh_interval_s: float = Field(
        default=120.0, validation_alias="ATM_REFRESH_INTERVAL_S"
    )

    dashboard_host: str = Field(default="127.0.0.1", validation_alias="DASHBOARD_HOST")
    dashboard_port: int = Field(default=9812, validation_alias="DASHBOARD_PORT")
    dashboard_token: SecretStr | None = Field(default=None, validation_alias="DASHBOARD_TOKEN")
    dashboard_cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        validation_alias="DASHBOARD_CORS_ORIGINS",
    )

    trading_enabled: bool = Field(default=False, validation_alias="TRADING_ENABLED")
    bot_loop_interval_s: float = Field(default=5.0, validation_alias="BOT_LOOP_INTERVAL_S")
    bot_max_concurrent_positions: int = Field(default=3, validation_alias="BOT_MAX_CONCURRENT_POSITIONS")
    # Cap on the % of available broker cash the bot may deploy per cycle.
    # 100 = use every rupee (recommended for options BUYING, where you pay
    # full premium upfront with no margin-call risk). Lower it (e.g. 80) if
    # you want to keep a passive cash buffer.
    bot_use_capital_pct: float = Field(default=100.0, validation_alias="BOT_USE_CAPITAL_PCT")
    bot_min_signal_strength: float = Field(default=0.0, validation_alias="BOT_MIN_SIGNAL_STRENGTH")
    # When true (default) the scanner drops OPTION rows whose 1-lot notional
    # already exceeds the available cash. Those tokens stop counting toward
    # the dashboard, decisions stream, brain analysis, and Angel rate-limit
    # budget. Index spot rows and equities are always kept so the user can
    # see what's moving even if they can't afford the option premium yet.
    bot_hide_unaffordable_lots: bool = Field(
        default=True, validation_alias="BOT_HIDE_UNAFFORDABLE_LOTS"
    )
    bot_default_product: str = Field(default="INTRADAY", validation_alias="BOT_DEFAULT_PRODUCT")
    bot_default_variety: str = Field(default="NORMAL", validation_alias="BOT_DEFAULT_VARIETY")

    # Backfill each watchlist symbol's candle aggregator from broker
    # history at bot start so the brain has ≥5 5m + ≥2 15m bars on cycle 1
    # — without this, every restart loses the in-memory aggregator and the
    # bot sits in ``warmup`` for 25-30 minutes before grading any signal.
    # Runs as a background task so the auto-trader loop starts scanning
    # immediately (live-tick polls + brain grades each instrument as soon
    # as its seed lands).
    bot_warmup_from_history: bool = Field(
        default=True, validation_alias="BOT_WARMUP_FROM_HISTORY"
    )
    # Tight lookback windows — just enough to clear the brain's warmup
    # gate (≥5 5m bars + ≥2 15m bars). 90 min of 5m → ~18 bars, 6 h of
    # 15m → ~24 bars (handles Monday morning when Friday closes still
    # count). Default 1m lookback is 0 minutes: the live-tick scanner
    # populates the first 1m bar within seconds, so paying for a
    # historical-API call for it is wasted rate budget on every restart.
    bot_warmup_lookback_5m_min: int = Field(
        default=90, validation_alias="BOT_WARMUP_LOOKBACK_5M_MIN"
    )
    bot_warmup_lookback_15m_min: int = Field(
        default=360, validation_alias="BOT_WARMUP_LOOKBACK_15M_MIN"
    )
    bot_warmup_lookback_1m_min: int = Field(
        default=0, validation_alias="BOT_WARMUP_LOOKBACK_1M_MIN"
    )
    bot_warmup_concurrency: int = Field(
        default=1, validation_alias="BOT_WARMUP_CONCURRENCY"
    )
    # Each ATM universe rebuild can introduce dozens of new option tokens.
    # Historical backfill for *all* of them in one burst triggers Angel 403s
    # and can starve order/LTP calls. We only backfill this many *new* keys
    # per refresh cycle; the rest are picked up on later cycles.
    bot_warmup_delta_max_keys: int = Field(
        default=4, validation_alias="BOT_WARMUP_DELTA_MAX_KEYS"
    )

    # Adopt long positions opened *directly on the Angel One platform* and
    # manage their exits with the same SL/TP/max-hold the bot uses for its
    # own trades. When a position the bot is managing disappears from the
    # broker book (the user closed it manually) the bot records the close as
    # ``external_close`` so realized P&L still flows into daily stats. Live
    # mode only — never touches paper / dry-run state.
    bot_adopt_external_positions: bool = Field(
        default=True, validation_alias="BOT_ADOPT_EXTERNAL_POSITIONS"
    )
    # Comma-separated product codes the bot is willing to adopt. Adopting a
    # CARRYFORWARD position by accident would leave a sell order behind on
    # exit so we keep the default conservative: INTRADAY only. Set this to
    # "INTRADAY,DELIVERY,CARRYFORWARD,MARGIN" to manage everything.
    bot_adopt_product_types: str = Field(
        default="INTRADAY",
        validation_alias="BOT_ADOPT_PRODUCT_TYPES",
    )
    bot_autostart: bool = Field(default=False, validation_alias="BOT_AUTOSTART")
    auto_connect_on_startup: bool = Field(default=True, validation_alias="AUTO_CONNECT_ON_STARTUP")
    auto_relogin_interval_s: float = Field(default=900.0, validation_alias="AUTO_RELOGIN_INTERVAL_S")

    scanner_watchlist_json: str = Field(
        default=(
            '{"NSE":['
            '{"name":"NIFTY","token":"99926000","kind":"INDEX","lot_size":50},'
            '{"name":"BANKNIFTY","token":"99926009","kind":"INDEX","lot_size":15},'
            '{"name":"FINNIFTY","token":"99926037","kind":"INDEX","lot_size":40},'
            '{"name":"RELIANCE","token":"2885","kind":"EQUITY","lot_size":250},'
            '{"name":"HDFCBANK","token":"1333","kind":"EQUITY","lot_size":550},'
            '{"name":"INFY","token":"1594","kind":"EQUITY","lot_size":400},'
            '{"name":"TCS","token":"11536","kind":"EQUITY","lot_size":175},'
            '{"name":"ICICIBANK","token":"4963","kind":"EQUITY","lot_size":700}'
            ']}'
        ),
        validation_alias="SCANNER_WATCHLIST_JSON",
    )

    # ------------------------- BRAIN STRATEGY (5m-primary) ---------------
    # Primary timeframe used for momentum/structure decisions.
    # 5m is the new default; 1m drives entry triggers and 15m is bias only.
    strategy_primary_timeframe: str = Field(
        default="5m", validation_alias="STRATEGY_PRIMARY_TIMEFRAME"
    )
    # Universe filters — very relaxed; LLM does the quality filtering.
    strategy_min_volatility_pct: float = Field(
        default=0.10, validation_alias="STRATEGY_MIN_VOLATILITY_PCT"
    )
    strategy_max_chop_score: float = Field(
        default=0.80, validation_alias="STRATEGY_MAX_CHOP_SCORE"
    )
    # Entry timing — 15m kept only as a *bias* check (very loose).
    strategy_min_15m_trend_slope: float = Field(
        default=0.0002, validation_alias="STRATEGY_MIN_15M_TREND_SLOPE"
    )
    # 5m slope = PRIMARY trend gate. Sized so a 100-200pt NIFTY push qualifies.
    strategy_min_5m_trend_slope: float = Field(
        default=0.0003, validation_alias="STRATEGY_MIN_5M_TREND_SLOPE"
    )
    strategy_min_breakout_clearance: float = Field(
        default=0.0003, validation_alias="STRATEGY_MIN_BREAKOUT_CLEARANCE"
    )
    # Allow "near breakout" entries up to this fraction below the swing.
    strategy_near_breakout_clearance: float = Field(
        default=0.0030, validation_alias="STRATEGY_NEAR_BREAKOUT_CLEARANCE"
    )
    # Relaxed: lets the bot enter mid-move; quick exits cap the downside.
    strategy_max_late_entry_pct: float = Field(
        default=0.0200, validation_alias="STRATEGY_MAX_LATE_ENTRY_PCT"
    )
    strategy_min_above_twap_pct: float = Field(
        default=0.0, validation_alias="STRATEGY_MIN_ABOVE_TWAP_PCT"
    )
    # Score weights (5m-focused). Sum ~= 1.0; volume_w stays 0 until WS QUOTE.
    score_w_volatility: float = Field(default=0.25, validation_alias="SCORE_W_VOLATILITY")
    score_w_momentum: float = Field(default=0.45, validation_alias="SCORE_W_MOMENTUM")
    score_w_breakout: float = Field(default=0.30, validation_alias="SCORE_W_BREAKOUT")
    score_w_volume: float = Field(default=0.00, validation_alias="SCORE_W_VOLUME")
    # Minimum brain score to even consider acting. Lowered to 0.30 so the
    # brain produces lots of candidates → LLM classifier picks the cleanest
    # via LLM_DECISION_THRESHOLD. Brain = scout, LLM = sniper.
    strategy_min_score: float = Field(default=0.30, validation_alias="STRATEGY_MIN_SCORE")
    # Pullback / continuation / scalp pattern detection thresholds
    strategy_pullback_min_uptrend_bars: int = Field(
        default=2, validation_alias="STRATEGY_PULLBACK_MIN_UPTREND_BARS"
    )
    strategy_pullback_max_retracement_pct: float = Field(
        default=0.020, validation_alias="STRATEGY_PULLBACK_MAX_RETRACEMENT_PCT"
    )
    strategy_continuation_consolidation_bars: int = Field(
        default=2, validation_alias="STRATEGY_CONTINUATION_CONSOLIDATION_BARS"
    )
    strategy_continuation_max_range_pct: float = Field(
        default=0.006, validation_alias="STRATEGY_CONTINUATION_MAX_RANGE_PCT"
    )
    # SCALP / MOMENTUM — fastest path, fires on any visible 5m push +
    # 1m close in the same direction. Survival is enforced by exit_management
    # (tight stop + quick target), not by entry rigidity.
    strategy_scalp_min_5m_slope: float = Field(
        default=0.0003, validation_alias="STRATEGY_SCALP_MIN_5M_SLOPE"
    )
    # Hard capital range per trade (per-lot notional). 0 disables the bound.
    strategy_min_trade_value: float = Field(
        default=0.0, validation_alias="STRATEGY_MIN_TRADE_VALUE"
    )
    strategy_max_trade_value: float = Field(
        default=0.0, validation_alias="STRATEGY_MAX_TRADE_VALUE"
    )

    # ------------------------- DRY-RUN / PAPER --------------------------
    # When 0, dry-run sizing uses live broker available cash. Set > 0 to
    # let the user simulate "what trades would the bot take with ₹X?"
    # without touching the real account. Adjustable in real time via the
    # dashboard; this is just the startup default.
    dryrun_capital_override: float = Field(
        default=0.0, validation_alias="DRYRUN_CAPITAL_OVERRIDE"
    )
    # Intraday index-option BUY defaults (fractions of option premium, not
    # underlying). Tight SL/TP gets chopped by normal premium wobble + theta;
    # ~10% / ~20% / ~20m matches typical intraday risk-reward for liquid CE/PE.
    paper_stop_loss_pct: float = Field(default=0.10, validation_alias="PAPER_STOP_LOSS_PCT")
    paper_take_profit_pct: float = Field(default=0.20, validation_alias="PAPER_TAKE_PROFIT_PCT")
    paper_max_hold_minutes: int = Field(default=20, validation_alias="PAPER_MAX_HOLD_MINUTES")
    paper_max_open_positions: int = Field(
        default=5, validation_alias="PAPER_MAX_OPEN_POSITIONS"
    )

    # Dynamic exits (score + volatility/momentum on ScannerHit). When True,
    # SL/TP/max-hold ignore LLM exit fields — same triple drives risk + paper + live.
    exit_dynamic_enabled: bool = Field(default=False, validation_alias="EXIT_DYNAMIC_ENABLED")
    exit_dynamic_score_mid: float = Field(default=0.5, validation_alias="EXIT_DYNAMIC_SCORE_MID")
    exit_dynamic_score_high: float = Field(default=0.7, validation_alias="EXIT_DYNAMIC_SCORE_HIGH")
    exit_dynamic_sl_weak: float = Field(default=0.10, validation_alias="EXIT_DYNAMIC_SL_WEAK")
    exit_dynamic_tp_weak: float = Field(default=0.15, validation_alias="EXIT_DYNAMIC_TP_WEAK")
    exit_dynamic_hold_weak: int = Field(default=20, validation_alias="EXIT_DYNAMIC_HOLD_WEAK")
    exit_dynamic_sl_mid: float = Field(default=0.10, validation_alias="EXIT_DYNAMIC_SL_MID")
    exit_dynamic_tp_mid: float = Field(default=0.20, validation_alias="EXIT_DYNAMIC_TP_MID")
    exit_dynamic_hold_mid: int = Field(default=25, validation_alias="EXIT_DYNAMIC_HOLD_MID")
    exit_dynamic_sl_strong: float = Field(default=0.12, validation_alias="EXIT_DYNAMIC_SL_STRONG")
    exit_dynamic_tp_strong: float = Field(default=0.28, validation_alias="EXIT_DYNAMIC_TP_STRONG")
    exit_dynamic_hold_strong: int = Field(default=28, validation_alias="EXIT_DYNAMIC_HOLD_STRONG")
    # Normalized volatility (0..1) from brain ScoreBreakdown — adjust exits, do not
    # duplicate brain entry filters unless ultra-low skip is explicitly enabled.
    exit_dynamic_vol_low: float = Field(default=0.33, validation_alias="EXIT_DYNAMIC_VOL_LOW")
    exit_dynamic_vol_high: float = Field(default=0.66, validation_alias="EXIT_DYNAMIC_VOL_HIGH")
    exit_dynamic_vol_low_tp_factor: float = Field(
        default=0.95, validation_alias="EXIT_DYNAMIC_VOL_LOW_TP_FACTOR"
    )
    exit_dynamic_vol_low_hold_trim: int = Field(
        default=3, validation_alias="EXIT_DYNAMIC_VOL_LOW_HOLD_TRIM"
    )
    exit_dynamic_vol_high_sl_add: float = Field(
        default=0.02, validation_alias="EXIT_DYNAMIC_VOL_HIGH_SL_ADD"
    )
    exit_dynamic_vol_high_tp_add: float = Field(
        default=0.05, validation_alias="EXIT_DYNAMIC_VOL_HIGH_TP_ADD"
    )
    exit_dynamic_momentum_high: float = Field(default=0.60, validation_alias="EXIT_DYNAMIC_MOMENTUM_HIGH")
    exit_dynamic_hold_momentum_cap: int = Field(
        default=15, validation_alias="EXIT_DYNAMIC_HOLD_MOMENTUM_CAP"
    )
    exit_dynamic_sl_max: float = Field(default=0.18, validation_alias="EXIT_DYNAMIC_SL_MAX")
    exit_dynamic_tp_max: float = Field(default=0.40, validation_alias="EXIT_DYNAMIC_TP_MAX")
    exit_dynamic_hold_min: int = Field(default=10, validation_alias="EXIT_DYNAMIC_HOLD_MIN")
    exit_dynamic_hold_max: int = Field(default=35, validation_alias="EXIT_DYNAMIC_HOLD_MAX")
    exit_dynamic_skip_ultra_low_vol: bool = Field(
        default=False, validation_alias="EXIT_DYNAMIC_SKIP_ULTRA_LOW_VOL"
    )
    exit_dynamic_vol_ultra_low: float = Field(
        default=0.12, validation_alias="EXIT_DYNAMIC_VOL_ULTRA_LOW"
    )

    # Trailing stop on option premium (long CE/PE): raises stop_price toward peak;
    # never below initial SL. Take-profit unchanged in v1.
    trail_stop_enabled: bool = Field(default=False, validation_alias="TRAIL_STOP_ENABLED")
    trail_stop_pct: float = Field(default=0.10, validation_alias="TRAIL_STOP_PCT")
    trail_arm_min_profit_pct: float = Field(default=0.05, validation_alias="TRAIL_ARM_MIN_PROFIT_PCT")

    # ------------------------- RATE LIMITS ------------------------------
    # Client-side guard for the published Angel One quotas
    # (https://smartapi.angelone.in/docs/RateLimit). Disabling this is only
    # safe in offline tests where the broker is not real.
    rate_limit_enabled: bool = Field(default=True, validation_alias="RATE_LIMIT_ENABLED")
    # 0..1 — run at this fraction of the documented cap to leave headroom
    # for in-flight retries and clock skew.
    rate_limit_safety_factor: float = Field(
        default=0.9, validation_alias="RATE_LIMIT_SAFETY_FACTOR"
    )
    # Minimum pause after each successful ``getCandleData`` response (seconds).
    # Historical candles share Angel's global gateway budget with LTP / RMS /
    # orders; this gap keeps us under the observed 403 threshold in practice.
    rate_limit_candle_min_gap_s: float = Field(
        default=0.75, validation_alias="RATE_LIMIT_CANDLE_MIN_GAP_S"
    )

    log_format: str = Field(default="console", validation_alias="LOG_FORMAT")

    @field_validator("log_format")
    @classmethod
    def log_format_ok(cls, v: str) -> str:
        x = v.strip().lower()
        if x not in ("console", "json"):
            raise ValueError("LOG_FORMAT must be console or json")
        return x

    def ltp_exchange_tokens(self) -> dict[str, list[str]]:
        raw: Any = json.loads(self.ltp_exchange_tokens_json)
        if not isinstance(raw, dict):
            raise ValueError("LTP_EXCHANGE_TOKENS_JSON must be a JSON object")
        out: dict[str, list[str]] = {}
        for k, v in raw.items():
            if not isinstance(k, str) or not isinstance(v, list):
                raise ValueError("LTP_EXCHANGE_TOKENS_JSON must map string -> list")
            out[k] = [str(x) for x in v]
        return out

    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.dashboard_cors_origins.split(",") if o.strip()]

    def scanner_watchlist(self) -> dict[str, list[dict[str, Any]]]:
        raw: Any = json.loads(self.scanner_watchlist_json)
        if not isinstance(raw, dict):
            raise ValueError("SCANNER_WATCHLIST_JSON must be a JSON object {exchange:[entries]}")
        out: dict[str, list[dict[str, Any]]] = {}
        for ex, items in raw.items():
            if not isinstance(items, list):
                continue
            out[str(ex).upper()] = [dict(it) for it in items if isinstance(it, dict)]
        return out

    def universe_spec(self) -> dict[str, Any] | None:
        raw = (self.universe_spec_json or "").strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"UNIVERSE_SPEC_JSON is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("UNIVERSE_SPEC_JSON must be a JSON object")
        return data


@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
