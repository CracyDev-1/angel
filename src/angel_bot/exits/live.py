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

    stop_loss_pct: float = 0.015     # 1.5% adverse from FILL price
    take_profit_pct: float = 0.04    # 4% favorable from FILL price
    max_hold_minutes: int = 55       # session-end style timeout


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
    # 'bot' for plans the bot opened itself, 'adopted' for positions opened
    # directly on the Angel One platform that the bot picked up to manage.
    source: str = "bot"

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
            source=str(row.get("source") or "bot").lower() or "bot",
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
    source: str = "bot"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdoptionEvent:
    """Emitted when the bot adopts a manual position or detects the user
    closed one of its managed positions outside the bot. Surfaced through
    the decisions stream so the dashboard explains what changed."""
    kind: str           # "adopted" | "qty_resync" | "external_close"
    plan_id: int
    tradingsymbol: str
    exchange: str
    symboltoken: str
    side: str
    qty: int
    entry_price: float
    exit_price: float | None = None
    realized_pnl: float | None = None
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
        # The bot only places BUYs — long the premium for both CE and PE
        # (a PE BUY is bullish on the put, NOT a short of the underlying
        # at the strike). Stop is below entry, target is above entry,
        # for every plan we open. The previous CE-vs-PE branch inverted
        # SL/TP for puts which made losing PE positions trigger
        # "target" (booking phantom profit) and winning ones trigger
        # "stop" (booking phantom losses).
        stop = planned_entry * (1.0 - sl_p)
        target = planned_entry * (1.0 + tp_p)
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
    # external position adoption
    # ------------------------------------------------------------------

    def reconcile_external_positions(
        self,
        broker_rows: list[dict[str, Any]],
        *,
        master: Any | None,
        product_types: set[str],
        sl_pct: float,
        tp_pct: float,
        max_hold_minutes: int,
        default_variety: str,
        now: datetime | None = None,
    ) -> list[AdoptionEvent]:
        """Pick up any long broker position that isn't already being managed.

        Three things happen each cycle:

        1. **Adopt** every broker row with ``net_qty > 0`` that has no open
           plan yet. We use the broker's own ``buy_avg`` as the "fill price"
           and compute SL/TP off of it so the bot manages it identically to
           a trade it placed itself. ``open_order_id`` is a synthetic
           ``ADOPTED:...`` token so the unique constraint on the column
           still holds.
        2. **Resync** the qty on plans whose broker net_qty has changed
           (e.g. the user manually sold half) — we never sell more lots
           than the broker still shows.
        3. **External-close**: when an open plan's broker position has
           dropped to zero (or vanished entirely) we record the close at
           the broker's ``sell_avg`` (or ``ltp`` fallback) with reason
           ``external_close`` and stop managing it.

        Returns a list of :class:`AdoptionEvent` so the runtime can stream
        them into the decisions log.
        """
        now = now or datetime.now(UTC)
        events: list[AdoptionEvent] = []

        # Index broker rows by (exchange, symboltoken) for quick lookup.
        rows_by_token: dict[tuple[str, str], dict[str, Any]] = {}
        for r in broker_rows or []:
            ex = str(r.get("exchange") or "").upper()
            tok = str(r.get("symboltoken") or "")
            if not ex or not tok:
                continue
            rows_by_token[(ex, tok)] = r

        # ----- 1+2) Adopt new positions / resync qty for known ones -----
        prods = {str(p).upper() for p in product_types if p}
        for (ex, tok), r in rows_by_token.items():
            net_qty = int(r.get("net_qty") or 0)
            if net_qty <= 0:
                continue   # short / flat — bot only manages longs
            sym = str(r.get("tradingsymbol") or "").strip()
            if not sym:
                continue
            prod = str(r.get("producttype") or "INTRADAY").upper()
            if prods and prod not in prods:
                continue

            existing = self.store.find_open_live_exit_plan_by_token(
                exchange=ex, symboltoken=tok
            )
            if existing is not None:
                old_qty = int(existing.get("qty") or 0)
                if net_qty != old_qty:
                    new_lots = max(1, net_qty // max(1, int(existing.get("lot_size") or 1)))
                    self.store.update_live_exit_plan_qty(
                        int(existing["id"]),
                        qty=net_qty,
                        lots=new_lots,
                        last_seen_qty=net_qty,
                    )
                    events.append(
                        AdoptionEvent(
                            kind="qty_resync",
                            plan_id=int(existing["id"]),
                            tradingsymbol=sym,
                            exchange=ex,
                            symboltoken=tok,
                            side=str(existing.get("side") or "-"),
                            qty=net_qty,
                            entry_price=float(existing.get("fill_price") or existing.get("planned_entry") or 0.0),
                            extra={"prev_qty": old_qty},
                        )
                    )
                else:
                    self.store.update_live_exit_plan_seen(int(existing["id"]), last_seen_qty=net_qty)
                continue

            buy_avg = float(r.get("buy_avg") or 0.0)
            ltp = float(r.get("ltp") or 0.0)
            entry = buy_avg if buy_avg > 0 else ltp
            if entry <= 0:
                # No price to anchor SL/TP against — try again next cycle.
                continue

            side = _classify_side_from_symbol(sym)
            signal = "BUY_CALL" if side != "PE" else "BUY_PUT"

            lot_size = _resolve_lot_size(master, ex, tok, default_qty=net_qty)
            lots = max(1, net_qty // max(1, lot_size))
            underlying = _resolve_underlying(master, ex, tok, fallback=sym)

            # Adopted broker positions are always BUYs (long premium /
            # long shares); SL is below entry, target above entry. See
            # comment in register_open for the full rationale.
            stop = entry * (1.0 - sl_pct)
            target = entry * (1.0 + tp_pct)

            synthetic_id = (
                f"ADOPTED:{ex}:{tok}:{int(now.timestamp() * 1000)}"
            )
            try:
                plan_id = self.store.create_live_exit_plan(
                    {
                        "open_order_id": synthetic_id,
                        "exchange": ex,
                        "symboltoken": tok,
                        "tradingsymbol": sym,
                        "kind": "OPTION" if side in ("CE", "PE") else "EQUITY",
                        "side": side,
                        "signal": signal,
                        "underlying": underlying,
                        "qty": net_qty,
                        "lots": lots,
                        "lot_size": lot_size,
                        "planned_entry": entry,
                        "fill_price": entry,
                        "filled_at": now.isoformat(),
                        "stop_price": stop,
                        "target_price": target,
                        "max_hold_minutes": max_hold_minutes,
                        "product": prod,
                        "variety": default_variety,
                        "opened_at": now.isoformat(),
                        "source": "adopted",
                    }
                )
            except Exception as e:  # noqa: BLE001 — never crash the loop
                log.warning(
                    "live_exit_adopt_failed", error=str(e), symbol=sym,
                    exchange=ex, symboltoken=tok,
                )
                continue
            log.info(
                "live_exit_adopted",
                plan_id=plan_id,
                symbol=sym,
                side=side,
                qty=net_qty,
                lots=lots,
                lot_size=lot_size,
                entry=round(entry, 4),
                stop=round(stop, 4),
                target=round(target, 4),
                product=prod,
            )
            events.append(
                AdoptionEvent(
                    kind="adopted",
                    plan_id=plan_id,
                    tradingsymbol=sym,
                    exchange=ex,
                    symboltoken=tok,
                    side=side,
                    qty=net_qty,
                    entry_price=entry,
                    extra={
                        "lot_size": lot_size,
                        "lots": lots,
                        "stop": stop,
                        "target": target,
                        "max_hold_minutes": max_hold_minutes,
                        "underlying": underlying,
                        "product": prod,
                    },
                )
            )

        # ----- 3) External-close: open plans whose broker row dropped to 0 -----
        for raw in self.store.list_open_live_exit_plans():
            try:
                plan = LiveExitPlan.from_row(raw)
            except Exception:  # noqa: BLE001
                continue
            row = rows_by_token.get((plan.exchange, plan.symboltoken))
            net_qty = int(row.get("net_qty") or 0) if row else 0
            if net_qty > 0:
                continue   # still open at the broker — let the normal path drive it
            # Position is gone. Compute realized P&L using whatever the broker
            # last reported as a sell average (preferred) or LTP (fallback).
            sell_avg = float(row.get("sell_avg") or 0.0) if row else 0.0
            ltp = float(row.get("ltp") or 0.0) if row else 0.0
            exit_price = sell_avg if sell_avg > 0 else (ltp if ltp > 0 else plan.effective_entry)
            entry = plan.effective_entry
            qty_for_pnl = int(raw.get("last_seen_qty") or plan.qty or 0)
            # See comment in _send_close: a PE BUY is LONG the option,
            # not short. PnL is (exit - entry) * qty for every position
            # the bot opens or adopts.
            pnl = (exit_price - entry) * qty_for_pnl
            try:
                self.store.close_live_exit_plan(
                    plan.id,
                    exit_price=exit_price,
                    exit_reason="external_close",
                    realized_pnl=pnl,
                    close_order_id=None,
                )
                self.store.add_mode_pnl("live", pnl_delta=pnl, trades_delta=1)
            except Exception as e:  # noqa: BLE001
                log.warning("live_exit_external_close_persist_failed", error=str(e), plan_id=plan.id)
                continue
            log.info(
                "live_exit_external_close",
                plan_id=plan.id,
                symbol=plan.tradingsymbol,
                side=plan.side,
                qty=qty_for_pnl,
                entry=round(entry, 4),
                exit=round(exit_price, 4),
                pnl=round(pnl, 2),
                source=plan.source,
            )
            events.append(
                AdoptionEvent(
                    kind="external_close",
                    plan_id=plan.id,
                    tradingsymbol=plan.tradingsymbol,
                    exchange=plan.exchange,
                    symboltoken=plan.symboltoken,
                    side=plan.side,
                    qty=qty_for_pnl,
                    entry_price=entry,
                    exit_price=exit_price,
                    realized_pnl=pnl,
                    extra={"source": plan.source},
                )
            )
        return events

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
        # silently widen the live risk-per-trade. Long-only: stop below
        # entry, target above entry, regardless of CE / PE / LONG.
        new_stop = avg_f * (1.0 - self.config.stop_loss_pct)
        new_target = avg_f * (1.0 + self.config.take_profit_pct)
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
        # Long-only trigger: hit the stop when price falls *below* it,
        # hit the target when price rises *above* it. Same rule for CE,
        # PE and cash long. The CE-vs-PE branch in the old code was the
        # original source of the sign-flipped P&L on put trades.
        if price <= plan.stop_price:
            return "stop"
        if price >= plan.target_price:
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
        # The bot only ever opens BUYs (long premium for CE *and* PE,
        # long shares for cash equity). Profit on a BUY is always
        # (exit - entry) * qty regardless of CE / PE / LONG. The old
        # code branched on side and inverted the sign for PE — that's
        # how a losing PE trade (bought 100, sold 98) was being booked
        # as a +200 *profit* instead of a -200 loss, and why the
        # dashboard's PnL diverged from Angel One.
        entry = plan.effective_entry
        pnl = (exit_price - entry) * plan.qty

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
            source=plan.source,
            extra={"resp": _safe_json(resp), "source": plan.source},
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


# Recognise the option suffix on Angel symbols: "...CE" / "...PE" (case
# insensitive). Anything else is treated as a long cash leg.
def _classify_side_from_symbol(symbol: str) -> str:
    s = (symbol or "").upper().strip()
    if s.endswith("CE"):
        return "CE"
    if s.endswith("PE"):
        return "PE"
    return "LONG"


def _resolve_lot_size(master: Any, exchange: str, symboltoken: str, *, default_qty: int) -> int:
    """Look up the contract's lot size from the master, falling back to 1.

    For options the lot size matters because broker square-off needs the
    same quantity granularity. If the master isn't available we treat the
    broker's net_qty as a single lot (lot_size=1) — that still squares off
    the right number of shares / contracts, just without a "lots" view.
    """
    if master is None:
        return 1
    try:
        inst = master.resolve_by_token(exchange, symboltoken)
    except Exception:  # noqa: BLE001
        return 1
    if inst is None:
        return 1
    try:
        ls = int(getattr(inst, "lot_size", None) or 0)
    except (TypeError, ValueError):
        ls = 0
    if ls > 0:
        return ls
    # If the master has no lot_size for a cash equity row, default to 1
    # share/lot which is the right answer for NSE EQ.
    return 1 if default_qty > 0 else 1


def _resolve_underlying(master: Any, exchange: str, symboltoken: str, *, fallback: str) -> str:
    """Get the underlying name (e.g. "NIFTY") for badge / decision context."""
    if master is None:
        return fallback
    try:
        inst = master.resolve_by_token(exchange, symboltoken)
    except Exception:  # noqa: BLE001
        return fallback
    if inst is None:
        return fallback
    return str(getattr(inst, "name", "") or fallback)


__all__ = [
    "AdoptionEvent",
    "LiveExitConfig",
    "LiveExitEvent",
    "LiveExitManager",
    "LiveExitPlan",
    "PriceLookup",
]


# Module-level use to silence "imported but unused" if asyncio is referenced
# only inside future helpers — we intentionally keep import surface narrow.
_ = timedelta  # noqa: F401
