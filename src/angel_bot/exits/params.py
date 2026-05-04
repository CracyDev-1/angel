"""Dynamic exit parameters for long option premium (CE/PE BUY).

When ``EXIT_DYNAMIC_ENABLED`` is true, :func:`resolve_exit_plan` is the single
source of truth for SL/TP/max-hold — the same values must feed risk sizing,
paper opens, and live exit plans (see ``runtime.TradingRuntime._consider_trade``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from angel_bot.config import Settings
from angel_bot.scanner.engine import ScannerHit


@dataclass(frozen=True)
class ExitPlan:
    """Resolved fractions of premium + minutes (intraday options)."""

    stop_loss_pct: float
    take_profit_pct: float
    max_hold_minutes: int
    meta: dict[str, Any] = field(default_factory=dict)


def resolve_exit_plan(hit: ScannerHit, settings: Settings) -> ExitPlan | None:
    """Map brain score + score_breakdown volatility/momentum to SL/TP/hold.

    Returns None only when optional ultra-low-vol skip fires (disabled by default).
    """
    s = settings
    bd: dict[str, Any] = dict(hit.score_breakdown or {})
    score = float(hit.score or 0.0)
    vol = float(bd.get("volatility") or 0.5)
    mom = float(bd.get("momentum") or 0.5)

    if s.exit_dynamic_skip_ultra_low_vol and vol < float(s.exit_dynamic_vol_ultra_low):
        return None

    sm = float(s.exit_dynamic_score_mid)
    sh = float(s.exit_dynamic_score_high)
    if score < sm:
        tier = "weak"
        sl = float(s.exit_dynamic_sl_weak)
        tp = float(s.exit_dynamic_tp_weak)
        mh = int(s.exit_dynamic_hold_weak)
    elif score < sh:
        tier = "mid"
        sl = float(s.exit_dynamic_sl_mid)
        tp = float(s.exit_dynamic_tp_mid)
        mh = int(s.exit_dynamic_hold_mid)
    else:
        tier = "strong"
        sl = float(s.exit_dynamic_sl_strong)
        tp = float(s.exit_dynamic_tp_strong)
        mh = int(s.exit_dynamic_hold_strong)

    v_low = float(s.exit_dynamic_vol_low)
    v_high = float(s.exit_dynamic_vol_high)
    vol_band = "mid"
    if vol < v_low:
        vol_band = "low"
        tp *= float(s.exit_dynamic_vol_low_tp_factor)
        mh = max(int(s.exit_dynamic_hold_min), mh - int(s.exit_dynamic_vol_low_hold_trim))
    elif vol > v_high:
        vol_band = "high"
        sl = min(
            float(s.exit_dynamic_sl_max),
            sl + float(s.exit_dynamic_vol_high_sl_add),
        )
        tp = min(
            float(s.exit_dynamic_tp_max),
            tp + float(s.exit_dynamic_vol_high_tp_add),
        )

    if mom >= float(s.exit_dynamic_momentum_high):
        mh = min(mh, int(s.exit_dynamic_hold_momentum_cap))

    sl = max(0.05, min(sl, float(s.exit_dynamic_sl_max)))
    tp = max(sl * 1.05, min(tp, float(s.exit_dynamic_tp_max)))
    mh = max(int(s.exit_dynamic_hold_min), min(mh, int(s.exit_dynamic_hold_max)))

    meta = {
        "source": "dynamic",
        "tier": tier,
        "vol_band": vol_band,
        "score": round(score, 4),
        "volatility": round(vol, 4),
        "momentum": round(mom, 4),
    }
    return ExitPlan(
        stop_loss_pct=round(sl, 4),
        take_profit_pct=round(tp, 4),
        max_hold_minutes=mh,
        meta=meta,
    )
