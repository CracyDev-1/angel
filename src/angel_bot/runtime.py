from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import structlog

from angel_bot.auth.session import AngelHttpError, AngelSession, totp_configured_in_env
from angel_bot.broker_models import normalize_positions, normalize_rms, summarize_orders_for_ui
from angel_bot.config import Settings, get_settings
from angel_bot.decisions import Decision, DecisionLog
from angel_bot.execution.orders import DuplicateOrderGuard, build_order_payload, validate_order_payload
from angel_bot.instruments.master import Instrument
from angel_bot.orders.tracker import OrderTracker, extract_place_order_id
from angel_bot.risk.engine import RiskEngine
from angel_bot.scanner.engine import ScannerEngine, ScannerHit
from angel_bot.smart_client import SmartApiClient
from angel_bot.state.store import StateStore

log = structlog.get_logger(__name__)


class TradingRuntime:
    """
    Single live Angel session + auto-trader bot. The bot loop:
      1. polls available funds (RMS),
      2. polls broker positions,
      3. runs the scanner over the watchlist,
      4. picks instruments whose lot fits the available funds,
      5. asks the strategy for a signal (currently a momentum heuristic),
      6. checks risk caps,
      7. logs a Decision (records "would trade" in dry-run; sends placeOrder when TRADING_ENABLED=true).
    """

    _instance: TradingRuntime | None = None

    def __init__(self) -> None:
        self.settings: Settings = get_settings()
        self.session: AngelSession | None = None
        self._bot_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_error: str | None = None
        self.connected_clientcode: str | None = None

        self.store = StateStore(self.settings.state_sqlite_path)
        self.scanner = ScannerEngine(self.settings)
        self.decisions = DecisionLog()
        self.risk = RiskEngine(self.settings)
        self._dup_guard = DuplicateOrderGuard(ttl_s=60.0)
        self._tracker = OrderTracker(self.store)

        self.last_funds: dict[str, Any] | None = None
        self.last_positions: dict[str, Any] | None = None
        self.last_scanner: list[ScannerHit] = []
        self.last_loop_at: str | None = None
        self.last_scan_summary: dict[str, Any] | None = None
        self.bot_started_at: str | None = None
        self.auto_mode: bool = totp_configured_in_env(self.settings)
        self._watchdog_task: asyncio.Task | None = None
        # runtime mode override — flipped by the dashboard "Go Live" toggle
        # without restarting the process. Initialized from .env (TRADING_ENABLED).
        self._runtime_trading_enabled: bool = bool(self.settings.trading_enabled)

    @classmethod
    def instance(cls) -> TradingRuntime:
        if cls._instance is None:
            cls._instance = TradingRuntime()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def connected(self) -> bool:
        return bool(self.session and self.session.jwt and self.session.refresh_token)

    def bot_running(self) -> bool:
        t = self._bot_task
        return t is not None and not t.done()

    def smart_client(self) -> SmartApiClient | None:
        if not self.session:
            return None
        return SmartApiClient(self.session, self.settings)

    @property
    def trading_enabled(self) -> bool:
        return self._runtime_trading_enabled

    def set_trading_enabled(self, enabled: bool) -> dict[str, Any]:
        prev = self._runtime_trading_enabled
        self._runtime_trading_enabled = bool(enabled)
        log.info("trading_mode_changed", from_=prev, to=self._runtime_trading_enabled)
        self.decisions.add(
            Decision(
                ts=DecisionLog.now_iso(),
                name="-",
                exchange="-",
                token="-",
                signal="MODE",
                reason=f"trading_{'live' if enabled else 'dry_run'}",
                last_price=None,
                quantity=0,
                lots=0,
                capital_used=0.0,
                side="-",
                placed=False,
                dry_run=not self._runtime_trading_enabled,
            )
        )
        return {"trading_enabled": self._runtime_trading_enabled}

    async def connect_with_totp(self, totp: str) -> dict:
        """Login with a one-time TOTP from the dashboard (not stored in .env)."""
        code = (totp or "").strip()
        if len(code) != 6 or not code.isdigit():
            raise ValueError("Enter the current 6-digit code from your authenticator app.")
        return await self._connect(runtime_totp=code)

    async def auto_connect(self) -> dict | None:
        """Connect using ANGEL_TOTP_SECRET (no UI). Returns None if no secret configured."""
        if not totp_configured_in_env(self.settings):
            return None
        try:
            return await self._connect(runtime_totp=None)
        except Exception as e:  # noqa: BLE001 — log + surface; backend keeps serving the UI
            self.last_error = f"auto_connect: {e}"
            log.warning("auto_connect_failed", error=str(e))
            return {"status": False, "error": str(e)}

    async def _connect(self, *, runtime_totp: str | None) -> dict:
        self.last_error = None
        self.connected_clientcode = None
        if self.session is None:
            self.session = AngelSession(self.settings)
        if runtime_totp:
            self.session.set_runtime_totp(runtime_totp)
        try:
            await self.session.ensure_login(force=True)
        except (AngelHttpError, ValueError) as e:
            self.last_error = str(e)
            raise

        prof = await self.session.get_profile()
        ok = bool(prof.get("status"))
        data = prof.get("data") if isinstance(prof, dict) else None
        self.connected_clientcode = (
            str(data.get("clientcode", "")).strip() or None if isinstance(data, dict) else None
        )
        if not ok:
            self.last_error = str(prof.get("message") or prof)
        return {
            "status": ok,
            "profile_message": prof.get("message"),
            "clientcode": self.connected_clientcode,
        }

    async def disconnect(self) -> None:
        await self.stop_bot()
        await self._stop_watchdog()
        if self.session:
            await self.session.aclose()
            self.session = None
        self.connected_clientcode = None

    async def stop_bot(self) -> None:
        self._stop.set()
        if self._bot_task:
            self._bot_task.cancel()
            try:
                await self._bot_task
            except asyncio.CancelledError:
                pass
            self._bot_task = None
        self._stop = asyncio.Event()

    async def start_bot(self) -> None:
        if not self.connected():
            raise RuntimeError("Connect with TOTP on the dashboard first.")
        await self.stop_bot()
        self._stop = asyncio.Event()
        self.bot_started_at = datetime.now(UTC).isoformat()
        self._bot_task = asyncio.create_task(self._auto_trader_loop(), name="angel-auto-trader")
        if self.auto_mode and self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(self._session_watchdog(), name="angel-session-watchdog")

    async def _stop_watchdog(self) -> None:
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None

    async def _session_watchdog(self) -> None:
        """Periodically verify the broker session and re-login when it expires.

        Angel One sessions expire daily (midnight); when getProfile starts to fail,
        refresh_tokens() will fall back to ensure_login(force=True), which uses
        ANGEL_TOTP_SECRET → fresh TOTP automatically (no UI interaction).
        """
        s = self.settings
        try:
            while True:
                await asyncio.sleep(s.auto_relogin_interval_s)
                if self.session is None:
                    continue
                try:
                    await self.session.get_profile()
                except AngelHttpError as e:
                    log.info("watchdog_relogin", reason=str(e))
                    try:
                        await self.session.refresh_tokens()
                        log.info("watchdog_relogin_ok")
                    except Exception as e2:  # noqa: BLE001
                        log.warning("watchdog_relogin_failed", error=str(e2))
                        self.last_error = f"watchdog_relogin: {e2}"
                except Exception as e:  # noqa: BLE001
                    log.warning("watchdog_check_error", error=str(e))
        except asyncio.CancelledError:
            return

    async def refresh_funds(self) -> dict[str, Any]:
        api = self.smart_client()
        if not api:
            return {"available_cash": 0.0, "net": 0.0, "utilised_margin": 0.0}
        try:
            payload = await api.get_rms()
            self.last_funds = normalize_rms(payload)
        except Exception as e:
            log.warning("rms_error", error=str(e))
            self.last_funds = self.last_funds or {
                "available_cash": 0.0,
                "net": 0.0,
                "utilised_margin": 0.0,
                "error": str(e),
            }
        return self.last_funds or {}

    async def refresh_positions(self) -> dict[str, Any]:
        api = self.smart_client()
        if not api:
            return {"rows": [], "open_positions": 0, "capital_used_ce": 0.0, "capital_used_pe": 0.0, "capital_used_total": 0.0, "pnl_total": 0.0}
        try:
            payload = await api.get_position()
            self.last_positions = normalize_positions(payload)
        except Exception as e:
            log.warning("positions_error", error=str(e))
            self.last_positions = self.last_positions or {"rows": [], "open_positions": 0, "capital_used_ce": 0.0, "capital_used_pe": 0.0, "capital_used_total": 0.0, "pnl_total": 0.0, "error": str(e)}
        return self.last_positions or {}

    async def reconcile_orders(self) -> int:
        api = self.smart_client()
        if not api:
            return 0
        try:
            return await self._tracker.reconcile_once(api)
        except Exception as e:
            log.warning("reconcile_error", error=str(e))
            return 0

    def snapshot(self) -> dict[str, Any]:
        positions = self.last_positions or {}
        return {
            "connected": self.connected(),
            "bot_running": self.bot_running(),
            "trading_enabled": self._runtime_trading_enabled,
            "auto_mode": self.auto_mode,
            "last_loop_at": self.last_loop_at,
            "last_scan_summary": self.last_scan_summary,
            "bot_started_at": self.bot_started_at,
            "last_error": self.last_error,
            "clientcode": self.connected_clientcode,
            "funds": self.last_funds,
            "positions": self.last_positions,
            "scanner": [h.to_dict() for h in self.last_scanner[:25]],
            "scanner_by_kind": self._scanner_by_kind(),
            "ce_pe_summary": self._ce_pe_summary(positions),
            "bot_today": self._bot_today_summary(positions),
            "recent_orders": summarize_orders_for_ui(self.store.recent_orders(50)),
            "decisions": [d.to_dict() for d in self.decisions.recent(120)],
            "daily": self._daily_stats(),
        }

    def _scanner_by_kind(self) -> dict[str, Any]:
        """Group scanner hits by kind (EQUITY / INDEX / COMMODITY) so the simple UI
        can show one card per category with how many are *tradable* with current cash.
        """
        buckets: dict[str, dict[str, Any]] = {}
        for h in self.last_scanner:
            kind = (h.kind or "EQUITY").upper()
            b = buckets.setdefault(
                kind,
                {"kind": kind, "count": 0, "tradable": 0, "names": [], "top_name": None, "top_score": 0.0},
            )
            b["count"] += 1
            if (h.affordable_lots or 0) >= 1:
                b["tradable"] += 1
                if len(b["names"]) < 4:
                    b["names"].append(h.name)
            if abs(h.score or 0) > b["top_score"]:
                b["top_score"] = abs(h.score or 0)
                b["top_name"] = h.name
        # Stable order so the UI cards don't shuffle.
        order = ["EQUITY", "INDEX", "COMMODITY"]
        ordered: list[dict[str, Any]] = []
        for k in order:
            if k in buckets:
                ordered.append(buckets[k])
        for k, v in buckets.items():
            if k not in order:
                ordered.append(v)
        return {"buckets": ordered}

    def _ce_pe_summary(self, positions: dict[str, Any]) -> dict[str, Any]:
        rows = positions.get("rows") or []
        ce_open = pe_open = 0
        ce_pnl = pe_pnl = 0.0
        for r in rows:
            if (r.get("net_qty") or 0) == 0:
                continue
            side = r.get("side")
            pnl = float(r.get("pnl") or 0.0)
            if side == "CE":
                ce_open += 1
                ce_pnl += pnl
            elif side == "PE":
                pe_open += 1
                pe_pnl += pnl
        return {
            "ce_open": ce_open,
            "pe_open": pe_open,
            "capital_ce": float(positions.get("capital_used_ce") or 0.0),
            "capital_pe": float(positions.get("capital_used_pe") or 0.0),
            "pnl_ce": ce_pnl,
            "pnl_pe": pe_pnl,
        }

    def _bot_today_summary(self, positions: dict[str, Any]) -> dict[str, Any]:
        """Roll-up of trades placed by THIS bot since UTC midnight."""
        rows = self.store.bot_orders_today()
        trades_placed = len(rows)
        unrealized = float(positions.get("pnl_total") or 0.0)
        realized_today = float(self._daily_stats().get("realized_pnl") or 0.0)
        return {
            "trades_placed": trades_placed,
            "pending": len([r for r in rows if (r.get("lifecycle_status") or "").lower() not in ("executed", "complete", "cancelled", "rejected")]),
            "filled": len([r for r in rows if (r.get("lifecycle_status") or "").lower() in ("executed", "complete")]),
            "rejected": len([r for r in rows if (r.get("lifecycle_status") or "").lower() == "rejected"]),
            "unrealized_pnl": unrealized,
            "realized_pnl": realized_today,
            "net_pnl": realized_today + unrealized,
        }

    def history(self, *, orders_limit: int = 200) -> dict[str, Any]:
        rows = self.store.recent_orders(orders_limit)
        all_days = self.store.all_daily_stats()
        total_pnl = sum(float(d.get("pnl") or 0.0) for d in all_days)
        total_trades = sum(int(d.get("trades") or 0) for d in all_days)
        return {
            "orders": summarize_orders_for_ui(rows),
            "all_days": all_days,
            "totals": {
                "trades": total_trades,
                "realized_pnl": total_pnl,
                "days_traded": len(all_days),
            },
        }

    def _daily_stats(self) -> dict[str, Any]:
        trades, pnl = self.store.get_daily_stats()
        cap = self.settings.risk_capital_rupees or 0
        loss_cap = -cap * (self.settings.risk_max_daily_loss_pct / 100.0)
        return {
            "trades": trades,
            "realized_pnl": pnl,
            "loss_limit": loss_cap,
            "max_trades": self.settings.risk_max_trades_per_day,
            "all_days": self.store.all_daily_stats()[:30],
        }

    async def _auto_trader_loop(self) -> None:
        s = self.settings
        api = self.smart_client()
        assert api is not None
        log.info(
            "auto_trader_started",
            interval_s=s.bot_loop_interval_s,
            trading_enabled=self._runtime_trading_enabled,
        )
        try:
            while not self._stop.is_set():
                self.last_loop_at = datetime.now(UTC).isoformat()
                try:
                    funds = await self.refresh_funds()
                    positions = await self.refresh_positions()
                    await self.reconcile_orders()
                    self.risk.sync_from_store(self.store)
                    self.risk.state.has_open_position = (positions.get("open_positions", 0) or 0) > 0

                    available = float(funds.get("available_cash") or 0.0)
                    deployable = available * (s.bot_use_capital_pct / 100.0)

                    hits = await self.scanner.poll_once(api, available_funds=deployable)
                    self.last_scanner = hits

                    self._record_scan_summary(hits, positions, available, deployable)

                    selected = self._pick_candidate(hits, positions)
                    await self._consider_trade(api, selected, deployable)
                except Exception as e:
                    self.last_error = str(e)
                    log.exception("auto_trader_iter_error")
                # sleep with stop responsiveness
                step = max(0.25, min(s.bot_loop_interval_s, 1.0))
                slept = 0.0
                while slept < s.bot_loop_interval_s and not self._stop.is_set():
                    await asyncio.sleep(step)
                    slept += step
        except asyncio.CancelledError:
            log.info("auto_trader_cancelled")
            raise
        finally:
            log.info("auto_trader_stopped")

    def _pick_candidate(self, hits: list[ScannerHit], positions: dict[str, Any]) -> ScannerHit | None:
        """Pick the highest-scoring instrument that the brain has actually
        signalled BUY_CALL / BUY_PUT on, subject to capital + risk filters.
        """
        if not hits:
            return None
        s = self.settings
        if positions.get("open_positions", 0) >= s.bot_max_concurrent_positions:
            return None
        for h in hits:
            if h.last_price is None or h.last_price <= 0:
                continue
            if not h.lot_size or not h.affordable_lots:
                continue
            if h.affordable_lots < 1:
                continue
            if h.score < max(s.strategy_min_score, s.bot_min_signal_strength):
                continue
            if h.signal_side not in ("BUY_CALL", "BUY_PUT"):
                continue
            return h
        return None

    def _record_scan_summary(
        self,
        hits: list[ScannerHit],
        positions: dict[str, Any],
        available: float,
        deployable: float,
    ) -> None:
        """Log a per-cycle "what the bot saw" entry so the UI shows continuous activity."""
        s = self.settings
        top: list[dict[str, Any]] = []
        for h in hits[:5]:
            top.append(
                {
                    "name": h.name,
                    "kind": h.kind,
                    "ltp": h.last_price,
                    "change_pct": h.change_pct,
                    "score": h.score,
                    "score_breakdown": h.score_breakdown,
                    "signal_side": h.signal_side,
                    "signal_reason": h.signal_reason,
                    "signal_confidence": h.signal_confidence,
                    "affordable_lots": h.affordable_lots,
                    "candles_15m": h.candles_15m,
                    "candles_5m": h.candles_5m,
                }
            )
        open_n = int(positions.get("open_positions") or 0)
        min_score = max(s.strategy_min_score, s.bot_min_signal_strength)
        if not hits:
            reason = "watchlist_empty_or_ltp_failed"
        elif open_n >= s.bot_max_concurrent_positions:
            reason = f"max_positions_open ({open_n}/{s.bot_max_concurrent_positions})"
        elif not any((h.affordable_lots or 0) >= 1 for h in hits):
            reason = "no_affordable_lots_for_capital"
        elif not any(h.score >= min_score for h in hits):
            reason = f"all_scores_below_min ({min_score:.2f})"
        elif not any(h.signal_side in ("BUY_CALL", "BUY_PUT") for h in hits):
            reason = "no_brain_entry_signal_yet"
        else:
            reason = "candidates_available"
        self.last_scan_summary = {
            "ts": DecisionLog.now_iso(),
            "instruments_scanned": len(hits),
            "available_cash": available,
            "deployable_cash": deployable,
            "open_positions": open_n,
            "reason": reason,
            "top": top,
            "min_score": min_score,
        }

    async def _consider_trade(self, api: SmartApiClient, hit: ScannerHit | None, deployable: float) -> None:
        s = self.settings
        if hit is None:
            scan_reason = (self.last_scan_summary or {}).get("reason", "no_candidate")
            self._record_skip(hit=None, signal="NO_TRADE", reason=f"no_candidate ({scan_reason})", price=None)
            return
        # The brain already produced a side; runtime trusts it but still applies risk + capital caps.
        signal = hit.signal_side
        reason = hit.signal_reason
        if signal == "NO_TRADE":
            self._record_skip(hit=hit, signal=signal, reason=reason, price=hit.last_price)
            return
        # Risk gate (uses last_price as entry, 1% adverse move as nominal stop for sizing).
        entry = float(hit.last_price or 0.0)
        if entry <= 0:
            self._record_skip(hit=hit, signal=signal, reason="bad_entry_price", price=entry)
            return
        nominal_stop = entry * 0.99 if signal == "BUY_CALL" else entry * 1.01
        decision = self.risk.evaluate_new_trade(entry=entry, stop=nominal_stop, lot_size=hit.lot_size or 1)
        if not decision.allowed:
            self._record_skip(hit=hit, signal=signal, reason=f"risk:{decision.reason}", price=entry)
            return

        # Cap qty by funds-deployable lots from scanner.
        max_lots_funds = hit.affordable_lots or 0
        risk_lots = (decision.quantity // (hit.lot_size or 1)) if hit.lot_size else 0
        chosen_lots = max(0, min(risk_lots, max_lots_funds))
        if chosen_lots < 1:
            self._record_skip(hit=hit, signal=signal, reason="zero_lots_after_funds_cap", price=entry)
            return
        chosen_qty = chosen_lots * (hit.lot_size or 1)
        capital_used = entry * chosen_qty
        side = "CE" if signal == "BUY_CALL" else "PE"

        # NOTE: hit.token here is the *underlying* (index/equity) token. Real placement requires the
        # specific option strike token (from the instrument master). When trading is in dry-run mode,
        # we always log a dry-run decision so the dashboard can show what the bot WOULD do.
        if not self._runtime_trading_enabled:
            self._record_decision(
                hit=hit, signal=signal, reason="dry_run", price=entry, qty=chosen_qty,
                lots=chosen_lots, capital=capital_used, side=side, placed=False, dry_run=True,
            )
            return

        # Live placement requires a resolved option Instrument; refuse if not provided.
        underlying_inst = Instrument(exchange=hit.exchange, tradingsymbol=hit.name, symboltoken=hit.token)
        if hit.kind in ("INDEX",):
            self._record_skip(hit=hit, signal=signal, reason="live_index_options_require_strike_resolution", price=entry)
            return
        # Equity / commodity: place a delivery/intraday order on the underlying as a placeholder.
        payload = build_order_payload(
            underlying_inst,
            variety=s.bot_default_variety,
            transactiontype="BUY",
            ordertype="MARKET",
            producttype=s.bot_default_product,
            quantity=chosen_qty,
        )
        try:
            validate_order_payload(payload)
        except ValueError as e:
            self._record_skip(hit=hit, signal=signal, reason=f"invalid_payload:{e}", price=entry)
            return
        if not self._dup_guard.check_and_remember(payload):
            self._record_skip(hit=hit, signal=signal, reason="duplicate_order_window", price=entry)
            return
        try:
            resp = await api.place_order(payload)
        except Exception as e:
            self.last_error = str(e)
            self._record_skip(hit=hit, signal=signal, reason=f"place_order_error:{e}", price=entry)
            return
        oid = extract_place_order_id(resp) if isinstance(resp, dict) else None
        if oid:
            self.store.log_order(
                payload, oid, status="placed", lifecycle_status="placed",
                placed_by_bot=True, intent="open",
            )
        self._record_decision(
            hit=hit, signal=signal, reason="placed", price=entry, qty=chosen_qty,
            lots=chosen_lots, capital=capital_used, side=side,
            placed=bool(oid), dry_run=False, broker_order_id=oid, extra={"resp": _redact(resp)},
        )

    def _record_skip(self, *, hit: ScannerHit | None, signal: str, reason: str, price: float | None) -> None:
        self.decisions.add(
            Decision(
                ts=DecisionLog.now_iso(),
                name=hit.name if hit else "-",
                exchange=hit.exchange if hit else "-",
                token=hit.token if hit else "-",
                signal=signal,
                reason=reason,
                last_price=price,
                quantity=0,
                lots=0,
                capital_used=0.0,
                side="-",
                placed=False,
                dry_run=not self._runtime_trading_enabled,
            )
        )

    def _record_decision(
        self,
        *,
        hit: ScannerHit,
        signal: str,
        reason: str,
        price: float,
        qty: int,
        lots: int,
        capital: float,
        side: str,
        placed: bool,
        dry_run: bool,
        broker_order_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.decisions.add(
            Decision(
                ts=DecisionLog.now_iso(),
                name=hit.name,
                exchange=hit.exchange,
                token=hit.token,
                signal=signal,
                reason=reason,
                last_price=price,
                quantity=qty,
                lots=lots,
                capital_used=capital,
                side=side,
                placed=placed,
                dry_run=dry_run,
                broker_order_id=broker_order_id,
                extra=extra or {},
            )
        )

    async def kill_switch(
        self,
        *,
        cancel_pending: bool = True,
        square_off: bool = True,
    ) -> dict[str, Any]:
        """One-call panic stop:
          1. stop the bot loop
          2. flip to dry-run so no further orders can go out
          3. (optional) cancel every still-pending order placed by THIS bot
          4. (optional) square-off every open position by sending a market reverse order
        Returns a structured report of what was done.
        """
        report: dict[str, Any] = {
            "stopped_bot": False,
            "set_dry_run": False,
            "cancelled": [],
            "cancel_failures": [],
            "squared_off": [],
            "squareoff_failures": [],
        }
        await self.stop_bot()
        report["stopped_bot"] = True
        self.set_trading_enabled(False)
        report["set_dry_run"] = True

        api = self.smart_client()
        if api is None:
            return report

        if cancel_pending:
            for o in self.store.pending_bot_orders():
                oid = o.get("broker_order_id")
                variety = (o.get("variety") or self.settings.bot_default_variety or "NORMAL").upper()
                if not oid:
                    continue
                try:
                    await api.cancel_order(variety=variety, orderid=str(oid))
                    report["cancelled"].append(str(oid))
                except Exception as e:  # noqa: BLE001 — collect, don't abort
                    report["cancel_failures"].append({"orderid": str(oid), "error": str(e)})
                    log.warning("kill_cancel_failed", orderid=oid, error=str(e))

        if square_off:
            await self.refresh_positions()
            for r in (self.last_positions or {}).get("rows", []):
                qty = int(r.get("net_qty") or 0)
                if qty == 0:
                    continue
                try:
                    res = await self._close_position_row(api, r)
                    report["squared_off"].append(res)
                except Exception as e:  # noqa: BLE001
                    report["squareoff_failures"].append({"symbol": r.get("tradingsymbol"), "error": str(e)})
                    log.warning("kill_squareoff_failed", symbol=r.get("tradingsymbol"), error=str(e))

        self.decisions.add(
            Decision(
                ts=DecisionLog.now_iso(),
                name="-",
                exchange="-",
                token="-",
                signal="MODE",
                reason=(
                    f"kill_switch: cancelled={len(report['cancelled'])} "
                    f"squared_off={len(report['squared_off'])} "
                    f"cancel_failures={len(report['cancel_failures'])} "
                    f"squareoff_failures={len(report['squareoff_failures'])}"
                ),
                last_price=None,
                quantity=0,
                lots=0,
                capital_used=0.0,
                side="-",
                placed=False,
                dry_run=True,
            )
        )
        return report

    async def close_position(
        self,
        *,
        tradingsymbol: str,
        exchange: str,
        symboltoken: str,
        net_qty: int,
        producttype: str | None = None,
    ) -> dict[str, Any]:
        """Send a market reverse order for a single broker position."""
        api = self.smart_client()
        if api is None:
            raise RuntimeError("Not connected.")
        row = {
            "tradingsymbol": tradingsymbol,
            "exchange": exchange,
            "symboltoken": symboltoken,
            "net_qty": net_qty,
            "producttype": producttype,
        }
        return await self._close_position_row(api, row)

    async def _close_position_row(self, api: SmartApiClient, r: dict[str, Any]) -> dict[str, Any]:
        qty = int(r.get("net_qty") or 0)
        if qty == 0:
            return {"symbol": r.get("tradingsymbol"), "skipped": "flat"}
        side = "SELL" if qty > 0 else "BUY"
        inst = Instrument(
            exchange=str(r.get("exchange") or "").upper(),
            tradingsymbol=str(r.get("tradingsymbol") or ""),
            symboltoken=str(r.get("symboltoken") or ""),
        )
        product = (r.get("producttype") or self.settings.bot_default_product or "INTRADAY").upper()
        payload = build_order_payload(
            inst,
            variety=self.settings.bot_default_variety,
            transactiontype=side,
            ordertype="MARKET",
            producttype=product,
            quantity=abs(qty),
        )
        validate_order_payload(payload)
        resp = await api.place_order(payload)
        oid = extract_place_order_id(resp) if isinstance(resp, dict) else None
        if oid:
            self.store.log_order(
                payload, oid, status="placed", lifecycle_status="placed",
                placed_by_bot=True, intent="close",
            )
        self.decisions.add(
            Decision(
                ts=DecisionLog.now_iso(),
                name=inst.tradingsymbol,
                exchange=inst.exchange,
                token=inst.symboltoken,
                signal="MODE",
                reason=f"manual_close_{side.lower()}",
                last_price=None,
                quantity=abs(qty),
                lots=0,
                capital_used=0.0,
                side="-",
                placed=bool(oid),
                dry_run=False,
                broker_order_id=oid,
            )
        )
        return {"symbol": inst.tradingsymbol, "side": side, "qty": abs(qty), "broker_order_id": oid}

    async def shutdown(self) -> None:
        await self.disconnect()


def _redact(obj: Any) -> Any:
    try:
        s = json.dumps(obj, default=str)
        return json.loads(s)
    except Exception:
        return None
