"""Smoke tests for InstrumentMaster + UniverseBuilder.

These don't hit the network; we feed in a small in-memory list of Instruments
that mimics the relevant slices of Angel's scrip master.
"""

from __future__ import annotations

from angel_bot.instruments.master import Instrument, InstrumentMaster
from angel_bot.instruments.universe import UniverseBuilder, UniverseSpec


def _sample_master() -> InstrumentMaster:
    rows = [
        # Indices (NSE cash AMXIDX)
        Instrument("NSE", "NIFTY 50", "99926000", name="NIFTY", instrument_type="AMXIDX", lot_size=1),
        Instrument("NSE", "BANKNIFTY", "99926009", name="BANKNIFTY", instrument_type="AMXIDX", lot_size=1),
        # Equities
        Instrument("NSE", "RELIANCE-EQ", "2885", name="RELIANCE", instrument_type="EQ", lot_size=1),
        Instrument("NSE", "HDFCBANK-EQ", "1333", name="HDFCBANK", instrument_type="EQ", lot_size=1),
        Instrument("NSE", "INFY-EQ", "1594", name="INFY", instrument_type="EQ", lot_size=1),
        # NIFTY weekly chain — three strikes around 24000
        Instrument(
            "NFO", "NIFTY30APR2624000CE", "OPT-24000-CE",
            name="NIFTY", instrument_type="OPTIDX",
            expiry="2026-04-30", strike=24000, lot_size=50,
        ),
        Instrument(
            "NFO", "NIFTY30APR2624000PE", "OPT-24000-PE",
            name="NIFTY", instrument_type="OPTIDX",
            expiry="2026-04-30", strike=24000, lot_size=50,
        ),
        Instrument(
            "NFO", "NIFTY30APR2624100CE", "OPT-24100-CE",
            name="NIFTY", instrument_type="OPTIDX",
            expiry="2026-04-30", strike=24100, lot_size=50,
        ),
        Instrument(
            "NFO", "NIFTY30APR2624100PE", "OPT-24100-PE",
            name="NIFTY", instrument_type="OPTIDX",
            expiry="2026-04-30", strike=24100, lot_size=50,
        ),
        Instrument(
            "NFO", "NIFTY30APR2623900CE", "OPT-23900-CE",
            name="NIFTY", instrument_type="OPTIDX",
            expiry="2026-04-30", strike=23900, lot_size=50,
        ),
        Instrument(
            "NFO", "NIFTY30APR2623900PE", "OPT-23900-PE",
            name="NIFTY", instrument_type="OPTIDX",
            expiry="2026-04-30", strike=23900, lot_size=50,
        ),
        # MCX commodity future
        Instrument(
            "MCX", "CRUDEOIL30APR26FUT", "MCX-CRUDE",
            name="CRUDEOIL", instrument_type="FUTCOM",
            expiry="2026-04-30", lot_size=100,
        ),
    ]
    return InstrumentMaster(rows)


def test_resolve_exact() -> None:
    m = _sample_master()
    assert m.resolve("NSE", "RELIANCE-EQ").symboltoken == "2885"
    assert m.resolve("nfo", "nifty30apr2624000ce").symboltoken == "OPT-24000-CE"


def test_equity_lookup_by_underlying_name() -> None:
    m = _sample_master()
    inst = m.equity("RELIANCE")
    assert inst is not None
    assert inst.symboltoken == "2885"
    assert inst.lot_size == 1


def test_index_lookup() -> None:
    m = _sample_master()
    assert (m.index("NIFTY") or _sentinel()).symboltoken == "99926000"


def test_option_chain_and_atm() -> None:
    m = _sample_master()
    chain = m.option_chain("NIFTY")
    # 6 rows (3 strikes × 2 sides)
    assert len(chain) == 6

    atm = m.atm_options("NIFTY", spot=24050.0)
    assert atm["expiry"] == "2026-04-30"
    # spot 24050 → ATM is whichever of {23900, 24000, 24100} is closest = 24000
    assert atm["atm_strike"] == 24000
    assert atm["rows"][0]["ce"].symboltoken == "OPT-24000-CE"
    assert atm["rows"][0]["pe"].symboltoken == "OPT-24000-PE"
    assert atm["rows"][0]["ce"].lot_size == 50


def test_atm_with_offsets() -> None:
    m = _sample_master()
    atm = m.atm_options("NIFTY", spot=23990.0, offsets=[-1, 0, 1])
    strikes = [r["strike"] for r in atm["rows"]]
    assert strikes == [23900.0, 24000.0, 24100.0]


def test_commodity_future_lookup() -> None:
    m = _sample_master()
    fut = m.commodity_future("CRUDEOIL")
    assert fut is not None
    assert fut.symboltoken == "MCX-CRUDE"
    assert fut.lot_size == 100


def _bse_master() -> InstrumentMaster:
    """Sample mixing NSE indices, BSE indices, and BFO options for SENSEX."""
    return InstrumentMaster([
        Instrument("NSE", "NIFTY 50", "99926000", name="NIFTY", instrument_type="AMXIDX"),
        Instrument("BSE", "SENSEX", "99919000", name="SENSEX", instrument_type="AMXIDX"),
        Instrument("BSE", "BANKEX", "99919012", name="BANKEX", instrument_type="AMXIDX"),
        # SENSEX weekly options on BFO at two expiries, two strikes each
        Instrument("BFO", "SENSEX07MAY2680000CE", "BFO-W1-80000-CE", name="SENSEX",
                   instrument_type="OPTIDX", expiry="2026-05-07", strike=80000, lot_size=10),
        Instrument("BFO", "SENSEX07MAY2680000PE", "BFO-W1-80000-PE", name="SENSEX",
                   instrument_type="OPTIDX", expiry="2026-05-07", strike=80000, lot_size=10),
        Instrument("BFO", "SENSEX14MAY2680000CE", "BFO-W2-80000-CE", name="SENSEX",
                   instrument_type="OPTIDX", expiry="2026-05-14", strike=80000, lot_size=10),
        Instrument("BFO", "SENSEX14MAY2680000PE", "BFO-W2-80000-PE", name="SENSEX",
                   instrument_type="OPTIDX", expiry="2026-05-14", strike=80000, lot_size=10),
    ])


def test_index_lookup_searches_nse_then_bse() -> None:
    m = _bse_master()
    nifty = m.index("NIFTY")
    assert nifty is not None and nifty.exchange == "NSE"
    sensex = m.index("SENSEX")
    assert sensex is not None and sensex.exchange == "BSE"
    bankex = m.index("BANKEX")
    assert bankex is not None and bankex.exchange == "BSE"


def test_option_chain_auto_routes_to_bfo_for_sensex() -> None:
    m = _bse_master()
    # exchange='auto' (default) should pick BFO for SENSEX
    chain = m.option_chain("SENSEX")
    assert len(chain) == 4
    assert all(r.exchange == "BFO" for r in chain)


def test_upcoming_expiries_returns_n_in_order() -> None:
    from datetime import date
    m = _bse_master()
    exps = m.upcoming_expiries("SENSEX", n=2, on=date(2026, 5, 1))
    assert exps == ["2026-05-07", "2026-05-14"]
    only_one = m.upcoming_expiries("SENSEX", n=1, on=date(2026, 5, 1))
    assert only_one == ["2026-05-07"]


def test_universe_builder_emits_multiple_expiries() -> None:
    # The builder uses the wall clock for upcoming_expiries(), so we craft a
    # master whose expiries are far in the future relative to test runtime.
    master = InstrumentMaster([
        Instrument("BSE", "SENSEX", "99919000", name="SENSEX", instrument_type="AMXIDX"),
        Instrument("BFO", "SENSEX25DEC9980000CE", "FAR1-CE", name="SENSEX",
                   instrument_type="OPTIDX", expiry="9999-12-25", strike=80000, lot_size=10),
        Instrument("BFO", "SENSEX25DEC9980000PE", "FAR1-PE", name="SENSEX",
                   instrument_type="OPTIDX", expiry="9999-12-25", strike=80000, lot_size=10),
        Instrument("BFO", "SENSEX31DEC9980000CE", "FAR2-CE", name="SENSEX",
                   instrument_type="OPTIDX", expiry="9999-12-31", strike=80000, lot_size=10),
        Instrument("BFO", "SENSEX31DEC9980000PE", "FAR2-PE", name="SENSEX",
                   instrument_type="OPTIDX", expiry="9999-12-31", strike=80000, lot_size=10),
    ])
    spec = UniverseSpec(
        indices=["SENSEX"], stocks=[], commodities=[],
        atm_for=["SENSEX"], atm_offsets=[0], atm_expiries=2,
    )
    builder = UniverseBuilder(master)
    watchlist, report = builder.build(spec, spot_provider=lambda u: 80000.0)
    bfo = watchlist.get("BFO", [])
    # 2 expiries × 1 strike × 2 sides (CE+PE) = 4 option entries
    option_entries = [e for e in bfo if e["kind"] == "OPTION"]
    assert len(option_entries) == 4
    distinct_expiries = {e.get("expiry") for e in option_entries}
    assert distinct_expiries == {"9999-12-25", "9999-12-31"}
    assert report.atm_resolved == 4


def test_search_substring() -> None:
    m = _sample_master()
    hits = m.search("NIFTY", exchange="NFO", limit=10)
    assert len(hits) == 6
    eq_hits = m.search("RELI", exchange="NSE")
    assert eq_hits[0].symboltoken == "2885"


def test_universe_builder_resolves_everything() -> None:
    m = _sample_master()
    spec = UniverseSpec(
        indices=["NIFTY"],
        stocks=["RELIANCE", "HDFCBANK"],
        commodities=["CRUDEOIL"],
        atm_for=["NIFTY"],
        atm_offsets=[0],
    )
    builder = UniverseBuilder(m)

    # Provide a fake spot for NIFTY
    def spot_for(name: str) -> float | None:
        return 24050.0 if name == "NIFTY" else None

    wl, report = builder.build(spec, spot_provider=spot_for)
    # NSE: 1 index + 2 stocks
    assert sum(1 for x in wl["NSE"] if x["kind"] == "INDEX") == 1
    assert sum(1 for x in wl["NSE"] if x["kind"] == "EQUITY") == 2
    # NFO: ATM CE + ATM PE for NIFTY
    nfo = wl.get("NFO", [])
    assert len(nfo) == 2
    sides = sorted([row["side"] for row in nfo])
    assert sides == ["CE", "PE"]
    # Lot size carried through from master
    assert all(row["lot_size"] == 50 for row in nfo)
    # MCX commodity future present
    mcx = wl.get("MCX", [])
    assert len(mcx) == 1
    assert mcx[0]["lot_size"] == 100
    # Report numbers
    assert report.atm_resolved == 2
    assert report.indices_resolved == 1
    assert report.stocks_resolved == 2
    assert report.commodities_resolved == 1


def test_universe_builder_handles_missing_spot() -> None:
    m = _sample_master()
    spec = UniverseSpec(indices=["NIFTY"], stocks=[], commodities=[], atm_for=["NIFTY"], atm_offsets=[0])
    wl, report = UniverseBuilder(m).build(spec, spot_provider=lambda _n: None)
    # No NFO entries
    assert wl.get("NFO") is None
    assert "NIFTY:no_spot" in (report.atm_missing or [])


def test_universe_builder_unknown_underlying_recorded() -> None:
    m = _sample_master()
    spec = UniverseSpec(indices=[], stocks=["DOES_NOT_EXIST"], commodities=[], atm_for=[], atm_offsets=[0])
    _, report = UniverseBuilder(m).build(spec)
    assert "DOES_NOT_EXIST" in (report.stocks_missing or [])


def _sentinel():
    """Return an object that fails the symboltoken assertion if used by mistake."""
    return type("Missing", (), {"symboltoken": None})
