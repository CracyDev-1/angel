from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class Candle:
    ts: datetime
    o: float
    h: float
    low: float
    c: float
    v: float = 0.0


def _floor_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def _floor_5m(ts: datetime) -> datetime:
    m = ts.minute - (ts.minute % 5)
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
    """Rolling 1m / 5m OHLC from last traded prices (volume unknown → 0)."""

    def __init__(self, *, max_1m: int = 300, max_5m: int = 200):
        self.max_1m = max_1m
        self.max_5m = max_5m
        self._1m: deque[Candle] = deque(maxlen=max_1m)
        self._5m: deque[Candle] = deque(maxlen=max_5m)
        self._cur_1m: _BarState | None = None
        self._cur_5m: _BarState | None = None

    def push_ltp(self, price: float, ts: datetime | None = None) -> None:
        ts = ts or datetime.now(UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        b1 = _floor_minute(ts)
        b5 = _floor_5m(ts)

        if self._cur_1m is None or self._cur_1m.bucket_start != b1:
            if self._cur_1m is not None:
                self._1m.append(
                    Candle(
                        ts=self._cur_1m.bucket_start,
                        o=self._cur_1m.o,
                        h=self._cur_1m.h,
                        low=self._cur_1m.low,
                        c=self._cur_1m.c,
                        v=self._cur_1m.v,
                    )
                )
            self._cur_1m = _BarState(bucket_start=b1, o=price, h=price, low=price, c=price, v=0.0)
        else:
            self._cur_1m.h = max(self._cur_1m.h, price)
            self._cur_1m.low = min(self._cur_1m.low, price)
            self._cur_1m.c = price

        if self._cur_5m is None or self._cur_5m.bucket_start != b5:
            if self._cur_5m is not None:
                self._5m.append(
                    Candle(
                        ts=self._cur_5m.bucket_start,
                        o=self._cur_5m.o,
                        h=self._cur_5m.h,
                        low=self._cur_5m.low,
                        c=self._cur_5m.c,
                        v=self._cur_5m.v,
                    )
                )
            self._cur_5m = _BarState(bucket_start=b5, o=price, h=price, low=price, c=price, v=0.0)
        else:
            self._cur_5m.h = max(self._cur_5m.h, price)
            self._cur_5m.low = min(self._cur_5m.low, price)
            self._cur_5m.c = price

    def snapshot_lists(self) -> tuple[list[Candle], list[Candle]]:
        """Completed candles only (current in-progress bar excluded)."""
        return (list(self._1m), list(self._5m))

    def all_candles_including_partial(self) -> tuple[list[Candle], list[Candle]]:
        one = list(self._1m)
        five = list(self._5m)
        if self._cur_1m is not None:
            one = one + [
                Candle(
                    ts=self._cur_1m.bucket_start,
                    o=self._cur_1m.o,
                    h=self._cur_1m.h,
                    low=self._cur_1m.low,
                    c=self._cur_1m.c,
                    v=self._cur_1m.v,
                )
            ]
        if self._cur_5m is not None:
            five = five + [
                Candle(
                    ts=self._cur_5m.bucket_start,
                    o=self._cur_5m.o,
                    h=self._cur_5m.h,
                    low=self._cur_5m.low,
                    c=self._cur_5m.c,
                    v=self._cur_5m.v,
                )
            ]
        return (one, five)
