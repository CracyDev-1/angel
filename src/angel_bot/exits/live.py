"""Live-mode exit manager.

Mirrors the paper trader's stop-loss / take-profit / max-hold logic, but for
real broker positions opened by the bot. The flow is:

  1. After a successful live ``place_order`` the runtime calls
     :meth:`LiveExitManager.register_open` with planned SL/TP/max-hold and the
     broker order id. We persist a ``live_exit_plans`` row immediately so the
     plan survives a restart inside the trading session.

  2. Each auto-trader cycle the runtime calls
     :meth:`LiveExitManager.mark_and_close`. For every still-open plan we:

       * back-fill the actual fill price + fill time from the bot_orders
         table (populated by the order tracker on each reconcile),
       * mark the plan to market using the freshest premium we can find
         (scanner.latest_prices first, broker positions table second),
       * trigger a market reverse order via ``close_position_row`` when SL,
         TP or max-hold fires,
       * record realized P&L into ``daily_stats_mode['live']`` and feed the
         loss into ``RiskEngine.record_close`` so the post-loss cooldown is
         respected.

The square-off uses MARKET orders for the same lot count we opened with —
the close P&L is recorded against the LTP at trigger time which is the
operationally honest choice for a market order on liquid options.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

import structlog

from angel_bot.execution.orders import build_order_payload, validate_order_payload
from angel_bot.instruments.master import Instrument
from angel_bot.orders.tracker import extract_place_order_id
from angel_bot.smart_client import SmartApiClient
from angel_bot.state.store import StateStore

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# config + dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LiveExitConfig:
    """Mirrors PaperConfig so live behaves identically to dry-run."""
    stop_loss_pct: float = 0.006      # 0.6% adverse from FILL price
    take_profit_pct: float = 0.012    # 1.2% favorable from FILL price
    max_hold_minutes: int = 25        # session-end style timeout


@dataclass
class LiveExitPlan:
    """Hydrated row from ``live_exit_plans``."""
    id: int
    open_order_id: str
    exchange: str
    symboltoken: str
    tradingsymbol: str
    kind: str | None
    side: str          # CE | PE | LONG
    signal: str        # BUY_CALL | BUY_PUT
    underlying: str | None
    qty: int
    lots: int
    lot_size: int
    planned_entry: float
    fill_price: float | None
    filled_at: datetime | None
    stop_price: float
    target_price: float
    max_hold_minutes: int
    product: str
    variety: str
    opened_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> LiveExitPlan:
        return cls(
            id=int(row["id"]),
            open_order_id=str(row["open_order_id"]),
            exchange=str(row["exchange"]).upper(),
            symboltoken=str(row["symboltoken"]),
            tradingsymbol=str(row["tradingsymbol"]),
            kind=row.get("kind"),
            side=str(row["side"]).upper(),
            signal=str(row["signal"]),
            underlying=row.get("underlying"),
            qty=int(row["qty"]),
            lots=int(row["lots"]),
            lot_size=int(row["lot_size"]),
            planned_entry=float(row["planned_entry"]),
            fill_price=float(row["fill_price"]) if row.get("fill_price") is not None else None,
            filled_at=_parse_iso(row.get("filled_at")),
            stop_price=float(row["stop_price"]),
            target_price=float(row["target_price"]),
            max_hold_minutes=int(row["max_hold_minutes"]),
            product=str(row["product"]),
            variety=str(row["variety"]),
            opened_at=_parse_iso(row.get("opened_at")) or datetime.now(UTC),
        )

    @property
    def effective_entry(self) -> float:
        """Fill price once the open is reconciled, planned price until then."""
        return self.fill_price if self.fill_price is not None else self.planned_entry

    @property
    def reference_time(self) -> datetime:
        """When the max-hold clock starts: fill time, else plan creation."""
        return self.filled_at or self.opened_at


@dataclass
class LiveExitEvent:
    """Result of a triggered exit, for downstream logging / decisions."""
    plan_id: int
    tradingsymbol: str
    side: str
    qty: int
    entry_price: float
    exit_price: float
    realized_pnl: float
    exit_reason: str
    close_order_id: str | None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# manager
# ---------------------------------------------------------------------------


# Type alias for the price-lookup callback the runtime injects (scanner cache
# first, broker positions table second). Keeps this module decoupled from
# both the scanner and the runtime.
PriceLookup = Callable[[str, str], float | None]


class LiveExitManager:
    """Owns the live-position exit policy. Stateless w.r.t. broker — all
    state lives in SQLite via :class:`StateStore`."""

    def __init__(
        self,
        store: StateStore,
        config: LiveExitConfig | None = None,
        *,
        price_lookup: PriceLookup | None = None,
    ) -> None:
        self.store = store
        self.config = config or LiveExitConfig()
        self._price_lookup = price_lookup

    def set_price_lookup(self, fn: PriceLookup | None) -> None:
        """Late binding from the runtime once the scanner exists."""
        self._price_lookup = fn

    # ------------------------------------------------------------------
    # registration (called by the runtime after a live place_order succeeds)
    # ------------------------------------------------------------------

    def register_open(
        self,
        *,
        open_order_id: str,
        exchange: str,
        symboltoken: str,
        tradingsymbol: str,
        kind: str | None,
        side: str,
        signal: str,
        underlying: str | None,
        qty: int,
        lots: int,
        lot_size: int,
        planned_entry: float,
        product: str,
        variety: str,
        sl_pct: float | None = None,
        tp_pct: float | None = None,
        max_hold_minutes: int | None = None,
    ) -> int:
        """Persist a new exit plan. Returns the plan's DB id.

        Stop / target are computed from ``planned_entry`` here; once the open
        order fills (reconciled into ``orders.avg_price``) we recompute SL/TP
        from the actual fill price. That keeps the plan honest when the open
        slips.
        """
        sl_p = self.config.stop_loss_pct if sl_pct is None else sl_pct
        tp_p = self.config.take_profit_pct if tp_pct is None else tp_pct
        mh = self.config.max_hold_minutes if max_hold_minutes is None else max_hold_minutes
        side_u = side.upper()
        is_long = side_u in ("CE", "LONG")
        if is_long:
            stop = planned_entry * (1.0 - sl_p)
            target = planned_entry * (1.0 + tp_p)
        else:
            stop = planned_entry * (1.0 + sl_p)
            target = planned_entry * (1.0 - tp_p)
        plan_id = self.store.create_live_exit_plan(
            {
                "open_order_id": open_order_id,
                "exchange": exchange,
                "symboltoken": symboltoken,
                "tradingsymbol": tradingsymbol,
                "kind": kind,
                "side": side_u,
                "signal": signal,
                "underlying": underlying,
                "qty": qty,
                "lots": lots,
                "lot_size": lot_size,
                "planned_entry": planned_entry,
                "stop_price": stop,
                "target_price": target,
                "max_hold_minutes": mh,
                "product": product,
                "variety": variety,
            }
        )
        log.info(
            "live_exit_registered",
            plan_id=plan_id,
            symbol=tradingsymbol,
            side=side_u,
            qty=qty,
            entry=planned_entry,
            stop=stop,
            target=target,
            max_hold_min=mh,
        )
        return plan_id

    # ------------------------------------------------------------------
    # mark-to-market and trigger
    # ------------------------------------------------------------------

    async def mark_and_close(
        self,
        api: SmartApiClient,
        *,
        now: datetime | None = None,
    ) -> list[LiveExitEvent]:
        """Iterate every open plan; close those whose SL / TP / max-hold fires.

        Returns the list of events, useful for the runtime to record into the
        decisions log and feed into ``RiskEngine.record_close``.
        """
        rows = self.store.list_open_live_exit_plans()
        if not rows:
            return []
        now = now or datetime.now(UTC)
        events: list[LiveExitEvent] = []
        for raw in rows:
            try:
                plan = LiveExitPlan.from_row(raw)
            except Exception as e:  # noqa: BLE001 — malformed row, skip
                log.warning("live_exit_bad_row", error=str(e), row=dict(raw))
                continue

            # 1) back-fill the fill price + recompute SL/TP from the actual
            #    avg_price the order tracker has reconciled into bot_orders.
            self._maybe_backfill_fill(plan)

            # 2) get the freshest premium we can.
            price = self._lookup_price(plan)
            if price is None:
                # No fresh price this cycle — only max-hold can still trigger.
                if not self._max_hold_elapsed(plan, now):
                    continue
                # We trigger square-off but at the planned entry as a stand-in
                # so realized_pnl is honest (worst-case 0). Logged.
                price = plan.effective_entry

            ev = await self._maybe_exit(api, plan, price, now)
            if ev is not None:
                events.append(ev)
        return events

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _maybe_backfill_fill(self, plan: LiveExitPlan) -> None:
        """Pull avg_price/fill time from bot_orders and persist into the plan
        once the open order has filled. Re-derives SL/TP from fill price."""
        if plan.fill_price is not None:
            return
        try:
            with self.store._connect() as con:  # noqa: SLF001
                row = con.execute(
                    """
                    SELECT lifecycle_status, avg_price, updated_at
                    FROM orders
                    WHERE broker_order_id = ?
                    """,
                    (plan.open_order_id,),
                ).fetchone()
        except Exception as e:  # noqa: BLE001
            log.warning("live_exit_backfill_failed", error=str(e))
            return
        if row is None:
            return
        life = (row["lifecycle_status"] or "").lower()
        if life not in ("executed", "complete", "partial"):
            return
        avg = row["avg_price"]
        if avg is None:
            return
        try:
            avg_f = float(avg)
        except (TypeError, ValueError):
            return
        if avg_f <= 0:
            return
        # Re-derive SL/TP from the actual fill price so a slipped open doesn't
        # silently widen the live risk-per-trade.
        is_long = plan.side in ("CE", "LONG")
        if is_long:
            new_stop = avg_f * (1.0 - self.config.stop_loss_pct)
            new_target = avg_f * (1.0 + self.config.take_profit_pct)
        else:
            new_stop = avg_f * (1.0 + self.config.stop_loss_pct)
            new_target = avg_f * (1.0 - self.config.take_profit_pct)
        try:
            with self.store._connect() as con:  # noqa: SLF001
                con.execute(
                    """
                    UPDATE live_exit_plans
                    SET fill_price = ?, filled_at = COALESCE(filled_at, ?),
                        stop_price = ?, target_price = ?
                    WHERE id = ? AND closed_at IS NULL
                    """,
                    (avg_f, str(row["updated_at"] or ""), new_stop, new_target, plan.id),
                )
        except Exception as e:  # noqa: BLE001
            log.warning("live_exit_backfill_update_failed", error=str(e))
            return
        plan.fill_price = avg_f
        plan.stop_price = new_stop
        plan.target_price = new_target
        if plan.filled_at is None and row["updated_at"]:
            plan.filled_at = _parse_iso(str(row["updated_at"])) or plan.filled_at
        log.info(
            "live_exit_filled",
            plan_id=plan.id,
            symbol=plan.tradingsymbol,
            avg_price=avg_f,
            new_stop=new_stop,
            new_target=new_target,
        )

    def _lookup_price(self, plan: LiveExitPlan) -> float | None:
        if self._price_lookup is None:
            return None
        try:
            return self._price_lookup(plan.exchange, plan.symboltoken)
        except Exception as e:  # noqa: BLE001
            log.warning("live_exit_price_lookup_error", error=str(e))
            return None

    def _max_hold_elapsed(self, plan: LiveExitPlan, now: datetime) -> bool:
        ref = plan.reference_time
        return (now - ref).total_seconds() >= plan.max_hold_minutes * 60

    def _decide_reason(self, plan: LiveExitPlan, price: float, now: datetime) -> str | None:
        is_long = plan.side in ("CE", "LONG")
        if is_long:
            if price <= plan.stop_price:
                return "stop"
            if price >= plan.target_price:
                return "target"
        else:
            if price >= plan.stop_price:
                return "stop"
            if price <= plan.target_price:
                return "target"
        if self._max_hold_elapsed(plan, now):
            return "session_end"
        return None

    async def _maybe_exit(
        self,
        api: SmartApiClient,
        plan: LiveExitPlan,
        price: float,
        now: datetime,
    ) -> LiveExitEvent | None:
        reason = self._decide_reason(plan, price, now)
        if reason is None:
            return None
        return await self._send_close(api, plan, exit_price=price, reason=reason)

    async def _send_close(
        self,
        api: SmartApiClient,
        plan: LiveExitPlan,
        *,
        exit_price: float,
        reason: str,
    ) -> LiveExitEvent | None:
        """Fire a market reverse order, persist the close, and emit an event.

        For BUYs (the only direction the bot opens today) the close is a SELL
        of the same quantity. We never short-close more than we own.
        """
        # Build a SELL market order on the same instrument & quantity. CE/PE/LONG
        # all opened as BUY, so close is always SELL.
        inst = Instrument(
            exchange=plan.exchange,
            tradingsymbol=plan.tradingsymbol,
            symboltoken=plan.symboltoken,
        )
        payload = build_order_payload(
            inst,
            variety=plan.variety,
            transactiontype="SELL",
            ordertype="MARKET",
            producttype=plan.product,
            quantity=int(plan.qty),
        )
        try:
            validate_order_payload(payload)
        except ValueError as e:
            log.warning("live_exit_invalid_payload", error=str(e), plan_id=plan.id)
            return None

        try:
            resp = await api.place_order(payload)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "live_exit_close_order_failed",
                error=str(e),
                plan_id=plan.id,
                symbol=plan.tradingsymbol,
                reason=reason,
            )
            return None

        close_oid = extract_place_order_id(resp) if isinstance(resp, dict) else None
        is_long = plan.side in ("CE", "LONG")
        entry = plan.effective_entry
        if is_long:
            pnl = (exit_price - entry) * plan.qty
        else:
            pnl = (entry - exit_price) * plan.qty

        # Persist close + per-mode realized P&L.
        self.store.close_live_exit_plan(
            plan.id,
            exit_price=exit_price,
            exit_reason=reason,
            realized_pnl=pnl,
            close_order_id=close_oid,
        )
        try:
            self.store.add_mode_pnl("live", pnl_delta=pnl, trades_delta=1)
        except Exception as e:  # noqa: BLE001
            log.warning("live_exit_mode_pnl_failed", error=str(e), plan_id=plan.id)
        try:
            # Log the close order itself in the orders table (best-effort) so
            # the history page links the open and close together.
            if close_oid is not None:
                self.store.log_order(
                    payload, close_oid,
                    status="placed", lifecycle_status="placed",
                    placed_by_bot=True, intent="close", mode="live",
                )
        except Exception as e:  # noqa: BLE001
            log.warning("live_exit_log_close_order_failed", error=str(e))

        log.info(
            "live_exit_closed",
            plan_id=plan.id,
            symbol=plan.tradingsymbol,
            side=plan.side,
            entry=entry,
            exit=exit_price,
            pnl=round(pnl, 2),
            reason=reason,
            close_order_id=close_oid,
        )
        return LiveExitEvent(
            plan_id=plan.id,
            tradingsymbol=plan.tradingsymbol,
            side=plan.side,
            qty=plan.qty,
            entry_price=entry,
            exit_price=exit_price,
            realized_pnl=pnl,
            exit_reason=reason,
            close_order_id=close_oid,
            extra={"resp": _safe_json(resp)},
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t


def _safe_json(obj: Any) -> Any:
    try:
        return json.loads(json.dumps(obj, default=str))
    except Exception:
        return None


__all__ = [
    "LiveExitConfig",
    "LiveExitEvent",
    "LiveExitManager",
    "LiveExitPlan",
    "PriceLookup",
]


# Module-level use to silence "imported but unused" if asyncio is referenced
# only inside future helpers — we intentionally keep import surface narrow.
_ = timedelta  # noqa: F401
