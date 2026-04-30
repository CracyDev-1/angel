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
    # Live broker cash, refreshed by the runtime each loop. Used only when
    # RISK_CAPITAL_RUPEES is 0 (= "use broker cash").
    broker_available_cash: float = 0.0


class RiskEngine:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.state = RiskState()

    def sync_from_store(self, store: StateStore) -> None:
        trades, pnl = store.get_daily_stats()
        self.state.trades_today = trades
        self.state.realized_pnl_today = pnl

    def set_broker_cash(self, cash: float) -> None:
        """Runtime calls this once per loop with the latest broker cash."""
        self.state.broker_available_cash = max(0.0, float(cash or 0.0))

    def effective_capital(self) -> float:
        """Capital base for sizing + daily loss cap.

        Resolves in priority order:
          1. Settings.risk_capital_rupees (if > 0)  — explicit user override
          2. Live broker cash                       — auto from RMS
          3. 0                                      — sizing returns 0 lots
        """
        cfg = float(self.settings.risk_capital_rupees or 0.0)
        if cfg > 0:
            return cfg
        return self.state.broker_available_cash

    def evaluate_new_trade(self, *, entry: float, stop: float, lot_size: int) -> RiskDecision:
        s = self.settings
        if s.risk_one_position_at_a_time and self.state.has_open_position:
            return RiskDecision(False, 0, "open_position")
        if self.state.trades_today >= s.risk_max_trades_per_day:
            return RiskDecision(False, 0, "max_trades")
        capital = self.effective_capital()
        if capital <= 0:
            return RiskDecision(False, 0, "no_capital")
        loss_cap = -capital * (s.risk_max_daily_loss_pct / 100.0)
        if self.state.realized_pnl_today <= loss_cap:
            return RiskDecision(False, 0, "max_daily_loss")

        qty = position_size_for_stop(
            capital=capital,
            risk_pct=s.risk_per_trade_pct,
            entry=entry,
            stop=stop,
            lot_size=lot_size,
        )
        if qty <= 0:
            return RiskDecision(False, 0, "zero_qty")
        return RiskDecision(True, qty, "ok")
