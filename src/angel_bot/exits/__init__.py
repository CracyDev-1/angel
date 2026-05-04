"""Position-exit management for live trading.

Paper trading already enforces tight stop-loss / take-profit / max-hold via
``PaperTrader.mark_and_close``. The live counterpart lives in :mod:`live` and
sends real market reverse orders for positions the bot opened.
"""

from angel_bot.exits.live import (
    LiveExitConfig,
    LiveExitEvent,
    LiveExitManager,
    LiveExitPlan,
)
from angel_bot.exits.params import ExitPlan, resolve_exit_plan

__all__ = [
    "ExitPlan",
    "LiveExitConfig",
    "LiveExitEvent",
    "LiveExitManager",
    "LiveExitPlan",
    "resolve_exit_plan",
]
