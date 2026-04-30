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


@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
