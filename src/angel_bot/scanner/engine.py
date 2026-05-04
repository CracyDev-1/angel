"""Scanner — one CandleAggregator per watchlist symbol, fed by REST LTP polls.

Per cycle:
  1. fetch LTP for every symbol in the watchlist
  2. push the LTP into the symbol's CandleAggregator (1m / 5m / 15m + session high/low + TWAP)
  3. ask BrainEngine to score and signal each symbol
  4. emit ScannerHit rows sorted by score so the runtime can pick the best
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import structlog

from angel_bot.config import Settings, get_settings
from angel_bot.market_data.candles import Candle, CandleAggregator
from angel_bot.smart_client import SmartApiClient
from angel_bot.strategy.brain import BrainConfig, BrainEngine, BrainOutput

# Asia/Kolkata is UTC+5:30 with no DST. Angel's historical-candle API expects
# both ``fromdate`` / ``todate`` and the returned timestamps in IST so we
# format and parse against this fixed offset.
IST = timezone(timedelta(hours=5, minutes=30))

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
    notional_per_lot: float | None       # cost of ONE lot at current LTP
    affordable_lots: int | None
    capital_short_for_one_lot: float | None = None  # how much MORE cash you need to buy 1 lot
    in_trade_value_range: bool = True              # honors STRATEGY_MIN/MAX_TRADE_VALUE
    capital_range_reason: str | None = None        # human reason if outside range
    # Underlying key from the universe spec (e.g. "NIFTY" for the index whose
    # broker tradingsymbol is "NIFTY 50"). Used by the runtime's spot lookup
    # so ATM resolution matches the spec name even when the display symbol
    # differs. Empty for instruments whose universe entry didn't set it.
    underlying: str = ""
    # Option-only metadata propagated from the universe entry. ``expiry`` is
    # ISO date (YYYY-MM-DD), ``option_side`` is "CE"/"PE", ``offset`` is the
    # signed strike offset from ATM (-1 / 0 / +1). All zero/empty for
    # non-option rows. Used by the dashboard to render a per-underlying
    # ATM CE/PE breakdown without an extra master lookup.
    expiry: str = ""
    strike: float = 0.0
    option_side: str = ""
    offset: int = 0
    tradingsymbol: str = ""
    # True for INDEX rows always, and for other kinds when the 1-lot notional
    # is within available cash. False marks rows that should be hidden from
    # the dashboard / brain candidate selector but still kept in the cache so
    # the runtime can resolve premium when a higher-level signal points at
    # them (and emit a clear "need_more_capital" skip reason instead of
    # silently failing with "no_execution_price").
    is_affordable: bool = True

    # brain output
    score: float = 0.0               # 0..1, ranking
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


@dataclass(frozen=True)
class WarmupResult:
    """Outcome of :meth:`ScannerEngine.warmup_from_history`."""

    seeded: int
    # ``"EXCHANGE:TOKEN"`` keys that actually received a non-empty
    # ``seed_history`` — used by the runtime to update ``_warmed_keys``
    # *only* on success. Marking failed tokens as "warmed" prevented any
    # retry after Angel returned 403 / empty data, leaving empty candles
    # and locking the brain in ``warmup`` (no live trades).
    ok_keys: frozenset[str]


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
        # Dynamic watchlist override. When set (by the runtime via the
        # universe builder), it takes precedence over SCANNER_WATCHLIST_JSON.
        self._dynamic_watchlist: dict[str, list[dict[str, Any]]] | None = None
        # How many OPTION rows we dropped from the most recent poll because
        # their 1-lot notional exceeded available cash. Surfaced by the
        # runtime in the scan summary so the dashboard explains the gap
        # between watchlist size and visible hits.
        self.last_hidden_unaffordable: int = 0

    def set_watchlist(self, watchlist: dict[str, list[dict[str, Any]]] | None) -> None:
        """Replace the active watchlist. Pass None to revert to the .env value."""
        self._dynamic_watchlist = watchlist or None

    async def warmup_from_history(
        self,
        api: SmartApiClient,
        *,
        only_keys: set[str] | None = None,
        lookback_5m_minutes: int = 90,
        lookback_15m_minutes: int = 6 * 60,
        lookback_1m_minutes: int = 0,
        max_concurrent: int = 1,
    ) -> WarmupResult:
        """Backfill each watchlist symbol's candle aggregator from broker history.

        Without this, every process restart leaves the brain stuck in
        ``warmup`` for ~25 minutes while the in-memory aggregator rebuilds
        from live LTP polls. We call Angel's ``getCandleData`` for each
        ``(exchange, token)`` we plan to watch and seed the 5m / 15m
        deques with the closed bars the broker already has — so the brain
        can grade signals very soon after start.

        Defaults are deliberately small to minimise total warmup time
        without losing signal quality:
          * 5m: 90-minute lookback → ~18 bars (brain needs ≥5)
          * 15m: 6-hour lookback → ~24 bars (brain needs ≥2; longer lookback
            is needed because Friday closes count for Monday morning).
          * 1m: skipped by default (``lookback_1m_minutes=0``) — the brain
            only needs ``len(c1) >= 1`` and the live-tick scanner provides
            that within seconds. Saves N getCandleData calls per restart.
          * concurrency 1 — Angel's gateway is stricter than the published
            3/sec on getCandleData; serialising avoids 403 thrash.

        ``only_keys`` (optional) limits the warmup to specific
        ``"EXCHANGE:TOKEN"`` keys, used when the universe rebuild adds new
        ATM strikes mid-session and we only need to backfill those.

        Returns the number of aggregators successfully seeded.
        """
        wl = self.active_watchlist()
        if not wl:
            return WarmupResult(0, frozenset())

        targets: list[tuple[str, str]] = []
        for ex, items in wl.items():
            for it in items:
                tok = str(it.get("token", "")).strip()
                if not tok:
                    continue
                key = f"{ex.upper()}:{tok}"
                if only_keys is not None and key not in only_keys:
                    continue
                targets.append((ex.upper(), tok))
        if not targets:
            return WarmupResult(0, frozenset())

        now_ist = datetime.now(IST)
        from_5m = now_ist - timedelta(minutes=lookback_5m_minutes)
        from_15m = now_ist - timedelta(minutes=lookback_15m_minutes)
        from_1m = (
            now_ist - timedelta(minutes=lookback_1m_minutes)
            if lookback_1m_minutes > 0
            else None
        )
        to_str = now_ist.strftime("%Y-%m-%d %H:%M")

        sem = asyncio.Semaphore(max(1, int(max_concurrent)))

        async def fetch_one(ex: str, tok: str, interval_min: int, frm: datetime) -> list[Candle]:
            async with sem:
                try:
                    resp = await api.get_candle_data(
                        exchange=ex,
                        symboltoken=tok,
                        interval_minutes=interval_min,
                        fromdate=frm.strftime("%Y-%m-%d %H:%M"),
                        todate=to_str,
                    )
                except Exception as e:  # noqa: BLE001 — never crash startup on warmup
                    log.warning(
                        "scanner_warmup_history_error",
                        exchange=ex, symboltoken=tok, interval_min=interval_min,
                        error=str(e),
                    )
                    return []
            if not isinstance(resp, dict) or not resp.get("status"):
                return []
            data = resp.get("data") or []
            if not isinstance(data, list):
                return []
            return _parse_candle_rows(data)

        async def seed_one(ex: str, tok: str) -> bool:
            # Sequential interval fetches — ``asyncio.gather`` here used to
            # issue two ``getCandleData`` calls back-to-back for the same
            # symbol and contributed to gateway 403 bursts alongside LTP /
            # position traffic.
            c5 = await fetch_one(ex, tok, 5, from_5m)
            c15 = await fetch_one(ex, tok, 15, from_15m)
            c1: list[Candle] = []
            if from_1m is not None:
                c1 = await fetch_one(ex, tok, 1, from_1m)
            # Seed even partial data — 5m alone is often enough to clear
            # the warmup gate. We require at least one timeframe to have
            # rows so we don't blank an aggregator that already has live
            # ticks (e.g. when only 1m was empty for an illiquid strike).
            if not (c1 or c5 or c15):
                return False
            self._aggs[f"{ex}:{tok}"].seed_history(
                candles_1m=c1 or None,
                candles_5m=c5 or None,
                candles_15m=c15 or None,
            )
            return True

        seeded = 0
        ok_keys: set[str] = set()
        for ex, tok in targets:
            if await seed_one(ex, tok):
                seeded += 1
                ok_keys.add(f"{ex.upper()}:{tok}")
        log.info(
            "scanner_warmup_history_done",
            requested=len(targets),
            seeded=seeded,
            lookback_5m_min=lookback_5m_minutes,
            lookback_15m_min=lookback_15m_minutes,
            lookback_1m_min=lookback_1m_minutes,
        )
        return WarmupResult(seeded, frozenset(ok_keys))

    def active_watchlist(self) -> dict[str, list[dict[str, Any]]]:
        if self._dynamic_watchlist is not None:
            return self._dynamic_watchlist
        return self.settings.scanner_watchlist()

    @property
    def last_hits(self) -> list[ScannerHit]:
        return list(self._last_hits)

    def watchlist_meta_lookup(self) -> dict[tuple[str, str], dict[str, Any]]:
        wl = self.active_watchlist()
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for ex, items in wl.items():
            for it in items:
                tok = str(it.get("token", "")).strip()
                if not tok:
                    continue
                out[(ex.upper(), tok)] = it
        return out

    def latest_prices(self) -> dict[tuple[str, str], float]:
        """Snapshot of the most recent LTP for every symbol the scanner has seen.

        Used by the paper trader to mark-to-market between cycles.
        """
        out: dict[tuple[str, str], float] = {}
        for h in self._last_hits:
            if h.last_price is None:
                continue
            out[(h.exchange.upper(), str(h.token))] = float(h.last_price)
        return out

    async def poll_once(
        self, api: SmartApiClient, available_funds: float | None
    ) -> list[ScannerHit]:
        wl = self.active_watchlist()
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
        hidden_unaffordable = 0
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
            shortfall: float | None = None
            if available_funds is not None and notional and notional > 0:
                affordable = max(0, int(available_funds // notional))
                if affordable < 1:
                    shortfall = max(0.0, notional - float(available_funds))

            in_range = True
            range_reason: str | None = None
            min_tv = self.settings.strategy_min_trade_value or 0.0
            max_tv = self.settings.strategy_max_trade_value or 0.0
            if notional is not None:
                if min_tv > 0 and notional < min_tv:
                    in_range = False
                    range_reason = f"below_min_trade_value ({notional:.0f} < {min_tv:.0f})"
                elif max_tv > 0 and notional > max_tv:
                    in_range = False
                    range_reason = f"above_max_trade_value ({notional:.0f} > {max_tv:.0f})"

            # Affordability flag (opt-in via BOT_HIDE_UNAFFORDABLE_LOTS).
            # OPTION rows whose 1-lot premium already exceeds available cash
            # are still kept in the cache so the runtime can find their
            # premium when an INDEX brain signal resolves to them — but the
            # flag tells the dashboard / candidate selector to hide them so
            # they don't pollute the UI. Index / equity rows always pass.
            kind_str = str(m.get("kind") or "EQUITY").upper()
            is_affordable = True
            if (
                self.settings.bot_hide_unaffordable_lots
                and kind_str == "OPTION"
                and notional is not None
                and notional > 0
                and available_funds is not None
                and notional > float(available_funds)
            ):
                is_affordable = False
                hidden_unaffordable += 1

            brain_out: BrainOutput = self.brain.evaluate(last_price=last, agg=agg)
            c1, c5, c15 = agg.all_candles_including_partial()

            try:
                strike_val = float(m.get("strike") or 0.0)
            except (TypeError, ValueError):
                strike_val = 0.0
            try:
                offset_val = int(m.get("offset") or 0)
            except (TypeError, ValueError):
                offset_val = 0
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
                    capital_short_for_one_lot=shortfall,
                    in_trade_value_range=in_range,
                    capital_range_reason=range_reason,
                    underlying=str(m.get("underlying") or "").upper(),
                    expiry=str(m.get("expiry") or ""),
                    strike=strike_val,
                    option_side=str(m.get("side") or "").upper(),
                    offset=offset_val,
                    tradingsymbol=str(m.get("tradingsymbol") or m.get("name") or ""),
                    is_affordable=is_affordable,
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
        self.last_hidden_unaffordable = hidden_unaffordable
        return hits

    # ------------------------------------------------------------------
    def _brain_config_from_settings(self) -> BrainConfig:
        s = self.settings
        return BrainConfig(
            min_volatility_pct=s.strategy_min_volatility_pct,
            max_chop_score=s.strategy_max_chop_score,
            min_15m_trend_slope=s.strategy_min_15m_trend_slope,
            min_5m_trend_slope=s.strategy_min_5m_trend_slope,
            min_5m_breakout_clearance=s.strategy_min_breakout_clearance,
            near_breakout_clearance=s.strategy_near_breakout_clearance,
            max_late_entry_pct=s.strategy_max_late_entry_pct,
            min_above_twap_pct=s.strategy_min_above_twap_pct,
            pullback_min_uptrend_bars=s.strategy_pullback_min_uptrend_bars,
            pullback_max_retracement_pct=s.strategy_pullback_max_retracement_pct,
            continuation_consolidation_bars=s.strategy_continuation_consolidation_bars,
            continuation_max_range_pct=s.strategy_continuation_max_range_pct,
            scalp_min_5m_slope=s.strategy_scalp_min_5m_slope,
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


def _parse_candle_rows(rows: list[Any]) -> list[Candle]:
    """Convert Angel's [ts, o, h, l, c, v] rows into Candle objects.

    Angel returns timestamps as either ISO strings with the IST offset
    (``"2026-05-04T09:15:00+05:30"``) or naive ``"YYYY-MM-DD HH:MM:SS"``
    in IST. We normalise everything to UTC because the in-memory
    aggregator stores all bucket starts in UTC.
    """
    out: list[Candle] = []
    for r in rows:
        if not isinstance(r, list) or len(r) < 5:
            continue
        ts_raw = r[0]
        ts: datetime | None = None
        if isinstance(ts_raw, str):
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                try:
                    ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
                except ValueError:
                    ts = None
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        ts_utc = ts.astimezone(UTC)
        try:
            o = float(r[1]); h = float(r[2]); lo = float(r[3]); c = float(r[4])
        except (TypeError, ValueError):
            continue
        try:
            v = float(r[5]) if len(r) > 5 and r[5] is not None else 0.0
        except (TypeError, ValueError):
            v = 0.0
        out.append(Candle(ts=ts_utc, o=o, h=h, low=lo, c=c, v=v))
    out.sort(key=lambda c: c.ts)
    return out
