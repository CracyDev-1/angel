"""Unit tests for long-premium trailing stop math."""

from __future__ import annotations

import pytest

from angel_bot.exits.trailing import trailing_stop_update_long_premium


def test_disabled_returns_unchanged() -> None:
    peak, stop = trailing_stop_update_long_premium(
        enabled=False,
        trail_pct=0.10,
        arm_profit_pct=0.05,
        entry=100.0,
        last_price=120.0,
        initial_stop=90.0,
        peak=100.0,
        current_stop=90.0,
    )
    assert peak == 100.0
    assert stop == 90.0


def test_before_arm_only_updates_peak() -> None:
    # Arm at 105 (100 * 1.05); last below arm → stop unchanged, peak tracks high
    peak, stop = trailing_stop_update_long_premium(
        enabled=True,
        trail_pct=0.10,
        arm_profit_pct=0.05,
        entry=100.0,
        last_price=104.0,
        initial_stop=90.0,
        peak=100.0,
        current_stop=90.0,
    )
    assert peak == 104.0
    assert stop == 90.0


def test_after_arm_ratchets_stop_never_below_initial() -> None:
    # Arm at 105. last 110 → trail 10% from peak 110 = 99; max(90, 99, 90) = 99
    peak, stop = trailing_stop_update_long_premium(
        enabled=True,
        trail_pct=0.10,
        arm_profit_pct=0.05,
        entry=100.0,
        last_price=110.0,
        initial_stop=90.0,
        peak=100.0,
        current_stop=90.0,
    )
    assert peak == 110.0
    assert stop == pytest.approx(99.0)


def test_trail_respects_initial_stop_floor() -> None:
    # Wide trail_pct would put trail_stop below the fixed initial SL; floor wins.
    peak, stop = trailing_stop_update_long_premium(
        enabled=True,
        trail_pct=0.50,
        arm_profit_pct=0.0,
        entry=100.0,
        last_price=110.0,
        initial_stop=90.0,
        peak=100.0,
        current_stop=90.0,
    )
    # new_peak=110, trail_stop=55, max(90, 55, 90)=90
    assert peak == 110.0
    assert stop == 90.0
