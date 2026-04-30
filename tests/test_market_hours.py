from datetime import datetime, timedelta, timezone

from angel_bot.market_hours import (
    IST,
    SESSIONS,
    all_market_status,
    kind_market_status,
)


def _ist(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=IST)


# --- helpers used to pick a known weekday/weekend ----------------------------
# 2026-04-30 is a Thursday (weekday=3). 2026-05-02 is Saturday.

THU = _ist(2026, 4, 30, 12, 0)        # Thursday noon
THU_BEFORE_OPEN = _ist(2026, 4, 30, 8, 30)
THU_AFTER_NSE_CLOSE = _ist(2026, 4, 30, 16, 0)   # NSE closed, MCX still open
THU_AFTER_MCX_CLOSE = _ist(2026, 4, 30, 23, 45)
SAT = _ist(2026, 5, 2, 12, 0)


def test_sessions_have_expected_kinds():
    assert set(SESSIONS) == {"EQUITY", "INDEX", "OPTION", "COMMODITY"}


def test_nse_open_at_noon_thursday():
    s = kind_market_status("EQUITY", now=THU)
    assert s.is_open is True
    assert s.reason == "open"
    assert s.label == "NSE Equity"
    assert "15:30" in (s.closes_at_label or "")


def test_options_follow_nse_hours():
    s = kind_market_status("OPTION", now=THU)
    assert s.is_open is True
    assert s.label == "NSE F&O"


def test_mcx_open_at_noon_thursday():
    s = kind_market_status("COMMODITY", now=THU)
    assert s.is_open is True
    assert s.reason == "open"


def test_nse_closed_after_330_pm_but_mcx_open():
    nse = kind_market_status("EQUITY", now=THU_AFTER_NSE_CLOSE)
    mcx = kind_market_status("COMMODITY", now=THU_AFTER_NSE_CLOSE)
    assert nse.is_open is False
    assert nse.reason == "after_close"
    assert mcx.is_open is True


def test_mcx_closed_late_night():
    s = kind_market_status("COMMODITY", now=THU_AFTER_MCX_CLOSE)
    assert s.is_open is False
    assert s.reason == "after_close"


def test_before_open_in_morning():
    s = kind_market_status("EQUITY", now=THU_BEFORE_OPEN)
    assert s.is_open is False
    assert s.reason == "before_open"
    assert "09:15" in (s.opens_at_label or "")


def test_weekend_all_closed_and_points_to_monday():
    everything = all_market_status(now=SAT)
    for k, st in everything.items():
        assert st["is_open"] is False, f"{k} should be closed on Saturday"
        assert st["is_weekend"] is True
        # opens_at_label should mention a weekday name (Mon)
        assert st["opens_at_label"] is not None and "Mon" in st["opens_at_label"]


def test_unknown_kind_treated_as_open():
    s = kind_market_status("BANANA", now=THU)
    assert s.is_open is True
    assert s.reason == "unknown_kind"


def test_naive_datetime_treated_as_utc():
    # Thursday 06:30 UTC == Thursday 12:00 IST → should be open for EQUITY.
    naive_utc = datetime(2026, 4, 30, 6, 30)
    s = kind_market_status("EQUITY", now=naive_utc)
    assert s.is_open is True


def test_aware_utc_input_converted_to_ist():
    aware = datetime(2026, 4, 30, 6, 30, tzinfo=timezone.utc)
    s = kind_market_status("EQUITY", now=aware)
    assert s.is_open is True


def test_close_time_boundary_is_inclusive_open():
    # 09:15 IST sharp → should be 'open' (boundary).
    boundary = _ist(2026, 4, 30, 9, 15)
    s = kind_market_status("EQUITY", now=boundary)
    assert s.is_open is True


def test_one_second_before_open_is_closed():
    just_before = _ist(2026, 4, 30, 9, 14) + timedelta(seconds=59)
    s = kind_market_status("EQUITY", now=just_before)
    assert s.is_open is False
    assert s.reason == "before_open"
