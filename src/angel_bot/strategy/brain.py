"""Multi-timeframe trading brain.

Pipeline (mirrors the implementation plan):

    raw LTPs  -->  CandleAggregator (1m / 5m / 15m)
                      |
                      v
                BrainEngine.evaluate
                      |
        +-------------+--------------+
        |                            |
        v                            v
    Score (ranking)            Signal (entry timing)
        |                            |
        +------- both required ------+
                      |
                      v
                  RiskEngine

Score is independent of side (used to rank instruments). Signal is the
actual side-aware entry decision and includes a transparent list of
checks so the dashboard can show *why* the bot acted or stood down.

Constraints we are honest about:
    * Volume is not available from Angel REST `getLtpData`. The volume
      factor weight stays at 0 unless the WebSocket QUOTE feed is wired.
      Volatility (intraday range %) is the proxy until then.
    * VWAP needs volume. We use a session TWAP (time-weighted mean of
      polled LTPs) as the "above mean" reference level — labelled as
      such in the score breakdown so a reviewer never confuses the two.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from angel_bot.market_data.candles import CandleAggregator


# ----------------------------------------------------------------------
# config / weights (read from .env via Settings, but defaults live here)
# ----------------------------------------------------------------------


@dataclass
class BrainConfig:
    # universe filters (early reject)
    min_volatility_pct: float = 0.20      # intraday (high-low)/price * 100, % units
    max_chop_score: float = 0.55          # 0..1, higher = choppier
    require_breakout_in_range: bool = True

    # entry timing thresholds
    min_15m_trend_slope: float = 0.0007   # last-vs-first close pct over 15m window
    min_5m_breakout_clearance: float = 0.0010  # how far past swing high/low (frac)
    max_late_entry_pct: float = 0.0040    # reject CALL if last ret > this; mirrored for PUT
    min_above_twap_pct: float = 0.0       # CALL needs price >= TWAP * (1+x)

    # score weights (normalized factors). volume_weight stays 0 until WS quote.
    w_volatility: float = 0.30
    w_momentum: float = 0.40
    w_breakout: float = 0.30
    w_volume: float = 0.00

    # ranking gate (used by runtime). 0..1 scale.
    min_score_to_act: float = 0.45


# ----------------------------------------------------------------------
# outputs
# ----------------------------------------------------------------------


@dataclass
class ScoreBreakdown:
    total: float                  # 0..1
    volatility: float             # 0..1 normalized
    momentum: float               # 0..1 normalized (uses |15m return|)
    breakout: float               # 0..1 normalized (proximity to / past swing)
    volume: float = 0.0           # 0..1 normalized (0 until WS QUOTE)
    inputs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EntryCheck:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Signal:
    side: str                       # "BUY_CALL" | "BUY_PUT" | "NO_TRADE"
    reason: str                     # short tag, e.g. "uptrend_breakout_confirmed"
    confidence: float               # 0..1, fraction of timing checks passed
    checks: list[EntryCheck] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "reason": self.reason,
            "confidence": self.confidence,
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass
class BrainOutput:
    score: ScoreBreakdown
    signal: Signal
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score.to_dict(),
            "signal": self.signal.to_dict(),
            "diagnostics": self.diagnostics,
        }


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _slope(values: list[float]) -> float | None:
    """Simple last-vs-first relative change. Robust to short series."""
    if len(values) < 2 or values[0] == 0:
        return None
    return (values[-1] - values[0]) / abs(values[0])


def _swing(highs: list[float], lows: list[float]) -> tuple[float, float] | None:
    if not highs or not lows:
        return None
    return (max(highs), min(lows))


def _chop(closes: list[float]) -> float | None:
    """Direction-flips / (n-2). 0 = pure trend, 1 = max chop."""
    if len(closes) < 3:
        return None
    dirs = [1 if closes[i] >= closes[i - 1] else -1 for i in range(1, len(closes))]
    flips = sum(1 for i in range(1, len(dirs)) if dirs[i] != dirs[i - 1])
    return flips / max(1, len(dirs) - 1)


def _scale(value: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


# ----------------------------------------------------------------------
# the brain
# ----------------------------------------------------------------------


class BrainEngine:
    """Stateless evaluator. State (candles) lives in CandleAggregator."""

    def __init__(self, config: BrainConfig | None = None) -> None:
        self.config = config or BrainConfig()

    # ---------- score (used for ranking, side-independent) ----------

    def score_instrument(
        self,
        *,
        last_price: float | None,
        agg: CandleAggregator,
    ) -> ScoreBreakdown:
        cfg = self.config
        if last_price is None or last_price <= 0:
            return ScoreBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, {"reason": "no_price"})

        c1, c5, c15 = agg.all_candles_including_partial()

        # 1) volatility — intraday session range / price (proxy for "movement potential")
        sess_range_pct = 0.0
        if agg.session_high is not None and agg.session_low is not None and last_price:
            sess_range_pct = (agg.session_high - agg.session_low) / last_price
        vol_norm = _scale(sess_range_pct * 100.0, 0.10, 1.50)  # 0.1% .. 1.5%

        # 2) momentum — magnitude of 15m close-to-close move (multi-timeframe)
        m15_closes = [b.c for b in c15[-5:]]
        slope15 = abs(_slope(m15_closes) or 0.0)
        mom_norm = _scale(slope15 * 100.0, 0.05, 1.50)  # 0.05% .. 1.5%

        # 3) breakout proximity — how close LTP is to the recent 5m swing
        sw5 = _swing([b.h for b in c5[-20:]], [b.low for b in c5[-20:]])
        brk_norm = 0.0
        sw_hi = sw_lo = None
        if sw5 is not None:
            sw_hi, sw_lo = sw5
            rng = sw_hi - sw_lo
            if rng > 0:
                # Distance from the nearer side; closer = better
                d_up = (sw_hi - last_price) / rng
                d_dn = (last_price - sw_lo) / rng
                d = min(abs(d_up), abs(d_dn))
                # invert: 0 distance = score 1
                brk_norm = max(0.0, 1.0 - min(1.0, d * 4.0))

        # 4) volume — placeholder (REST has no vol). Wire later from WS QUOTE.
        vol_part = 0.0

        total = (
            cfg.w_volatility * vol_norm
            + cfg.w_momentum * mom_norm
            + cfg.w_breakout * brk_norm
            + cfg.w_volume * vol_part
        )

        return ScoreBreakdown(
            total=round(total, 4),
            volatility=round(vol_norm, 4),
            momentum=round(mom_norm, 4),
            breakout=round(brk_norm, 4),
            volume=round(vol_part, 4),
            inputs={
                "session_range_pct": round(sess_range_pct * 100.0, 4),
                "slope_15m_pct": round(slope15 * 100.0, 4),
                "swing_high_5m": sw_hi,
                "swing_low_5m": sw_lo,
                "candles": {"1m": len(c1), "5m": len(c5), "15m": len(c15)},
            },
        )

    # ---------- signal (side-aware entry timing) ----------

    def evaluate(
        self,
        *,
        last_price: float | None,
        agg: CandleAggregator,
    ) -> BrainOutput:
        cfg = self.config
        score = self.score_instrument(last_price=last_price, agg=agg)
        diag: dict[str, Any] = {"score_inputs": score.inputs}

        if last_price is None or last_price <= 0:
            return BrainOutput(score, Signal("NO_TRADE", "no_price", 0.0, []), diag)

        c1, c5, c15 = agg.all_candles_including_partial()

        # Need enough warmup for multi-timeframe rules
        if len(c5) < 5 or len(c15) < 2:
            return BrainOutput(
                score,
                Signal(
                    "NO_TRADE",
                    "warmup",
                    0.0,
                    [
                        EntryCheck(
                            "warmup",
                            False,
                            f"need>=5 5m bars and >=2 15m (have {len(c5)}/{len(c15)})",
                        )
                    ],
                ),
                diag,
            )

        # ---- universe filters (mirror the plan: ignore choppy / range-bound) ----
        chop = _chop([b.c for b in c5[-12:]])
        sess_range_pct = 0.0
        if agg.session_high is not None and agg.session_low is not None:
            sess_range_pct = (agg.session_high - agg.session_low) / last_price
        twap = agg.session_twap

        filters: list[EntryCheck] = []
        filters.append(
            EntryCheck(
                "volatility_ok",
                sess_range_pct * 100.0 >= cfg.min_volatility_pct,
                f"intraday range {sess_range_pct * 100.0:.3f}% vs min {cfg.min_volatility_pct:.2f}%",
            )
        )
        filters.append(
            EntryCheck(
                "chop_ok",
                chop is None or chop <= cfg.max_chop_score,
                f"chop_score {chop:.2f}" if chop is not None else "chop_score n/a",
            )
        )

        if any(not f.ok for f in filters):
            failed = next(f for f in filters if not f.ok)
            return BrainOutput(
                score,
                Signal("NO_TRADE", f"filter:{failed.name}", 0.0, filters),
                diag,
            )

        # ---- multi-timeframe context ----
        m15_closes = [b.c for b in c15[-5:]]
        m5_closes = [b.c for b in c5[-5:]]
        sw5 = _swing([b.h for b in c5[-20:-1]], [b.low for b in c5[-20:-1]])
        if sw5 is None:
            return BrainOutput(score, Signal("NO_TRADE", "no_swing", 0.0, filters), diag)
        prev_swing_hi, prev_swing_lo = sw5

        slope_15m = _slope(m15_closes) or 0.0
        slope_5m = _slope(m5_closes) or 0.0
        ret_1 = (
            (c1[-1].c - c1[-2].c) / c1[-2].c if len(c1) >= 2 and c1[-2].c else 0.0
        )

        # bullish / bearish 1m candle (last completed)
        last_1m = c1[-2] if len(c1) >= 2 else c1[-1]
        body = last_1m.c - last_1m.o
        rng_1m = max(1e-9, last_1m.h - last_1m.low)
        bullish_1m = body > 0 and (body / rng_1m) >= 0.45
        bearish_1m = body < 0 and (-body / rng_1m) >= 0.45

        diag.update(
            {
                "slope_15m": round(slope_15m * 100.0, 4),
                "slope_5m": round(slope_5m * 100.0, 4),
                "ret_1": round(ret_1 * 100.0, 4),
                "twap": twap,
                "swing_hi_5m": prev_swing_hi,
                "swing_lo_5m": prev_swing_lo,
                "chop": chop,
            }
        )

        # ---- CALL checks ----
        call_checks = [
            *filters,
            EntryCheck(
                "trend_15m_up",
                slope_15m >= cfg.min_15m_trend_slope,
                f"15m slope {slope_15m * 100.0:.3f}% (min {cfg.min_15m_trend_slope * 100.0:.3f}%)",
            ),
            EntryCheck(
                "trend_5m_up",
                slope_5m > 0,
                f"5m slope {slope_5m * 100.0:.3f}%",
            ),
            EntryCheck(
                "breakout_5m",
                last_price > prev_swing_hi * (1.0 + cfg.min_5m_breakout_clearance),
                f"price {last_price:.2f} vs swingHi {prev_swing_hi:.2f}",
            ),
            EntryCheck("bullish_1m_close", bullish_1m, "last 1m candle bullish + body>=45%"),
            EntryCheck(
                "above_twap",
                twap is None or last_price >= twap * (1.0 + cfg.min_above_twap_pct),
                f"price {last_price:.2f} vs twap {twap:.2f}" if twap else "twap n/a",
            ),
            EntryCheck(
                "not_late",
                ret_1 <= cfg.max_late_entry_pct,
                f"1m return {ret_1 * 100.0:.3f}% (max {cfg.max_late_entry_pct * 100.0:.3f}%)",
            ),
        ]

        # ---- PUT checks (mirror) ----
        put_checks = [
            *filters,
            EntryCheck(
                "trend_15m_down",
                slope_15m <= -cfg.min_15m_trend_slope,
                f"15m slope {slope_15m * 100.0:.3f}% (max -{cfg.min_15m_trend_slope * 100.0:.3f}%)",
            ),
            EntryCheck(
                "trend_5m_down",
                slope_5m < 0,
                f"5m slope {slope_5m * 100.0:.3f}%",
            ),
            EntryCheck(
                "breakdown_5m",
                last_price < prev_swing_lo * (1.0 - cfg.min_5m_breakout_clearance),
                f"price {last_price:.2f} vs swingLo {prev_swing_lo:.2f}",
            ),
            EntryCheck("bearish_1m_close", bearish_1m, "last 1m candle bearish + body>=45%"),
            EntryCheck(
                "below_twap",
                twap is None or last_price <= twap * (1.0 - cfg.min_above_twap_pct),
                f"price {last_price:.2f} vs twap {twap:.2f}" if twap else "twap n/a",
            ),
            EntryCheck(
                "not_late",
                ret_1 >= -cfg.max_late_entry_pct,
                f"1m return {ret_1 * 100.0:.3f}%",
            ),
        ]

        call_pass = sum(1 for c in call_checks if c.ok)
        put_pass = sum(1 for c in put_checks if c.ok)
        # require ALL checks (strict, profit-preserving stance)
        if call_pass == len(call_checks):
            return BrainOutput(
                score,
                Signal(
                    "BUY_CALL",
                    "uptrend_breakout_confirmed",
                    1.0,
                    call_checks,
                ),
                diag,
            )
        if put_pass == len(put_checks):
            return BrainOutput(
                score,
                Signal(
                    "BUY_PUT",
                    "downtrend_breakdown_confirmed",
                    1.0,
                    put_checks,
                ),
                diag,
            )

        # Soft NO_TRADE — emit the side that came closest, for transparency
        if call_pass >= put_pass:
            return BrainOutput(
                score,
                Signal(
                    "NO_TRADE",
                    f"call_partial_{call_pass}/{len(call_checks)}",
                    call_pass / len(call_checks),
                    call_checks,
                ),
                diag,
            )
        return BrainOutput(
            score,
            Signal(
                "NO_TRADE",
                f"put_partial_{put_pass}/{len(put_checks)}",
                put_pass / len(put_checks),
                put_checks,
            ),
            diag,
        )
