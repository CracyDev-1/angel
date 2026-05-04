"""Tests for dynamic exit resolution (score / volatility / momentum)."""

from __future__ import annotations

import pytest

from angel_bot.config import Settings
from angel_bot.exits.params import resolve_exit_plan
from angel_bot.scanner.engine import ScannerHit


def _hit(score: float, vol: float, mom: float) -> ScannerHit:
    return ScannerHit(
        name="NIFTY",
        exchange="NSE",
        token="99926000",
        kind="INDEX",
        last_price=25000.0,
        prev_close=24900.0,
        change_pct=0.4,
        lot_size=50,
        notional_per_lot=1.0,
        affordable_lots=10,
        score=score,
        score_breakdown={
            "total": score,
            "volatility": vol,
            "momentum": mom,
            "breakout": 0.5,
            "volume": 0.0,
        },
        signal_side="BUY_CALL",
        signal_reason="test",
    )


def test_resolve_weak_mid_strong_tiers() -> None:
    s = Settings(
        ANGEL_API_KEY="k",
        ANGEL_CLIENT_CODE="c",
        ANGEL_PIN="p",
        EXIT_DYNAMIC_ENABLED=True,
        EXIT_DYNAMIC_VOL_LOW=0.0,
        EXIT_DYNAMIC_VOL_HIGH=1.0,
        EXIT_DYNAMIC_MOMENTUM_HIGH=2.0,
    )
    p_weak = resolve_exit_plan(_hit(0.4, 0.5, 0.5), s)
    assert p_weak is not None
    assert p_weak.meta["tier"] == "weak"
    assert p_weak.stop_loss_pct == 0.10
    assert p_weak.take_profit_pct == 0.15

    p_mid = resolve_exit_plan(_hit(0.55, 0.5, 0.5), s)
    assert p_mid is not None and p_mid.meta["tier"] == "mid"
    assert p_mid.take_profit_pct == 0.20

    p_strong = resolve_exit_plan(_hit(0.85, 0.5, 0.5), s)
    assert p_strong is not None and p_strong.meta["tier"] == "strong"
    assert p_strong.take_profit_pct == 0.28


def test_vol_low_adjusts_tp_and_hold() -> None:
    s = Settings(
        ANGEL_API_KEY="k",
        ANGEL_CLIENT_CODE="c",
        ANGEL_PIN="p",
        EXIT_DYNAMIC_ENABLED=True,
        EXIT_DYNAMIC_VOL_LOW=0.50,
        EXIT_DYNAMIC_VOL_HIGH=0.90,
        EXIT_DYNAMIC_VOL_LOW_TP_FACTOR=0.95,
        EXIT_DYNAMIC_VOL_LOW_HOLD_TRIM=3,
        EXIT_DYNAMIC_MOMENTUM_HIGH=2.0,
    )
    p = resolve_exit_plan(_hit(0.55, 0.4, 0.5), s)
    assert p is not None
    assert p.meta["vol_band"] == "low"
    assert p.take_profit_pct == pytest.approx(0.20 * 0.95)
    assert p.max_hold_minutes == 25 - 3


def test_vol_high_widens_sl_tp() -> None:
    s = Settings(
        ANGEL_API_KEY="k",
        ANGEL_CLIENT_CODE="c",
        ANGEL_PIN="p",
        EXIT_DYNAMIC_ENABLED=True,
        EXIT_DYNAMIC_VOL_LOW=0.0,
        EXIT_DYNAMIC_VOL_HIGH=0.40,
        EXIT_DYNAMIC_VOL_HIGH_SL_ADD=0.02,
        EXIT_DYNAMIC_VOL_HIGH_TP_ADD=0.05,
        EXIT_DYNAMIC_MOMENTUM_HIGH=2.0,
    )
    p = resolve_exit_plan(_hit(0.55, 0.9, 0.5), s)
    assert p is not None
    assert p.meta["vol_band"] == "high"
    assert p.stop_loss_pct == pytest.approx(0.12)
    assert p.take_profit_pct == pytest.approx(0.25)


def test_momentum_caps_hold() -> None:
    s = Settings(
        ANGEL_API_KEY="k",
        ANGEL_CLIENT_CODE="c",
        ANGEL_PIN="p",
        EXIT_DYNAMIC_ENABLED=True,
        EXIT_DYNAMIC_VOL_LOW=0.0,
        EXIT_DYNAMIC_VOL_HIGH=1.0,
        EXIT_DYNAMIC_MOMENTUM_HIGH=0.55,
        EXIT_DYNAMIC_HOLD_MOMENTUM_CAP=15,
    )
    p = resolve_exit_plan(_hit(0.55, 0.5, 0.99), s)
    assert p is not None
    assert p.max_hold_minutes == 15


def test_ultra_low_vol_skip_when_enabled() -> None:
    s = Settings(
        ANGEL_API_KEY="k",
        ANGEL_CLIENT_CODE="c",
        ANGEL_PIN="p",
        EXIT_DYNAMIC_ENABLED=True,
        EXIT_DYNAMIC_SKIP_ULTRA_LOW_VOL=True,
        EXIT_DYNAMIC_VOL_ULTRA_LOW=0.20,
        EXIT_DYNAMIC_VOL_LOW=0.0,
        EXIT_DYNAMIC_VOL_HIGH=1.0,
        EXIT_DYNAMIC_MOMENTUM_HIGH=2.0,
    )
    assert resolve_exit_plan(_hit(0.8, 0.05, 0.5), s) is None


def test_ultra_low_vol_no_skip_by_default() -> None:
    s = Settings(
        ANGEL_API_KEY="k",
        ANGEL_CLIENT_CODE="c",
        ANGEL_PIN="p",
        EXIT_DYNAMIC_ENABLED=True,
        EXIT_DYNAMIC_VOL_ULTRA_LOW=0.99,
        EXIT_DYNAMIC_VOL_LOW=0.0,
        EXIT_DYNAMIC_VOL_HIGH=1.0,
        EXIT_DYNAMIC_MOMENTUM_HIGH=2.0,
    )
    p = resolve_exit_plan(_hit(0.8, 0.05, 0.5), s)
    assert p is not None
