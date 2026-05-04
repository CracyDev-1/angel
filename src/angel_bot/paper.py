"""Paper-trading engine for the dry-run mode.

When ``TRADING_ENABLED`` is false the runtime *still* runs the entire
strategy / brain / sizing pipeline — but instead of sending an order to Angel
it opens a synthetic position here. Each scanner LTP cycle marks every open
paper position to market; positions auto-close when stop / target / max-hold
fire. Realized P&L is appended to the per-mode daily ledger so the dashboard
can show "live" and "dry-run" P&L side by side.

Important:
* Same brain, same sizing, same lot-fit guard as live → we are NOT cheating.
* Capital used for *sizing* is whatever the runtime passes in as
  ``deployable_capital`` — typically the live RMS available cash, OR a
  user-supplied dry-run override (so the user can stress-test "what trades
  would you take if I had ₹5L?" without touching the real account).
* Paper P&L never affects the live broker; the only side effect is on the
  local SQLite store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from angel_bot.config import get_settings
from angel_bot.exits.trailing import trailing_stop_update_long_premium
from angel_bot.state.store import StateStore

log = structlog.get_logger(__name__)


@dataclass
class PaperConfig:
    stop_loss_pct: float = 0.10      # % adverse move on option premium from entry
    take_profit_pct: float = 0.20    # % favorable move on option premium from entry
    max_hold_minutes: int = 20       # time exit if SL/TP not hit (intraday)
    max_open_positions: int = 5      # safety cap


@dataclass
class PaperOpenRequest:
    exchange: str
    symboltoken: str
    tradingsymbol: str
    kind: str
    signal: str           # "BUY_CALL" | "BUY_PUT"
    side: str             # "CE" | "PE"
    entry_price: float
    lots: int
    lot_size: int
    capital_at_open: float
    reason: str
    # Optional overrides (e.g. from LLM). When None, PaperConfig defaults apply.
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    max_hold_minutes: int | None = None


@dataclass
class PaperCloseEvent:
    paper_id: int
    tradingsymbol: str
    side: str
    qty: int
    entry_price: float
    exit_price: float
    realized_pnl: float
    exit_reason: str


class PaperTrader:
    """Owns the paper book. Persists everything to ``StateStore``."""

    def __init__(self, store: StateStore, config: PaperConfig | None = None) -> None:
        self.store = store
        self.config = config or PaperConfig()

    # ------------------------------------------------------------------
    # Open / close
    # ------------------------------------------------------------------

    def has_capacity(self) -> bool:
        return len(self.store.list_open_paper_positions()) < self.config.max_open_positions

    def open(self, req: PaperOpenRequest) -> int:
        """Persist a new paper position. Returns its DB id."""
        side = req.side.upper()
        # The bot only opens BUYs — CE BUY *and* PE BUY are both LONG
        # the option premium. Profit comes from the premium going up,
        # so stop is below entry and target is above entry for either
        # side. The previous CE/PE branch inverted the stops on puts
        # which made paper PE trades book the wrong sign on P&L (and,
        # via the same logic in live.py, real trades too).
        sl_p = self.config.stop_loss_pct if req.stop_loss_pct is None else float(req.stop_loss_pct)
        tp_p = self.config.take_profit_pct if req.take_profit_pct is None else float(req.take_profit_pct)
        mh = self.config.max_hold_minutes if req.max_hold_minutes is None else int(req.max_hold_minutes)
        stop = req.entry_price * (1 - sl_p)
        target = req.entry_price * (1 + tp_p)
        qty = req.lots * req.lot_size
        capital_used = req.entry_price * qty
        pid = self.store.open_paper_position(
            {
                "exchange": req.exchange,
                "symboltoken": req.symboltoken,
                "tradingsymbol": req.tradingsymbol,
                "kind": req.kind,
                "side": side,
                "signal": req.signal,
                "lots": req.lots,
                "lot_size": req.lot_size,
                "qty": qty,
                "entry_price": req.entry_price,
                "stop_price": stop,
                "target_price": target,
                "capital_used": capital_used,
                "capital_at_open": req.capital_at_open,
                "reason_at_open": req.reason,
                "max_hold_minutes": mh,
                "initial_stop_price": stop,
                "peak_premium": req.entry_price,
            }
        )
        log.info(
            "paper_open",
            id=pid,
            symbol=req.tradingsymbol,
            side=side,
            qty=qty,
            entry=req.entry_price,
            stop=stop,
            target=target,
        )
        return pid

    def manual_close(self, pid: int, last_price: float) -> PaperCloseEvent | None:
        """Square off a paper position immediately at last_price."""
        rows = [r for r in self.store.list_open_paper_positions() if int(r["id"]) == int(pid)]
        if not rows:
            return None
        return self._close(rows[0], exit_price=last_price, reason="manual")

    def reset(self) -> None:
        """Wipe everything paper-related. Live data is untouched."""
        self.store.reset_mode("dryrun")
        log.warning("paper_reset")

    # ------------------------------------------------------------------
    # Mark-to-market
    # ------------------------------------------------------------------

    def mark_and_close(
        self,
        latest_prices: dict[tuple[str, str], float],
        *,
        now: datetime | None = None,
    ) -> list[PaperCloseEvent]:
        """For each open paper position:
          1. update last_price using latest_prices[(exchange, token)]
          2. if price hits stop / target OR position is older than
             ``max_hold_minutes``, close it and append realized P&L
             to today's per-mode ledger.

        Returns the list of positions that were closed in this call.
        """
        now = now or datetime.now(UTC)
        events: list[PaperCloseEvent] = []
        open_rows = self.store.list_open_paper_positions()
        for r in open_rows:
            key = (str(r["exchange"]).upper(), str(r["symboltoken"]))
            price = latest_prices.get(key)
            if price is not None:
                self.store.update_paper_mark(int(r["id"]), float(price))
                r = self._apply_paper_trail(r, float(price))
            else:
                # No fresh price for this symbol this cycle — try max-hold check below.
                price = float(r.get("last_price") or r["entry_price"])

            ev = self._maybe_exit(r, price, now)
            if ev:
                events.append(ev)
        return events

    def _apply_paper_trail(self, row: dict[str, Any], last_price: float) -> dict[str, Any]:
        settings = get_settings()
        if not settings.trail_stop_enabled:
            return row
        entry = float(row["entry_price"])
        initial = float(row.get("initial_stop_price") or row["stop_price"])
        peak = float(row.get("peak_premium") or entry)
        current_stop = float(row["stop_price"])
        new_peak, new_stop = trailing_stop_update_long_premium(
            enabled=True,
            trail_pct=settings.trail_stop_pct,
            arm_profit_pct=settings.trail_arm_min_profit_pct,
            entry=entry,
            last_price=last_price,
            initial_stop=initial,
            peak=peak,
            current_stop=current_stop,
        )
        if new_peak == peak and new_stop == current_stop:
            return row
        self.store.update_paper_trailing_stop(
            int(row["id"]), peak_premium=new_peak, stop_price=new_stop
        )
        out = dict(row)
        out["peak_premium"] = new_peak
        out["stop_price"] = new_stop
        return out

    def _maybe_exit(
        self,
        row: dict[str, Any],
        last_price: float,
        now: datetime,
    ) -> PaperCloseEvent | None:
        stop = row.get("stop_price")
        target = row.get("target_price")
        opened_at = _parse_iso(row.get("opened_at"))
        held_minutes = ((now - opened_at).total_seconds() / 60.0) if opened_at else 0.0

        # Long-only: stop fires when price falls below stop, target
        # fires when price rises above target. Same rule for CE and PE
        # since both are option BUYs.
        reason: str | None = None
        if stop is not None and last_price <= float(stop):
            reason = "stop"
        elif target is not None and last_price >= float(target):
            reason = "target"

        if reason is None and held_minutes >= float(
            row.get("max_hold_minutes") or self.config.max_hold_minutes
        ):
            reason = "session_end"

        if reason is None:
            return None
        return self._close(row, exit_price=last_price, reason=reason)

    def _close(self, row: dict[str, Any], *, exit_price: float, reason: str) -> PaperCloseEvent:
        side = str(row["side"]).upper()
        qty = int(row["qty"])
        entry = float(row["entry_price"])
        # Long-only PnL — same formula for CE and PE since the bot
        # only ever BUYs the option.
        pnl = (exit_price - entry) * qty
        pid = int(row["id"])
        self.store.close_paper_position(
            pid,
            exit_price=exit_price,
            exit_reason=reason,
            realized_pnl=pnl,
        )
        self.store.add_mode_pnl("dryrun", pnl_delta=pnl, trades_delta=1)
        ev = PaperCloseEvent(
            paper_id=pid,
            tradingsymbol=str(row["tradingsymbol"]),
            side=side,
            qty=qty,
            entry_price=entry,
            exit_price=exit_price,
            realized_pnl=pnl,
            exit_reason=reason,
        )
        log.info(
            "paper_close",
            id=pid,
            symbol=ev.tradingsymbol,
            side=side,
            entry=entry,
            exit=exit_price,
            pnl=round(pnl, 2),
            reason=reason,
        )
        return ev

    # ------------------------------------------------------------------
    # Read helpers (snapshot / dashboard)
    # ------------------------------------------------------------------

    def open_positions_summary(self) -> dict[str, Any]:
        rows = self.store.list_open_paper_positions()
        out_rows: list[dict[str, Any]] = []
        ce_open = pe_open = 0
        cap_ce = cap_pe = 0.0
        unreal_ce = unreal_pe = 0.0
        for r in rows:
            side = str(r["side"]).upper()
            entry = float(r["entry_price"])
            qty = int(r["qty"])
            last = float(r.get("last_price") or entry)
            unreal = (last - entry) * qty if side == "CE" else (entry - last) * qty
            cap = float(r["capital_used"])
            out_rows.append(
                {
                    "id": int(r["id"]),
                    "tradingsymbol": r["tradingsymbol"],
                    "exchange": r["exchange"],
                    "symboltoken": r["symboltoken"],
                    "kind": r.get("kind"),
                    "side": side,
                    "signal": r["signal"],
                    "lots": int(r["lots"]),
                    "lot_size": int(r["lot_size"]),
                    "qty": qty,
                    "entry_price": entry,
                    "stop_price": r.get("stop_price"),
                    "target_price": r.get("target_price"),
                    "last_price": last,
                    "capital_used": cap,
                    "unrealized_pnl": unreal,
                    "opened_at": r.get("opened_at"),
                    "last_marked_at": r.get("last_marked_at"),
                    "reason_at_open": r.get("reason_at_open"),
                }
            )
            if side == "CE":
                ce_open += 1
                cap_ce += cap
                unreal_ce += unreal
            else:
                pe_open += 1
                cap_pe += cap
                unreal_pe += unreal
        return {
            "rows": out_rows,
            "open_positions": len(out_rows),
            "ce_open": ce_open,
            "pe_open": pe_open,
            "capital_used_ce": cap_ce,
            "capital_used_pe": cap_pe,
            "capital_used_total": cap_ce + cap_pe,
            "unrealized_pnl_ce": unreal_ce,
            "unrealized_pnl_pe": unreal_pe,
            "unrealized_pnl_total": unreal_ce + unreal_pe,
        }

    def today_summary(self) -> dict[str, Any]:
        trades, realized = self.store.get_mode_daily_stats("dryrun")
        opens = self.open_positions_summary()
        return {
            "trades": trades,
            "realized_pnl": realized,
            "unrealized_pnl": opens["unrealized_pnl_total"],
            "net_pnl": realized + opens["unrealized_pnl_total"],
            "open_positions": opens["open_positions"],
        }


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # SQLite gives us ISO with timezone offset already.
        return datetime.fromisoformat(str(s))
    except Exception:
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except Exception:
            return None


__all__ = [
    "PaperConfig",
    "PaperOpenRequest",
    "PaperCloseEvent",
    "PaperTrader",
]


# Re-exporting timedelta for tests that want to inject "now" easily.
_ = timedelta  # noqa: F401 — kept to avoid pruning the import in tests
