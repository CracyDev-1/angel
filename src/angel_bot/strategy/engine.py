from __future__ import annotations

from enum import Enum

from angel_bot.features.engine import FeatureSnapshot


class Signal(str, Enum):
    BUY_CALL = "BUY_CALL"
    BUY_PUT = "BUY_PUT"
    NO_TRADE = "NO_TRADE"


def evaluate_rules(features: FeatureSnapshot) -> Signal:
    """
    Rule-based bias: trend + breakout/breakdown + chop / late-entry filters.
    Tune thresholds for your product (index vs options).
    """
    if features.last_price is None:
        return Signal.NO_TRADE

    if features.chop_score is not None and features.chop_score > 0.55:
        return Signal.NO_TRADE

    if features.range_market and not (features.breakout or features.breakdown):
        return Signal.NO_TRADE

    if features.breakout and (features.momentum or 0) > 0 and features.trend_up:
        if features.ret_1 is not None and features.ret_1 > 0.003:
            return Signal.NO_TRADE
        return Signal.BUY_CALL

    if features.breakdown and (features.momentum or 0) < 0 and features.trend_down:
        if features.ret_1 is not None and features.ret_1 < -0.003:
            return Signal.NO_TRADE
        return Signal.BUY_PUT

    return Signal.NO_TRADE
