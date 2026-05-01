"""Angel One instrument master.

Angel publishes a JSON file with every tradeable instrument:
https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPI_ScripMaster.json

Each row looks like:
    {
      "token": "99926000",
      "symbol": "Nifty 50",
      "name": "NIFTY",
      "expiry": "",
      "strike": "-1.000000",
      "lotsize": "1",
      "instrumenttype": "AMXIDX",
      "exch_seg": "NSE",
      "tick_size": "5"
    }

This module:
  * Loads the master from JSON or the older CSV column format.
  * Indexes every row five different ways so the bot can resolve quickly:
      (exchange, tradingsymbol)        — exact unique key
      (exchange, symboltoken)
      name                              — group by underlying ("NIFTY")
      (instrument_type, exchange)
      (name, expiry)                    — option/futures chain
  * Exposes ``option_chain`` and ``atm_options`` so a CALL/PUT signal on the
    NIFTY index can be turned into the *exact* `NIFTY25APR24500CE` token to
    place the order. This is what the user's plan calls "dynamic resolution".
  * Returns the lot size from the master (so we never hard-code 50/15/40 again).

Backwards compat: the lightweight ``Instrument`` dataclass that other modules
construct directly (`Instrument(exchange=..., tradingsymbol=..., symboltoken=...)`)
still works — we just added optional fields with safe defaults.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

import structlog

from angel_bot.config import Settings, get_settings

log = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# Instrument record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Instrument:
    """One tradeable row from the Angel scrip master.

    Only ``exchange / tradingsymbol / symboltoken`` are required so existing
    callers that build a fake ``Instrument`` for an order payload keep working.
    """

    exchange: str
    tradingsymbol: str
    symboltoken: str
    # rich fields below — populated when loaded from the official master
    name: str = ""                 # underlying (e.g. "NIFTY" for NIFTY24500CE)
    instrument_type: str = ""      # OPTIDX / OPTSTK / FUTIDX / FUTSTK / EQ / INDEX / AMXIDX / COMDTY ...
    expiry: str = ""               # ISO date YYYY-MM-DD ("" for non-derivatives)
    strike: float = 0.0            # rupees (Angel JSON stores strike × 100; we normalize)
    lot_size: int = 1
    tick_size: float = 0.05

    # Convenient shorthands -------------------------------------------------
    @property
    def is_option(self) -> bool:
        return self.instrument_type.upper() in {"OPTIDX", "OPTSTK", "OPTCUR", "OPTCOM"}

    @property
    def is_future(self) -> bool:
        return self.instrument_type.upper() in {"FUTIDX", "FUTSTK", "FUTCOM", "FUTCUR"}

    @property
    def is_equity(self) -> bool:
        ex = self.exchange.upper()
        it = self.instrument_type.upper()
        # Angel sometimes leaves instrumenttype blank for cash-segment equities.
        if it == "EQ":
            return True
        if not it and ex in {"NSE", "BSE"}:
            return True
        return False

    @property
    def is_index(self) -> bool:
        # Angel uses "AMXIDX" for index-like rows in NSE/BSE cash segment.
        return self.instrument_type.upper() in {"AMXIDX", "INDEX"}

    @property
    def option_side(self) -> str:
        """'CE' / 'PE' / '' depending on the trading symbol."""
        s = self.tradingsymbol.upper()
        if s.endswith("CE"):
            return "CE"
        if s.endswith("PE"):
            return "PE"
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "tradingsymbol": self.tradingsymbol,
            "symboltoken": self.symboltoken,
            "name": self.name,
            "instrument_type": self.instrument_type,
            "expiry": self.expiry,
            "strike": self.strike,
            "lot_size": self.lot_size,
            "tick_size": self.tick_size,
        }


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

_EXPIRY_FORMATS = (
    "%d%b%Y",     # 25APR2026
    "%d%b%y",     # 25APR26
    "%d-%b-%Y",   # 25-APR-2026
    "%Y-%m-%d",   # 2026-04-25 (already ISO)
)


def _parse_expiry(raw: Any) -> str:
    if not raw:
        return ""
    s = str(raw).strip().upper()
    if not s or s in {"-1", "0"}:
        return ""
    for fmt in _EXPIRY_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s  # leave as-is rather than dropping data


def _to_float(x: Any) -> float:
    try:
        return float(str(x))
    except (TypeError, ValueError):
        return 0.0


def _to_int(x: Any) -> int:
    try:
        return int(float(str(x)))
    except (TypeError, ValueError):
        return 0


def _from_angel_row(row: dict[str, Any]) -> Instrument | None:
    ex = str(row.get("exch_seg") or row.get("exchange") or "").strip().upper()
    sym = str(row.get("symbol") or row.get("tradingsymbol") or "").strip().upper()
    tok = str(row.get("token") or row.get("symboltoken") or "").strip()
    if not (ex and sym and tok):
        return None
    name = str(row.get("name") or "").strip().upper()
    itype = str(row.get("instrumenttype") or row.get("instrument_type") or "").strip().upper()
    raw_strike = _to_float(row.get("strike"))
    # Angel JSON encodes strike × 100. Don't divide CSV rows that already use rupees.
    if raw_strike > 0 and ("scrip" in str(row).lower() or raw_strike >= 1000):
        # Heuristic: JSON values are large (e.g. 2400000.000000 = ₹24000).
        # If raw_strike < 1000 we treat as already in rupees (CSV variant).
        strike = raw_strike / 100.0 if raw_strike >= 1000 else raw_strike
    else:
        strike = raw_strike
    lotsize = _to_int(row.get("lotsize") or row.get("lot_size") or 1) or 1
    tick = _to_float(row.get("tick_size") or row.get("ticksize") or 5) or 5.0
    return Instrument(
        exchange=ex,
        tradingsymbol=sym,
        symboltoken=tok,
        name=name,
        instrument_type=itype,
        expiry=_parse_expiry(row.get("expiry")),
        strike=strike,
        lot_size=lotsize,
        tick_size=tick,
    )


# --------------------------------------------------------------------------- #
# Master container
# --------------------------------------------------------------------------- #


@dataclass
class MasterStats:
    total: int = 0
    by_exchange: dict[str, int] = field(default_factory=dict)
    by_instrument_type: dict[str, int] = field(default_factory=dict)


class InstrumentMaster:
    """Indexed catalogue of every tradeable Angel One instrument.

    Reading APIs:
      * ``resolve(exchange, tradingsymbol)`` — exact lookup, raises KeyError.
      * ``resolve_by_token(exchange, symboltoken)`` — exact lookup.
      * ``equity(name, exchange='NSE')`` — find a cash equity row by underlying.
      * ``index(name, exchange='NSE')`` — find the index row (AMXIDX) by name.
      * ``option_chain(underlying, expiry=None, exchange='NFO')`` — list option rows.
      * ``nearest_expiry(underlying, on=None)`` — earliest non-past option expiry.
      * ``atm_options(underlying, spot, ...)`` — best CE+PE for the given spot.
      * ``search(query, *, exchange=None, kind=None, limit=20)`` — autocomplete.
    """

    def __init__(self, rows: Iterable[Instrument]) -> None:
        self._rows: list[Instrument] = list(rows)
        # Multi-index: by (exchange, tradingsymbol) — should be unique per exchange
        self._by_symbol: dict[tuple[str, str], Instrument] = {}
        self._by_token: dict[tuple[str, str], Instrument] = {}
        self._dupes: int = 0
        # Group by underlying name (used for option chain / atm lookup)
        self._by_name: dict[str, list[Instrument]] = {}
        # Group by instrument type (used for "all NSE equities")
        self._by_type: dict[tuple[str, str], list[Instrument]] = {}

        for r in self._rows:
            k_sym = (r.exchange, r.tradingsymbol)
            if k_sym in self._by_symbol:
                # Angel master occasionally has duplicate (exchange, symbol) for the
                # same instrument across days. Keep the first; warn count.
                self._dupes += 1
            else:
                self._by_symbol[k_sym] = r
            self._by_token[(r.exchange, r.symboltoken)] = r
            if r.name:
                self._by_name.setdefault(r.name, []).append(r)
            self._by_type.setdefault((r.instrument_type, r.exchange), []).append(r)

        if self._dupes:
            log.info("instrument_master_duplicates", count=self._dupes)

    # -------------------------------------------------------------- loaders

    @classmethod
    def empty(cls) -> InstrumentMaster:
        return cls([])

    @classmethod
    def from_angel_json(cls, path: str | Path) -> InstrumentMaster:
        p = Path(path)
        with p.open("r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"Expected a JSON array of rows, got {type(data).__name__}")
        rows: list[Instrument] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            inst = _from_angel_row(row)
            if inst is not None:
                rows.append(inst)
        return cls(rows)

    @classmethod
    def from_angel_csv(cls, path: str | Path) -> InstrumentMaster:
        p = Path(path)
        with p.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            rows: list[Instrument] = []
            for row in reader:
                inst = _from_angel_row(row)
                if inst is not None:
                    rows.append(inst)
        return cls(rows)

    @classmethod
    def from_path(cls, path: str | Path) -> InstrumentMaster:
        """Sniff JSON vs CSV by extension."""
        p = Path(path)
        suffix = p.suffix.lower()
        if suffix in (".json", ".js"):
            return cls.from_angel_json(p)
        return cls.from_angel_csv(p)

    # -------------------------------------------------------------- introspection

    def __len__(self) -> int:
        return len(self._rows)

    def __iter__(self) -> Iterator[Instrument]:
        return iter(self._rows)

    def stats(self) -> MasterStats:
        s = MasterStats(total=len(self._rows))
        for r in self._rows:
            s.by_exchange[r.exchange] = s.by_exchange.get(r.exchange, 0) + 1
            s.by_instrument_type[r.instrument_type] = s.by_instrument_type.get(r.instrument_type, 0) + 1
        return s

    # -------------------------------------------------------------- exact lookups

    def resolve(self, exchange: str, tradingsymbol: str) -> Instrument:
        key = (exchange.strip().upper(), tradingsymbol.strip().upper())
        if key not in self._by_symbol:
            raise KeyError(f"Unknown instrument: {exchange} {tradingsymbol}")
        return self._by_symbol[key]

    def maybe_resolve(self, exchange: str, tradingsymbol: str) -> Instrument | None:
        try:
            return self.resolve(exchange, tradingsymbol)
        except KeyError:
            return None

    def resolve_by_token(self, exchange: str, symboltoken: str) -> Instrument | None:
        return self._by_token.get((exchange.strip().upper(), str(symboltoken).strip()))

    # -------------------------------------------------------------- group lookups

    def equity(self, name: str, exchange: str = "NSE") -> Instrument | None:
        ex = exchange.strip().upper()
        n = name.strip().upper()
        # Prefer rows that look like a clean equity (e.g., RELIANCE-EQ → RELIANCE).
        candidates = self._by_name.get(n, [])
        for c in candidates:
            if c.exchange == ex and c.is_equity:
                return c
        # Fallback: tradingsymbol match RELIANCE-EQ / RELIANCE
        return self.maybe_resolve(ex, n) or self.maybe_resolve(ex, f"{n}-EQ")

    # Underlyings whose options are listed on BSE F&O (BFO) instead of NSE F&O.
    # Everything else defaults to NFO; MCX bullion-index uses MCX.
    _BFO_UNDERLYINGS: frozenset[str] = frozenset({"SENSEX", "SENSEX50", "BANKEX"})
    _MCX_INDEX_UNDERLYINGS: frozenset[str] = frozenset({"MCXBULLDEX"})

    def index(self, name: str, exchange: str | None = None) -> Instrument | None:
        """Find an index row.

        ``exchange`` defaults to "any": searches NSE first then BSE. This way
        the universe spec can list NIFTY and SENSEX side-by-side without
        having to encode where each one lives.
        """
        n = name.strip().upper()
        if exchange:
            exes = [exchange.strip().upper()]
        else:
            exes = ["NSE", "BSE"]
        for ex in exes:
            for c in self._by_name.get(n, []):
                if c.exchange == ex and c.is_index:
                    return c
            # Some index rows have empty `name` (e.g., NIFTY 50 vs NIFTY).
            inst = self.maybe_resolve(ex, n)
            if inst is not None:
                return inst
        return None

    def options_exchange_for(self, underlying: str) -> str:
        """Return the F&O segment where this underlying's options are listed."""
        n = underlying.strip().upper()
        if n in self._BFO_UNDERLYINGS:
            return "BFO"
        if n in self._MCX_INDEX_UNDERLYINGS:
            return "MCX"
        return "NFO"

    def commodity_future(self, name: str, exchange: str = "MCX") -> Instrument | None:
        """Pick the *nearest* MCX future for a given commodity (CRUDEOIL, GOLD…)."""
        n = name.strip().upper()
        ex = exchange.strip().upper()
        rows = [
            r
            for r in self._by_name.get(n, [])
            if r.exchange == ex and r.is_future
        ]
        if not rows:
            return None
        rows.sort(key=lambda r: r.expiry or "9999-12-31")
        return rows[0]

    def option_chain(
        self,
        underlying: str,
        *,
        expiry: str | None = None,
        exchange: str = "auto",
    ) -> list[Instrument]:
        """List option rows for an underlying. ``exchange='auto'`` (default)
        picks NFO / BFO / MCX based on where the underlying's options live."""
        n = underlying.strip().upper()
        ex = (
            self.options_exchange_for(n)
            if exchange.strip().upper() == "AUTO"
            else exchange.strip().upper()
        )
        rows = [
            r
            for r in self._by_name.get(n, [])
            if r.exchange == ex and r.is_option
        ]
        if expiry:
            target = _parse_expiry(expiry) or expiry.upper()
            rows = [r for r in rows if r.expiry == target]
        rows.sort(key=lambda r: (r.expiry or "9999-12-31", r.strike, r.option_side))
        return rows

    def list_expiries(self, underlying: str, *, exchange: str = "auto") -> list[str]:
        chain = self.option_chain(underlying, exchange=exchange)
        seen: list[str] = []
        for r in chain:
            if r.expiry and r.expiry not in seen:
                seen.append(r.expiry)
        return seen

    def upcoming_expiries(
        self,
        underlying: str,
        *,
        exchange: str = "auto",
        n: int = 1,
        on: date | None = None,
    ) -> list[str]:
        """Return the next ``n`` non-past expiries (closest first)."""
        today = on or datetime.now(UTC).date()
        out: list[str] = []
        for exp in self.list_expiries(underlying, exchange=exchange):
            try:
                d = date.fromisoformat(exp)
            except ValueError:
                continue
            if d >= today:
                out.append(exp)
                if len(out) >= max(1, n):
                    return out
        if out:
            return out
        # Stale master fallback: return last known expiries.
        all_exp = self.list_expiries(underlying, exchange=exchange)
        return all_exp[-max(1, n):]

    def nearest_expiry(
        self,
        underlying: str,
        *,
        exchange: str = "auto",
        on: date | None = None,
    ) -> str | None:
        ups = self.upcoming_expiries(underlying, exchange=exchange, n=1, on=on)
        return ups[0] if ups else None

    def atm_options(
        self,
        underlying: str,
        spot: float,
        *,
        expiry: str | None = None,
        exchange: str = "auto",
        offsets: Iterable[int] = (0,),
    ) -> dict[str, Any]:
        """Resolve ATM (and optional offset) CE/PE for an underlying at given spot.

        Returns a dict like::
            {
              "underlying": "NIFTY",
              "spot": 24050,
              "expiry": "2026-04-30",
              "atm_strike": 24050,
              "rows": [
                {"offset": 0, "strike": 24050,
                 "ce": <Instrument>|None, "pe": <Instrument>|None}, ...
              ]
            }
        """
        if not expiry:
            expiry = self.nearest_expiry(underlying, exchange=exchange) or ""
        chain = self.option_chain(underlying, expiry=expiry, exchange=exchange)
        if not chain:
            return {"underlying": underlying, "spot": spot, "expiry": expiry, "rows": []}
        # Distinct strikes available for this expiry.
        strikes = sorted({r.strike for r in chain if r.strike > 0})
        if not strikes:
            return {"underlying": underlying, "spot": spot, "expiry": expiry, "rows": []}
        atm = min(strikes, key=lambda s: abs(s - spot))
        atm_idx = strikes.index(atm)
        # Index CE/PE per strike for fast pickup.
        by_strike: dict[float, dict[str, Instrument]] = {}
        for r in chain:
            slot = by_strike.setdefault(r.strike, {})
            if r.option_side in ("CE", "PE"):
                slot[r.option_side] = r
        rows: list[dict[str, Any]] = []
        for off in offsets:
            i = atm_idx + off
            if i < 0 or i >= len(strikes):
                continue
            k = strikes[i]
            slot = by_strike.get(k, {})
            rows.append(
                {
                    "offset": off,
                    "strike": k,
                    "ce": slot.get("CE"),
                    "pe": slot.get("PE"),
                }
            )
        return {
            "underlying": underlying.upper(),
            "spot": spot,
            "expiry": expiry,
            "atm_strike": atm,
            "rows": rows,
        }

    # -------------------------------------------------------------- search

    def search(
        self,
        query: str,
        *,
        exchange: str | None = None,
        kind: str | None = None,
        limit: int = 25,
    ) -> list[Instrument]:
        """Cheap substring search on tradingsymbol + name. Used by the dashboard."""
        q = (query or "").strip().upper()
        if not q:
            return []
        rgx = re.compile(re.escape(q))
        ex = exchange.strip().upper() if exchange else None
        kind_norm = kind.strip().upper() if kind else None
        out: list[Instrument] = []
        for r in self._rows:
            if ex and r.exchange != ex:
                continue
            if kind_norm and r.instrument_type != kind_norm:
                continue
            if rgx.search(r.tradingsymbol) or (r.name and rgx.search(r.name)):
                out.append(r)
                if len(out) >= limit:
                    break
        return out


# --------------------------------------------------------------------------- #
# Settings glue
# --------------------------------------------------------------------------- #


def load_master_from_settings(settings: Settings | None = None) -> InstrumentMaster:
    settings = settings or get_settings()
    path = settings.instrument_master_path or settings.instrument_master_csv
    if not path:
        raise ValueError(
            "Set INSTRUMENT_MASTER_PATH (or INSTRUMENT_MASTER_CSV) to the Angel scrip master file."
        )
    return InstrumentMaster.from_path(path)
