"""Technical indicators derived from OHLC candles (used by BrainEngine)."""

from __future__ import annotations

from angel_bot.market_data.candles import Candle


def _wilder_smooth(values: list[float], length: int) -> list[float]:
    """Wilder's smoothing: seed = SMA(first `length` values), then
    S_i = (S_{i-1} * (length - 1) + x_i) / length.
    """
    L = max(1, int(length))
    if len(values) < L:
        return []
    out: list[float] = []
    seed = sum(values[:L]) / L
    out.append(seed)
    for i in range(L, len(values)):
        prev = out[-1]
        out.append((prev * (L - 1) + values[i]) / L)
    return out


def wilder_atr(candles: list[Candle], *, period: int = 14) -> float | None:
    """Wilder ATR on closed candles (e.g. 5m). Last value in price units."""
    p = max(2, int(period))
    if len(candles) < p + 1:
        return None
    tr_list: list[float] = []
    for i in range(1, len(candles)):
        cur = candles[i]
        prev = candles[i - 1]
        tr = max(
            cur.h - cur.low,
            abs(cur.h - prev.c),
            abs(cur.low - prev.c),
        )
        tr_list.append(tr)
    atr_s = _wilder_smooth(tr_list, p)
    if not atr_s:
        return None
    return float(atr_s[-1])


def wilder_adx(candles: list[Candle], *, period: int = 14) -> float | None:
    """Wilder ADX on a single timeframe (e.g. closed 5m bars).

    Returns None if insufficient history or degenerate input.
    """
    p = max(2, int(period))
    if len(candles) < p * 2:
        return None

    tr_list: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []

    for i in range(1, len(candles)):
        cur = candles[i]
        prev = candles[i - 1]
        tr = max(
            cur.h - cur.low,
            abs(cur.h - prev.c),
            abs(cur.low - prev.c),
        )
        up_move = cur.h - prev.h
        down_move = prev.low - cur.low
        p_dm = up_move if up_move > down_move and up_move > 0 else 0.0
        m_dm = down_move if down_move > up_move and down_move > 0 else 0.0
        tr_list.append(tr)
        plus_dm.append(p_dm)
        minus_dm.append(m_dm)

    if len(tr_list) < p:
        return None

    atr_s = _wilder_smooth(tr_list, p)
    p_s = _wilder_smooth(plus_dm, p)
    m_s = _wilder_smooth(minus_dm, p)
    if not atr_s or not p_s or not m_s:
        return None
    n = min(len(atr_s), len(p_s), len(m_s))
    if n < 1:
        return None

    dx_vals: list[float] = []
    for i in range(n):
        atr = atr_s[i]
        if atr <= 0:
            continue
        pdi = 100.0 * p_s[i] / atr
        mdi = 100.0 * m_s[i] / atr
        denom = pdi + mdi
        if denom <= 0:
            continue
        dx_vals.append(100.0 * abs(pdi - mdi) / denom)

    if len(dx_vals) < p:
        return None
    adx_s = _wilder_smooth(dx_vals, p)
    if not adx_s:
        return None
    return float(adx_s[-1])
