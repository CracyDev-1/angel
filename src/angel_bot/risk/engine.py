from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import structlog

from angel_bot.config import Settings, get_settings
from angel_bot.state.store import StateStore

log = structlog.get_logger(__name__)


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
    open_position_count: int = 0
    # Live broker cash, refreshed by the runtime each loop. Used only when
    # RISK_CAPITAL_RUPEES is 0 (= "use broker cash").
    broker_available_cash: float = 0.0
    # Rolling list of UTC timestamps of recent ENTRY events (any close type),
    # used to enforce the per-hour cap. Trimmed on every check.
    recent_entries: list[datetime] = field(default_factory=list)
    # Last losing close (UTC); used to enforce post-loss cooldown.
    last_loss_at: datetime | None = None


class RiskEngine:
    def __init__(self, settings: Settings | None = None, store: StateStore | None = None):
        self.settings = settings or get_settings()
        self.state = RiskState()
        # Optional store reference enables write-through persistence of
        # ``recent_entries`` and ``last_loss_at`` so the per-hour cap and
        # post-loss cooldown survive a process restart.
        self._store: StateStore | None = store
        self._restored_from_store: bool = False

    def attach_store(self, store: StateStore) -> None:
        """Wire a SQLite store after construction. Idempotent."""
        self._store = store
        # Reset the restore flag so the next ``sync_from_store`` call rehydrates
        # in-memory state from the freshly-attached store.
        self._restored_from_store = False

    def sync_from_store(self, store: StateStore) -> None:
        trades, pnl = store.get_daily_stats()
        self.state.trades_today = trades
        self.state.realized_pnl_today = pnl
        # Restore persistent risk fields exactly once per attached store. After
        # the first call, in-memory state is the source of truth (every write
        # already goes through the store anyway, so they stay in sync).
        if not self._restored_from_store:
            self._restore_persistent(store)
            self._restored_from_store = True

    def _restore_persistent(self, store: StateStore) -> None:
        """Rehydrate ``recent_entries`` (within the 1h window) and
        ``last_loss_at`` from SQLite."""
        try:
            snap = store.get_risk_state()
        except Exception as e:  # noqa: BLE001
            log.warning("risk_state_restore_failed", error=str(e))
            return

        last_loss_iso = snap.get("last_loss_at")
        if last_loss_iso:
            t = _parse_iso(last_loss_iso)
            if t is not None:
                self.state.last_loss_at = t

        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        loaded: list[datetime] = []
        for ts in snap.get("recent_entries", []) or []:
            t = _parse_iso(ts)
            if t is None:
                continue
            if t >= cutoff:
                loaded.append(t)
        self.state.recent_entries = loaded
        # Clean up anything we already filtered out so the table doesn't grow.
        try:
            store.trim_risk_entries(cutoff.isoformat())
        except Exception as e:  # noqa: BLE001
            log.warning("risk_state_trim_failed", error=str(e))

    def set_broker_cash(self, cash: float) -> None:
        """Runtime calls this once per loop with the latest broker cash."""
        self.state.broker_available_cash = max(0.0, float(cash or 0.0))

    def set_open_count(self, count: int) -> None:
        """Live count of currently-open positions (broker or paper)."""
        self.state.open_position_count = max(0, int(count or 0))
        self.state.has_open_position = self.state.open_position_count > 0

    def record_entry(self, when: datetime | None = None) -> None:
        """Call right after a successful order placement (live or paper).

        Writes through to SQLite when a store is attached so the per-hour cap
        survives a restart inside the trading session.
        """
        now = when or datetime.now(timezone.utc)
        self.state.recent_entries.append(now)
        # Trim anything older than 1h to keep the list bounded.
        cutoff = now - timedelta(hours=1)
        self.state.recent_entries = [t for t in self.state.recent_entries if t >= cutoff]
        if self._store is not None:
            try:
                self._store.add_risk_entry(now.isoformat())
                self._store.trim_risk_entries(cutoff.isoformat())
            except Exception as e:  # noqa: BLE001
                log.warning("risk_persist_entry_failed", error=str(e))

    def record_close(self, *, realized_pnl: float, when: datetime | None = None) -> None:
        """Record a closed trade. Losing closes start the cooldown timer."""
        if realized_pnl < 0:
            t = when or datetime.now(timezone.utc)
            self.state.last_loss_at = t
            if self._store is not None:
                try:
                    self._store.upsert_risk_last_loss(t.isoformat())
                except Exception as e:  # noqa: BLE001
                    log.warning("risk_persist_loss_failed", error=str(e))

    def trades_last_hour(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=1)
        # Trim while we're here so the list doesn't grow unboundedly.
        self.state.recent_entries = [t for t in self.state.recent_entries if t >= cutoff]
        return len(self.state.recent_entries)

    def in_loss_cooldown(self, now: datetime | None = None) -> tuple[bool, float]:
        """Returns (in_cooldown, seconds_remaining)."""
        mins = max(0, int(self.settings.risk_loss_cooldown_minutes))
        if mins <= 0 or self.state.last_loss_at is None:
            return False, 0.0
        now = now or datetime.now(timezone.utc)
        elapsed = (now - self.state.last_loss_at).total_seconds()
        remaining = mins * 60 - elapsed
        return remaining > 0, max(0.0, remaining)

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
        # 1) concurrency cap (legacy boolean still wins if explicitly set true)
        if s.risk_one_position_at_a_time and self.state.has_open_position:
            return RiskDecision(False, 0, "open_position")
        if self.state.open_position_count >= s.bot_max_concurrent_positions:
            return RiskDecision(
                False, 0,
                f"max_concurrent ({self.state.open_position_count}/{s.bot_max_concurrent_positions})",
            )
        # 2) daily trade cap
        if self.state.trades_today >= s.risk_max_trades_per_day:
            return RiskDecision(False, 0, "max_trades_today")
        # 3) per-hour trade cap
        if s.risk_max_trades_per_hour > 0:
            n_hour = self.trades_last_hour()
            if n_hour >= s.risk_max_trades_per_hour:
                return RiskDecision(
                    False, 0,
                    f"max_trades_hour ({n_hour}/{s.risk_max_trades_per_hour})",
                )
        # 4) post-loss cooldown
        cooling, remaining = self.in_loss_cooldown()
        if cooling:
            return RiskDecision(
                False, 0,
                f"loss_cooldown ({int(remaining // 60)}m{int(remaining % 60)}s left)",
            )
        # 5) capital + daily-loss kill switch
        capital = self.effective_capital()
        if capital <= 0:
            return RiskDecision(False, 0, "no_capital")
        loss_cap = -capital * (s.risk_max_daily_loss_pct / 100.0)
        if self.state.realized_pnl_today <= loss_cap:
            return RiskDecision(False, 0, "max_daily_loss")

        # 6) position sizing from stop distance
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


def _parse_iso(s: str) -> datetime | None:
    """Robust ISO-8601 -> aware UTC datetime. Returns None on garbage input."""
    if not s:
        return None
    try:
        # Python 3.11+ accepts 'Z' suffix; older versions need replacement.
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t
