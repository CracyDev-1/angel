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

    risk_capital_rupees: float = Field(default=100_000.0, validation_alias="RISK_CAPITAL_RUPEES")
    risk_per_trade_pct: float = Field(default=0.75, validation_alias="RISK_PER_TRADE_PCT")
    risk_max_daily_loss_pct: float = Field(default=2.5, validation_alias="RISK_MAX_DAILY_LOSS_PCT")
    risk_max_trades_per_day: int = Field(default=4, validation_alias="RISK_MAX_TRADES_PER_DAY")
    risk_one_position_at_a_time: bool = Field(
        default=True, validation_alias="RISK_ONE_POSITION_AT_A_TIME"
    )

    instrument_master_csv: str | None = Field(default=None, validation_alias="INSTRUMENT_MASTER_CSV")

    dashboard_host: str = Field(default="127.0.0.1", validation_alias="DASHBOARD_HOST")
    dashboard_port: int = Field(default=9812, validation_alias="DASHBOARD_PORT")
    dashboard_token: SecretStr | None = Field(default=None, validation_alias="DASHBOARD_TOKEN")
    dashboard_cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        validation_alias="DASHBOARD_CORS_ORIGINS",
    )

    trading_enabled: bool = Field(default=False, validation_alias="TRADING_ENABLED")
    bot_loop_interval_s: float = Field(default=5.0, validation_alias="BOT_LOOP_INTERVAL_S")
    bot_max_concurrent_positions: int = Field(default=1, validation_alias="BOT_MAX_CONCURRENT_POSITIONS")
    bot_use_capital_pct: float = Field(default=70.0, validation_alias="BOT_USE_CAPITAL_PCT")
    bot_min_signal_strength: float = Field(default=0.0, validation_alias="BOT_MIN_SIGNAL_STRENGTH")
    bot_default_product: str = Field(default="INTRADAY", validation_alias="BOT_DEFAULT_PRODUCT")
    bot_default_variety: str = Field(default="NORMAL", validation_alias="BOT_DEFAULT_VARIETY")
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

    # ------------------------- BRAIN STRATEGY ---------------------------
    # Universe filters
    strategy_min_volatility_pct: float = Field(
        default=0.20, validation_alias="STRATEGY_MIN_VOLATILITY_PCT"
    )
    strategy_max_chop_score: float = Field(
        default=0.55, validation_alias="STRATEGY_MAX_CHOP_SCORE"
    )
    # Entry timing
    strategy_min_15m_trend_slope: float = Field(
        default=0.0007, validation_alias="STRATEGY_MIN_15M_TREND_SLOPE"
    )
    strategy_min_breakout_clearance: float = Field(
        default=0.0010, validation_alias="STRATEGY_MIN_BREAKOUT_CLEARANCE"
    )
    strategy_max_late_entry_pct: float = Field(
        default=0.0040, validation_alias="STRATEGY_MAX_LATE_ENTRY_PCT"
    )
    strategy_min_above_twap_pct: float = Field(
        default=0.0, validation_alias="STRATEGY_MIN_ABOVE_TWAP_PCT"
    )
    # Score weights (must sum to ~1.0; volume_w stays 0 until WS QUOTE wired)
    score_w_volatility: float = Field(default=0.30, validation_alias="SCORE_W_VOLATILITY")
    score_w_momentum: float = Field(default=0.40, validation_alias="SCORE_W_MOMENTUM")
    score_w_breakout: float = Field(default=0.30, validation_alias="SCORE_W_BREAKOUT")
    score_w_volume: float = Field(default=0.00, validation_alias="SCORE_W_VOLUME")
    # Minimum score to even consider acting
    strategy_min_score: float = Field(default=0.45, validation_alias="STRATEGY_MIN_SCORE")

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
