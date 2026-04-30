"""Smoke tests for the paper trader.

Cover the three close paths (target, stop, session-end) plus realised P&L
landing in daily_stats_mode['dryrun'].
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from angel_bot.paper import PaperConfig, PaperOpenRequest, PaperTrader
from angel_bot.state.store import StateStore


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.sqlite3")


def _open_call(p: PaperTrader, *, entry: float = 100.0, lots: int = 1, lot_size: int = 10) -> int:
    req = PaperOpenRequest(
        exchange="NSE",
        symboltoken="9999",
        tradingsymbol="TEST",
        kind="EQUITY",
        signal="BUY_CALL",
        side="CE",
        entry_price=entry,
        lots=lots,
        lot_size=lot_size,
        capital_at_open=10_000.0,
        reason="unit_test",
    )
    return p.open(req)


def _open_put(p: PaperTrader, *, entry: float = 200.0) -> int:
    req = PaperOpenRequest(
        exchange="NSE",
        symboltoken="8888",
        tradingsymbol="PUTSYM",
        kind="EQUITY",
        signal="BUY_PUT",
        side="PE",
        entry_price=entry,
        lots=1,
        lot_size=5,
        capital_at_open=5_000.0,
        reason="unit_test",
    )
    return p.open(req)


def test_call_hits_target_and_realised_pnl_lands(store: StateStore) -> None:
    trader = PaperTrader(store, PaperConfig(stop_loss_pct=0.01, take_profit_pct=0.02))
    pid = _open_call(trader, entry=100.0, lots=1, lot_size=10)
    assert pid > 0
    # +2% triggers the target
    events = trader.mark_and_close({("NSE", "9999"): 102.0})
    assert len(events) == 1
    ev = events[0]
    assert ev.exit_reason == "target"
    assert ev.realized_pnl == pytest.approx((102.0 - 100.0) * 10)
    trades, pnl = store.get_mode_daily_stats("dryrun")
    assert trades == 1
    assert pnl == pytest.approx(20.0)
    assert trader.open_positions_summary()["open_positions"] == 0


def test_call_hits_stop_records_loss(store: StateStore) -> None:
    trader = PaperTrader(store, PaperConfig(stop_loss_pct=0.01, take_profit_pct=0.02))
    _open_call(trader, entry=100.0, lots=1, lot_size=10)
    events = trader.mark_and_close({("NSE", "9999"): 99.0})
    assert len(events) == 1
    assert events[0].exit_reason == "stop"
    assert events[0].realized_pnl == pytest.approx(-10.0)
    _, pnl = store.get_mode_daily_stats("dryrun")
    assert pnl == pytest.approx(-10.0)


def test_put_hits_target_when_price_drops(store: StateStore) -> None:
    trader = PaperTrader(store, PaperConfig(stop_loss_pct=0.01, take_profit_pct=0.02))
    _open_put(trader, entry=200.0)  # PE wants price down
    events = trader.mark_and_close({("NSE", "8888"): 196.0})  # -2% -> target
    assert len(events) == 1
    assert events[0].exit_reason == "target"
    assert events[0].realized_pnl == pytest.approx((200.0 - 196.0) * 5)


def test_session_timeout_closes_at_max_hold(store: StateStore) -> None:
    trader = PaperTrader(store, PaperConfig(max_hold_minutes=1, stop_loss_pct=0.5, take_profit_pct=0.5))
    _open_call(trader, entry=100.0)
    # No price move. Force the mark to be 5 minutes after open.
    later = datetime.now(UTC) + timedelta(minutes=5)
    events = trader.mark_and_close({("NSE", "9999"): 100.05}, now=later)
    assert len(events) == 1
    assert events[0].exit_reason == "session_end"


def test_unrealized_pnl_reflects_last_mark(store: StateStore) -> None:
    trader = PaperTrader(store, PaperConfig(stop_loss_pct=0.10, take_profit_pct=0.10))
    _open_call(trader, entry=100.0, lots=2, lot_size=5)  # qty 10
    trader.mark_and_close({("NSE", "9999"): 100.5})  # +0.5 within both bands
    summary = trader.open_positions_summary()
    assert summary["open_positions"] == 1
    assert summary["unrealized_pnl_total"] == pytest.approx(5.0)


def test_reset_wipes_paper_and_dryrun_history(store: StateStore) -> None:
    trader = PaperTrader(store, PaperConfig())
    _open_call(trader)
    trader.mark_and_close({("NSE", "9999"): 102.0})  # close on target
    assert store.get_mode_daily_stats("dryrun")[0] == 1
    trader.reset()
    assert store.list_open_paper_positions() == []
    assert store.get_mode_daily_stats("dryrun") == (0, 0.0)


def test_max_open_positions_cap(store: StateStore) -> None:
    trader = PaperTrader(store, PaperConfig(max_open_positions=2))
    _open_call(trader)
    _open_put(trader)
    assert trader.has_capacity() is False


def test_mode_history_isolation_between_live_and_dryrun(store: StateStore) -> None:
    trader = PaperTrader(store, PaperConfig())
    _open_call(trader)
    trader.mark_and_close({("NSE", "9999"): 102.0})
    store.add_mode_pnl("live", 500.0)  # pretend the live broker booked a profit
    live = store.get_mode_daily_stats("live")
    dry = store.get_mode_daily_stats("dryrun")
    assert live[1] == pytest.approx(500.0)
    assert dry[1] == pytest.approx(20.0)
    assert live != dry
