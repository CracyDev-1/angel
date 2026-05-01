"""RiskEngine persistence tests.

Recent-entries (per-hour cap) and last-loss-at (post-loss cooldown) used to
live in-memory only, so a process restart inside a trading session forgot
both — letting the bot bypass its own caps until a full hour had passed.
These tests pin the new write-through-to-SQLite behaviour.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from angel_bot.config import Settings
from angel_bot.risk.engine import RiskEngine
from angel_bot.state.store import StateStore


@pytest.fixture
def store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "risk.sqlite3")


@pytest.fixture
def settings() -> Settings:
    # Tiny env override; only the risk-related fields matter here.
    return Settings(
        ANGEL_API_KEY="k",
        ANGEL_CLIENT_CODE="C0001",
        ANGEL_PIN="0000",
        RISK_CAPITAL_RUPEES=100_000.0,
        RISK_PER_TRADE_PCT=1.0,
        RISK_MAX_DAILY_LOSS_PCT=10.0,
        RISK_MAX_TRADES_PER_DAY=999,
        RISK_MAX_TRADES_PER_HOUR=2,        # easy to hit in a test
        RISK_LOSS_COOLDOWN_MINUTES=15,     # easy to detect
    )


# ---------------------------------------------------------------------------
# record_entry → SQLite → restore on a fresh RiskEngine
# ---------------------------------------------------------------------------


def test_record_entry_persists_and_restores(store: StateStore, settings: Settings) -> None:
    r1 = RiskEngine(settings, store=store)
    r1.record_entry()
    r1.record_entry()
    assert r1.trades_last_hour() == 2

    # Brand-new engine instance (simulates a process restart). It must rehydrate
    # the per-hour cap from SQLite on the first ``sync_from_store`` call.
    r2 = RiskEngine(settings, store=store)
    r2.sync_from_store(store)
    assert r2.trades_last_hour() == 2


def test_record_entry_drops_entries_older_than_an_hour(
    store: StateStore, settings: Settings
) -> None:
    r1 = RiskEngine(settings, store=store)
    long_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    r1.record_entry(when=long_ago)
    r1.record_entry()  # this one stays
    assert r1.trades_last_hour() == 1

    r2 = RiskEngine(settings, store=store)
    r2.sync_from_store(store)
    # The 2-hour-old entry must be trimmed out of the in-memory list AND the
    # SQLite table on restore.
    assert r2.trades_last_hour() == 1
    snap = store.get_risk_state()
    assert len(snap["recent_entries"]) == 1


def test_evaluate_new_trade_blocks_when_hourly_cap_persisted(
    store: StateStore, settings: Settings
) -> None:
    # Two entries: hits the configured per-hour cap of 2.
    r1 = RiskEngine(settings, store=store)
    r1.record_entry()
    r1.record_entry()
    r1.set_broker_cash(100_000.0)
    decision1 = r1.evaluate_new_trade(entry=100.0, stop=99.0, lot_size=1)
    assert decision1.allowed is False
    assert "max_trades_hour" in decision1.reason

    # Restart: the cap must STILL be exhausted because we restored from disk.
    r2 = RiskEngine(settings, store=store)
    r2.sync_from_store(store)
    r2.set_broker_cash(100_000.0)
    decision2 = r2.evaluate_new_trade(entry=100.0, stop=99.0, lot_size=1)
    assert decision2.allowed is False
    assert "max_trades_hour" in decision2.reason


# ---------------------------------------------------------------------------
# record_close → SQLite → restore + cooldown still active
# ---------------------------------------------------------------------------


def test_losing_close_persists_cooldown_and_restores(
    store: StateStore, settings: Settings
) -> None:
    r1 = RiskEngine(settings, store=store)
    loss_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    r1.record_close(realized_pnl=-500.0, when=loss_at)
    cooling, remaining = r1.in_loss_cooldown()
    assert cooling is True
    assert remaining > 0

    # Fresh engine instance — cooldown must survive the restart.
    r2 = RiskEngine(settings, store=store)
    r2.sync_from_store(store)
    cooling2, remaining2 = r2.in_loss_cooldown()
    assert cooling2 is True
    assert remaining2 > 0


def test_winning_close_does_not_set_cooldown(store: StateStore, settings: Settings) -> None:
    r1 = RiskEngine(settings, store=store)
    r1.record_close(realized_pnl=+250.0)
    assert r1.in_loss_cooldown() == (False, 0.0)
    assert store.get_risk_state()["last_loss_at"] is None


def test_evaluate_new_trade_blocks_during_persisted_cooldown(
    store: StateStore, settings: Settings
) -> None:
    r1 = RiskEngine(settings, store=store)
    r1.record_close(realized_pnl=-100.0)

    r2 = RiskEngine(settings, store=store)
    r2.sync_from_store(store)
    r2.set_broker_cash(100_000.0)
    d = r2.evaluate_new_trade(entry=100.0, stop=99.0, lot_size=1)
    assert d.allowed is False
    assert "loss_cooldown" in d.reason


# ---------------------------------------------------------------------------
# attach_store after construction
# ---------------------------------------------------------------------------


def test_attach_store_late_binding_writes_through(store: StateStore, settings: Settings) -> None:
    r = RiskEngine(settings)            # no store at construction
    r.attach_store(store)
    r.record_entry()
    snap = store.get_risk_state()
    assert len(snap["recent_entries"]) == 1
