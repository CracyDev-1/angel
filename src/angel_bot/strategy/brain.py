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
    # Universe filters (very relaxed — let the LLM be the picky one)
    min_volatility_pct: float = 0.10      # was 0.20; 0.10% intraday range = OK
    max_chop_score: float = 0.80          # was 0.70; only the *very* sideways stuff is rejected

    # Entry timing thresholds — 5m PRIMARY, 15m bias-only.
    # Lowered ~3× so a 100-200pt NIFTY push (≈ 0.04-0.08%) actually qualifies.
    min_15m_trend_slope: float = 0.0002          # was 0.0003 — bias gate, very loose
    min_5m_trend_slope: float = 0.0003           # was 0.0008 — PRIMARY gate, fires on small pushes
    min_5m_breakout_clearance: float = 0.0003    # was 0.0006 — accept tiny clean break
    near_breakout_clearance: float = 0.0030      # was 0.0020 — wider near-breakout band
    max_late_entry_pct: float = 0.0200           # was 0.0080 — allow 2% late, exits do the protecting
    min_above_twap_pct: float = 0.0

    # Pullback (uptrend retracement) — easier to satisfy
    pullback_min_uptrend_bars: int = 2            # was 3
    pullback_max_retracement_pct: float = 0.020   # was 0.012 — allow deeper pullbacks

    # Continuation (consolidation after breakout) — easier to satisfy
    continuation_consolidation_bars: int = 2
    continuation_max_range_pct: float = 0.006     # was 0.004 — looser consolidation

    # SCALP / MOMENTUM — the high-frequency path. Only 3 checks:
    # 5m up-slope (any), 1m close in direction, 15m not against.
    # Designed to catch 100-200pt NIFTY-style pushes without waiting for
    # full breakout structure. The LLM classifier acts as the quality gate.
    scalp_min_5m_slope: float = 0.0003

    # score weights — 5m focused. volume_w stays 0 until WS quote.
    w_volatility: float = 0.25
    w_momentum: float = 0.45
    w_breakout: float = 0.30      # 5m STRUCTURE slot
    w_volume: float = 0.00

    # ranking gate (used by runtime). 0..1 scale.
    # Lowered to 0.30 so brain produces many candidates → LLM classifier
    # then decides which actually trade (LLM_DECISION_THRESHOLD enforces quality).
    min_score_to_act: float = 0.30


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
    pattern: str = "other"          # "breakout" | "pullback" | "continuation" | "other"
    structure: dict[str, Any] = field(default_factory=dict)   # raw signals for LLM ctx

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "reason": self.reason,
            "confidence": self.confidence,
            "checks": [c.to_dict() for c in self.checks],
            "pattern": self.pattern,
            "structure": self.structure,
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
        """5m-primary score.

        weights: 0.25 volatility + 0.45 momentum(5m) + 0.30 structure(5m).
        Structure = max(breakout proximity, pullback bounce, continuation tightness).
        """
        cfg = self.config
        if last_price is None or last_price <= 0:
            return ScoreBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, {"reason": "no_price"})

        c1, c5, c15 = agg.all_candles_including_partial()

        # 1) volatility — intraday session range / price (movement potential)
        sess_range_pct = 0.0
        if agg.session_high is not None and agg.session_low is not None and last_price:
            sess_range_pct = (agg.session_high - agg.session_low) / last_price
        vol_norm = _scale(sess_range_pct * 100.0, 0.10, 1.50)  # 0.1% .. 1.5%

        # 2) momentum — 5m close-to-close slope is now PRIMARY
        m5_closes = [b.c for b in c5[-5:]]
        slope5 = abs(_slope(m5_closes) or 0.0)
        mom_norm = _scale(slope5 * 100.0, 0.05, 1.20)        # 0.05% .. 1.2%

        # 15m kept around as bias only — included in inputs, NOT in score.
        m15_closes = [b.c for b in c15[-5:]]
        slope15 = (_slope(m15_closes) or 0.0)

        # 3) STRUCTURE (5m): max of three different setups
        sw5 = _swing([b.h for b in c5[-20:]], [b.low for b in c5[-20:]])
        sw_hi = sw_lo = None
        brk_score = 0.0       # closeness to / past breakout
        pb_score = 0.0        # uptrend pullback to support
        cont_score = 0.0      # consolidation after breakout
        if sw5 is not None:
            sw_hi, sw_lo = sw5
            rng = sw_hi - sw_lo
            if rng > 0:
                d_up = (sw_hi - last_price) / rng
                d_dn = (last_price - sw_lo) / rng
                d = min(abs(d_up), abs(d_dn))
                # Softer than before (was d*4) — being within 50% of the swing
                # range still scores some credit, and being right at it scores 1.
                brk_score = max(0.0, 1.0 - min(1.0, d * 2.0))

        # Pullback bounce (CALL side): 3+ green 5m bars then a small red, now > open.
        if len(c5) >= cfg.pullback_min_uptrend_bars + 1 and sw_hi is not None:
            recent = c5[-(cfg.pullback_min_uptrend_bars + 1) :]
            uptrend = sum(1 for b in recent[:-1] if b.c > b.o)
            if uptrend >= cfg.pullback_min_uptrend_bars - 1:
                pull = recent[-1]
                retr = (sw_hi - pull.low) / max(sw_hi, 1e-9)
                if 0 < retr <= cfg.pullback_max_retracement_pct:
                    pb_score = 1.0 - (retr / cfg.pullback_max_retracement_pct) * 0.4

        # Continuation: last N 5m bars all inside a tight range
        cb = cfg.continuation_consolidation_bars
        if len(c5) >= cb + 1 and sw_hi is not None:
            cons = c5[-cb:]
            hi = max(b.h for b in cons)
            lo = min(b.low for b in cons)
            mid = (hi + lo) / 2.0 if hi and lo else 0.0
            if mid > 0 and (hi - lo) / mid <= cfg.continuation_max_range_pct:
                # Only counts if we're consolidating ABOVE prior swing high (bullish)
                if lo >= sw_hi * (1.0 - cfg.near_breakout_clearance):
                    cont_score = 0.85

        struct_norm = max(brk_score, pb_score, cont_score)

        # 4) volume — placeholder (REST has no vol). Wire later from WS QUOTE.
        vol_part = 0.0

        total = (
            cfg.w_volatility * vol_norm
            + cfg.w_momentum * mom_norm
            + cfg.w_breakout * struct_norm     # weight slot reused for STRUCTURE
            + cfg.w_volume * vol_part
        )

        return ScoreBreakdown(
            total=round(total, 4),
            volatility=round(vol_norm, 4),
            momentum=round(mom_norm, 4),
            breakout=round(struct_norm, 4),     # field kept for backward compat
            volume=round(vol_part, 4),
            inputs={
                "session_range_pct": round(sess_range_pct * 100.0, 4),
                "slope_5m_pct": round(slope5 * 100.0, 4),
                "slope_15m_pct": round(slope15 * 100.0, 4),
                "swing_high_5m": sw_hi,
                "swing_low_5m": sw_lo,
                "structure_components": {
                    "breakout": round(brk_score, 3),
                    "pullback": round(pb_score, 3),
                    "continuation": round(cont_score, 3),
                },
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

        # Need enough warmup for multi-timeframe rules. We also require at
        # least one 1m bar because the pattern-detection block below indexes
        # ``c1[-1]`` for the last 1m candle — historical-candle backfill
        # may seed only 5m + 15m for instruments with no 1m history.
        if len(c5) < 5 or len(c15) < 2 or len(c1) < 1:
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
                            f"need>=5 5m, >=2 15m, >=1 1m (have {len(c5)}/{len(c15)}/{len(c1)})",
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

        # ---- pattern detection (CALL side) -------------------------------
        # 5m PRIMARY trend gate; 15m bias-only ("not against").
        c5_recent = c5[-(cfg.pullback_min_uptrend_bars + 1) :]
        uptrend_bars = sum(1 for b in c5_recent[:-1] if b.c > b.o)
        downtrend_bars = sum(1 for b in c5_recent[:-1] if b.c < b.o)

        # CALL — breakout (now allows near-breakout, not just strict cross)
        breakout_floor_call = prev_swing_hi * (1.0 - cfg.near_breakout_clearance)
        call_breakout_checks = [
            *filters,
            EntryCheck(
                "trend_5m_up",
                slope_5m >= cfg.min_5m_trend_slope,
                f"5m slope {slope_5m * 100.0:.3f}% (min {cfg.min_5m_trend_slope * 100.0:.3f}%)",
            ),
            EntryCheck(
                "trend_15m_not_against",
                slope_15m >= -cfg.min_15m_trend_slope,
                f"15m slope {slope_15m * 100.0:.3f}% (bias gate)",
            ),
            EntryCheck(
                "breakout_5m",
                last_price >= breakout_floor_call,
                f"price {last_price:.2f} vs swingHi {prev_swing_hi:.2f} (near-breakout band)",
            ),
            EntryCheck("bullish_1m_close", bullish_1m, "last 1m candle bullish + body>=45%"),
            EntryCheck(
                "not_late",
                ret_1 <= cfg.max_late_entry_pct,
                f"1m return {ret_1 * 100.0:.3f}% (max {cfg.max_late_entry_pct * 100.0:.3f}%)",
            ),
        ]

        # CALL — pullback (entering on a bounce within an established uptrend)
        retracement = (
            (prev_swing_hi - last_price) / prev_swing_hi if prev_swing_hi else 0.0
        )
        call_pullback_checks = [
            *filters,
            EntryCheck(
                "uptrend_5m_established",
                uptrend_bars >= cfg.pullback_min_uptrend_bars - 1
                and slope_5m >= cfg.min_5m_trend_slope * 0.5,
                f"{uptrend_bars} green 5m bars + slope {slope_5m * 100.0:.3f}%",
            ),
            EntryCheck(
                "pullback_within_band",
                0 < retracement <= cfg.pullback_max_retracement_pct,
                f"retraced {retracement * 100.0:.2f}% from swingHi (max {cfg.pullback_max_retracement_pct * 100.0:.2f}%)",
            ),
            EntryCheck(
                "bounce_candle_1m",
                bullish_1m,
                "last 1m candle bullish + body>=45% (bounce confirm)",
            ),
            EntryCheck(
                "trend_15m_not_against",
                slope_15m >= -cfg.min_15m_trend_slope,
                f"15m slope {slope_15m * 100.0:.3f}%",
            ),
        ]

        # CALL — continuation (consolidation above prior swing high then resume)
        cont_recent = c5[-cfg.continuation_consolidation_bars :]
        if cont_recent:
            cons_hi = max(b.h for b in cont_recent)
            cons_lo = min(b.low for b in cont_recent)
            cons_range_pct = (
                (cons_hi - cons_lo) / cons_hi if cons_hi else 0.0
            )
        else:
            cons_hi = cons_lo = 0.0
            cons_range_pct = 0.0
        call_continuation_checks = [
            *filters,
            EntryCheck(
                "broke_out_earlier",
                cons_lo >= prev_swing_hi * (1.0 - cfg.near_breakout_clearance),
                f"consolidation low {cons_lo:.2f} vs prior swingHi {prev_swing_hi:.2f}",
            ),
            EntryCheck(
                "tight_consolidation",
                cons_range_pct > 0
                and cons_range_pct <= cfg.continuation_max_range_pct,
                f"consolidation range {cons_range_pct * 100.0:.2f}% (max {cfg.continuation_max_range_pct * 100.0:.2f}%)",
            ),
            EntryCheck(
                "resume_up",
                bullish_1m and last_price >= cons_hi,
                f"price {last_price:.2f} reclaiming consolidation hi {cons_hi:.2f}",
            ),
            EntryCheck(
                "trend_15m_not_against",
                slope_15m >= -cfg.min_15m_trend_slope,
                f"15m slope {slope_15m * 100.0:.3f}%",
            ),
        ]

        # PUT — breakdown (mirror; now allows near-breakdown band too)
        breakdown_ceiling_put = prev_swing_lo * (1.0 + cfg.near_breakout_clearance)
        put_breakdown_checks = [
            *filters,
            EntryCheck(
                "trend_5m_down",
                slope_5m <= -cfg.min_5m_trend_slope,
                f"5m slope {slope_5m * 100.0:.3f}% (max -{cfg.min_5m_trend_slope * 100.0:.3f}%)",
            ),
            EntryCheck(
                "trend_15m_not_against",
                slope_15m <= cfg.min_15m_trend_slope,
                f"15m slope {slope_15m * 100.0:.3f}% (bias gate)",
            ),
            EntryCheck(
                "breakdown_5m",
                last_price <= breakdown_ceiling_put,
                f"price {last_price:.2f} vs swingLo {prev_swing_lo:.2f} (near-breakdown band)",
            ),
            EntryCheck("bearish_1m_close", bearish_1m, "last 1m candle bearish + body>=45%"),
            EntryCheck(
                "not_late",
                ret_1 >= -cfg.max_late_entry_pct,
                f"1m return {ret_1 * 100.0:.3f}% (max -{cfg.max_late_entry_pct * 100.0:.3f}%)",
            ),
        ]

        # PUT — pullback (rally back to resistance in a downtrend)
        retracement_dn = (
            (last_price - prev_swing_lo) / prev_swing_lo if prev_swing_lo else 0.0
        )
        put_pullback_checks = [
            *filters,
            EntryCheck(
                "downtrend_5m_established",
                downtrend_bars >= cfg.pullback_min_uptrend_bars - 1
                and slope_5m <= -cfg.min_5m_trend_slope * 0.5,
                f"{downtrend_bars} red 5m bars + slope {slope_5m * 100.0:.3f}%",
            ),
            EntryCheck(
                "pullback_within_band",
                0 < retracement_dn <= cfg.pullback_max_retracement_pct,
                f"rallied {retracement_dn * 100.0:.2f}% from swingLo",
            ),
            EntryCheck(
                "rejection_candle_1m",
                bearish_1m,
                "last 1m candle bearish + body>=45% (rejection confirm)",
            ),
            EntryCheck(
                "trend_15m_not_against",
                slope_15m <= cfg.min_15m_trend_slope,
                f"15m slope {slope_15m * 100.0:.3f}%",
            ),
        ]

        # SCALP / MOMENTUM — fastest path. Just 3 checks, designed to catch
        # 100-200pt index pushes (or proportional move on stocks/commodities).
        # Survival relies on tight stop + quick target in PaperConfig / live exits.
        call_scalp_checks = [
            EntryCheck(
                "trend_5m_up_any",
                slope_5m >= cfg.scalp_min_5m_slope,
                f"5m slope {slope_5m * 100.0:.3f}% (min {cfg.scalp_min_5m_slope * 100.0:.3f}%)",
            ),
            EntryCheck(
                "bullish_1m_close",
                bullish_1m,
                "last completed 1m candle bullish + body>=45%",
            ),
            EntryCheck(
                "trend_15m_not_against",
                slope_15m >= -cfg.min_15m_trend_slope,
                f"15m slope {slope_15m * 100.0:.3f}% (bias gate)",
            ),
        ]
        put_scalp_checks = [
            EntryCheck(
                "trend_5m_down_any",
                slope_5m <= -cfg.scalp_min_5m_slope,
                f"5m slope {slope_5m * 100.0:.3f}%",
            ),
            EntryCheck(
                "bearish_1m_close",
                bearish_1m,
                "last completed 1m candle bearish + body>=45%",
            ),
            EntryCheck(
                "trend_15m_not_against",
                slope_15m <= cfg.min_15m_trend_slope,
                f"15m slope {slope_15m * 100.0:.3f}% (bias gate)",
            ),
        ]

        # Build the candidate set of (pattern, side, checks, reason).
        # NOTE order matters as a tiebreaker — structural setups first
        # (breakout > pullback > continuation > scalp) so when multiple fire
        # we prefer the cleaner setup, but the scalp keeps the bot active
        # when nothing structural is happening.
        candidates: list[tuple[str, str, list[EntryCheck], str]] = [
            ("breakout",     "BUY_CALL", call_breakout_checks,     "uptrend_breakout_confirmed"),
            ("pullback",     "BUY_CALL", call_pullback_checks,     "uptrend_pullback_bounce"),
            ("continuation", "BUY_CALL", call_continuation_checks, "uptrend_continuation_resume"),
            ("scalp",        "BUY_CALL", call_scalp_checks,        "scalp_call_5m_momentum"),
            ("breakout",     "BUY_PUT",  put_breakdown_checks,     "downtrend_breakdown_confirmed"),
            ("pullback",     "BUY_PUT",  put_pullback_checks,      "downtrend_pullback_rejection"),
            ("scalp",        "BUY_PUT",  put_scalp_checks,         "scalp_put_5m_momentum"),
        ]

        # Rank each candidate by # of passing checks; require ALL to fire.
        scored = []
        for pat, side, checks, reason in candidates:
            passed = sum(1 for c in checks if c.ok)
            scored.append((pat, side, checks, reason, passed))
        scored.sort(key=lambda t: (-t[4], t[0]))   # most-passes first, stable

        for pat, side, checks, reason, passed in scored:
            if passed == len(checks):
                structure_blob = {
                    "pattern": pat,
                    "uptrend_bars_5m": uptrend_bars,
                    "downtrend_bars_5m": downtrend_bars,
                    "retracement_pct": round(retracement * 100.0, 3),
                    "consolidation_range_pct": round(cons_range_pct * 100.0, 3),
                    "near_breakout": last_price > prev_swing_hi * (1.0 - cfg.near_breakout_clearance)
                    and last_price <= prev_swing_hi * (1.0 + cfg.min_5m_breakout_clearance),
                    "late_entry_1m_pct": round(ret_1 * 100.0, 3),
                    "twap": twap,
                    "swing_hi": prev_swing_hi,
                    "swing_lo": prev_swing_lo,
                }
                return BrainOutput(
                    score,
                    Signal(
                        side=side,
                        reason=reason,
                        confidence=1.0,
                        checks=checks,
                        pattern=pat,
                        structure=structure_blob,
                    ),
                    diag,
                )

        # Nothing fired — surface the closest candidate so the UI shows progress.
        best = scored[0]
        pat, side, checks, reason, passed = best
        return BrainOutput(
            score,
            Signal(
                side="NO_TRADE",
                reason=f"{pat}_{side.lower()}_partial_{passed}/{len(checks)}",
                confidence=passed / max(1, len(checks)),
                checks=checks,
                pattern=pat,
                structure={
                    "uptrend_bars_5m": uptrend_bars,
                    "downtrend_bars_5m": downtrend_bars,
                    "retracement_pct": round(retracement * 100.0, 3),
                    "late_entry_1m_pct": round(ret_1 * 100.0, 3),
                },
            ),
            diag,
        )
