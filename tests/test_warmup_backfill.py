"""Tests for the historical-candle warmup backfill.

The bot fetches 5m / 15m / 1m candles from Angel's getCandleData on
startup so the brain doesn't sit in ``warmup`` for 25-30 minutes after
every process restart. These tests verify:

  * CandleAggregator.seed_history populates all three deques and resets
    session aggregates correctly.
  * The brain leaves warmup as soon as enough seeded bars are present.
  * ScannerEngine.warmup_from_history calls Angel for each watchlist
    token and seeds the matching aggregator.
  * Failures (HTTP error / non-success body / empty data) are non-fatal
    and the aggregator is left untouched.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from angel_bot.config import get_settings
from angel_bot.market_data.candles import Candle, CandleAggregator
from angel_bot.scanner.engine import ScannerEngine
from angel_bot.strategy.brain import BrainEngine


def _make_candles(start: datetime, step_min: int, n: int, base: float = 100.0) -> list[Candle]:
    out: list[Candle] = []
    for i in range(n):
        ts = start + timedelta(minutes=i * step_min)
        # Mild monotonic rise so the brain has a usable trend if it wants one.
        c = base + i * 0.05
        out.append(Candle(ts=ts, o=c - 0.02, h=c + 0.05, low=c - 0.05, c=c, v=0.0))
    return out


def test_seed_history_populates_all_three_deques() -> None:
    agg = CandleAggregator()
    base = datetime(2026, 5, 4, 9, 15, tzinfo=UTC)
    c1 = _make_candles(base, 1, 30)
    c5 = _make_candles(base, 5, 12)
    c15 = _make_candles(base, 15, 5)

    agg.seed_history(candles_1m=c1, candles_5m=c5, candles_15m=c15)

    out_1m, out_5m, out_15m = agg.snapshot_lists()
    assert len(out_1m) == 30
    assert len(out_5m) == 12
    assert len(out_15m) == 5
    # Session high / low should reflect the seeded range, not be None.
    assert agg.session_high is not None
    assert agg.session_low is not None
    assert agg.session_high >= agg.session_low


def test_brain_clears_warmup_after_seeding() -> None:
    agg = CandleAggregator()
    brain = BrainEngine()
    base = datetime(2026, 5, 4, 9, 15, tzinfo=UTC)
    # Brain warmup gate: ≥5 5m, ≥2 15m, ≥1 1m. Production backfill always
    # seeds all three timeframes so this is the realistic path.
    agg.seed_history(
        candles_1m=_make_candles(base, 1, 30),
        candles_5m=_make_candles(base, 5, 8),
        candles_15m=_make_candles(base, 15, 3),
    )
    out = brain.evaluate(last_price=100.4, agg=agg)
    assert out.signal.reason != "warmup", (
        f"brain still warming up after seeding; reason={out.signal.reason}"
    )


def test_brain_warmup_blocks_when_only_5m_15m_seeded() -> None:
    """If history fetch returned 5m + 15m but no 1m bars (e.g. an
    illiquid strike with no 1m history yet), the brain must NOT crash and
    must remain in warmup until at least one 1m bar is available."""
    agg = CandleAggregator()
    brain = BrainEngine()
    base = datetime(2026, 5, 4, 9, 15, tzinfo=UTC)
    agg.seed_history(
        candles_5m=_make_candles(base, 5, 8),
        candles_15m=_make_candles(base, 15, 3),
    )
    out = brain.evaluate(last_price=100.4, agg=agg)
    assert out.signal.side == "NO_TRADE"
    assert out.signal.reason == "warmup"


def test_seed_history_clamps_to_deque_maxlen() -> None:
    agg = CandleAggregator(max_5m=4)
    base = datetime(2026, 5, 4, 9, 15, tzinfo=UTC)
    # Seed more bars than the deque can hold — the most recent should win.
    agg.seed_history(candles_5m=_make_candles(base, 5, 10))
    _, c5, _ = agg.snapshot_lists()
    assert len(c5) == 4
    # Last candle must be the freshest seeded one.
    assert c5[-1].ts == (base + timedelta(minutes=5 * 9))


def _angel_history_response(rows: list[Candle]) -> dict[str, Any]:
    """Build the shape Angel returns from /historical/v1/getCandleData."""
    payload = []
    for c in rows:
        # IST ISO with offset is what Angel sends; our parser handles it.
        payload.append([
            c.ts.isoformat(),
            c.o,
            c.h,
            c.low,
            c.c,
            c.v,
        ])
    return {"status": True, "message": "SUCCESS", "data": payload}


def _scanner_with_watchlist() -> ScannerEngine:
    settings = get_settings()
    scanner = ScannerEngine(settings=settings)
    scanner.set_watchlist(
        {
            "NSE": [{"token": "99926000", "name": "NIFTY", "kind": "INDEX"}],
            "NFO": [{"token": "12345", "name": "NIFTY26MAY24500CE", "kind": "OPTION"}],
        }
    )
    return scanner


def test_warmup_from_history_seeds_each_watchlist_token() -> None:
    base = datetime(2026, 5, 4, 9, 15, tzinfo=UTC)
    api = AsyncMock()
    api.get_candle_data = AsyncMock(
        side_effect=lambda *, exchange, symboltoken, interval_minutes, fromdate, todate: (
            _angel_history_response(
                _make_candles(
                    base,
                    interval_minutes,
                    {1: 30, 5: 10, 15: 4}.get(interval_minutes, 0),
                )
            )
        )
    )
    scanner = _scanner_with_watchlist()
    seeded = asyncio.run(scanner.warmup_from_history(api))
    assert seeded == 2
    # Each (exchange, token) made exactly 3 calls (1m / 5m / 15m).
    assert api.get_candle_data.await_count == 6
    # Seeded data is reachable through the aggregator.
    agg_nifty = scanner._aggs["NSE:99926000"]  # noqa: SLF001
    _, c5, c15 = agg_nifty.snapshot_lists()
    assert len(c5) == 10
    assert len(c15) == 4


def test_warmup_only_keys_filters_targets() -> None:
    api = AsyncMock()
    api.get_candle_data = AsyncMock(
        return_value={"status": True, "data": [["2026-05-04T09:15:00+05:30", 100, 101, 99, 100, 0]]}
    )
    scanner = _scanner_with_watchlist()
    asyncio.run(scanner.warmup_from_history(api, only_keys={"NFO:12345"}))
    # Only the option leg should have been called (1m + 5m + 15m = 3 calls).
    assert api.get_candle_data.await_count == 3
    called_tokens = {kw.get("symboltoken") for _, kw in api.get_candle_data.await_args_list}
    assert called_tokens == {"12345"}


def test_warmup_ignores_http_errors_and_keeps_aggregator_clean() -> None:
    api = AsyncMock()
    api.get_candle_data = AsyncMock(side_effect=RuntimeError("broker down"))
    scanner = _scanner_with_watchlist()
    seeded = asyncio.run(scanner.warmup_from_history(api))
    assert seeded == 0
    # Aggregators were NOT created with empty seed data; an unseeded
    # aggregator should still be in its pristine state.
    agg = scanner._aggs["NSE:99926000"]  # noqa: SLF001
    c1, c5, c15 = agg.snapshot_lists()
    assert c1 == [] and c5 == [] and c15 == []


def test_warmup_skips_when_status_false() -> None:
    api = AsyncMock()
    api.get_candle_data = AsyncMock(return_value={"status": False, "message": "AB1010"})
    scanner = _scanner_with_watchlist()
    seeded = asyncio.run(scanner.warmup_from_history(api))
    assert seeded == 0


def test_warmup_handles_iso_with_explicit_offset_and_naive_strings() -> None:
    """Angel sometimes returns timestamps as 'YYYY-MM-DD HH:MM:SS' (naive,
    IST) and sometimes as ISO with the offset; both must round-trip into UTC."""
    api = AsyncMock()
    api.get_candle_data = AsyncMock(
        return_value={
            "status": True,
            "data": [
                ["2026-05-04 09:15:00", 100.0, 101.0, 99.5, 100.5, 0.0],
                ["2026-05-04T09:20:00+05:30", 100.5, 102.0, 100.0, 101.5, 0.0],
            ],
        }
    )
    scanner = _scanner_with_watchlist()
    asyncio.run(scanner.warmup_from_history(api, only_keys={"NSE:99926000"}))
    agg = scanner._aggs["NSE:99926000"]  # noqa: SLF001
    _, c5, _ = agg.snapshot_lists()
    assert len(c5) == 2
    # 09:15 IST = 03:45 UTC
    assert c5[0].ts.hour == 3 and c5[0].ts.minute == 45
    # 09:20 IST = 03:50 UTC
    assert c5[1].ts.hour == 3 and c5[1].ts.minute == 50


@pytest.mark.parametrize("interval_minutes", [1, 5, 15])
def test_smartclient_get_candle_data_maps_intervals(interval_minutes: int) -> None:
    """The wrapper translates minute counts into Angel's enum strings."""
    from angel_bot.smart_client import HIST_INTERVAL_BY_MINUTES, SmartApiClient

    expected = HIST_INTERVAL_BY_MINUTES[interval_minutes]
    sent: dict[str, Any] = {}

    class _FakeSession:
        async def ensure_login(self) -> None:
            return None

    client = SmartApiClient.__new__(SmartApiClient)
    client.session = _FakeSession()  # type: ignore[assignment]
    client.settings = get_settings()

    async def _fake_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        sent["path"] = path
        sent["body"] = body
        return {"status": True, "data": []}

    client._post_with_auth_retry = _fake_post  # type: ignore[assignment]

    asyncio.run(
        client.get_candle_data(
            exchange="NSE",
            symboltoken="99926000",
            interval_minutes=interval_minutes,
            fromdate="2026-05-04 09:15",
            todate="2026-05-04 11:30",
        )
    )

    assert sent["path"].endswith("/getCandleData")
    assert sent["body"]["interval"] == expected
    assert sent["body"]["exchange"] == "NSE"
    assert sent["body"]["symboltoken"] == "99926000"
