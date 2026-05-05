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
from datetime import datetime
from typing import Any

from angel_bot.market_data.candles import Candle, CandleAggregator, RetestBreakoutSetup
from angel_bot.strategy.indicators import wilder_adx, wilder_atr


# Minimum candle depth for regime gate + evaluate warmup (aggregated buckets / closed 5m).
MIN_REGIME_BUCKETS_5M = 10
MIN_REGIME_BUCKETS_15M = 5
MIN_REGIME_CLOSED_5M = 10


# ----------------------------------------------------------------------
# config / weights (read from .env via Settings, but defaults live here)
# ----------------------------------------------------------------------


@dataclass
class BrainConfig:
    # Universe filters (very relaxed — let the LLM be the picky one)
    min_volatility_pct: float = 0.10      # was 0.20; 0.10% intraday range = OK
    max_chop_score: float = 0.80          # was 0.70; only the *very* sideways stuff is rejected

    # Master switch: selective gates + weighted tie-break + pattern toggles.
    # When False, legacy permissive behavior (for tests): loose regime, scalp on.
    selective_entry_enabled: bool = True

    # Entry timing thresholds — 5m PRIMARY, 15m bias-only.
    min_15m_trend_slope: float = 0.0002
    min_5m_trend_slope: float = 0.0003
    min_5m_breakout_clearance: float = 0.0003
    near_breakout_clearance: float = 0.0030
    max_late_entry_pct: float = 0.0200
    min_above_twap_pct: float = 0.0

    # --- Regime gate (sideways chop + weak trend strength) ---
    # If both |5m| and |15m| slopes are below these (fractional per bar window), NO_TRADE.
    # Set to 0.0 to disable that branch.
    regime_min_abs_slope_5m: float = 0.00008
    regime_min_abs_slope_15m: float = 0.00006
    # ADX on closed 5m bars (Wilder). If None from insufficient data, gate skipped.
    # Set regime_adx_min to 0.0 to disable ADX gate.
    regime_adx_period: int = 14
    regime_adx_min: float = 11.0
    # When |price−TWAP|/TWAP exceeds reference_max_distance_pct, allow only if trend strong + fresh leg.
    regime_extension_adx_min: float = 22.0
    # Block when last-N 5m range is below this fraction of ATR(14) (dead tape).
    regime_recent_vol_atr_ratio: float = 0.5

    # TWAP is session mean of polled LTPs (not exchange VWAP — see module docstring).
    # If price is within this fractional distance of TWAP, NO_TRADE (chop zone).
    # 0.0 disables.
    min_twap_deviation_pct: float = 0.0004
    # TWAP distance band (vs TWAP): too close = chop, too far = exhaustion.
    reference_min_distance_pct: float = 0.0015
    reference_max_distance_pct: float = 0.018

    # Directional enforcement for selective mode: require 15m slope sign, TWAP side, structure.
    directional_bias_enabled: bool = True
    directional_15m_eps: float = 0.00005  # must be clearly positive/negative

    # Strict structure: breakout/breakdown beyond prior swing (additional fractional clearance).
    strict_swing_break_eps: float = 0.00015

    # Pattern toggles (selective mode defaults: structural only, no scalp).
    enable_pullback_patterns: bool = False
    enable_scalp_patterns: bool = False
    enable_continuation_patterns: bool = False

    # Pullback / continuation params (unchanged semantics)
    pullback_min_uptrend_bars: int = 2
    pullback_max_retracement_pct: float = 0.020
    continuation_consolidation_bars: int = 2
    continuation_max_range_pct: float = 0.006

    scalp_min_5m_slope: float = 0.0003

    # score weights — 5m focused. volume_w stays 0 until WS quote.
    w_volatility: float = 0.25
    w_momentum: float = 0.45
    w_breakout: float = 0.30
    w_volume: float = 0.00

    # ranking gate (used by runtime). 0..1 scale.
    min_score_to_act: float = 0.30

    # Weighted checklist quality (tie-break and optional partial threshold)
    pattern_weight_trend: float = 0.45
    pattern_weight_structure: float = 0.35
    pattern_weight_vol_proxy: float = 0.15
    pattern_weight_momentum: float = 0.05
    # Minimum weighted checklist score to accept a pattern (0..1).
    min_pattern_score: float = 0.72
    # If no pattern reaches min_pattern_score with all checks True, allow best partial
    # only if fraction of passing checks >= this AND weighted score >= min_pattern_score.
    min_pattern_check_ratio: float = 0.85

    # --- Breakout quality (extension / spike / confirm bar / HTF / score floor) ---
    breakout_max_extension_pct: float = 0.0035
    breakout_max_distance_from_level_pct: float = 0.004
    breakout_spike_range_mult: float = 1.8
    breakout_spike_avg_bars: int = 10
    breakout_confirm_max_retrace_of_range: float = 0.30
    enable_breakout_bar_confirmation: bool = True
    require_strict_htf_trend: bool = True
    min_htf_slope_eps: float = 0.0
    # Minimum brain ranking score on 0..100 scale before any trade (0 = disabled).
    min_brain_score_0_100: float = 75.0
    # Reject when session range %% of price below this (low-volatility days). 0 disables.
    min_session_range_pct_breakout: float = 0.0

    # ATR(14) on 5m vs distance from swing (weak move / exhaustion). Disabled if atr None.
    atr_period: int = 14
    breakout_atr_min_multiple: float = 0.5
    breakout_atr_max_multiple: float = 2.0

    # Penalties applied to ranking score (0..1): reduce weight of late / spike candles.
    rank_penalty_move_scale: float = 25.0
    rank_penalty_spike_scale: float = 0.12

    # Global regime: reject when |15m slope| below this (too flat). 0 disables.
    regime_global_flat_slope_eps: float = 0.00005

    # --- Retest-after-breakout (5m closed bars; one setup per CandleAggregator) ---
    retest_zone_tolerance_pct: float = 0.0015   # ±0.15% around breakout_level
    retest_max_post_breakout_bars: int = 3      # retest touch must occur within N bars after breakout
    retest_confirm_max_bars: int = 3             # confirmation candle after first touch
    retest_max_retrace_fraction: float = 0.50    # vs breakout candle range
    retest_engulf_body_min: float = 0.45         # body/range for engulfing rejection

    # Fail-closed regime: thin structure / missing slope TWAP blocks trading;
    # missing ADX/ATR may warn + fallback (see unified_regime_gate).
    regime_fail_closed_indicators: bool = True
    regime_require_twap: bool = True
    # Global staleness: abort pending retest/setup when closed-bar age exceeds this (0 = disabled).
    signal_max_age_closed_bars: int = 3

    # Closed 5m swing bias: block CALL when BEARISH, block PUT when BULLISH (selective mode only).
    structure_bias_filter_enabled: bool = True


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


def _check_weight_category(name: str) -> str:
    n = name.lower()
    if n.startswith("trend_") or "uptrend" in n or "downtrend" in n:
        return "trend"
    if any(
        x in n
        for x in (
            "breakout",
            "breakdown",
            "pullback",
            "consolidation",
            "broke_out",
            "resume",
            "volatility_ok",
            "chop_ok",
        )
    ):
        return "structure"
    if "volatility" in n or "chop" in n:
        return "vol"
    return "momentum"


def _weighted_checklist_score(checks: list[EntryCheck], cfg: BrainConfig) -> float:
    wt = cfg.pattern_weight_trend
    ws = cfg.pattern_weight_structure
    wv = cfg.pattern_weight_vol_proxy
    wm = cfg.pattern_weight_momentum
    num = 0.0
    den = 0.0
    for c in checks:
        cat = _check_weight_category(c.name)
        w = wt if cat == "trend" else ws if cat == "structure" else wv if cat == "vol" else wm
        num += w * (1.0 if c.ok else 0.0)
        den += w
    return num / den if den > 0 else 0.0


def _pick_rank_tuple(wscore: float, chase_pct: float, pattern: str) -> tuple[float, float, str]:
    """Prefer higher weighted score, then less extension past structure (lower chase_pct)."""
    return (wscore, -chase_pct, pattern)


def _avg_candle_range_5m(c5_closed: list[Candle], last_n: int) -> float | None:
    if len(c5_closed) < 2 or last_n < 1:
        return None
    tail = c5_closed[-(last_n + 1) : -1]
    if not tail:
        return None
    ranges = [max(1e-12, b.h - b.low) for b in tail]
    return sum(ranges) / len(ranges)


def _recent_high_low_range_5m(c5_closed: list[Candle], *, last_n: int = 5) -> float | None:
    """Absolute high-low span of the last ``last_n`` closed 5m bars (price units)."""
    if len(c5_closed) < last_n or last_n < 1:
        return None
    tail = c5_closed[-last_n:]
    hi = max(b.h for b in tail)
    lo = min(b.low for b in tail)
    return float(hi - lo)


def _detect_structure_bias(c5_closed: list[Candle]) -> str:
    """Return ``BULLISH`` | ``BEARISH`` | ``NEUTRAL`` from closed 5m closes only."""
    if not c5_closed or len(c5_closed) < 25:
        return "NEUTRAL"
    closes = [b.c for b in c5_closed]
    recent = closes[-20:-1]
    last = closes[-1]
    last_swing_high = max(recent)
    last_swing_low = min(recent)
    broke_up = last > last_swing_high
    broke_down = last < last_swing_low
    if broke_up and not broke_down:
        return "BULLISH"
    if broke_down and not broke_up:
        return "BEARISH"
    return "NEUTRAL"


def _confirm_structure_bias(c5_closed: list[Candle], prev_bias: str | None) -> str:
    """One closed-bar confirmation; bias flips only after a full candle beyond the window."""
    if not c5_closed or len(c5_closed) < 26:
        return prev_bias or "NEUTRAL"
    closes = [b.c for b in c5_closed]
    prev_c = closes[-2]
    curr = closes[-1]
    recent = closes[-21:-2]
    swing_high = max(recent)
    swing_low = min(recent)
    if prev_c < swing_low and curr <= prev_c:
        return "BEARISH"
    if prev_c > swing_high and curr >= prev_c:
        return "BULLISH"
    return prev_bias or "NEUTRAL"


def unified_regime_gate(
    cfg: BrainConfig,
    agg: CandleAggregator,
    last_price: float | None,
    out_diagnostics: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """STEP 1–7 unified regime (selective mode).

    Returns (True, reason) when entries should be blocked, else (False, "").

    Critical blocks: insufficient candle depth, missing 15m slope (``regime:missing_slope``),
    chop/extension/flat-HTF. Missing Wilder ADX/ATR is non-blocking (warnings + fallbacks).
    ``regime:low_adx`` applies only when ADX is actually computed and trend is weak on both
    structure and momentum dimensions—strong 15m slope or recent range vs ATR bypasses it.
    """
    if not cfg.selective_entry_enabled:
        return False, ""
    if last_price is None or last_price <= 0:
        return False, ""

    c1, c5, c15 = agg.all_candles_including_partial()
    c5_closed = agg.snapshot_lists()[1]

    # STEP 1 — depth + optional TWAP only (warmup-scale gates live in evaluate too).
    if len(c5) < MIN_REGIME_BUCKETS_5M or len(c15) < MIN_REGIME_BUCKETS_15M:
        if cfg.regime_fail_closed_indicators:
            return True, "regime:missing_slope"
        return False, ""

    if cfg.regime_fail_closed_indicators:
        if len(c5_closed) < MIN_REGIME_CLOSED_5M:
            return True, "regime:missing_slope"
        if cfg.regime_require_twap:
            tw_fc = agg.session_twap
            if tw_fc is None or tw_fc <= 0:
                return True, "regime:missing_slope"

    # STEP 2 — 15m slope: unknown structure → hard block (never treat as flat trend).
    n15 = min(10, len(c15))
    if n15 < 3:
        if cfg.regime_fail_closed_indicators:
            return True, "regime:missing_slope"
        return False, ""
    m15_closes = [b.c for b in c15[-n15:]]
    slope_15m_raw = _slope(m15_closes)
    if slope_15m_raw is None:
        return True, "regime:missing_slope"
    slope_15m = float(slope_15m_raw)

    adx_raw = wilder_adx(c5_closed, period=cfg.regime_adx_period)
    atr_raw = wilder_atr(c5_closed, period=cfg.atr_period)
    adx_missing_raw = adx_raw is None
    adx_5m = adx_raw
    atr_5m = atr_raw
    recent_range = _recent_high_low_range_5m(c5_closed, last_n=5)

    if adx_5m is None:
        if out_diagnostics is not None:
            out_diagnostics["warn_adx_missing"] = True
        adx_5m = 0.0

    if atr_5m is None:
        if out_diagnostics is not None:
            out_diagnostics["warn_atr_missing"] = True
        try:
            if len(c5_closed) >= 5:
                highs = [b.h for b in c5_closed[-5:]]
                lows = [b.low for b in c5_closed[-5:]]
                atr_5m = max(highs) - min(lows)
            else:
                atr_5m = 0.0
        except Exception:
            atr_5m = 0.0

    twap = agg.session_twap
    dist: float | None = None
    if twap is not None and twap > 0:
        dist = abs(float(last_price) - twap) / twap

    # STEP 3 — hard rejection
    if cfg.regime_global_flat_slope_eps > 0 and abs(slope_15m) < cfg.regime_global_flat_slope_eps:
        return True, "regime:flat_htf"
    # low_adx only when Wilder ADX exists; missing ADX never maps to low_adx.
    if cfg.regime_adx_min > 0 and not adx_missing_raw and adx_5m < cfg.regime_adx_min:
        eps = cfg.regime_global_flat_slope_eps
        structure_strong = eps <= 0 or abs(slope_15m) >= eps
        momentum_strong = (
            atr_5m > 0
            and recent_range is not None
            and recent_range >= cfg.regime_recent_vol_atr_ratio * atr_5m
        )
        if not structure_strong and not momentum_strong:
            return True, "regime:low_adx"
    if (
        atr_5m > 0
        and recent_range is not None
        and recent_range < cfg.regime_recent_vol_atr_ratio * atr_5m
    ):
        return True, "regime:low_volatility"

    chop_frac = cfg.reference_min_distance_pct  # spec: ~0.0015 TWAP chop zone
    if dist is not None and chop_frac > 0 and dist < chop_frac:
        return True, "regime:twap_chop"

    # STEP 4–6 — extension (dist beyond reference_max): smart filter, not instant hard block
    max_ref = cfg.reference_max_distance_pct
    if dist is not None and max_ref > 0 and float(dist) > float(max_ref):
        sw5 = _swing([b.h for b in c5[-20:-1]], [b.low for b in c5[-20:-1]])
        if sw5 is None:
            return True, "regime:twap_extended_weak"
        prev_hi, prev_lo = sw5
        chase_hi = _breakout_chase_pct("BUY_CALL", float(last_price), prev_hi)
        chase_lo = _breakout_chase_pct("BUY_PUT", float(last_price), prev_lo)

        if twap is not None:
            if float(last_price) > twap:
                move_pct = chase_hi
            elif float(last_price) < twap:
                move_pct = chase_lo
            else:
                move_pct = max(chase_hi, chase_lo)
        else:
            move_pct = min(chase_hi, chase_lo)

        spike_ok = True
        if len(c5_closed) >= cfg.breakout_spike_avg_bars + 1:
            lb = c5_closed[-1]
            rng_last = lb.h - lb.low
            avg_r = _avg_candle_range_5m(c5_closed, cfg.breakout_spike_avg_bars)
            if avg_r is not None and avg_r > 0:
                spike_ok = rng_last <= cfg.breakout_spike_range_mult * avg_r

        fresh = move_pct < cfg.breakout_max_extension_pct
        adx_strong = adx_missing_raw or (adx_5m > cfg.regime_extension_adx_min)
        if not (fresh and spike_ok and adx_strong):
            return True, "regime:twap_extended_weak"

    return False, ""


_REGIME_DATA_WARMUP_REASONS: frozenset[str] = frozenset({"regime:missing_slope"})


def regime_data_inputs_ready(
    cfg: BrainConfig,
    agg: CandleAggregator,
    last_price: float | None,
) -> bool:
    """True when thin history does not block with ``regime:missing_slope`` only.

    Other regime vetoes (low ADX, chop, etc.) still mean indicators are *loaded*.
    """
    if not cfg.selective_entry_enabled:
        return True
    if last_price is None or last_price <= 0:
        return False
    blocked, reason = unified_regime_gate(cfg, agg, last_price)
    if not blocked:
        return True
    return reason not in _REGIME_DATA_WARMUP_REASONS


def pre_brain_regime_blocks(
    cfg: BrainConfig,
    agg: CandleAggregator,
    last_price: float | None,
    out_diagnostics: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Thin wrapper around :func:`unified_regime_gate` (tests / callers); scanner uses ``evaluate``."""
    return unified_regime_gate(cfg, agg, last_price, out_diagnostics)


def _fail_retest_breakout_state(agg: CandleAggregator) -> None:
    """Invalidate retest FSM + global signal anchor (failed / rolled forward)."""
    agg.active_retest_setup = None
    agg.last_retest_entry_meta = None
    agg.signal_created_closed_index = None


def _bar_index_for_ts(closed: list[Candle], ts: datetime) -> int | None:
    for i, c in enumerate(closed):
        if c.ts == ts:
            return i
    return None


def _strong_bearish_engulf(prev: Candle, cur: Candle, body_frac: float) -> bool:
    rng_p = max(1e-12, prev.h - prev.low)
    rng_c = max(1e-12, cur.h - cur.low)
    if prev.c <= prev.o or cur.c >= cur.o:
        return False
    pb = prev.c - prev.o
    cb = cur.o - cur.c
    return (
        cur.o >= prev.c
        and cur.c <= prev.o
        and cb / rng_c >= body_frac
        and pb / rng_p >= body_frac * 0.5
    )


def _strong_bullish_engulf(prev: Candle, cur: Candle, body_frac: float) -> bool:
    rng_p = max(1e-12, prev.h - prev.low)
    rng_c = max(1e-12, cur.h - cur.low)
    if prev.c >= prev.o or cur.c <= cur.o:
        return False
    pb = prev.o - prev.c
    cb = cur.c - cur.o
    return (
        cur.o <= prev.c
        and cur.c >= prev.o
        and cb / rng_c >= body_frac
        and pb / rng_p >= body_frac * 0.5
    )


def _retest_breakout_allow(
    cfg: BrainConfig,
    agg: CandleAggregator,
    *,
    side: str,
    swing_hi: float,
    swing_lo: float,
    c5_closed: list[Candle],
) -> tuple[bool, str | None, dict[str, Any]]:
    """Retest + confirmation on closed 5m bars only; mutates ``agg.active_retest_setup``."""
    extras: dict[str, Any] = {}
    if not cfg.selective_entry_enabled:
        return True, None, extras
    if len(c5_closed) < 2:
        return False, "retest:need_history", extras

    level = swing_hi if side == "BUY_CALL" else swing_lo
    if level <= 0:
        return False, "retest:bad_level", extras

    tol = cfg.retest_zone_tolerance_pct
    max_rw = cfg.retest_max_post_breakout_bars
    max_cf = cfg.retest_confirm_max_bars
    mx_rt = cfg.retest_max_retrace_fraction
    eb = cfg.retest_engulf_body_min

    st = agg.active_retest_setup
    if st is not None and st.side != side:
        return False, "retest:active_other_side", extras

    prev_b, last_b = c5_closed[-2], c5_closed[-1]

    def crosses_now() -> bool:
        if side == "BUY_CALL":
            return last_b.c > level and prev_b.c <= level
        return last_b.c < level and prev_b.c >= level

    if st is None:
        if crosses_now():
            br = max(1e-12, last_b.h - last_b.low)
            agg.last_retest_entry_meta = None
            agg.active_retest_setup = RetestBreakoutSetup(
                side=side,
                breakout_level=float(level),
                breakout_bucket_start=last_b.ts,
                breakout_range=br,
                breakout_hi=last_b.h,
                breakout_lo=last_b.low,
            )
            agg.signal_created_closed_index = len(c5_closed) - 1
            return False, "retest:breakout_armed", extras
        return False, "retest:no_setup_yet", extras

    bi = _bar_index_for_ts(c5_closed, st.breakout_bucket_start)
    if bi is None:
        _fail_retest_breakout_state(agg)
        return False, "retest:stale_rollforward", extras

    last_i = len(c5_closed) - 1
    age = last_i - bi
    R = max(1e-12, st.breakout_range)

    if age == 0:
        return False, "retest:on_breakout_bar", extras

    touch_seen = st.retest_touch_seen
    ext_lo = st.retest_extreme_lo
    ext_hi = st.retest_extreme_hi
    ft_idx = st.first_touch_index

    for j in range(bi + 1, last_i + 1):
        b = c5_closed[j]
        in_win = j <= bi + max_rw

        if side == "BUY_CALL":
            ext_lo = b.low if ext_lo is None else min(ext_lo, b.low)
            if b.c < st.breakout_level:
                _fail_retest_breakout_state(agg)
                return False, "retest:fake_breakout_close", extras
            if in_win:
                deep = (st.breakout_hi - b.low) / R
                if deep > mx_rt:
                    _fail_retest_breakout_state(agg)
                    return False, "retest:pullback_too_deep", extras
                if j > bi + 1:
                    if _strong_bearish_engulf(c5_closed[j - 1], b, eb):
                        _fail_retest_breakout_state(agg)
                        return False, "retest:bearish_engulfing", extras
            near = abs(b.low - st.breakout_level) / st.breakout_level <= tol
            if near and b.low < c5_closed[bi].c:
                touch_seen = True
                if ft_idx is None:
                    ft_idx = j
        else:
            ext_hi = b.h if ext_hi is None else max(ext_hi, b.h)
            if b.c > st.breakout_level:
                _fail_retest_breakout_state(agg)
                return False, "retest:fake_breakout_close", extras
            if in_win:
                deep = (b.h - st.breakout_lo) / R
                if deep > mx_rt:
                    _fail_retest_breakout_state(agg)
                    return False, "retest:rally_too_deep", extras
                if j > bi + 1:
                    if _strong_bullish_engulf(c5_closed[j - 1], b, eb):
                        _fail_retest_breakout_state(agg)
                        return False, "retest:bullish_engulfing", extras
            near = abs(b.h - st.breakout_level) / st.breakout_level <= tol
            if near and b.h > c5_closed[bi].c:
                touch_seen = True
                if ft_idx is None:
                    ft_idx = j

    st.retest_touch_seen = touch_seen
    st.retest_extreme_lo = ext_lo
    st.retest_extreme_hi = ext_hi
    st.first_touch_index = ft_idx

    if not touch_seen:
        if last_i > bi + max_rw:
            _fail_retest_breakout_state(agg)
            return False, "retest:expired_no_touch", extras
        return False, "retest:await_touch", extras

    assert ft_idx is not None
    if last_i > ft_idx + max_cf:
        _fail_retest_breakout_state(agg)
        return False, "retest:expired_no_confirm", extras

    if last_i < bi + 2:
        return False, "retest:await_confirm", extras

    cur = c5_closed[last_i]
    prv = c5_closed[last_i - 1]
    if side == "BUY_CALL":
        ok_go = cur.c > prv.h and cur.c > cur.o
        anchor = ext_lo if ext_lo is not None else cur.low
        extras["underlying_stop_anchor"] = anchor
        extras["underlying_stop_side"] = "below"
    else:
        ok_go = cur.c < prv.low and cur.c < cur.o
        anchor = ext_hi if ext_hi is not None else cur.h
        extras["underlying_stop_anchor"] = anchor
        extras["underlying_stop_side"] = "above"

    if not ok_go:
        return False, "retest:await_confirm", extras

    if ft_idx is not None and c5_closed[ft_idx].v > 0 and cur.v > 0 and cur.v >= c5_closed[ft_idx].v:
        extras["retest_volume_note"] = "confirm_vol_ge_retest"

    extras["retest_flow"] = "breakout_retest_v1"
    agg.last_retest_entry_meta = dict(extras)
    agg.active_retest_setup = None
    agg.signal_created_closed_index = None
    return True, None, extras


def _breakout_chase_pct(side: str, last_price: float, level: float) -> float:
    if level <= 0:
        return 0.0
    return abs(last_price - level) / level


def _penalized_rank_score(
    base: ScoreBreakdown,
    cfg: BrainConfig,
    *,
    chase_hi: float,
    chase_lo: float,
    rng_last5: float,
    avg_r: float | None,
) -> ScoreBreakdown:
    """Lower ranking score when extension/spike vs avg range is large (anti-chase)."""
    move = max(chase_hi, chase_lo)
    pen_m = cfg.rank_penalty_move_scale * move
    pen_s = 0.0
    if avg_r and avg_r > 0 and rng_last5 >= 0:
        pen_s = cfg.rank_penalty_spike_scale * max(0.0, rng_last5 / avg_r - 1.0)
    pen = min(0.95, pen_m + pen_s)
    adj = max(0.0, min(1.0, base.total - pen))
    inp = dict(base.inputs)
    inp["ranking_penalty"] = round(pen, 4)
    inp["raw_score_total"] = base.total
    return ScoreBreakdown(
        total=round(adj, 4),
        volatility=base.volatility,
        momentum=base.momentum,
        breakout=base.breakout,
        volume=base.volume,
        inputs=inp,
    )


def _entry_check_atr_breakout(
    cfg: BrainConfig,
    atr: float | None,
    last_price: float,
    level: float,
    label: str,
) -> EntryCheck:
    if atr is None or atr <= 0:
        if cfg.regime_fail_closed_indicators:
            return EntryCheck(
                "breakout_atr_band",
                False,
                "indicator:atr_missing",
            )
        return EntryCheck("breakout_atr_band", True, "ATR n/a — band skipped")
    sz = abs(last_price - level)
    ok = (sz >= cfg.breakout_atr_min_multiple * atr) and (sz <= cfg.breakout_atr_max_multiple * atr)
    return EntryCheck(
        "breakout_atr_band",
        ok,
        f"{label}: |price−level|={sz:.4f} ATR={atr:.4f} "
        f"(band {cfg.breakout_atr_min_multiple:.1f}–{cfg.breakout_atr_max_multiple:.1f}×)",
    )


# ----------------------------------------------------------------------
# the brain
# ----------------------------------------------------------------------


class BrainEngine:
    """Evaluator: candle state lives on ``CandleAggregator``; confirmed structure bias is kept here."""

    def __init__(self, config: BrainConfig | None = None) -> None:
        self.config = config or BrainConfig()
        self._market_bias: str = "NEUTRAL"

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

        def norm_band(x: float, lo: float, hi: float) -> float:
            """Linear map x from [lo, hi] to [0, 1], clamped (module-safe)."""
            if hi <= lo:
                return 0.0
            return max(0.0, min(1.0, (x - lo) / (hi - lo)))

        if last_price is None or last_price <= 0:
            return ScoreBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, {"reason": "no_price"})

        c1, c5, c15 = agg.all_candles_including_partial()

        # 1) volatility — intraday session range / price (movement potential)
        sess_range_pct = 0.0
        if agg.session_high is not None and agg.session_low is not None and last_price:
            sess_range_pct = (agg.session_high - agg.session_low) / last_price
        vol_norm = norm_band(sess_range_pct * 100.0, 0.10, 1.50)  # 0.1% .. 1.5%

        # 2) momentum — 5m close-to-close slope is now PRIMARY
        m5_closes = [b.c for b in c5[-5:]]
        slope5 = abs(_slope(m5_closes) or 0.0)
        mom_norm = norm_band(slope5 * 100.0, 0.05, 1.20)  # 0.05% .. 1.2%

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
        diag: dict[str, Any] = {}

        if last_price is None or last_price <= 0:
            score = self.score_instrument(last_price=last_price, agg=agg)
            diag["score_inputs"] = score.inputs
            return BrainOutput(score, Signal("NO_TRADE", "no_price", 0.0, []), diag)

        c1, c5, c15 = agg.all_candles_including_partial()

        # Warmup depth: full regime alignment only in selective mode (production).
        # Legacy/tests often disable selective — keep the lighter 5 / 3 bucket minimum there.
        sel = cfg.selective_entry_enabled
        req5 = MIN_REGIME_BUCKETS_5M if sel else 5
        req15 = MIN_REGIME_BUCKETS_15M if sel else 3
        # Need enough warmup for multi-timeframe rules. We also require at
        # least one 1m bar because the pattern-detection block below indexes
        # ``c1[-1]`` for the last 1m candle — historical-candle backfill
        # may seed only 5m + 15m for instruments with no 1m history.
        if len(c5) < req5 or len(c15) < req15 or len(c1) < 1:
            score = self.score_instrument(last_price=last_price, agg=agg)
            diag["score_inputs"] = score.inputs
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
                            f"need>={req5} 5m, >={req15} 15m, >=1 1m "
                            f"(have {len(c5)}/{len(c15)}/{len(c1)})",
                        )
                    ],
                ),
                diag,
            )

        c5_closed = agg.snapshot_lists()[1]
        if (
            cfg.selective_entry_enabled
            and cfg.regime_fail_closed_indicators
            and len(c5_closed) < MIN_REGIME_CLOSED_5M
        ):
            score = self.score_instrument(last_price=last_price, agg=agg)
            diag["score_inputs"] = score.inputs
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
                            f"need>={MIN_REGIME_CLOSED_5M} closed 5m bars for regime (have {len(c5_closed)})",
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
            score = self.score_instrument(last_price=last_price, agg=agg)
            diag["score_inputs"] = score.inputs
            return BrainOutput(
                score,
                Signal("NO_TRADE", f"filter:{failed.name}", 0.0, filters),
                diag,
            )

        # Optional: reject dead sessions with tiny intraday range (% of price).
        if (
            cfg.min_session_range_pct_breakout > 0
            and sess_range_pct * 100.0 < cfg.min_session_range_pct_breakout
        ):
            score = self.score_instrument(last_price=last_price, agg=agg)
            diag["score_inputs"] = score.inputs
            return BrainOutput(
                score,
                Signal(
                    "NO_TRADE",
                    "regime:low_session_range",
                    0.0,
                    [
                        EntryCheck(
                            "session_range_ok",
                            False,
                            f"range {sess_range_pct * 100.0:.3f}% < min "
                            f"{cfg.min_session_range_pct_breakout:.3f}%",
                        )
                    ],
                ),
                diag,
            )

        # ---- unified regime gate (STEP 1–7); scanner does not duplicate this ----
        if cfg.selective_entry_enabled:
            regime_block, regime_reason = unified_regime_gate(cfg, agg, last_price, diag)
            if regime_block:
                score = self.score_instrument(last_price=last_price, agg=agg)
                diag["score_inputs"] = score.inputs
                return BrainOutput(
                    score,
                    Signal(
                        "NO_TRADE",
                        regime_reason,
                        0.0,
                        filters
                        + [
                            EntryCheck(
                                "unified_regime_gate",
                                False,
                                regime_reason,
                            )
                        ],
                    ),
                    diag,
                )

        sw5 = _swing([b.h for b in c5[-20:-1]], [b.low for b in c5[-20:-1]])
        if sw5 is None:
            score = self.score_instrument(last_price=last_price, agg=agg)
            diag["score_inputs"] = score.inputs
            return BrainOutput(score, Signal("NO_TRADE", "no_swing", 0.0, filters), diag)
        prev_swing_hi, prev_swing_lo = sw5

        chase_hi_pre = _breakout_chase_pct("BUY_CALL", float(last_price), prev_swing_hi)
        chase_lo_pre = _breakout_chase_pct("BUY_PUT", float(last_price), prev_swing_lo)
        if max(chase_hi_pre, chase_lo_pre) > cfg.breakout_max_extension_pct:
            score = ScoreBreakdown(
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                {
                    "reason": "entry:late_extension",
                    "extension_precheck_max_pct": max(chase_hi_pre, chase_lo_pre) * 100.0,
                },
            )
            diag["score_inputs"] = score.inputs
            return BrainOutput(
                score,
                Signal(
                    "NO_TRADE",
                    "entry:late_extension",
                    0.0,
                    filters
                    + [
                        EntryCheck(
                            "extension_precheck",
                            False,
                            f"max extension {max(chase_hi_pre, chase_lo_pre) * 100.0:.4f}% "
                            f"> cap {cfg.breakout_max_extension_pct * 100.0:.4f}%",
                        )
                    ],
                ),
                diag,
            )

        if cfg.signal_max_age_closed_bars > 0 and agg.signal_created_closed_index is not None:
            cur_i = len(c5_closed) - 1
            age_bars = cur_i - int(agg.signal_created_closed_index)
            if age_bars > cfg.signal_max_age_closed_bars:
                _fail_retest_breakout_state(agg)
                score = self.score_instrument(last_price=last_price, agg=agg)
                diag["score_inputs"] = score.inputs
                return BrainOutput(
                    score,
                    Signal(
                        "NO_TRADE",
                        "signal:expired",
                        0.0,
                        filters
                        + [
                            EntryCheck(
                                "signal_age",
                                False,
                                f"pending signal age {age_bars} closed bars "
                                f"> max {cfg.signal_max_age_closed_bars}",
                            )
                        ],
                    ),
                    diag,
                )

        score = self.score_instrument(last_price=last_price, agg=agg)
        diag["score_inputs"] = score.inputs

        # ---- multi-timeframe context ----
        m15_closes = [b.c for b in c15[-5:]]
        m5_closes = [b.c for b in c5[-5:]]

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

        adx_5m = wilder_adx(c5_closed, period=cfg.regime_adx_period)
        twap_dev = None
        twap_dist_vs_twap: float | None = None
        if twap is not None and last_price and last_price > 0:
            twap_dev = abs(last_price - twap) / last_price
            if twap > 0:
                twap_dist_vs_twap = abs(last_price - twap) / twap

        diag.update(
            {
                "slope_15m": round(slope_15m * 100.0, 4),
                "slope_5m": round(slope_5m * 100.0, 4),
                "ret_1": round(ret_1 * 100.0, 4),
                "twap": twap,
                "twap_deviation_pct": round((twap_dev or 0.0) * 100.0, 5) if twap_dev is not None else None,
                "twap_distance_vs_twap_pct": round((twap_dist_vs_twap or 0.0) * 100.0, 5)
                if twap_dist_vs_twap is not None
                else None,
                "adx_5m": round(adx_5m, 3) if adx_5m is not None else None,
                "swing_hi_5m": prev_swing_hi,
                "swing_lo_5m": prev_swing_lo,
                "chop": chop,
            }
        )

        # 5m PRIMARY trend gate; 15m bias-only ("not against").
        c5_recent = c5[-(cfg.pullback_min_uptrend_bars + 1) :]
        uptrend_bars = sum(1 for b in c5_recent[:-1] if b.c > b.o)
        downtrend_bars = sum(1 for b in c5_recent[:-1] if b.c < b.o)

        retracement = (
            (prev_swing_hi - last_price) / prev_swing_hi if prev_swing_hi else 0.0
        )
        retracement_dn = (
            (last_price - prev_swing_lo) / prev_swing_lo if prev_swing_lo else 0.0
        )

        cont_recent = c5[-cfg.continuation_consolidation_bars :]
        if cont_recent:
            cons_hi = max(b.h for b in cont_recent)
            cons_lo = min(b.low for b in cont_recent)
            cons_range_pct = (cons_hi - cons_lo) / cons_hi if cons_hi else 0.0
        else:
            cons_hi = cons_lo = 0.0
            cons_range_pct = 0.0

        dir_eps = cfg.directional_15m_eps
        if cfg.selective_entry_enabled and cfg.directional_bias_enabled:
            # Strict TWAP side (STEP 5): calls only above TWAP, puts only below; 15m slope must align.
            if twap is not None and twap > 0:
                allow_call = last_price > twap and slope_15m >= dir_eps
                allow_put = last_price < twap and slope_15m <= -dir_eps
            else:
                allow_call = slope_15m >= dir_eps
                allow_put = slope_15m <= -dir_eps
        else:
            allow_call = allow_put = True

        allow_call_directional = allow_call
        allow_put_directional = allow_put

        raw_structure_bias = _detect_structure_bias(c5_closed)
        self._market_bias = _confirm_structure_bias(c5_closed, self._market_bias)
        diag["structure_bias_raw"] = raw_structure_bias
        diag["structure_bias"] = self._market_bias

        structure_blocked_call = False
        structure_blocked_put = False
        if cfg.structure_bias_filter_enabled and cfg.selective_entry_enabled:
            if self._market_bias == "BEARISH":
                if allow_call:
                    structure_blocked_call = True
                allow_call = False
            elif self._market_bias == "BULLISH":
                if allow_put:
                    structure_blocked_put = True
                allow_put = False

        diag["allow_call"] = allow_call
        diag["allow_put"] = allow_put

        chase_hi = _breakout_chase_pct("BUY_CALL", last_price, prev_swing_hi)
        chase_lo = _breakout_chase_pct("BUY_PUT", last_price, prev_swing_lo)

        avg_r_spike: float | None = None
        rng_last5 = 0.0
        spike_vs_avg_ok = True
        spike_detail = "need more 5m history for spike filter"
        if len(c5_closed) >= cfg.breakout_spike_avg_bars + 1:
            lb = c5_closed[-1]
            rng_last5 = lb.h - lb.low
            avg_r_spike = _avg_candle_range_5m(c5_closed, cfg.breakout_spike_avg_bars)
            if avg_r_spike and avg_r_spike > 0:
                spike_vs_avg_ok = rng_last5 <= cfg.breakout_spike_range_mult * avg_r_spike
                spike_detail = (
                    f"last 5m range {rng_last5:.4f} vs avg({cfg.breakout_spike_avg_bars}) "
                    f"{avg_r_spike:.4f} (reject if range > {cfg.breakout_spike_range_mult}x avg)"
                )

        atr_last = wilder_atr(c5_closed, period=cfg.atr_period)
        score_out = _penalized_rank_score(
            score,
            cfg,
            chase_hi=chase_hi,
            chase_lo=chase_lo,
            rng_last5=rng_last5,
            avg_r=avg_r_spike,
        )
        diag["ranking_total"] = score_out.total
        if atr_last is not None:
            diag["atr_5m"] = round(atr_last, 4)

        if cfg.min_brain_score_0_100 > 0 and score_out.total * 100.0 < cfg.min_brain_score_0_100:
            return BrainOutput(
                score_out,
                Signal(
                    "NO_TRADE",
                    "score:below_min",
                    0.0,
                    [
                        EntryCheck(
                            "min_brain_score",
                            False,
                            f"ranking score {score_out.total * 100.0:.1f} < {cfg.min_brain_score_0_100:.0f}",
                        )
                    ],
                ),
                diag,
            )

        eps_htf = cfg.min_htf_slope_eps
        if cfg.require_strict_htf_trend:
            call_15m_entry = EntryCheck(
                "htf_trend_align",
                slope_15m > eps_htf,
                f"15m slope {slope_15m * 100.0:.4f}% must be > 0 for CALL breakout",
            )
            put_15m_entry = EntryCheck(
                "htf_trend_align",
                slope_15m < -eps_htf,
                f"15m slope {slope_15m * 100.0:.4f}% must be < 0 for PUT breakout",
            )
        else:
            call_15m_entry = EntryCheck(
                "trend_15m_not_against",
                slope_15m >= -cfg.min_15m_trend_slope,
                f"15m slope {slope_15m * 100.0:.3f}% (bias gate)",
            )
            put_15m_entry = EntryCheck(
                "trend_15m_not_against",
                slope_15m <= cfg.min_15m_trend_slope,
                f"15m slope {slope_15m * 100.0:.3f}% (bias gate)",
            )

        breakout_floor_call = prev_swing_hi * (1.0 - cfg.near_breakout_clearance)
        strict_call = (
            cfg.selective_entry_enabled
            and cfg.strict_swing_break_eps > 0
            and last_price >= prev_swing_hi * (1.0 + cfg.strict_swing_break_eps)
        )
        loose_call = last_price >= breakout_floor_call
        call_breakout_ok = strict_call if (cfg.selective_entry_enabled and cfg.strict_swing_break_eps > 0) else loose_call
        if cfg.selective_entry_enabled and cfg.strict_swing_break_eps > 0:
            br_detail = (
                f"strict break {last_price:.2f} vs swingHi {prev_swing_hi:.2f} "
                f"(>= {(1.0 + cfg.strict_swing_break_eps) * 100.0:.3f}% hi)"
            )
        else:
            br_detail = (
                f"price {last_price:.2f} vs swingHi {prev_swing_hi:.2f} (near-breakout band)"
            )

        call_breakout_checks = [
            *filters,
            EntryCheck(
                "trend_5m_up",
                slope_5m >= cfg.min_5m_trend_slope,
                f"5m slope {slope_5m * 100.0:.3f}% (min {cfg.min_5m_trend_slope * 100.0:.3f}%)",
            ),
            call_15m_entry,
            EntryCheck(
                "breakout_extension_ok",
                chase_hi <= cfg.breakout_max_extension_pct,
                f"extension from swingHi {chase_hi * 100.0:.3f}% (max "
                f"{cfg.breakout_max_extension_pct * 100.0:.3f}%)",
            ),
            EntryCheck(
                "breakout_distance_ok",
                chase_hi <= cfg.breakout_max_distance_from_level_pct,
                f"distance from swingHi {chase_hi * 100.0:.3f}% (max "
                f"{cfg.breakout_max_distance_from_level_pct * 100.0:.3f}%)",
            ),
            EntryCheck(
                "breakout_spike_candle_ok",
                spike_vs_avg_ok,
                spike_detail,
            ),
            _entry_check_atr_breakout(cfg, atr_last, last_price, prev_swing_hi, "CALL"),
            EntryCheck(
                "breakout_5m",
                call_breakout_ok,
                br_detail,
            ),
            EntryCheck("bullish_1m_close", bullish_1m, "last 1m candle bullish + body>=45%"),
            EntryCheck(
                "not_late",
                ret_1 <= cfg.max_late_entry_pct,
                f"1m return {ret_1 * 100.0:.3f}% (max {cfg.max_late_entry_pct * 100.0:.3f}%)",
            ),
        ]

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

        breakdown_ceiling_put = prev_swing_lo * (1.0 + cfg.near_breakout_clearance)
        strict_put = (
            cfg.selective_entry_enabled
            and cfg.strict_swing_break_eps > 0
            and last_price <= prev_swing_lo * (1.0 - cfg.strict_swing_break_eps)
        )
        loose_put = last_price <= breakdown_ceiling_put
        put_breakdown_ok = strict_put if (cfg.selective_entry_enabled and cfg.strict_swing_break_eps > 0) else loose_put
        if cfg.selective_entry_enabled and cfg.strict_swing_break_eps > 0:
            bd_detail = (
                f"strict break {last_price:.2f} vs swingLo {prev_swing_lo:.2f} "
                f"(<= lo * {1.0 - cfg.strict_swing_break_eps:.5f})"
            )
        else:
            bd_detail = (
                f"price {last_price:.2f} vs swingLo {prev_swing_lo:.2f} (near-breakdown band)"
            )

        put_breakdown_checks = [
            *filters,
            EntryCheck(
                "trend_5m_down",
                slope_5m <= -cfg.min_5m_trend_slope,
                f"5m slope {slope_5m * 100.0:.3f}% (max -{cfg.min_5m_trend_slope * 100.0:.3f}%)",
            ),
            put_15m_entry,
            EntryCheck(
                "breakout_extension_ok",
                chase_lo <= cfg.breakout_max_extension_pct,
                f"extension from swingLo {chase_lo * 100.0:.3f}% (max "
                f"{cfg.breakout_max_extension_pct * 100.0:.3f}%)",
            ),
            EntryCheck(
                "breakout_distance_ok",
                chase_lo <= cfg.breakout_max_distance_from_level_pct,
                f"distance from swingLo {chase_lo * 100.0:.3f}% (max "
                f"{cfg.breakout_max_distance_from_level_pct * 100.0:.3f}%)",
            ),
            EntryCheck(
                "breakout_spike_candle_ok",
                spike_vs_avg_ok,
                spike_detail,
            ),
            _entry_check_atr_breakout(cfg, atr_last, last_price, prev_swing_lo, "PUT"),
            EntryCheck(
                "breakdown_5m",
                put_breakdown_ok,
                bd_detail,
            ),
            EntryCheck("bearish_1m_close", bearish_1m, "last 1m candle bearish + body>=45%"),
            EntryCheck(
                "not_late",
                ret_1 >= -cfg.max_late_entry_pct,
                f"1m return {ret_1 * 100.0:.3f}% (max -{cfg.max_late_entry_pct * 100.0:.3f}%)",
            ),
        ]

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

        candidates: list[tuple[str, str, list[EntryCheck], str]] = []
        if allow_call:
            candidates.append(
                ("breakout", "BUY_CALL", call_breakout_checks, "uptrend_breakout_confirmed")
            )
            if cfg.enable_pullback_patterns:
                candidates.append(
                    ("pullback", "BUY_CALL", call_pullback_checks, "uptrend_pullback_bounce")
                )
            if cfg.enable_continuation_patterns:
                candidates.append(
                    (
                        "continuation",
                        "BUY_CALL",
                        call_continuation_checks,
                        "uptrend_continuation_resume",
                    )
                )
            if cfg.enable_scalp_patterns:
                candidates.append(
                    ("scalp", "BUY_CALL", call_scalp_checks, "scalp_call_5m_momentum")
                )
        if allow_put:
            candidates.append(
                ("breakout", "BUY_PUT", put_breakdown_checks, "downtrend_breakdown_confirmed")
            )
            if cfg.enable_pullback_patterns:
                candidates.append(
                    ("pullback", "BUY_PUT", put_pullback_checks, "downtrend_pullback_rejection")
                )
            if cfg.enable_scalp_patterns:
                candidates.append(
                    ("scalp", "BUY_PUT", put_scalp_checks, "scalp_put_5m_momentum")
                )

        if cfg.structure_bias_filter_enabled and cfg.selective_entry_enabled:
            filtered_candidates: list[tuple[str, str, list[EntryCheck], str]] = []
            for row in candidates:
                pat, side, checks, reason = row
                if side == "BUY_CALL" and not allow_call:
                    continue
                if side == "BUY_PUT" and not allow_put:
                    continue
                filtered_candidates.append(row)
            candidates = filtered_candidates

        if not candidates:
            no_side_reason = "directional:no_side"
            if cfg.structure_bias_filter_enabled and cfg.selective_entry_enabled:
                if structure_blocked_call or structure_blocked_put:
                    no_side_reason = f"bias_block:{self._market_bias}"
            return BrainOutput(
                score_out,
                Signal(
                    "NO_TRADE",
                    no_side_reason,
                    0.0,
                    filters,
                    "other",
                    {
                        "allow_call": allow_call,
                        "allow_put": allow_put,
                        "allow_call_directional": allow_call_directional,
                        "allow_put_directional": allow_put_directional,
                        "structure_bias": self._market_bias,
                    },
                ),
                diag,
            )

        structure_common: dict[str, Any] = {
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
            "weighted_pattern_score": None,
            "allow_call": allow_call,
            "allow_put": allow_put,
        }

        scored_rows: list[tuple[str, str, list[EntryCheck], str, int, float, float]] = []
        for pat, side, checks, reason in candidates:
            passed = sum(1 for c in checks if c.ok)
            wscore = _weighted_checklist_score(checks, cfg)
            chase = (
                chase_hi
                if pat == "breakout" and side == "BUY_CALL"
                else chase_lo
                if pat == "breakout" and side == "BUY_PUT"
                else 0.0
            )
            scored_rows.append((pat, side, checks, reason, passed, wscore, chase))

        if not cfg.selective_entry_enabled:
            scored_rows.sort(key=lambda t: (-t[4], -t[5], -t[6]))
            for pat, side, checks, reason, passed, _w, _ch in scored_rows:
                if passed == len(checks):
                    blob = {**structure_common, "pattern": pat, "weighted_pattern_score": 1.0}
                    return BrainOutput(
                        score_out,
                        Signal(side, reason, 1.0, checks, pat, blob),
                        diag,
                    )
            best = scored_rows[0]
            pat, side, checks, reason, passed, wsc, _ch = best
            return BrainOutput(
                score_out,
                Signal(
                    "NO_TRADE",
                    f"setup_quality_partial_{passed}_of_{len(checks)}",
                    passed / max(1, len(checks)),
                    checks,
                    pat,
                    {
                        "uptrend_bars_5m": uptrend_bars,
                        "downtrend_bars_5m": downtrend_bars,
                        "retracement_pct": round(retracement * 100.0, 3),
                        "late_entry_1m_pct": round(ret_1 * 100.0, 3),
                    },
                ),
                diag,
            )

        full_pool = [
            t
            for t in scored_rows
            if t[4] == len(t[2]) and t[5] >= cfg.min_pattern_score
        ]
        partial_pool = [
            t
            for t in scored_rows
            if t[0] != "breakout"
            and t[4] < len(t[2])
            and (t[4] / max(1, len(t[2]))) >= cfg.min_pattern_check_ratio
            and t[5] >= cfg.min_pattern_score
        ]

        confirmed_full: list[tuple[str, str, list[EntryCheck], str, int, float, float]] = []
        for t in full_pool:
            pat, side, checks, reason, passed, wsc, chase = t
            if pat != "breakout":
                confirmed_full.append(t)
                continue
            allow, msg, retest_x = _retest_breakout_allow(
                cfg,
                agg,
                side=side,
                swing_hi=prev_swing_hi,
                swing_lo=prev_swing_lo,
                c5_closed=c5_closed,
            )
            if not allow:
                return BrainOutput(
                    score_out,
                    Signal(
                        "NO_TRADE",
                        msg or "retest:blocked",
                        0.0,
                        checks,
                        pat,
                        {
                            **structure_common,
                            **retest_x,
                            "pattern": pat,
                            "retest_gate": msg,
                        },
                    ),
                    diag,
                )
            confirmed_full.append(t)

        full_pool = confirmed_full

        pick_pool = full_pool if full_pool else partial_pool

        if pick_pool:
            pat, side, checks, reason, passed, wsc, chase = max(
                pick_pool,
                key=lambda t: _pick_rank_tuple(t[5], t[6], t[0]),
            )
            conf = 1.0 if passed == len(checks) else passed / max(1, len(checks))
            rex: dict[str, Any] = {}
            if pat == "breakout" and agg.last_retest_entry_meta:
                rex = dict(agg.last_retest_entry_meta)
            agg.last_retest_entry_meta = None
            blob = {**structure_common, **rex, "pattern": pat, "weighted_pattern_score": round(wsc, 4)}
            return BrainOutput(
                score_out,
                Signal(side, reason, conf, checks, pat, blob),
                diag,
            )

        scored_rows.sort(key=lambda t: (-t[4], -t[5], -t[6]))
        best = scored_rows[0]
        pat, side, checks, reason, passed, wsc, _chx = best
        return BrainOutput(
            score_out,
            Signal(
                "NO_TRADE",
                f"setup_quality_partial_{passed}_of_{len(checks)}",
                passed / max(1, len(checks)),
                checks,
                pat,
                {
                    "uptrend_bars_5m": uptrend_bars,
                    "downtrend_bars_5m": downtrend_bars,
                    "retracement_pct": round(retracement * 100.0, 3),
                    "late_entry_1m_pct": round(ret_1 * 100.0, 3),
                    "weighted_pattern_score": round(wsc, 4),
                },
            ),
            diag,
        )
