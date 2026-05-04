"""LiveExitManager tests.

Verify SL / TP / max-hold trigger the right square-off, that the close P&L
gets routed into ``daily_stats_mode['live']``, that the open-fill back-fill
re-derives SL/TP from the actual fill price, and that a process restart
finds the still-open plans waiting in SQLite.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from angel_bot.exits.live import LiveExitConfig, LiveExitManager
from angel_bot.state.store import StateStore


# ---------------------------------------------------------------------------
# fixtures + small fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "live_exits.sqlite3")


@pytest.fixture
def fake_api() -> AsyncMock:
    api = AsyncMock()
    api.place_order = AsyncMock(
        return_value={"status": True, "data": {"orderid": "CLOSE-1"}, "message": "SUCCESS"}
    )
    return api


def _register_call_plan(
    mgr: LiveExitManager,
    *,
    open_oid: str = "OPEN-1",
    side: str = "CE",
    entry: float = 100.0,
    qty: int = 50,
) -> int:
    return mgr.register_open(
        open_order_id=open_oid,
        exchange="NFO",
        symboltoken="12345",
        tradingsymbol="NIFTY30APR2624000CE",
        kind="INDEX",
        side=side,
        signal="BUY_CALL" if side == "CE" else "BUY_PUT",
        underlying="NIFTY",
        qty=qty,
        lots=1,
        lot_size=qty,
        planned_entry=entry,
        product="INTRADAY",
        variety="NORMAL",
    )


# ---------------------------------------------------------------------------
# triggers — stop / target / max-hold
# ---------------------------------------------------------------------------


def test_call_stop_loss_triggers_market_sell(store: StateStore, fake_api: AsyncMock) -> None:
    mgr = LiveExitManager(
        store,
        LiveExitConfig(stop_loss_pct=0.01, take_profit_pct=0.02, max_hold_minutes=60),
        # CE entry at 100 → stop = 99. Mark at 98.5 → must trigger.
        price_lookup=lambda ex, tok: 98.5,
    )
    plan_id = _register_call_plan(mgr, entry=100.0)

    events = asyncio.run(mgr.mark_and_close(fake_api))

    assert len(events) == 1
    ev = events[0]
    assert ev.plan_id == plan_id
    assert ev.exit_reason == "stop"
    assert ev.exit_price == 98.5
    # CE long side: pnl = (exit - entry) * qty = (98.5 - 100) * 50 = -75
    assert ev.realized_pnl == pytest.approx(-75.0)

    # Order was sent as a SELL MARKET on the same instrument and quantity.
    fake_api.place_order.assert_awaited_once()
    sent = fake_api.place_order.await_args.args[0]
    assert sent["transactiontype"] == "SELL"
    assert sent["ordertype"] == "MARKET"
    assert sent["quantity"] == "50"
    assert sent["symboltoken"] == "12345"

    # Plan is now closed and ledger shows the live loss.
    assert not store.list_open_live_exit_plans()
    trades, pnl = store.get_mode_daily_stats("live")
    assert trades == 1
    assert pnl == pytest.approx(-75.0)


def test_call_take_profit_triggers(store: StateStore, fake_api: AsyncMock) -> None:
    mgr = LiveExitManager(
        store,
        LiveExitConfig(stop_loss_pct=0.01, take_profit_pct=0.02, max_hold_minutes=60),
        # CE entry 100 → target 102. Mark at 102.5 → triggers target.
        price_lookup=lambda ex, tok: 102.5,
    )
    _register_call_plan(mgr, entry=100.0)
    events = asyncio.run(mgr.mark_and_close(fake_api))
    assert events[0].exit_reason == "target"
    assert events[0].realized_pnl == pytest.approx((102.5 - 100.0) * 50)


def test_put_stop_loss_uses_inverse_direction(store: StateStore, fake_api: AsyncMock) -> None:
    mgr = LiveExitManager(
        store,
        LiveExitConfig(stop_loss_pct=0.01, take_profit_pct=0.02, max_hold_minutes=60),
        # PE entry 100 → stop = 101 (price RISING is bad). Mark 101.5 → stop.
        price_lookup=lambda ex, tok: 101.5,
    )
    _register_call_plan(mgr, side="PE", entry=100.0)
    events = asyncio.run(mgr.mark_and_close(fake_api))
    assert events[0].exit_reason == "stop"
    # PE pnl = (entry - exit) * qty = (100 - 101.5) * 50 = -75.
    assert events[0].realized_pnl == pytest.approx(-75.0)


def test_max_hold_session_end_triggers_even_without_price(
    store: StateStore, fake_api: AsyncMock
) -> None:
    # Price lookup returns nothing — only max-hold can fire.
    mgr = LiveExitManager(
        store,
        LiveExitConfig(stop_loss_pct=0.01, take_profit_pct=0.02, max_hold_minutes=10),
        price_lookup=lambda ex, tok: None,
    )
    plan_id = _register_call_plan(mgr, entry=100.0)
    # Manually rewind opened_at so the plan looks 30 minutes old.
    with store._connect() as con:  # noqa: SLF001
        con.execute(
            "UPDATE live_exit_plans SET opened_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(minutes=30)).isoformat(), plan_id),
        )
    events = asyncio.run(mgr.mark_and_close(fake_api))
    assert len(events) == 1
    assert events[0].exit_reason == "session_end"


def test_no_trigger_when_within_band(store: StateStore, fake_api: AsyncMock) -> None:
    mgr = LiveExitManager(
        store,
        LiveExitConfig(stop_loss_pct=0.01, take_profit_pct=0.02, max_hold_minutes=60),
        # 100.5 sits inside [99, 102] AND max-hold not elapsed → no exit.
        price_lookup=lambda ex, tok: 100.5,
    )
    _register_call_plan(mgr, entry=100.0)
    events = asyncio.run(mgr.mark_and_close(fake_api))
    assert events == []
    fake_api.place_order.assert_not_awaited()
    assert len(store.list_open_live_exit_plans()) == 1


# ---------------------------------------------------------------------------
# fill back-fill — SL/TP must re-derive from actual avg_price
# ---------------------------------------------------------------------------


def test_backfill_overwrites_stop_and_target_from_fill_price(
    store: StateStore, fake_api: AsyncMock
) -> None:
    mgr = LiveExitManager(
        store,
        LiveExitConfig(stop_loss_pct=0.01, take_profit_pct=0.02, max_hold_minutes=60),
        # Returning a price comfortably inside the *new* SL/TP band so the
        # mark-and-close pass only triggers the back-fill, not an exit.
        price_lookup=lambda ex, tok: 110.6,
    )
    _register_call_plan(mgr, open_oid="OPEN-2", entry=100.0)

    # Simulate the order tracker reconciling a slipped fill of 110 (10% above plan).
    store.log_order(
        {
            "tradingsymbol": "NIFTY30APR2624000CE",
            "exchange": "NFO",
            "symboltoken": "12345",
            "transactiontype": "BUY",
            "variety": "NORMAL",
        },
        broker_order_id="OPEN-2",
        status="placed",
        lifecycle_status="placed",
        placed_by_bot=True,
        intent="open",
        mode="live",
    )
    store.upsert_broker_order(
        broker_order_id="OPEN-2",
        lifecycle_status="executed",
        broker_status="complete",
        filled_qty=50, pending_qty=0, avg_price=110.0,
        raw_row={"orderid": "OPEN-2", "averageprice": "110"},
    )

    asyncio.run(mgr.mark_and_close(fake_api))

    rows = store.list_open_live_exit_plans()
    assert len(rows) == 1
    plan = rows[0]
    assert float(plan["fill_price"]) == pytest.approx(110.0)
    # New SL = fill * (1 - 1%) = 108.9; target = fill * (1 + 2%) = 112.2.
    assert float(plan["stop_price"]) == pytest.approx(108.9)
    assert float(plan["target_price"]) == pytest.approx(112.2)
    fake_api.place_order.assert_not_awaited()  # the cycle must NOT have fired an exit


# ---------------------------------------------------------------------------
# persistence — plans survive a process restart
# ---------------------------------------------------------------------------


def test_open_plans_survive_a_fresh_manager(store: StateStore, fake_api: AsyncMock) -> None:
    mgr1 = LiveExitManager(
        store,
        LiveExitConfig(stop_loss_pct=0.01, take_profit_pct=0.02, max_hold_minutes=60),
        price_lookup=lambda ex, tok: 100.5,
    )
    plan_id = _register_call_plan(mgr1, entry=100.0)
    assert plan_id > 0

    # Brand-new manager (process restart). Same store. The plan must still be
    # there and the next mark-and-close has the option to act on it.
    mgr2 = LiveExitManager(
        store,
        LiveExitConfig(stop_loss_pct=0.01, take_profit_pct=0.02, max_hold_minutes=60),
        price_lookup=lambda ex, tok: 98.5,  # this time SL fires
    )
    events = asyncio.run(mgr2.mark_and_close(fake_api))
    assert len(events) == 1
    assert events[0].plan_id == plan_id
    assert events[0].exit_reason == "stop"


# ---------------------------------------------------------------------------
# idempotent registration — same broker_order_id twice
# ---------------------------------------------------------------------------


def test_register_open_is_idempotent(store: StateStore) -> None:
    mgr = LiveExitManager(store)
    pid1 = _register_call_plan(mgr, open_oid="DUPE-1", entry=100.0)
    pid2 = _register_call_plan(mgr, open_oid="DUPE-1", entry=100.0)
    assert pid1 == pid2
    assert len(store.list_open_live_exit_plans()) == 1


# ---------------------------------------------------------------------------
# place_order failure must not close the plan locally
# ---------------------------------------------------------------------------


def test_close_order_failure_keeps_plan_open(store: StateStore) -> None:
    api = AsyncMock()
    api.place_order = AsyncMock(side_effect=RuntimeError("broker down"))
    mgr = LiveExitManager(
        store,
        LiveExitConfig(stop_loss_pct=0.01, take_profit_pct=0.02, max_hold_minutes=60),
        price_lookup=lambda ex, tok: 98.0,  # SL would normally fire
    )
    _register_call_plan(mgr, entry=100.0)
    events = asyncio.run(mgr.mark_and_close(api))
    # No event emitted, plan still open so the next cycle retries.
    assert events == []
    assert len(store.list_open_live_exit_plans()) == 1


# ---------------------------------------------------------------------------
# price-lookup failure is non-fatal
# ---------------------------------------------------------------------------


def test_price_lookup_exception_does_not_crash(store: StateStore, fake_api: AsyncMock) -> None:
    def boom(ex: str, tok: str) -> Any:
        raise ValueError("scanner unhappy")

    mgr = LiveExitManager(
        store,
        LiveExitConfig(stop_loss_pct=0.01, take_profit_pct=0.02, max_hold_minutes=999),
        price_lookup=boom,
    )
    _register_call_plan(mgr, entry=100.0)
    # Should swallow the error, find no usable price, and skip exit checks.
    events = asyncio.run(mgr.mark_and_close(fake_api))
    assert events == []
    fake_api.place_order.assert_not_awaited()


# ---------------------------------------------------------------------------
# adoption — picking up positions opened directly on the Angel One platform
# ---------------------------------------------------------------------------


class _FakeMaster:
    """Tiny stub of InstrumentMaster used in adoption tests."""

    def __init__(self, by_token: dict[tuple[str, str], Any]) -> None:
        self._by_token = by_token

    def resolve_by_token(self, exchange: str, symboltoken: str):
        return self._by_token.get((str(exchange).upper(), str(symboltoken)))


class _FakeInst:
    def __init__(self, *, name: str, lot_size: int) -> None:
        self.name = name
        self.lot_size = lot_size


def _adopt_kwargs() -> dict[str, Any]:
    return dict(
        sl_pct=0.01,
        tp_pct=0.02,
        max_hold_minutes=30,
        default_variety="NORMAL",
        product_types={"INTRADAY"},
    )


def test_adopts_external_long_option_position(store: StateStore) -> None:
    mgr = LiveExitManager(store)
    master = _FakeMaster(
        {("NFO", "12345"): _FakeInst(name="NIFTY", lot_size=75)}
    )
    rows = [
        {
            "tradingsymbol": "NIFTY30APR2624000CE",
            "exchange": "NFO",
            "symboltoken": "12345",
            "side": "CE",
            "net_qty": 75,
            "buy_qty": 75,
            "sell_qty": 0,
            "buy_avg": 100.0,
            "sell_avg": 0.0,
            "ltp": 102.0,
            "producttype": "INTRADAY",
        }
    ]

    events = mgr.reconcile_external_positions(rows, master=master, **_adopt_kwargs())

    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "adopted"
    assert ev.tradingsymbol == "NIFTY30APR2624000CE"
    assert ev.qty == 75
    assert ev.entry_price == pytest.approx(100.0)
    plans = store.list_open_live_exit_plans()
    assert len(plans) == 1
    p = plans[0]
    assert p["source"] == "adopted"
    assert int(p["qty"]) == 75
    assert int(p["lot_size"]) == 75
    assert int(p["lots"]) == 1
    assert float(p["fill_price"]) == pytest.approx(100.0)
    # SL = 100 * (1 - 1%) = 99, TP = 100 * (1 + 2%) = 102
    assert float(p["stop_price"]) == pytest.approx(99.0)
    assert float(p["target_price"]) == pytest.approx(102.0)
    assert p["open_order_id"].startswith("ADOPTED:NFO:12345:")
    assert p["underlying"] == "NIFTY"


def test_adoption_is_idempotent_across_cycles(store: StateStore) -> None:
    mgr = LiveExitManager(store)
    master = _FakeMaster(
        {("NFO", "12345"): _FakeInst(name="NIFTY", lot_size=75)}
    )
    rows = [
        {
            "tradingsymbol": "NIFTY30APR2624000CE",
            "exchange": "NFO",
            "symboltoken": "12345",
            "side": "CE",
            "net_qty": 75,
            "buy_qty": 75,
            "sell_qty": 0,
            "buy_avg": 100.0,
            "ltp": 100.0,
            "producttype": "INTRADAY",
        }
    ]
    e1 = mgr.reconcile_external_positions(rows, master=master, **_adopt_kwargs())
    e2 = mgr.reconcile_external_positions(rows, master=master, **_adopt_kwargs())
    assert len(e1) == 1
    assert e2 == []
    assert len(store.list_open_live_exit_plans()) == 1


def test_qty_resync_when_user_partially_exits(store: StateStore) -> None:
    mgr = LiveExitManager(store)
    master = _FakeMaster(
        {("NFO", "12345"): _FakeInst(name="NIFTY", lot_size=75)}
    )
    full = [
        {
            "tradingsymbol": "NIFTY30APR2624000CE",
            "exchange": "NFO",
            "symboltoken": "12345",
            "side": "CE",
            "net_qty": 150,
            "buy_qty": 150,
            "buy_avg": 100.0,
            "ltp": 100.0,
            "producttype": "INTRADAY",
        }
    ]
    mgr.reconcile_external_positions(full, master=master, **_adopt_kwargs())

    # User manually sells 1 lot on the Angel app — net_qty drops to 75.
    half = [dict(full[0], net_qty=75)]
    events = mgr.reconcile_external_positions(half, master=master, **_adopt_kwargs())

    assert any(e.kind == "qty_resync" for e in events)
    plan = store.list_open_live_exit_plans()[0]
    assert int(plan["qty"]) == 75
    assert int(plan["lots"]) == 1


def test_external_close_books_realized_pnl_into_live_stats(store: StateStore) -> None:
    mgr = LiveExitManager(store)
    master = _FakeMaster(
        {("NFO", "12345"): _FakeInst(name="NIFTY", lot_size=75)}
    )
    rows = [
        {
            "tradingsymbol": "NIFTY30APR2624000CE",
            "exchange": "NFO",
            "symboltoken": "12345",
            "side": "CE",
            "net_qty": 75,
            "buy_qty": 75,
            "buy_avg": 100.0,
            "ltp": 100.0,
            "producttype": "INTRADAY",
        }
    ]
    mgr.reconcile_external_positions(rows, master=master, **_adopt_kwargs())

    # User closes the position on the Angel One app at 105 (5 rs profit/share).
    closed = [
        dict(
            rows[0],
            net_qty=0,
            buy_qty=75,
            sell_qty=75,
            sell_avg=105.0,
            ltp=105.0,
        )
    ]
    events = mgr.reconcile_external_positions(closed, master=master, **_adopt_kwargs())

    ext = [e for e in events if e.kind == "external_close"]
    assert len(ext) == 1
    ev = ext[0]
    assert ev.exit_price == pytest.approx(105.0)
    assert ev.realized_pnl == pytest.approx((105.0 - 100.0) * 75)
    assert store.list_open_live_exit_plans() == []
    trades, pnl = store.get_mode_daily_stats("live")
    assert trades == 1
    assert pnl == pytest.approx(375.0)


def test_short_or_excluded_product_is_not_adopted(store: StateStore) -> None:
    mgr = LiveExitManager(store)
    master = _FakeMaster({})
    rows = [
        # net_qty < 0 (short / written option) — bot only manages longs.
        {
            "tradingsymbol": "NIFTY30APR2624000PE",
            "exchange": "NFO",
            "symboltoken": "99999",
            "side": "PE",
            "net_qty": -75,
            "buy_avg": 50.0,
            "ltp": 48.0,
            "producttype": "INTRADAY",
        },
        # Excluded product — user only allowed INTRADAY adoption by default.
        {
            "tradingsymbol": "RELIANCE-EQ",
            "exchange": "NSE",
            "symboltoken": "2885",
            "side": "-",
            "net_qty": 10,
            "buy_avg": 2900.0,
            "ltp": 2950.0,
            "producttype": "DELIVERY",
        },
    ]
    events = mgr.reconcile_external_positions(rows, master=master, **_adopt_kwargs())
    assert events == []
    assert store.list_open_live_exit_plans() == []


def test_adopted_position_then_marks_to_market_and_exits(
    store: StateStore, fake_api: AsyncMock
) -> None:
    """End-to-end: adopt → SL hits → mark_and_close fires the close order
    and books P&L into live stats just like a bot-opened plan."""
    mgr = LiveExitManager(
        store,
        LiveExitConfig(stop_loss_pct=0.01, take_profit_pct=0.02, max_hold_minutes=60),
        price_lookup=lambda ex, tok: 98.5,  # CE adopted at 100 → stop at 99
    )
    master = _FakeMaster(
        {("NFO", "12345"): _FakeInst(name="NIFTY", lot_size=75)}
    )
    rows = [
        {
            "tradingsymbol": "NIFTY30APR2624000CE",
            "exchange": "NFO",
            "symboltoken": "12345",
            "side": "CE",
            "net_qty": 75,
            "buy_qty": 75,
            "buy_avg": 100.0,
            "ltp": 100.0,
            "producttype": "INTRADAY",
        }
    ]
    mgr.reconcile_external_positions(rows, master=master, **_adopt_kwargs())

    events = asyncio.run(mgr.mark_and_close(fake_api))

    assert len(events) == 1
    ev = events[0]
    assert ev.exit_reason == "stop"
    assert ev.source == "adopted"
    assert ev.realized_pnl == pytest.approx((98.5 - 100.0) * 75)
    sent = fake_api.place_order.await_args.args[0]
    assert sent["transactiontype"] == "SELL"
    assert sent["quantity"] == "75"
    assert sent["symboltoken"] == "12345"
    trades, pnl = store.get_mode_daily_stats("live")
    assert trades == 1
    assert pnl == pytest.approx((98.5 - 100.0) * 75)
