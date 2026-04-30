"""Dynamic watchlist / universe builder.

Turns a high-level *spec* into a concrete watchlist with every token and lot
size resolved from the instrument master. The runtime calls this:
  * once at startup to seed the scanner watchlist
  * periodically (every ``ATM_REFRESH_INTERVAL_S`` seconds) so the ATM option
    contracts always reflect the current spot — as the underlying moves, the
    "at-the-money" strike changes and the watchlist is rebuilt accordingly.

Spec shape (parsed from ``UNIVERSE_SPEC_JSON`` or built programmatically):

    {
      "indices":     ["NIFTY", "BANKNIFTY", "FINNIFTY"],
      "stocks":      ["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK"],
      "commodities": ["CRUDEOIL", "GOLD", "SILVER"],
      "atm_for":     ["NIFTY", "BANKNIFTY"],
      "atm_offsets": [-1, 0, 1]
    }

Returns the same JSON shape the scanner already understands::

    {
      "NSE": [{"name": "RELIANCE", "token": "...", "kind": "EQUITY", "lot_size": 1}],
      "NFO": [{"name": "NIFTY24APR24500CE", "token": "...", "kind": "OPTION",
               "lot_size": 50, "underlying": "NIFTY", "expiry": "2026-04-30",
               "strike": 24500, "side": "CE"}],
      "MCX": [...]
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import structlog

from angel_bot.instruments.master import Instrument, InstrumentMaster

log = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# Spec
# --------------------------------------------------------------------------- #


@dataclass
class UniverseSpec:
    indices: list[str]
    stocks: list[str]
    commodities: list[str]
    atm_for: list[str]            # underlyings that should auto-resolve ATM CE+PE
    atm_offsets: list[int]        # e.g. [-1, 0, 1] = ITM-1, ATM, OTM-1

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UniverseSpec:
        def _list(key: str) -> list[str]:
            v = d.get(key) or []
            return [str(x).strip().upper() for x in v if str(x).strip()]

        offs = d.get("atm_offsets") or [0]
        try:
            offs_i = [int(x) for x in offs]
        except (TypeError, ValueError):
            offs_i = [0]

        return cls(
            indices=_list("indices"),
            stocks=_list("stocks"),
            commodities=_list("commodities"),
            atm_for=_list("atm_for"),
            atm_offsets=offs_i,
        )

    @classmethod
    def default(cls) -> UniverseSpec:
        # Production-ready default. The bot will only *trade* what fits your
        # available cash — the rest are watched so you can see live prices
        # and rotate when funds change. Toggle whole categories from the
        # dashboard, or override entirely via UNIVERSE_SPEC_JSON in .env.
        #
        # MCX commodity coverage (matches the standard MCX board):
        #   Energy   : CRUDEOIL, CRUDEOILM (mini), NATURALGAS, NATGASMINI
        #   Bullion  : GOLD, GOLDM (mini), GOLDGUINEA (8g), GOLDPETAL (1g),
        #              GOLDTEN (10g), SILVER, SILVERM (mini), SILVERMIC (micro)
        #   Base Met.: COPPER, ZINC, ZINCMINI, LEAD, LEADMINI,
        #              ALUMINIUM, ALUMINI, NICKEL
        return cls(
            indices=["NIFTY", "BANKNIFTY", "FINNIFTY"],
            stocks=["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK"],
            commodities=[
                # Energy
                "CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI",
                # Bullion
                "GOLD", "GOLDM", "GOLDGUINEA", "GOLDPETAL", "GOLDTEN",
                "SILVER", "SILVERM", "SILVERMIC",
                # Base metals
                "COPPER", "ZINC", "ZINCMINI", "LEAD", "LEADMINI",
                "ALUMINIUM", "ALUMINI", "NICKEL",
            ],
            atm_for=["NIFTY", "BANKNIFTY"],
            atm_offsets=[0],
        )


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #


def _entry(inst: Instrument, kind: str, **extra: Any) -> dict[str, Any]:
    out = {
        "name": inst.tradingsymbol,
        "token": inst.symboltoken,
        "kind": kind,
        "lot_size": inst.lot_size or 1,
    }
    if extra:
        out.update(extra)
    return out


@dataclass
class BuildReport:
    indices_resolved: int = 0
    indices_missing: list[str] | None = None
    stocks_resolved: int = 0
    stocks_missing: list[str] | None = None
    commodities_resolved: int = 0
    commodities_missing: list[str] | None = None
    atm_resolved: int = 0
    atm_missing: list[str] | None = None
    notes: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "indices_resolved": self.indices_resolved,
            "indices_missing": self.indices_missing or [],
            "stocks_resolved": self.stocks_resolved,
            "stocks_missing": self.stocks_missing or [],
            "commodities_resolved": self.commodities_resolved,
            "commodities_missing": self.commodities_missing or [],
            "atm_resolved": self.atm_resolved,
            "atm_missing": self.atm_missing or [],
            "notes": self.notes or [],
        }


class UniverseBuilder:
    """Resolves a ``UniverseSpec`` into a concrete watchlist using the master.

    ATM option resolution needs the *current spot* of each underlying. The
    runtime passes a ``spot_provider`` that, given an underlying name, returns
    its last-known LTP (or None when not available — those get skipped).
    """

    def __init__(self, master: InstrumentMaster) -> None:
        self.master = master

    def build(
        self,
        spec: UniverseSpec,
        *,
        spot_provider: callable | None = None,
        atm_offsets: Iterable[int] | None = None,
        disabled_kinds: set[str] | None = None,
    ) -> tuple[dict[str, list[dict[str, Any]]], BuildReport]:
        """Build the watchlist. ``disabled_kinds`` (uppercase) lets the runtime
        suppress entire categories at user request without losing the spec.

        Recognised kinds: INDEX, EQUITY, COMMODITY, OPTION (ATM CE/PE).
        """
        out: dict[str, list[dict[str, Any]]] = {}
        report = BuildReport(
            indices_missing=[], stocks_missing=[], commodities_missing=[],
            atm_missing=[], notes=[],
        )
        disabled = {k.upper() for k in (disabled_kinds or set())}

        if "INDEX" in disabled:
            report.notes.append("INDEX category disabled by user")
        if "EQUITY" in disabled:
            report.notes.append("EQUITY category disabled by user")
        if "COMMODITY" in disabled:
            report.notes.append("COMMODITY category disabled by user")
        if "OPTION" in disabled:
            report.notes.append("OPTION category disabled by user")

        # ----- indices ----------------------------------------------------
        if "INDEX" in disabled:
            spec_indices: list[str] = []
        else:
            spec_indices = spec.indices
        for name in spec_indices:
            inst = self.master.index(name) or self.master.maybe_resolve("NSE", name)
            if inst is None:
                report.indices_missing.append(name)
                continue
            out.setdefault(inst.exchange, []).append(_entry(inst, "INDEX"))
            report.indices_resolved += 1

        # ----- stocks (NSE cash) -----------------------------------------
        spec_stocks = [] if "EQUITY" in disabled else spec.stocks
        for name in spec_stocks:
            inst = self.master.equity(name, exchange="NSE")
            if inst is None:
                report.stocks_missing.append(name)
                continue
            out.setdefault(inst.exchange, []).append(_entry(inst, "EQUITY"))
            report.stocks_resolved += 1

        # ----- commodities (MCX nearest future) --------------------------
        spec_commodities = [] if "COMMODITY" in disabled else spec.commodities
        for name in spec_commodities:
            inst = self.master.commodity_future(name, exchange="MCX")
            if inst is None:
                report.commodities_missing.append(name)
                continue
            out.setdefault(inst.exchange, []).append(
                _entry(inst, "COMMODITY", expiry=inst.expiry)
            )
            report.commodities_resolved += 1

        # ----- ATM options -----------------------------------------------
        offs = list(atm_offsets) if atm_offsets is not None else spec.atm_offsets
        spec_atm_for = [] if "OPTION" in disabled else spec.atm_for
        for underlying in spec_atm_for:
            spot = spot_provider(underlying) if spot_provider else None
            if spot is None or spot <= 0:
                report.atm_missing.append(f"{underlying}:no_spot")
                continue
            chain = self.master.atm_options(underlying, spot, offsets=offs)
            if not chain.get("rows"):
                report.atm_missing.append(f"{underlying}:no_chain")
                continue
            for row in chain["rows"]:
                for side_key in ("ce", "pe"):
                    inst = row.get(side_key)
                    if inst is None:
                        continue
                    out.setdefault(inst.exchange, []).append(
                        _entry(
                            inst,
                            "OPTION",
                            underlying=underlying,
                            expiry=chain.get("expiry"),
                            strike=row["strike"],
                            side=side_key.upper(),
                            offset=row["offset"],
                        )
                    )
                    report.atm_resolved += 1

        return out, report
