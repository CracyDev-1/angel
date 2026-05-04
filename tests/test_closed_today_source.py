"""Regression tests for the SQL-backed "closed today" source.

The dashboard's ``Closed today`` panel and the ``PnL today`` tile must
agree at all times. They previously diverged because:

  - "Closed today" was rebuilt from the in-memory decision log, capped
    at 120 events. Once a session pushed past 120 events the closures
    silently fell out and the panel showed "0 trades closed yet today".
  - "PnL today" came from ``daily_stats_mode["live"]``, an SQLite
    accumulator that never forgets — so the user saw a non-zero
    realized total alongside an empty closed-trade list.

Both views are now sourced from ``live_exit_plans`` filtered by the
IST trading-day boundary, which is what these tests pin.
"""

from __future__ import annotations

from datetime import UTC, datetime, time as dtime, timedelta, timezone

import pytest

from angel_bot.state.store import StateStore


_IST = timezone(timedelta(hours=5, minutes=30))


def _ist_midnight_utc_iso() -> str:
    now_ist = datetime.now(_IST)
    midnight_ist = datetime.combine(now_ist.date(), dtime(0, 0, tzinfo=_IST))
    return midnight_ist.astimezone(UTC).isoformat()


def _seed_open_plan(store: StateStore, *, oid: str, side: str = "CE", qty: int = 50) -> int:
    return store.create_live_exit_plan({
        "open_order_id": oid,
        "exchange": "NFO",
        "symboltoken": "12345",
        "tradingsymbol": f"NIFTY{oid}",
        "kind": "INDEX",
        "side": side,
        "signal": "BUY_CALL",
        "underlying": "NIFTY",
        "qty": qty,
        "lots": 1,
        "lot_size": qty,
        "planned_entry": 100.0,
        "stop_price": 99.0,
        "target_price": 102.0,
        "max_hold_minutes": 60,
        "product": "INTRADAY",
        "variety": "NORMAL",
        "source": "bot",
    })


@pytest.fixture()
def store(tmp_path) -> StateStore:
    return StateStore(str(tmp_path / "state.sqlite3"))


def test_closed_today_only_returns_today_rows(store: StateStore) -> None:
    """Plans closed yesterday must NOT appear in today's list."""
    yesterday_oid = _seed_open_plan(store, oid="YDAY")
    today_oid = _seed_open_plan(store, oid="TDAY")

    # Close one with a backdated closed_at (pretend yesterday) — we
    # bypass close_live_exit_plan so we can stamp an arbitrary time.
    yesterday_iso = (datetime.now(_IST) - timedelta(days=2)).astimezone(UTC).isoformat()
    with store._connect() as con:  # noqa: SLF001 — test-only DB poke
        con.execute(
            "UPDATE live_exit_plans SET closed_at=?, exit_price=?, exit_reason=?, realized_pnl=? WHERE id=?",
            (yesterday_iso, 95.0, "stop", -250.0, yesterday_oid),
        )

    store.close_live_exit_plan(today_oid, exit_price=102.0, exit_reason="target", realized_pnl=100.0)

    rows = store.list_closed_live_plans_since(_ist_midnight_utc_iso())
    assert len(rows) == 1
    assert rows[0]["realized_pnl"] == pytest.approx(100.0)


def test_closed_pnl_aggregate_matches_row_sum(store: StateStore) -> None:
    """``closed_live_pnl_since`` must equal SUM(realized_pnl) for the
    same window — the tile and the panel can't disagree."""
    a = _seed_open_plan(store, oid="A")
    b = _seed_open_plan(store, oid="B", side="PE")
    c = _seed_open_plan(store, oid="C")

    store.close_live_exit_plan(a, exit_price=102.0, exit_reason="target", realized_pnl=100.0)
    store.close_live_exit_plan(b, exit_price=98.0, exit_reason="stop", realized_pnl=-100.0)
    store.close_live_exit_plan(c, exit_price=103.0, exit_reason="target", realized_pnl=150.0)

    since = _ist_midnight_utc_iso()
    rows = store.list_closed_live_plans_since(since)
    n, total = store.closed_live_pnl_since(since)

    assert n == len(rows) == 3
    assert total == pytest.approx(sum(r["realized_pnl"] for r in rows))
    assert total == pytest.approx(150.0)


def test_open_plans_are_excluded(store: StateStore) -> None:
    """Plans with closed_at IS NULL must not be reported as closed."""
    open_id = _seed_open_plan(store, oid="OPEN")
    closed_id = _seed_open_plan(store, oid="DONE")
    store.close_live_exit_plan(closed_id, exit_price=101.0, exit_reason="target", realized_pnl=50.0)

    rows = store.list_closed_live_plans_since(_ist_midnight_utc_iso())
    assert len(rows) == 1
    assert rows[0]["open_order_id"] == "DONE"
    # The open one must still appear in the open list.
    assert any(p["id"] == open_id for p in store.list_open_live_exit_plans())


def test_empty_window_returns_zero_count_and_pnl(store: StateStore) -> None:
    n, total = store.closed_live_pnl_since(_ist_midnight_utc_iso())
    assert n == 0
    assert total == 0.0
