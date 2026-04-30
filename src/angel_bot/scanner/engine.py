from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from angel_bot.config import Settings, get_settings
from angel_bot.smart_client import SmartApiClient

log = structlog.get_logger(__name__)


@dataclass
class ScannerHit:
    name: str
    exchange: str
    token: str
    kind: str
    last_price: float | None
    change_pct: float | None
    momentum_5: float | None
    score: float
    lot_size: int | None
    notional_per_lot: float | None
    affordable_lots: int | None
    as_of: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class _Series:
    prices: deque = field(default_factory=lambda: deque(maxlen=120))
    timestamps: deque = field(default_factory=lambda: deque(maxlen=120))


class ScannerEngine:
    """
    Lightweight scanner: polls LTP for the configured watchlist, keeps short
    rolling history per token, ranks instruments by recent momentum + magnitude,
    and tells you how many lots fit your available funds.

    Heuristic only — not a guarantee of profitability.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._series: dict[str, _Series] = defaultdict(_Series)
        self._last_hits: list[ScannerHit] = []

    @property
    def last_hits(self) -> list[ScannerHit]:
        return list(self._last_hits)

    def watchlist_meta_lookup(self) -> dict[tuple[str, str], dict[str, Any]]:
        wl = self.settings.scanner_watchlist()
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for ex, items in wl.items():
            for it in items:
                tok = str(it.get("token", "")).strip()
                if not tok:
                    continue
                out[(ex.upper(), tok)] = it
        return out

    async def poll_once(self, api: SmartApiClient, available_funds: float | None) -> list[ScannerHit]:
        wl = self.settings.scanner_watchlist()
        if not wl:
            return []
        exchange_tokens: dict[str, list[str]] = {ex: [str(it["token"]) for it in items if it.get("token")] for ex, items in wl.items()}
        try:
            resp = await api.get_ltp(exchange_tokens)
        except Exception as e:
            log.warning("scanner_ltp_error", error=str(e))
            return []
        if not resp.get("status"):
            log.warning("scanner_ltp_failed", body=resp)
            return []
        rows = resp.get("data") or []
        if isinstance(rows, dict):
            # Some endpoints wrap data in {"fetched": [...]} — normalize.
            rows = rows.get("fetched") or rows.get("rows") or []
        meta = self.watchlist_meta_lookup()

        now_iso = datetime.now(UTC).isoformat()
        hits: list[ScannerHit] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            ex = str(row.get("exchange") or "").upper()
            tok = str(row.get("symboltoken") or row.get("symbolToken") or row.get("token") or "").strip()
            if not (ex and tok):
                continue
            last = _to_float(row.get("ltp") or row.get("last_traded_price"))
            close = _to_float(row.get("close") or row.get("previousClose"))
            ts_key = (ex, tok)
            series = self._series[f"{ex}:{tok}"]
            if last is not None:
                series.prices.append(last)
                series.timestamps.append(now_iso)
            change_pct = None
            if last is not None and close not in (None, 0):
                change_pct = (last - close) / abs(close)
            mom = None
            if len(series.prices) >= 6 and series.prices[-6] not in (0, None):
                mom = (series.prices[-1] - series.prices[-6]) / abs(series.prices[-6])
            score = abs(change_pct or 0) * 0.6 + abs(mom or 0) * 0.4

            m = meta.get(ts_key, {})
            lot_size = int(m.get("lot_size") or 0) or None
            notional = (last * lot_size) if (last is not None and lot_size) else None
            affordable = None
            if available_funds is not None and notional and notional > 0:
                affordable = max(0, int(available_funds // notional))

            hits.append(
                ScannerHit(
                    name=str(m.get("name") or row.get("tradingsymbol") or tok),
                    exchange=ex,
                    token=tok,
                    kind=str(m.get("kind") or "EQUITY"),
                    last_price=last,
                    change_pct=change_pct,
                    momentum_5=mom,
                    score=score,
                    lot_size=lot_size,
                    notional_per_lot=notional,
                    affordable_lots=affordable,
                    as_of=now_iso,
                )
            )
        hits.sort(key=lambda h: (h.score, abs(h.change_pct or 0)), reverse=True)
        self._last_hits = hits
        return hits


def _to_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(str(x))
        return v
    except (TypeError, ValueError):
        return None
