from __future__ import annotations

from dataclasses import dataclass

from angel_bot.config import Settings, get_settings
from angel_bot.state.store import StateStore


@dataclass
class RiskDecision:
    allowed: bool
    quantity: int
    reason: str


def position_size_for_stop(
    *,
    capital: float,
    risk_pct: float,
    entry: float,
    stop: float,
    lot_size: int,
) -> int:
    """Risk-based sizing from stop distance; rounds down to whole lots."""
    risk_rupees = capital * (risk_pct / 100.0)
    per_unit = abs(entry - stop)
    if per_unit <= 0:
        return 0
    qty_float = risk_rupees / per_unit
    lots = int(qty_float // lot_size)
    return max(0, lots * lot_size)


@dataclass
class RiskState:
    trades_today: int = 0
    realized_pnl_today: float = 0.0
    has_open_position: bool = False


class RiskEngine:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.state = RiskState()

    def sync_from_store(self, store: StateStore) -> None:
        trades, pnl = store.get_daily_stats()
        self.state.trades_today = trades
        self.state.realized_pnl_today = pnl

    def evaluate_new_trade(self, *, entry: float, stop: float, lot_size: int) -> RiskDecision:
        s = self.settings
        if s.risk_one_position_at_a_time and self.state.has_open_position:
            return RiskDecision(False, 0, "open_position")
        if self.state.trades_today >= s.risk_max_trades_per_day:
            return RiskDecision(False, 0, "max_trades")
        loss_cap = -s.risk_capital_rupees * (s.risk_max_daily_loss_pct / 100.0)
        if self.state.realized_pnl_today <= loss_cap:
            return RiskDecision(False, 0, "max_daily_loss")

        qty = position_size_for_stop(
            capital=s.risk_capital_rupees,
            risk_pct=s.risk_per_trade_pct,
            entry=entry,
            stop=stop,
            lot_size=lot_size,
        )
        if qty <= 0:
            return RiskDecision(False, 0, "zero_qty")
        return RiskDecision(True, qty, "ok")
