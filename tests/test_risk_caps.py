"""Tests for the new risk-engine controls: hourly cap, post-loss cooldown,
N concurrent positions, and capital effective-source resolution."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from angel_bot.config import Settings
from angel_bot.risk.engine import RiskEngine, position_size_for_stop


def _settings(**overrides) -> Settings:
    base = dict(
        ANGEL_API_KEY="k",
        ANGEL_CLIENT_CODE="c",
        ANGEL_PIN="p",
        RISK_CAPITAL_RUPEES="0",
        RISK_PER_TRADE_PCT="1.0",
        RISK_MAX_DAILY_LOSS_PCT="2.5",
        RISK_MAX_TRADES_PER_DAY="12",
        RISK_ONE_POSITION_AT_A_TIME="false",
        RISK_MAX_TRADES_PER_HOUR="3",
        RISK_LOSS_COOLDOWN_MINUTES="10",
        BOT_MAX_CONCURRENT_POSITIONS="3",
    )
    base.update(overrides)
    import os
    for k, v in base.items():
        os.environ[k] = str(v)
    return Settings()


def test_position_size_rounds_to_lots():
    qty = position_size_for_stop(
        capital=5_000, risk_pct=1.0, entry=100, stop=99, lot_size=50,
    )
    assert qty == 50


def test_concurrent_cap_blocks_when_full():
    s = _settings()
    e = RiskEngine(s)
    e.set_broker_cash(100_000)
    e.set_open_count(3)   # at cap
    d = e.evaluate_new_trade(entry=100, stop=99, lot_size=50)
    assert not d.allowed
    assert "max_concurrent" in d.reason


def test_one_position_at_a_time_overrides_concurrent_cap():
    s = _settings(RISK_ONE_POSITION_AT_A_TIME="true")
    e = RiskEngine(s)
    e.set_broker_cash(100_000)
    e.set_open_count(1)
    d = e.evaluate_new_trade(entry=100, stop=99, lot_size=50)
    assert not d.allowed
    assert d.reason == "open_position"


def test_hourly_cap_blocks_after_n_entries():
    s = _settings(RISK_MAX_TRADES_PER_HOUR="2")
    e = RiskEngine(s)
    e.set_broker_cash(100_000)
    now = datetime.now(timezone.utc)
    e.record_entry(now - timedelta(minutes=10))
    e.record_entry(now - timedelta(minutes=2))
    d = e.evaluate_new_trade(entry=100, stop=99, lot_size=50)
    assert not d.allowed
    assert "max_trades_hour" in d.reason


def test_old_entries_drop_out_of_hourly_window():
    s = _settings(RISK_MAX_TRADES_PER_HOUR="2")
    e = RiskEngine(s)
    e.set_broker_cash(100_000)
    now = datetime.now(timezone.utc)
    e.record_entry(now - timedelta(minutes=120))   # outside window
    e.record_entry(now - timedelta(minutes=70))    # outside window
    e.record_entry(now - timedelta(minutes=5))     # inside
    d = e.evaluate_new_trade(entry=100, stop=99, lot_size=50)
    assert d.allowed
    # And the bookkeeping list trimmed itself
    assert e.trades_last_hour() == 1


def test_zero_hourly_cap_disables_throttle():
    """Default behaviour must allow unlimited entries when the cap is 0."""
    s = _settings(RISK_MAX_TRADES_PER_HOUR="0")
    e = RiskEngine(s)
    e.set_broker_cash(100_000)
    now = datetime.now(timezone.utc)
    for i in range(20):
        e.record_entry(now - timedelta(seconds=i))
    d = e.evaluate_new_trade(entry=100, stop=99, lot_size=50)
    assert d.allowed
    assert "max_trades_hour" not in d.reason


def test_zero_daily_cap_disables_throttle():
    s = _settings(RISK_MAX_TRADES_PER_DAY="0")
    e = RiskEngine(s)
    e.set_broker_cash(100_000)
    e.state.trades_today = 50  # would have blown past the legacy 12/day cap
    d = e.evaluate_new_trade(entry=100, stop=99, lot_size=50)
    assert d.allowed
    assert "max_trades_today" not in d.reason


def test_post_loss_cooldown_blocks_new_entries():
    s = _settings(RISK_LOSS_COOLDOWN_MINUTES="10")
    e = RiskEngine(s)
    e.set_broker_cash(100_000)
    e.record_close(realized_pnl=-500.0)
    cooling, remaining = e.in_loss_cooldown()
    assert cooling
    assert remaining > 0
    d = e.evaluate_new_trade(entry=100, stop=99, lot_size=50)
    assert not d.allowed
    assert "loss_cooldown" in d.reason


def test_winning_close_does_not_trigger_cooldown():
    s = _settings()
    e = RiskEngine(s)
    e.set_broker_cash(100_000)
    e.record_close(realized_pnl=+500.0)
    cooling, _ = e.in_loss_cooldown()
    assert not cooling


def test_zero_cooldown_setting_disables_cooldown():
    s = _settings(RISK_LOSS_COOLDOWN_MINUTES="0")
    e = RiskEngine(s)
    e.set_broker_cash(100_000)
    e.record_close(realized_pnl=-1000.0)
    cooling, _ = e.in_loss_cooldown()
    assert not cooling


def test_effective_capital_uses_broker_cash_when_setting_is_zero():
    s = _settings(RISK_CAPITAL_RUPEES="0")
    e = RiskEngine(s)
    e.set_broker_cash(7_500)
    assert e.effective_capital() == 7_500


def test_effective_capital_override_when_setting_positive():
    s = _settings(RISK_CAPITAL_RUPEES="50000")
    e = RiskEngine(s)
    e.set_broker_cash(7_500)
    assert e.effective_capital() == 50_000
