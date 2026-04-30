"""Smoke tests for the multi-timeframe BrainEngine.

These are not statistical proofs of profitability — they verify the
mechanics: warmup, filters, side selection on a hand-constructed price path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from angel_bot.market_data.candles import CandleAggregator
from angel_bot.strategy.brain import BrainConfig, BrainEngine


def _push_path(agg: CandleAggregator, start: datetime, prices: list[float], step_s: int = 5) -> None:
    """Push prices spaced `step_s` seconds apart so we accumulate 1m/5m/15m bars."""
    for i, p in enumerate(prices):
        agg.push_ltp(p, ts=start + timedelta(seconds=i * step_s))


def test_brain_warmup_returns_no_trade_until_enough_bars():
    agg = CandleAggregator()
    brain = BrainEngine()
    # Only a handful of points → not enough 5m / 15m bars
    base = datetime(2026, 4, 30, 9, 15, tzinfo=UTC)
    _push_path(agg, base, [100.0, 100.1, 100.2, 100.3])
    out = brain.evaluate(last_price=100.3, agg=agg)
    assert out.signal.side == "NO_TRADE"
    assert out.signal.reason in ("warmup", "filter:volatility_ok")


def test_brain_calls_on_clean_uptrend_breakout():
    agg = CandleAggregator()
    brain = BrainEngine(BrainConfig(min_volatility_pct=0.05, min_15m_trend_slope=0.0001))
    base = datetime(2026, 4, 30, 9, 15, tzinfo=UTC)

    # 30 minutes of monotonically rising prices, every 5s.
    # Climb from 100 -> ~101.5 (1.5% over 30m) — clean uptrend.
    n = 360  # 5s * 360 = 1800s = 30 minutes
    prices = [100.0 + (1.5 * i / n) for i in range(n)]
    # Tail with a sharp breakout above the recent high.
    prices += [101.55, 101.60, 101.65, 101.70, 101.80, 101.90]
    _push_path(agg, base, prices)

    out = brain.evaluate(last_price=prices[-1], agg=agg)
    # Should be a CALL or — at minimum — a partial CALL with high confidence.
    assert out.signal.side in ("BUY_CALL", "NO_TRADE")
    if out.signal.side == "NO_TRADE":
        assert out.signal.reason.startswith("call_partial_")
        assert out.signal.confidence >= 0.6


def test_brain_no_trade_in_chop():
    agg = CandleAggregator()
    brain = BrainEngine(BrainConfig(min_volatility_pct=0.05))
    base = datetime(2026, 4, 30, 9, 15, tzinfo=UTC)
    # 30 minutes of zigzag around 100 — should chop-filter out.
    n = 360
    prices = [100.0 + (0.05 if i % 2 == 0 else -0.05) for i in range(n)]
    _push_path(agg, base, prices)
    out = brain.evaluate(last_price=prices[-1], agg=agg)
    assert out.signal.side == "NO_TRADE"


def test_score_breakdown_keys():
    agg = CandleAggregator()
    brain = BrainEngine()
    base = datetime(2026, 4, 30, 9, 15, tzinfo=UTC)
    _push_path(agg, base, [100.0, 100.5, 101.0, 100.8, 101.5])
    sb = brain.score_instrument(last_price=101.5, agg=agg)
    keys = set(sb.to_dict().keys())
    assert {"total", "volatility", "momentum", "breakout", "volume", "inputs"}.issubset(keys)
    assert 0.0 <= sb.total <= 1.0
