"""Tests for the candidate-selection affordability filter and the
decision-log dedupe.

Background
----------
Before the filter, the brain would happily emit a BUY_CALL on an index
like NIFTYNXT50 → the runtime resolved that to its ATM CE option at
₹72k/lot → the funds check skipped with ``need_more_capital`` and that
exact same skip got logged every 5 seconds, flooding the decision log
with identical rows. We now do two things:

1. Drop INDEX candidates whose resolved ATM CE / PE 1-lot premium is
   already above ``deployable`` cash, so they never reach
   ``_consider_trade``.
2. Collapse repeated identical skip reasons (need_more_capital,
   market_closed, kind_disabled, …) to one decision row per 5 minutes.

These tests construct the runtime via ``__new__`` and inject just the
attributes those two pure helpers actually touch — that keeps the test
fast and free of network / sqlite setup.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from angel_bot.config import get_settings
from angel_bot.instruments.master import Instrument
from angel_bot.runtime import TradingRuntime
from angel_bot.scanner.engine import ScannerHit


def _make_index_hit(name: str = "NIFTYNXT50") -> ScannerHit:
    """A BUY_CALL INDEX hit that has cleared the score / signal gates."""
    return ScannerHit(
        name=name,
        exchange="NSE",
        token="99926013",
        kind="INDEX",
        last_price=70000.0,
        prev_close=69500.0,
        change_pct=0.007,
        lot_size=None,
        notional_per_lot=None,
        affordable_lots=None,
        capital_short_for_one_lot=None,
        in_trade_value_range=True,
        capital_range_reason=None,
        underlying=name,
        score=0.95,
        signal_side="BUY_CALL",
        signal_reason="bullish_breakout",
        signal_confidence=0.9,
    )


def _stub_runtime(
    *,
    runtime_trading_enabled: bool = False,
    paper_open_count: int = 0,
    resolve_to: tuple[Instrument | None, int, float | None, str] = (None, 0, None, "stub"),
) -> TradingRuntime:
    """Construct a minimal TradingRuntime for unit-testing pure helpers.

    We use ``__new__`` to skip the heavy ``__init__`` (sqlite, smart
    client, etc.) and only attach the attributes the methods under test
    touch. Settings is the real one (reads .env) — _resolve_executable is
    monkeypatched on the instance so we don't need a master file.
    """
    rt = TradingRuntime.__new__(TradingRuntime)
    rt.settings = get_settings()
    rt._runtime_trading_enabled = runtime_trading_enabled
    rt._last_index_unaffordable = 0
    rt._last_skip_at = {}

    class _PaperStub:
        def __init__(self, n: int) -> None:
            self._n = n

        def open_positions_summary(self) -> dict[str, Any]:
            return {"open_positions": self._n}

    rt.paper = _PaperStub(paper_open_count)  # type: ignore[assignment]

    class _DecisionsStub:
        def __init__(self) -> None:
            self.added: list[Any] = []

        def add(self, dec: Any) -> None:
            self.added.append(dec)

    rt.decisions = _DecisionsStub()  # type: ignore[assignment]

    def _resolve(self, hit: ScannerHit, signal: str) -> tuple[Instrument | None, int, float | None, str]:
        return resolve_to

    rt._resolve_executable = _resolve.__get__(rt, TradingRuntime)  # type: ignore[assignment]
    return rt


def test_index_dropped_when_atm_lot_premium_exceeds_deployable() -> None:
    inst = Instrument(
        exchange="NFO", tradingsymbol="NIFTYNXT5026MAY2670700CE", symboltoken="111", lot_size=25,
    )
    rt = _stub_runtime(resolve_to=(inst, 25, 2900.0, "ok"))  # 25 * 2900 = 72,500 ₹/lot

    hit = _make_index_hit()
    cands = rt._select_top_candidates([hit], {"open_positions": 0}, n=3, deployable=12_000.0)

    assert cands == [], "permanently unaffordable INDEX must not be proposed"
    assert rt._last_index_unaffordable == 1


def test_index_kept_when_atm_lot_premium_fits_deployable() -> None:
    inst = Instrument(
        exchange="NFO", tradingsymbol="NIFTY26MAY2424500CE", symboltoken="222", lot_size=50,
    )
    # 50 * 100 = 5,000 ₹/lot — well within 12,000 budget.
    rt = _stub_runtime(resolve_to=(inst, 50, 100.0, "ok"))

    hit = _make_index_hit("NIFTY")
    cands = rt._select_top_candidates([hit], {"open_positions": 0}, n=3, deployable=12_000.0)

    assert len(cands) == 1
    assert cands[0].name == "NIFTY"
    assert rt._last_index_unaffordable == 0


def test_index_kept_when_resolve_fails_so_consider_trade_can_log_clearly() -> None:
    """If we can't resolve the ATM option yet (e.g. master not loaded,
    or scanner hasn't priced the option this cycle) we must NOT silently
    swallow the candidate — _consider_trade will log a clean
    resolve / no_execution_price skip and the user gets actionable signal."""
    rt = _stub_runtime(resolve_to=(None, 0, None, "no_atm_chain"))

    hit = _make_index_hit()
    cands = rt._select_top_candidates([hit], {"open_positions": 0}, n=3, deployable=12_000.0)

    assert len(cands) == 1
    assert rt._last_index_unaffordable == 0


def test_no_deployable_means_no_index_filtering() -> None:
    """Backwards-compat: callers that don't pass deployable still get the
    pre-existing behaviour (no ATM-affordability gate)."""
    inst = Instrument(exchange="NFO", tradingsymbol="X", symboltoken="333", lot_size=25)
    rt = _stub_runtime(resolve_to=(inst, 25, 2900.0, "ok"))

    hit = _make_index_hit()
    cands = rt._select_top_candidates([hit], {"open_positions": 0}, n=3)

    assert len(cands) == 1


def test_max_concurrent_short_circuits_before_filtering() -> None:
    """If we're already at the position cap, no candidates should come
    back — and the unaffordable counter is reset so a stale value from
    a prior cycle doesn't leak into the dashboard."""
    rt = _stub_runtime(paper_open_count=10)
    rt._last_index_unaffordable = 5  # leftover from a previous cycle

    hit = _make_index_hit()
    cands = rt._select_top_candidates([hit], {"open_positions": 0}, n=3, deployable=12_000.0)

    assert cands == []
    assert rt._last_index_unaffordable == 0


# ---------------------------------------------------------------------------
# Dedupe of repeated noisy skips
# ---------------------------------------------------------------------------


def test_repeated_need_more_capital_collapses_to_one_decision() -> None:
    rt = _stub_runtime()
    hit = _make_index_hit()

    rt._record_skip(
        hit=hit, signal="BUY_CALL",
        reason="need_more_capital:₹59741_for_1_lot of NIFTYNXT5026MAY2670700CE",
        price=2900.0,
    )
    rt._record_skip(
        hit=hit, signal="BUY_CALL",
        reason="need_more_capital:₹59741_for_1_lot of NIFTYNXT5026MAY2670700CE",
        price=2900.0,
    )
    rt._record_skip(
        hit=hit, signal="BUY_CALL",
        reason="need_more_capital:₹59741_for_1_lot of NIFTYNXT5026MAY2670700CE",
        price=2900.0,
    )

    assert len(rt.decisions.added) == 1


def test_dedupe_window_expires_and_a_fresh_skip_is_logged() -> None:
    rt = _stub_runtime()
    hit = _make_index_hit()

    rt._record_skip(
        hit=hit, signal="BUY_CALL",
        reason="need_more_capital:₹59741_for_1_lot of NIFTYNXT5026MAY2670700CE",
        price=2900.0,
    )
    # Fast-forward the dedupe map past the 5-minute window.
    key = next(iter(rt._last_skip_at.keys()))
    rt._last_skip_at[key] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()

    rt._record_skip(
        hit=hit, signal="BUY_CALL",
        reason="need_more_capital:₹59741_for_1_lot of NIFTYNXT5026MAY2670700CE",
        price=2900.0,
    )
    assert len(rt.decisions.added) == 2


def test_different_instruments_do_not_dedupe_each_other() -> None:
    rt = _stub_runtime()
    a = _make_index_hit("NIFTYNXT50")
    b = _make_index_hit("FINNIFTY")
    b.token = "99926037"

    rt._record_skip(hit=a, signal="BUY_CALL", reason="need_more_capital:₹X", price=1.0)
    rt._record_skip(hit=b, signal="BUY_CALL", reason="need_more_capital:₹X", price=1.0)
    assert len(rt.decisions.added) == 2


def test_dedupe_only_applies_to_stable_reason_families() -> None:
    """Transient reasons (warmup / risk / llm) must always log so the
    user sees them flip cycle-to-cycle."""
    rt = _stub_runtime()
    hit = _make_index_hit()

    rt._record_skip(hit=hit, signal="BUY_CALL", reason="warmup", price=None)
    rt._record_skip(hit=hit, signal="BUY_CALL", reason="warmup", price=None)
    rt._record_skip(hit=hit, signal="BUY_CALL", reason="risk:max_trades_hour (2/2)", price=None)
    rt._record_skip(hit=hit, signal="BUY_CALL", reason="risk:max_trades_hour (2/2)", price=None)

    assert len(rt.decisions.added) == 4


@pytest.mark.parametrize(
    "reason",
    [
        "need_more_capital:₹59741_for_1_lot of X",
        "option_lot_value_below_min:₹100<₹500",
        "option_lot_value_above_max:₹100000>₹50000",
        "kind_disabled:OPTION",
        "market_closed:NFO reopens 09:15",
        "duplicate_order_window",
        "no_execution_price for ATM_CE",
        "resolve:no_atm_chain",
    ],
)
def test_each_dedupe_family_collapses_repeated_skips(reason: str) -> None:
    rt = _stub_runtime()
    hit = _make_index_hit()
    rt._record_skip(hit=hit, signal="BUY_CALL", reason=reason, price=None)
    rt._record_skip(hit=hit, signal="BUY_CALL", reason=reason, price=None)
    assert len(rt.decisions.added) == 1
