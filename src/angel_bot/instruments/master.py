from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from angel_bot.config import Settings, get_settings


@dataclass(frozen=True)
class Instrument:
    exchange: str
    tradingsymbol: str
    symboltoken: str


class InstrumentMaster:
    """Lookup tradingsymbol + exchange → token. Reject ambiguous or unknown."""

    def __init__(self, rows: Iterable[Instrument]):
        self._by_key: dict[tuple[str, str], Instrument] = {}
        for r in rows:
            k = (r.exchange.strip().upper(), r.tradingsymbol.strip().upper())
            if k in self._by_key:
                raise ValueError(f"Ambiguous instrument key: {k}")
            self._by_key[k] = r

    @classmethod
    def from_angel_csv(cls, path: str | Path) -> InstrumentMaster:
        p = Path(path)
        with p.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            rows: list[Instrument] = []
            for row in reader:
                ex = (row.get("exch_seg") or row.get("exchange") or "").strip()
                sym = (row.get("symbol") or row.get("tradingsymbol") or "").strip()
                tok = str(row.get("token") or row.get("symboltoken") or "").strip()
                if not (ex and sym and tok):
                    continue
                rows.append(Instrument(exchange=ex, tradingsymbol=sym, symboltoken=tok))
        return cls(rows)

    def resolve(self, exchange: str, tradingsymbol: str) -> Instrument:
        k = (exchange.strip().upper(), tradingsymbol.strip().upper())
        if k not in self._by_key:
            raise KeyError(f"Unknown instrument: {exchange} {tradingsymbol}")
        return self._by_key[k]


def load_master_from_settings(settings: Settings | None = None) -> InstrumentMaster:
    settings = settings or get_settings()
    if not settings.instrument_master_csv:
        raise ValueError("Set INSTRUMENT_MASTER_CSV to Angel instrument master file path.")
    return InstrumentMaster.from_angel_csv(settings.instrument_master_csv)
