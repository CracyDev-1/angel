"""Tests for ``normalize_positions``.

Live-mode P&L on the dashboard must track Angel One's app to within a
fresh-tick. Previously the helper fell back to ``netvalue`` (=
sell_amount – buy_amount) when the broker hadn't populated ``pnl`` yet,
which on a fresh open BUY position produced a phantom "loss" equal to
the entire premium spent. These tests pin the new resolution order:

  1. Explicit ``realised`` + ``unrealised`` from Angel.
  2. Mark-to-market with the freshest LTP we have (scanner cache > broker).
  3. Broker ``pnl`` field as a last resort.

And explicitly assert we NEVER fall back to ``netvalue``.
"""

from __future__ import annotations

from angel_bot.broker_models import normalize_positions


def _open_long_row(**overrides) -> dict:
    """A typical Angel getPosition row for a fresh long-CE buy with
    nothing sold yet."""
    base = {
        "exchange": "NFO",
        "tradingsymbol": "NIFTY05MAY2624500CE",
        "symboltoken": "12345",
        "buyqty": "65",
        "sellqty": "0",
        "buyavgprice": "100.00",
        "sellavgprice": "0",
        "totalbuyavgprice": "100.00",
        "totalsellavgprice": "0",
        "buyamount": "6500.00",
        "sellamount": "0",
        "netvalue": "-6500.00",
        "netqty": "65",
        "ltp": "108.00",
        "producttype": "INTRADAY",
    }
    base.update(overrides)
    return base


def test_open_long_with_explicit_realised_unrealised_uses_them() -> None:
    row = _open_long_row(realised="0", unrealised="520")
    out = normalize_positions({"data": [row]})
    assert len(out["rows"]) == 1
    r = out["rows"][0]
    assert r["pnl"] == 520.0
    assert r["realised_pnl"] == 0.0
    assert r["unrealised_pnl"] == 520.0
    assert r["pnl_source"] == "broker_realised_unrealised"
    assert out["pnl_total"] == 520.0
    assert out["unrealized_pnl_total"] == 520.0
    assert out["realized_pnl_total"] == 0.0


def test_open_long_marks_to_market_when_realised_unrealised_missing() -> None:
    """No realised/unrealised in the broker row → MUST recompute from
    LTP × qty, NOT fall back to netvalue."""
    row = _open_long_row()
    out = normalize_positions({"data": [row]})
    r = out["rows"][0]
    # (108 - 100) * 65 = 520
    assert r["pnl"] == 520.0
    assert r["unrealised_pnl"] == 520.0
    assert r["realised_pnl"] == 0.0
    assert r["pnl_source"] == "mark_to_market"
    # The decisive assertion: we must NOT have used netvalue (-6500).
    assert r["pnl"] != -6500.0
    assert out["pnl_total"] == 520.0


def test_open_long_uses_scanner_ltp_over_broker_ltp() -> None:
    """Scanner cache is fresher than the broker's getPosition snapshot."""
    row = _open_long_row(ltp="108.00")
    out = normalize_positions(
        {"data": [row]},
        fresh_prices={("NFO", "12345"): 115.0},
    )
    r = out["rows"][0]
    # Marked at 115, not 108: (115 - 100) * 65 = 975
    assert r["pnl"] == 975.0
    assert r["ltp"] == 115.0
    assert r["scanner_ltp"] == 115.0
    assert r["broker_ltp"] == 108.0


def test_open_long_with_only_pnl_field_uses_it() -> None:
    """No realised/unrealised AND no LTP — fall back to broker pnl."""
    row = _open_long_row(ltp=None, buyavgprice=None, totalbuyavgprice=None, pnl="42.50")
    # Strip avg-buy fields and ltp so mark-to-market can't fire.
    row.pop("buyavgprice", None)
    row["totalbuyavgprice"] = "0"
    row["ltp"] = "0"
    out = normalize_positions({"data": [row]})
    r = out["rows"][0]
    assert r["pnl"] == 42.5
    assert r["pnl_source"] == "broker_pnl"


def test_never_falls_back_to_netvalue() -> None:
    """Stripped row: no realised/unrealised, no pnl, no ltp, no avg-buy.
    Result should be pnl=None, NOT netvalue."""
    row = _open_long_row()
    for k in ("realised", "unrealised", "pnl"):
        row.pop(k, None)
    row["ltp"] = "0"
    row["buyavgprice"] = "0"
    row["totalbuyavgprice"] = "0"
    row["netvalue"] = "-9999.99"
    out = normalize_positions({"data": [row]})
    r = out["rows"][0]
    assert r["pnl"] is None
    assert r["pnl_source"] == "unknown"
    assert out["pnl_total"] == 0.0


def test_partial_close_splits_realised_and_unrealised() -> None:
    """Bought 65, sold 30 — synthetic split should show realised on
    closed leg and unrealised on remaining leg."""
    row = _open_long_row(
        buyqty="65",
        sellqty="30",
        sellavgprice="105.00",
        totalsellavgprice="105.00",
        sellamount="3150.00",
        netqty="35",
        ltp="108.00",
    )
    out = normalize_positions({"data": [row]})
    r = out["rows"][0]
    # mark_to_market on net_qty=35: (108 - 100) * 35 = 280
    assert r["pnl"] == 280.0
    # realised on closed leg: (105 - 100) * 30 = 150
    assert r["realised_pnl"] == 150.0
    # unrealised = total - realised = 280 - 150 = 130
    assert r["unrealised_pnl"] == 130.0


def test_two_position_total_sums_correctly() -> None:
    a = _open_long_row(symboltoken="11", realised="0", unrealised="100")
    b = _open_long_row(
        symboltoken="22",
        tradingsymbol="NIFTY05MAY2624500PE",
        realised="50",
        unrealised="-30",
    )
    out = normalize_positions({"data": [a, b]})
    assert out["pnl_total"] == 120.0
    assert out["realized_pnl_total"] == 50.0
    assert out["unrealized_pnl_total"] == 70.0


def test_capital_used_unaffected() -> None:
    """Capital allocation logic is independent of P&L logic; sanity-check
    we didn't break it."""
    row = _open_long_row()  # buy 65 @ 100 → 6500
    out = normalize_positions({"data": [row]})
    assert out["capital_used_ce"] == 6500.0
    assert out["capital_used_pe"] == 0.0
    assert out["capital_used_total"] == 6500.0


def test_handles_empty_payload() -> None:
    out = normalize_positions({})
    assert out["rows"] == []
    assert out["pnl_total"] == 0.0
    assert out["unrealized_pnl_total"] == 0.0
