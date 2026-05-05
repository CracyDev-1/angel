from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class BreakoutConfirmPending:
    """5m bar bucket when a raw breakout first qualified; next bar may confirm."""

    candle_bucket_start: datetime
    side: str  # BUY_CALL | BUY_PUT
    swing_level: float
    candle_hi: float
    candle_lo: float


@dataclass
class Candle:
    ts: datetime
    o: float
    h: float
    low: float
    c: float
    v: float = 0.0


def _floor_minute(ts: datetime, step_minutes: int) -> datetime:
    m = ts.minute - (ts.minute % step_minutes)
    return ts.replace(minute=m, second=0, microsecond=0)


@dataclass
class _BarState:
    bucket_start: datetime
    o: float
    h: float
    low: float
    c: float
    v: float


class CandleAggregator:
    """Rolling 1m / 5m / 15m OHLC from last traded prices.

    Volume is 0 unless a tick source provides it (Angel REST LTP doesn't).
    """

    def __init__(
        self,
        *,
        max_1m: int = 300,
        max_5m: int = 200,
        max_15m: int = 96,
    ) -> None:
        self.max_1m = max_1m
        self.max_5m = max_5m
        self.max_15m = max_15m
        self._1m: deque[Candle] = deque(maxlen=max_1m)
        self._5m: deque[Candle] = deque(maxlen=max_5m)
        self._15m: deque[Candle] = deque(maxlen=max_15m)
        self._cur_1m: _BarState | None = None
        self._cur_5m: _BarState | None = None
        self._cur_15m: _BarState | None = None
        # session high/low/twap accumulator (since first push of the calendar day)
        self._session_day: str | None = None
        self.session_high: float | None = None
        self.session_low: float | None = None
        self._twap_sum: float = 0.0
        self._twap_n: int = 0
        # One-bar-delay confirmation for breakout (see BrainEngine.evaluate).
        self.breakout_confirm: BreakoutConfirmPending | None = None

    # ------------------------------------------------------------------
    # ingest
    # ------------------------------------------------------------------
    def push_ltp(self, price: float, ts: datetime | None = None) -> None:
        ts = ts or datetime.now(UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)

        # Reset session aggregates at calendar-day boundary (UTC).
        day = ts.date().isoformat()
        if day != self._session_day:
            self._session_day = day
            self.session_high = price
            self.session_low = price
            self._twap_sum = price
            self._twap_n = 1
            self.breakout_confirm = None
        else:
            self.session_high = price if self.session_high is None else max(self.session_high, price)
            self.session_low = price if self.session_low is None else min(self.session_low, price)
            self._twap_sum += price
            self._twap_n += 1

        self._update_bucket(price, ts, step=1, deck=self._1m, attr="_cur_1m")
        self._update_bucket(price, ts, step=5, deck=self._5m, attr="_cur_5m")
        self._update_bucket(price, ts, step=15, deck=self._15m, attr="_cur_15m")

    def _update_bucket(
        self,
        price: float,
        ts: datetime,
        *,
        step: int,
        deck: deque[Candle],
        attr: str,
    ) -> None:
        bucket_start = _floor_minute(ts, step)
        cur: _BarState | None = getattr(self, attr)
        if cur is None or cur.bucket_start != bucket_start:
            if cur is not None:
                deck.append(
                    Candle(
                        ts=cur.bucket_start, o=cur.o, h=cur.h, low=cur.low, c=cur.c, v=cur.v
                    )
                )
            setattr(
                self,
                attr,
                _BarState(bucket_start=bucket_start, o=price, h=price, low=price, c=price, v=0.0),
            )
        else:
            cur.h = max(cur.h, price)
            cur.low = min(cur.low, price)
            cur.c = price

    def seed_history(
        self,
        *,
        candles_1m: list[Candle] | None = None,
        candles_5m: list[Candle] | None = None,
        candles_15m: list[Candle] | None = None,
    ) -> None:
        """Bulk-load historical candles into each timeframe deque.

        Used at bot startup to backfill warmup state from the broker's
        historical-candle API so the brain can grade signals on cycle 1
        instead of waiting 25-30 minutes for the in-memory aggregator to
        rebuild ≥5 5m bars and ≥2 15m bars from live ticks.

        Existing in-progress bars (``_cur_*``) are cleared because the
        seeded candles already cover their bucket. Session high/low/twap
        are recomputed from the freshest seeded data so the brain's TWAP
        and intraday-range checks have realistic numbers immediately.
        """
        def _seed(deck: deque[Candle], rows: list[Candle] | None) -> None:
            if not rows:
                return
            deck.clear()
            for c in rows[-deck.maxlen :] if deck.maxlen else rows:
                deck.append(c)

        _seed(self._1m, candles_1m)
        _seed(self._5m, candles_5m)
        _seed(self._15m, candles_15m)
        self._cur_1m = None
        self._cur_5m = None
        self._cur_15m = None
        self.breakout_confirm = None

        # Reset session aggregates from the seeded data. Prefer 1m when
        # available (highest resolution), otherwise 5m, otherwise 15m.
        seed = candles_1m or candles_5m or candles_15m or []
        if not seed:
            return
        today_iso = (seed[-1].ts.date()).isoformat()
        same_day = [c for c in seed if c.ts.date().isoformat() == today_iso]
        ref = same_day or seed
        highs = [c.h for c in ref]
        lows = [c.low for c in ref]
        closes = [c.c for c in ref if c.c]
        self._session_day = today_iso
        self.session_high = max(highs) if highs else None
        self.session_low = min(lows) if lows else None
        self._twap_sum = sum(closes)
        self._twap_n = len(closes) or 0

    # ------------------------------------------------------------------
    # snapshots
    # ------------------------------------------------------------------
    def snapshot_lists(self) -> tuple[list[Candle], list[Candle], list[Candle]]:
        """Closed candles only (in-progress bar excluded)."""
        return (list(self._1m), list(self._5m), list(self._15m))

    def all_candles_including_partial(
        self,
    ) -> tuple[list[Candle], list[Candle], list[Candle]]:
        return (
            self._with_partial(self._1m, self._cur_1m),
            self._with_partial(self._5m, self._cur_5m),
            self._with_partial(self._15m, self._cur_15m),
        )

    @staticmethod
    def _with_partial(deck: deque[Candle], cur: _BarState | None) -> list[Candle]:
        out = list(deck)
        if cur is not None:
            out.append(
                Candle(
                    ts=cur.bucket_start, o=cur.o, h=cur.h, low=cur.low, c=cur.c, v=cur.v
                )
            )
        return out

    @property
    def session_twap(self) -> float | None:
        if self._twap_n <= 0:
            return None
        return self._twap_sum / self._twap_n
