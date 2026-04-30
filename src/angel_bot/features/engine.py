from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from angel_bot.market_data.candles import Candle, CandleAggregator

CandleInterval = Literal["1m", "5m"]


@dataclass
class FeatureSnapshot:
    """Structured price-derived state (not a full TA suite)."""

    last_price: float | None
    ret_1: float | None = None
    range_pct: float | None = None
    momentum: float | None = None
    swing_high: float | None = None
    swing_low: float | None = None
    breakout: bool = False
    breakdown: bool = False
    candles_1m: list[Candle] = field(default_factory=list)
    candles_5m: list[Candle] = field(default_factory=list)
    trend_up: bool = False
    trend_down: bool = False
    range_market: bool = False
    chop_score: float | None = None

    def to_llm_context(self) -> dict:
        """Structured context only — no secrets, no broker tokens."""
        return {
            "last_price": self.last_price,
            "ret_1": self.ret_1,
            "range_pct": self.range_pct,
            "momentum": self.momentum,
            "swing_high": self.swing_high,
            "swing_low": self.swing_low,
            "breakout": self.breakout,
            "breakdown": self.breakdown,
            "trend_up": self.trend_up,
            "trend_down": self.trend_down,
            "range_market": self.range_market,
            "chop_score": self.chop_score,
            "as_of": datetime.now(UTC).isoformat(),
        }


def update_features_from_ltp(prev: FeatureSnapshot | None, ltp: float) -> FeatureSnapshot:
    snap = FeatureSnapshot(last_price=ltp)
    if prev and prev.last_price:
        snap.ret_1 = (ltp - prev.last_price) / prev.last_price
    return snap


def _pct(a: float, b: float) -> float | None:
    if b == 0:
        return None
    return (a - b) / abs(b)


def compute_features(
    *,
    last_price: float | None,
    agg: CandleAggregator,
    prev_close_for_ret: float | None = None,
    swing_lookback: int = 20,
    chop_lookback: int = 12,
) -> FeatureSnapshot:
    """
    Derive swings, range proxy, momentum, chop, breakout flags from closed + in-progress candles.
    """
    if last_price is None:
        return FeatureSnapshot(last_price=None)

    c1, c5, _c15 = agg.all_candles_including_partial()
    snap = FeatureSnapshot(last_price=last_price, candles_1m=list(c1[-swing_lookback:]), candles_5m=list(c5[-swing_lookback:]))

    base = prev_close_for_ret or (c1[-2].c if len(c1) >= 2 else None)
    if base is not None:
        snap.ret_1 = _pct(last_price, base)

    if len(c5) >= 2:
        hi = max(x.h for x in c5[-swing_lookback:])
        lo = min(x.low for x in c5[-swing_lookback:])
        snap.swing_high = hi
        snap.swing_low = lo
        if hi and lo:
            snap.range_pct = (hi - lo) / last_price if last_price else None
        if snap.range_pct is not None and snap.range_pct < 0.0015:
            snap.range_market = True

        recent = c5[-chop_lookback:]
        if len(recent) >= 3:
            dirs = []
            for i in range(1, len(recent)):
                dirs.append(1 if recent[i].c >= recent[i - 1].c else -1)
            changes = sum(1 for i in range(1, len(dirs)) if dirs[i] != dirs[i - 1])
            snap.chop_score = changes / max(1, len(dirs) - 1)

        if len(c5) >= 4:
            snap.trend_up = (
                c5[-1].c >= c5[-2].c and c5[-2].c >= c5[-3].c and c5[-3].c >= c5[-4].c
            )
            snap.trend_down = (
                c5[-1].c <= c5[-2].c and c5[-2].c <= c5[-3].c and c5[-3].c <= c5[-4].c
            )

        if len(c5) >= swing_lookback + 1:
            completed = c5[:-1]
            window = completed[-swing_lookback:]
            prev_hi = max(x.h for x in window)
            prev_lo = min(x.low for x in window)
            snap.breakout = last_price > prev_hi
            snap.breakdown = last_price < prev_lo

    if len(c1) >= 6:
        snap.momentum = _pct(c1[-1].c, c1[-6].c)

    return snap
