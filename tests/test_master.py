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
