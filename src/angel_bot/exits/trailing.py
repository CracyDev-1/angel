"""Trailing stop for long option premium (CE/PE BUY).

Ratchet rule (exit layer only; entry unchanged):
  peak = max(peak, last_price)
  After arm threshold: trail_stop = peak * (1 - trail_pct)
  stop = max(initial_stop, trail_stop, current_stop) — never loosen below initial.
"""

from __future__ import annotations


def trailing_stop_update_long_premium(
    *,
    enabled: bool,
    trail_pct: float,
    arm_profit_pct: float,
    entry: float,
    last_price: float,
    initial_stop: float,
    peak: float,
    current_stop: float,
) -> tuple[float, float]:
    """Return (new_peak, new_working_stop).

    When disabled or invalid inputs, returns (peak, current_stop) unchanged.
    """
    if not enabled:
        return peak, current_stop
    if entry <= 0 or last_price <= 0 or initial_stop <= 0:
        return peak, current_stop
    tr = max(0.0, min(0.95, float(trail_pct)))
    arm = max(0.0, float(arm_profit_pct))

    new_peak = max(float(peak), float(last_price))
    arm_level = float(entry) * (1.0 + arm)
    if float(last_price) < arm_level:
        return new_peak, float(current_stop)

    trail_stop = new_peak * (1.0 - tr)
    candidate = max(float(initial_stop), trail_stop)
    new_stop = max(float(current_stop), candidate)
    return new_peak, new_stop
