"""Scanner — one CandleAggregator per watchlist symbol, fed by REST LTP polls.

Per cycle:
  1. fetch LTP for every symbol in the watchlist
  2. push the LTP into the symbol's CandleAggregator (1m / 5m / 15m + session high/low + TWAP)
  3. ask BrainEngine to score and signal each symbol
  4. emit ScannerHit rows sorted by score so the runtime can pick the best
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from angel_bot.config import Settings, get_settings
from angel_bot.market_data.candles import CandleAggregator
from angel_bot.smart_client import SmartApiClient
from angel_bot.strategy.brain import BrainConfig, BrainEngine, BrainOutput

log = structlog.get_logger(__name__)


@dataclass
class ScannerHit:
    name: str
    exchange: str
    token: str
    kind: str

    # raw inputs
    last_price: float | None
    prev_close: float | None
    change_pct: float | None         # vs previous close (legacy display field)

    # capacity
    lot_size: int | None
    notional_per_lot: float | None
    affordable_lots: int | None

    # brain output
    score: float                     # 0..1, ranking
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    signal_side: str = "NO_TRADE"
    signal_reason: str = "warmup"
    signal_confidence: float = 0.0
    checks: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    # candle warmup transparency
    candles_1m: int = 0
    candles_5m: int = 0
    candles_15m: int = 0

    as_of: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ScannerEngine:
    """Stateful: keeps one CandleAggregator per (exchange,token)."""

    def __init__(self, settings: Settings | None = None, brain: BrainEngine | None = None):
        self.settings = settings or get_settings()
        self._aggs: dict[str, CandleAggregator] = defaultdict(CandleAggregator)
        self._last_hits: list[ScannerHit] = []
        # Poll-count history (used purely for legacy momentum_5 fallback if
        # caller still wants change_pct etc; brain itself uses the candles).
        self._series_count: dict[str, int] = defaultdict(int)
        self.brain = brain or BrainEngine(self._brain_config_from_settings())

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

    async def poll_once(
        self, api: SmartApiClient, available_funds: float | None
    ) -> list[ScannerHit]:
        wl = self.settings.scanner_watchlist()
        if not wl:
            return []
        exchange_tokens: dict[str, list[str]] = {
            ex: [str(it["token"]) for it in items if it.get("token")]
            for ex, items in wl.items()
        }
        try:
            resp = await api.get_ltp(exchange_tokens)
        except Exception as e:  # noqa: BLE001
            log.warning("scanner_ltp_error", error=str(e))
            return []
        if not resp.get("status"):
            log.warning("scanner_ltp_failed", body=resp)
            return []
        rows = resp.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("fetched") or rows.get("rows") or []
        meta = self.watchlist_meta_lookup()

        now = datetime.now(UTC)
        now_iso = now.isoformat()
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
            key = f"{ex}:{tok}"
            agg = self._aggs[key]
            if last is not None:
                agg.push_ltp(last, ts=now)
            self._series_count[key] += 1

            change_pct = None
            if last is not None and close not in (None, 0):
                change_pct = (last - close) / abs(close)

            m = meta.get((ex, tok), {})
            lot_size = int(m.get("lot_size") or 0) or None
            notional = (last * lot_size) if (last is not None and lot_size) else None
            affordable = None
            if available_funds is not None and notional and notional > 0:
                affordable = max(0, int(available_funds // notional))

            brain_out: BrainOutput = self.brain.evaluate(last_price=last, agg=agg)
            c1, c5, c15 = agg.all_candles_including_partial()

            hits.append(
                ScannerHit(
                    name=str(m.get("name") or row.get("tradingsymbol") or tok),
                    exchange=ex,
                    token=tok,
                    kind=str(m.get("kind") or "EQUITY").upper(),
                    last_price=last,
                    prev_close=close,
                    change_pct=change_pct,
                    lot_size=lot_size,
                    notional_per_lot=notional,
                    affordable_lots=affordable,
                    score=brain_out.score.total,
                    score_breakdown=brain_out.score.to_dict(),
                    signal_side=brain_out.signal.side,
                    signal_reason=brain_out.signal.reason,
                    signal_confidence=brain_out.signal.confidence,
                    checks=[c.to_dict() for c in brain_out.signal.checks],
                    diagnostics=brain_out.diagnostics,
                    candles_1m=len(c1),
                    candles_5m=len(c5),
                    candles_15m=len(c15),
                    as_of=now_iso,
                )
            )
        # Rank by score; tie-break by absolute change% so display is stable.
        hits.sort(
            key=lambda h: (h.score, abs(h.change_pct or 0)),
            reverse=True,
        )
        self._last_hits = hits
        return hits

    # ------------------------------------------------------------------
    def _brain_config_from_settings(self) -> BrainConfig:
        s = self.settings
        return BrainConfig(
            min_volatility_pct=s.strategy_min_volatility_pct,
            max_chop_score=s.strategy_max_chop_score,
            min_15m_trend_slope=s.strategy_min_15m_trend_slope,
            min_5m_breakout_clearance=s.strategy_min_breakout_clearance,
            max_late_entry_pct=s.strategy_max_late_entry_pct,
            min_above_twap_pct=s.strategy_min_above_twap_pct,
            w_volatility=s.score_w_volatility,
            w_momentum=s.score_w_momentum,
            w_breakout=s.score_w_breakout,
            w_volume=s.score_w_volume,
            min_score_to_act=s.strategy_min_score,
        )


def _to_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(str(x))
    except (TypeError, ValueError):
        return None
