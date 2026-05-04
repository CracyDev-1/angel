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
from angel_bot.exits.live import LiveExitConfig, LiveExitManager
from angel_bot.instruments.loader import MasterStatus, ensure_local_master
from angel_bot.instruments.master import Instrument, InstrumentMaster
from angel_bot.instruments.universe import BuildReport, UniverseBuilder, UniverseSpec
from angel_bot.llm import (
    LlmClassification,
    LlmDecision,
    llm_classify_setup,
    llm_filter_setup,
)
from angel_bot.orders.tracker import OrderTracker, extract_place_order_id
from angel_bot.market_hours import all_market_status, kind_market_status
from angel_bot.paper import PaperConfig, PaperOpenRequest, PaperTrader
from angel_bot.ratelimit import get_rate_limiter
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
        self.risk = RiskEngine(self.settings, store=self.store)
        self._dup_guard = DuplicateOrderGuard(ttl_s=60.0)
        self._tracker = OrderTracker(self.store)
        self.paper = PaperTrader(
            self.store,
            PaperConfig(
                stop_loss_pct=self.settings.paper_stop_loss_pct,
                take_profit_pct=self.settings.paper_take_profit_pct,
                max_hold_minutes=self.settings.paper_max_hold_minutes,
                max_open_positions=self.settings.paper_max_open_positions,
            ),
        )
        # Live-mode equivalent of PaperTrader. Uses the SAME SL / TP / max-hold
        # numbers so live behavior mirrors what dry-run was showing the user.
        # Wired with a price-lookup callback that prefers the scanner cache
        # (always-fresh) and falls back to broker positions LTP.
        self.live_exits = LiveExitManager(
            self.store,
            LiveExitConfig(
                stop_loss_pct=self.settings.paper_stop_loss_pct,
                take_profit_pct=self.settings.paper_take_profit_pct,
                max_hold_minutes=self.settings.paper_max_hold_minutes,
            ),
            price_lookup=self._live_exit_price_lookup,
        )

        self.last_funds: dict[str, Any] | None = None
        self.last_positions: dict[str, Any] | None = None
        self.last_scanner: list[ScannerHit] = []
        self.last_loop_at: str | None = None
        self.last_scan_summary: dict[str, Any] | None = None
        self.bot_started_at: str | None = None
        # How many aggregators were successfully seeded from broker history
        # at startup. 0 means we're back to live-tick warmup. Surfaced in
        # the snapshot so the dashboard can show "warmed from history".
        self._warmup_seeded: int = 0
        # Set of watchlist keys ("EXCHANGE:TOKEN") we've already seeded so
        # universe rebuilds can detect *new* tokens and only backfill those.
        self._warmed_keys: set[str] = set()
        # How many INDEX candidates the last scan cycle dropped because
        # their resolved ATM CE / PE 1-lot premium is above the user's
        # deployable cash. Surfaced in the scan summary so the dashboard
        # can show "Filtered N indexes — lot above your cash" instead of
        # spamming the decision log with identical need_more_capital rows.
        self._last_index_unaffordable: int = 0
        # Per-skip-key dedupe: collapses repeated identical skip reasons
        # (most commonly need_more_capital) so the decision log isn't
        # flooded with the same row every scan cycle. Maps the dedup key
        # to the ISO timestamp of the last decision we recorded.
        self._last_skip_at: dict[str, str] = {}
        self.auto_mode: bool = totp_configured_in_env(self.settings)
        self._watchdog_task: asyncio.Task | None = None
        # runtime mode override — flipped by the dashboard "Go Live" toggle
        # without restarting the process. Initialized from .env (TRADING_ENABLED).
        self._runtime_trading_enabled: bool = bool(self.settings.trading_enabled)
        # Dry-run capital override. 0 = use real broker available_cash. Adjustable
        # in real time from the dashboard so the user can stress-test "what would
        # the bot do with ₹X?" without changing the live account.
        self._dryrun_capital_override: float = float(self.settings.dryrun_capital_override or 0.0)

        # Instrument master + dynamic universe. Master is loaded lazily because
        # the file may not exist on first run; load_master() can be called from
        # the dashboard or auto on startup. Until then, the scanner falls back
        # to SCANNER_WATCHLIST_JSON.
        self.master: InstrumentMaster | None = None
        self.master_status: MasterStatus | None = None
        self.universe_spec: UniverseSpec = self._read_universe_spec()
        self._dynamic_watchlist: dict[str, list[dict[str, Any]]] | None = None
        self._last_universe_report: BuildReport | None = None
        self._last_atm_refresh_at: str | None = None
        self._atm_task: asyncio.Task | None = None
        # Per-kind toggles. ON = scanner polls + bot may trade; OFF = excluded
        # from the watchlist entirely (no API calls, no trades). Toggleable
        # at runtime from the dashboard.
        # COMMODITY defaults to OFF: each MCX symbol consumes part of
        # Angel's per-request Quote token budget, and combined with the
        # 7-underlying ATM option chain (~140 rows) it reliably triggers
        # AB1004 ("Tokens max limit exceeded"). Users who explicitly want
        # commodities can re-enable from the dashboard *and* repopulate
        # `commodities` in UNIVERSE_SPEC_JSON; nothing in the code path
        # has been removed.
        self.kind_enabled: dict[str, bool] = {
            "INDEX": True,
            "EQUITY": True,
            "COMMODITY": False,
            "OPTION": True,
        }

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

    @property
    def mode(self) -> str:
        return "live" if self._runtime_trading_enabled else "dryrun"

    @property
    def dryrun_capital_override(self) -> float:
        return self._dryrun_capital_override

    def set_dryrun_capital(self, amount: float) -> dict[str, Any]:
        """Set / clear the dry-run capital override (₹). 0 = use live broker cash."""
        amt = max(0.0, float(amount or 0.0))
        prev = self._dryrun_capital_override
        self._dryrun_capital_override = amt
        log.info("dryrun_capital_changed", from_=prev, to=amt)
        self.decisions.add(
            Decision(
                ts=DecisionLog.now_iso(),
                name="-",
                exchange="-",
                token="-",
                signal="MODE",
                reason=f"dryrun_capital_set:{amt:.0f}" if amt > 0 else "dryrun_capital_cleared",
                last_price=None,
                quantity=0,
                lots=0,
                capital_used=amt,
                side="-",
                placed=False,
                dry_run=not self._runtime_trading_enabled,
            )
        )
        return {"dryrun_capital_override": self._dryrun_capital_override}

    def close_paper_position(self, paper_id: int) -> dict[str, Any]:
        """Square off a single open paper position at its last marked price."""
        rows = [r for r in self.store.list_open_paper_positions() if int(r["id"]) == int(paper_id)]
        if not rows:
            return {"closed": False, "reason": "not_open"}
        last = float(rows[0].get("last_price") or rows[0]["entry_price"])
        ev = self.paper.manual_close(int(paper_id), last)
        if ev is None:
            return {"closed": False, "reason": "race"}
        self.decisions.add(
            Decision(
                ts=DecisionLog.now_iso(),
                name=ev.tradingsymbol,
                exchange="-",
                token="-",
                signal="MODE",
                reason=f"paper_close_manual: pnl ₹{ev.realized_pnl:+.2f}",
                last_price=ev.exit_price,
                quantity=ev.qty,
                lots=0,
                capital_used=ev.entry_price * ev.qty,
                side=ev.side,
                placed=False,
                dry_run=True,
            )
        )
        return {"closed": True, "event": ev.__dict__}

    def _read_universe_spec(self) -> UniverseSpec:
        try:
            raw = self.settings.universe_spec()
            return UniverseSpec.from_dict(raw) if raw else UniverseSpec.default()
        except Exception as e:  # noqa: BLE001
            log.warning("invalid_universe_spec", error=str(e))
            return UniverseSpec.default()

    async def ensure_master(self, *, force_download: bool = False) -> dict[str, Any]:
        """Make sure the instrument master is loaded; (re)download if needed.

        Returns the new master status. Safe to call from the API.
        """
        try:
            master, st = await ensure_local_master(self.settings, force=force_download)
        except Exception as e:  # noqa: BLE001
            self.last_error = f"instrument_master: {e}"
            log.warning("instrument_master_load_failed", error=str(e))
            return {
                "ok": False,
                "error": str(e),
                "status": self.master_status.__dict__ if self.master_status else None,
            }
        self.master = master
        self.master_status = st
        # Rebuild the dynamic watchlist immediately (without ATM yet — needs spot).
        self._rebuild_universe(spot_provider=None)
        return {
            "ok": True,
            "status": st.__dict__,
            "report": self._last_universe_report.to_dict() if self._last_universe_report else None,
        }

    def set_universe_spec(self, spec_dict: dict[str, Any]) -> dict[str, Any]:
        """Replace the live universe spec (does not persist to .env)."""
        self.universe_spec = UniverseSpec.from_dict(spec_dict)
        if self.master is not None:
            self._rebuild_universe(spot_provider=self._scanner_spot_provider)
        return {"spec": self.universe_spec.__dict__}

    def _scanner_spot_provider(self, underlying: str) -> float | None:
        """Look up the latest spot from whatever the scanner has cached.

        Match priority — the universe spec uses keys like ``"NIFTY"`` /
        ``"BANKNIFTY"`` while the broker display tradingsymbol may be
        ``"NIFTY 50"`` / ``"NIFTY BANK"`` etc. We therefore try the explicit
        ``underlying`` field first (set by the universe builder for INDEX
        rows), then the display name, then a loose first-token match for
        backwards compatibility with manually-curated watchlists.
        """
        u = underlying.strip().upper()
        if not u:
            return None
        for h in self.scanner.last_hits:
            if not h.last_price:
                continue
            if h.underlying and h.underlying.upper() == u:
                return float(h.last_price)
            if h.name.upper() == u:
                return float(h.last_price)
        # Fallback: prefix match (e.g. "NIFTY" matching legacy "NIFTY 50").
        for h in self.scanner.last_hits:
            if not h.last_price:
                continue
            first = h.name.split()[0].upper() if h.name else ""
            if first and first == u:
                return float(h.last_price)
        return None

    def _disabled_kinds(self) -> set[str]:
        return {k for k, v in self.kind_enabled.items() if not v}

    def _rebuild_universe(
        self, *, spot_provider: callable | None
    ) -> None:
        if self.master is None:
            return
        builder = UniverseBuilder(self.master)
        watchlist, report = builder.build(
            self.universe_spec,
            spot_provider=spot_provider,
            disabled_kinds=self._disabled_kinds(),
        )
        self._dynamic_watchlist = watchlist
        self._last_universe_report = report
        self._last_atm_refresh_at = datetime.now(UTC).isoformat()
        # Push into the scanner so the next poll picks it up.
        self.scanner.set_watchlist(watchlist)
        log.info(
            "universe_rebuilt",
            indices=report.indices_resolved,
            stocks=report.stocks_resolved,
            commodities=report.commodities_resolved,
            atm=report.atm_resolved,
            missing_atm=len(report.atm_missing or []),
        )

    def universe_state(self) -> dict[str, Any]:
        return {
            "master": (self.master_status.__dict__ if self.master_status else None),
            "spec": self.universe_spec.__dict__,
            "report": self._last_universe_report.to_dict() if self._last_universe_report else None,
            "watchlist": self._dynamic_watchlist or self.settings.scanner_watchlist(),
            "last_atm_refresh_at": self._last_atm_refresh_at,
            "kind_enabled": dict(self.kind_enabled),
        }

    def set_kind_enabled(self, kinds: dict[str, bool]) -> dict[str, Any]:
        """Toggle which instrument categories the bot watches and trades.

        Body: ``{"INDEX": true, "EQUITY": false, "COMMODITY": true, "OPTION": true}``.
        Disabled kinds are excluded from the next universe rebuild and the
        scanner stops polling them. They are also skipped at trade time.
        """
        for raw_k, v in (kinds or {}).items():
            k = str(raw_k).strip().upper()
            if k in self.kind_enabled:
                self.kind_enabled[k] = bool(v)
        # Rebuild immediately so the change is visible in the next scan.
        if self.master is not None:
            self._rebuild_universe(spot_provider=self._scanner_spot_provider)
        return {"kind_enabled": dict(self.kind_enabled)}

    def search_instruments(
        self,
        query: str,
        *,
        exchange: str | None = None,
        kind: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        if self.master is None:
            return []
        rows = self.master.search(query, exchange=exchange, kind=kind, limit=limit)
        return [r.to_dict() for r in rows]

    def reset_paper(self) -> dict[str, Any]:
        """Wipe paper book + dry-run daily stats + dry-run order history."""
        self.paper.reset()
        self.decisions.add(
            Decision(
                ts=DecisionLog.now_iso(),
                name="-",
                exchange="-",
                token="-",
                signal="MODE",
                reason="paper_reset",
                last_price=None,
                quantity=0,
                lots=0,
                capital_used=0.0,
                side="-",
                placed=False,
                dry_run=True,
            )
        )
        return {"reset": True}

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
        if self._atm_task:
            self._atm_task.cancel()
            try:
                await self._atm_task
            except asyncio.CancelledError:
                pass
            self._atm_task = None
        self._stop = asyncio.Event()

    async def _atm_refresher_loop(self) -> None:
        """Rebuild ATM option subscriptions every N seconds while the bot runs.

        Index spot moves throughout the day; the ATM strike with it. Without
        this task the watchlist would freeze on whatever the ATM was at start.
        Newly added tokens are also backfilled from broker history so the
        brain isn't stuck in warmup on a fresh strike.
        """
        interval = max(15.0, float(self.settings.atm_refresh_interval_s))
        try:
            # Wait for the scanner to populate at least one cycle before the
            # first rebuild so we have spot prices to anchor on.
            await asyncio.sleep(min(15.0, interval / 2))
            while not self._stop.is_set():
                try:
                    self._rebuild_universe(spot_provider=self._scanner_spot_provider)
                    await self._warmup_new_universe_keys()
                except Exception as e:  # noqa: BLE001
                    log.warning("atm_refresh_failed", error=str(e))
                slept = 0.0
                step = 1.0
                while slept < interval and not self._stop.is_set():
                    await asyncio.sleep(step)
                    slept += step
        except asyncio.CancelledError:
            return

    async def _warmup_new_universe_keys(self) -> None:
        """Seed history for any watchlist tokens we haven't backfilled yet.

        Called after each ATM rebuild so a newly resolved CE / PE strike
        doesn't sit in ``warmup`` for 25 minutes — its 5m / 15m bars are
        pulled from the broker's historical-candle API the moment it
        joins the watchlist.
        """
        s = self.settings
        if not s.bot_warmup_from_history:
            return
        api = self.smart_client()
        if api is None:
            return
        wl = self.scanner.active_watchlist()
        all_keys: set[str] = set()
        for ex, items in (wl or {}).items():
            for it in items:
                tok = str(it.get("token", "")).strip()
                if tok:
                    all_keys.add(f"{ex.upper()}:{tok}")
        new_keys = all_keys - self._warmed_keys
        if not new_keys:
            return
        try:
            seeded = await self.scanner.warmup_from_history(
                api,
                only_keys=new_keys,
                lookback_5m_minutes=int(s.bot_warmup_lookback_5m_min),
                lookback_15m_minutes=int(s.bot_warmup_lookback_15m_min),
                lookback_1m_minutes=int(s.bot_warmup_lookback_1m_min),
                max_concurrent=int(s.bot_warmup_concurrency),
            )
            self._warmed_keys |= new_keys
            self._warmup_seeded += int(seeded or 0)
            log.info(
                "scanner_warmup_universe_delta",
                new_keys=len(new_keys),
                seeded=seeded,
                total_warmed=len(self._warmed_keys),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("scanner_warmup_universe_delta_failed", error=str(e))

    async def start_bot(self) -> None:
        if not self.connected():
            raise RuntimeError("Connect with TOTP on the dashboard first.")
        await self.stop_bot()
        self._stop = asyncio.Event()
        self.bot_started_at = datetime.now(UTC).isoformat()
        self._bot_task = asyncio.create_task(self._auto_trader_loop(), name="angel-auto-trader")
        if self.auto_mode and self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(self._session_watchdog(), name="angel-session-watchdog")
        # Spin up the ATM refresher only if there are atm_for entries; otherwise
        # the universe is purely static and a periodic rebuild adds no value.
        if self.master is not None and self.universe_spec.atm_for and self._atm_task is None:
            self._atm_task = asyncio.create_task(self._atm_refresher_loop(), name="angel-atm-refresher")

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
        paper_open = self.paper.open_positions_summary()
        funds = self.last_funds or {}
        live_cash = float(funds.get("available_cash") or 0.0)
        return {
            "connected": self.connected(),
            "bot_running": self.bot_running(),
            "trading_enabled": self._runtime_trading_enabled,
            "mode": self.mode,
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
            "rate_limit": self._rate_limit_summary(),
            "paper": {
                "config": {
                    "stop_loss_pct": self.settings.paper_stop_loss_pct,
                    "take_profit_pct": self.settings.paper_take_profit_pct,
                    "max_hold_minutes": self.settings.paper_max_hold_minutes,
                    "max_open_positions": self.settings.paper_max_open_positions,
                },
                "open": paper_open,
                "today": self.paper.today_summary(),
            },
            "dryrun": {
                "capital_override": self._dryrun_capital_override,
                "live_available_cash": live_cash,
                "deployable_cash": self._deployable_cash(live_cash),
            },
            "universe": self.universe_state(),
            "market_hours": all_market_status(),
            "live_exits": self._live_exits_snapshot(),
            "warmup": {
                "from_history": bool(self.settings.bot_warmup_from_history),
                "seeded_aggregators": int(self._warmup_seeded),
                "warmed_tokens": len(self._warmed_keys),
            },
        }

    def _live_exits_snapshot(self) -> dict[str, Any]:
        """Compact summary of every open ``live_exit_plans`` row.

        The dashboard uses this to badge bot-managed positions on the broker
        positions table and to surface the SL / TP / max-hold the bot will
        enforce on each one. Adopted plans show ``source='adopted'`` so the
        UI can label them as picked up from the Angel One platform.
        """
        try:
            rows = self.store.list_open_live_exit_plans()
        except Exception as e:  # noqa: BLE001
            log.warning("live_exits_snapshot_failed", error=str(e))
            return {"open": [], "managed_count": 0, "adopted_count": 0}
        out: list[dict[str, Any]] = []
        adopted = 0
        for r in rows:
            src = str(r.get("source") or "bot").lower() or "bot"
            if src == "adopted":
                adopted += 1
            out.append(
                {
                    "plan_id": int(r.get("id") or 0),
                    "tradingsymbol": r.get("tradingsymbol"),
                    "exchange": r.get("exchange"),
                    "symboltoken": str(r.get("symboltoken") or ""),
                    "side": r.get("side"),
                    "qty": int(r.get("qty") or 0),
                    "lots": int(r.get("lots") or 0),
                    "lot_size": int(r.get("lot_size") or 1),
                    "fill_price": r.get("fill_price"),
                    "planned_entry": r.get("planned_entry"),
                    "stop_price": r.get("stop_price"),
                    "target_price": r.get("target_price"),
                    "max_hold_minutes": int(r.get("max_hold_minutes") or 0),
                    "opened_at": r.get("opened_at"),
                    "filled_at": r.get("filled_at"),
                    "source": src,
                    "underlying": r.get("underlying"),
                    "kind": r.get("kind"),
                }
            )
        return {
            "open": out,
            "managed_count": len(out),
            "adopted_count": adopted,
        }

    def _deployable_cash(self, live_cash: float) -> float:
        """How much capital the bot will *use* this cycle for sizing.

        In live mode: live_cash * BOT_USE_CAPITAL_PCT.
        In dry-run with override: override * BOT_USE_CAPITAL_PCT (so the user's
        chosen capital still respects the same risk-per-cycle cap).
        In dry-run without override: live_cash * BOT_USE_CAPITAL_PCT (so the
        sim mirrors what live would do today).
        """
        s = self.settings
        base = live_cash
        if not self._runtime_trading_enabled and self._dryrun_capital_override > 0:
            base = self._dryrun_capital_override
        return base * (s.bot_use_capital_pct / 100.0)

    def _rate_limit_summary(self) -> dict[str, Any]:
        """Compact view of the limiter so the dashboard can show 'are we
        anywhere near a cap?' without dumping every endpoint."""
        try:
            full = get_rate_limiter().stats()
        except Exception:  # noqa: BLE001 — never break the snapshot
            return {"enabled": True, "near_cap": [], "calls_total": 0, "waits_total": 0}
        endpoints = full.get("endpoints", {}) or {}
        groups = full.get("groups", {}) or {}
        near_cap: list[dict[str, Any]] = []
        for path, rows in endpoints.items():
            for r in rows:
                limit = int(r.get("limit") or 0)
                used = int(r.get("in_window") or 0)
                if limit and used / limit >= 0.7:
                    near_cap.append(
                        {
                            "path": path.rsplit("/", 1)[-1],
                            "window_s": r.get("window_s"),
                            "used": used,
                            "limit": limit,
                        }
                    )
        for grp, rows in groups.items():
            for r in rows:
                limit = int(r.get("limit") or 0)
                used = int(r.get("in_window") or 0)
                if limit and used / limit >= 0.7:
                    near_cap.append(
                        {
                            "path": f"group:{grp}",
                            "window_s": r.get("window_s"),
                            "used": used,
                            "limit": limit,
                        }
                    )
        return {
            "enabled": full.get("enabled", True),
            "safety_factor": full.get("safety_factor", 0.9),
            "calls_total": full.get("calls_total", 0),
            "waits_total": full.get("waits_total", 0),
            "last_wait_s": full.get("last_wait_s", 0.0),
            "near_cap": near_cap,
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
        """Roll-up of what the bot did *today* in the current mode.

        In live mode:
          - trades_placed counts real bot-placed orders
          - unrealized_pnl = broker open positions P&L
          - realized_pnl = today's daily_stats_mode['live']
        In dry-run mode:
          - trades_placed counts paper opens AND closes for the day
          - unrealized_pnl = mark-to-market on open paper positions
          - realized_pnl = today's daily_stats_mode['dryrun']
        """
        if self._runtime_trading_enabled:
            rows = [r for r in self.store.bot_orders_today() if (r.get("mode") or "live").lower() == "live"]
            trades_placed = len(rows)
            unrealized = float(positions.get("pnl_total") or 0.0)
            realized_today, _ = (self.store.get_mode_daily_stats("live")[1], 0)
            realized_today = self.store.get_mode_daily_stats("live")[1]
            return {
                "mode": "live",
                "trades_placed": trades_placed,
                "pending": len([r for r in rows if (r.get("lifecycle_status") or "").lower() not in ("executed", "complete", "cancelled", "rejected")]),
                "filled": len([r for r in rows if (r.get("lifecycle_status") or "").lower() in ("executed", "complete")]),
                "rejected": len([r for r in rows if (r.get("lifecycle_status") or "").lower() == "rejected"]),
                "unrealized_pnl": unrealized,
                "realized_pnl": realized_today,
                "net_pnl": realized_today + unrealized,
            }
        # Dry-run summary comes straight from the paper trader.
        psum = self.paper.today_summary()
        opens = self.paper.open_positions_summary()
        return {
            "mode": "dryrun",
            "trades_placed": opens["open_positions"] + psum["trades"],  # opens + closes today
            "pending": 0,
            "filled": psum["trades"],
            "rejected": 0,
            "unrealized_pnl": psum["unrealized_pnl"],
            "realized_pnl": psum["realized_pnl"],
            "net_pnl": psum["net_pnl"],
        }

    def history(self, *, orders_limit: int = 200, mode: str = "live") -> dict[str, Any]:
        m = (mode or "live").lower()
        if m not in ("live", "dryrun"):
            m = "live"
        if m == "live":
            rows = self.store.recent_orders_by_mode("live", orders_limit)
            all_days = self.store.all_mode_daily_stats("live")
            # Backfill from the legacy daily_stats table for old runs that didn't
            # write to daily_stats_mode. Avoid double-counting same day.
            seen = {d["day"] for d in all_days}
            for d in self.store.all_daily_stats():
                if d["day"] not in seen:
                    all_days.append(d)
            all_days.sort(key=lambda d: d["day"], reverse=True)
        else:
            rows = self.store.recent_orders_by_mode("dryrun", orders_limit)
            all_days = self.store.all_mode_daily_stats("dryrun")
        total_pnl = sum(float(d.get("pnl") or 0.0) for d in all_days)
        total_trades = sum(int(d.get("trades") or 0) for d in all_days)
        out: dict[str, Any] = {
            "mode": m,
            "orders": summarize_orders_for_ui(rows),
            "all_days": all_days,
            "totals": {
                "trades": total_trades,
                "realized_pnl": total_pnl,
                "days_traded": len(all_days),
            },
        }
        if m == "dryrun":
            out["paper_positions"] = self.store.list_recent_paper_positions(orders_limit)
        return out

    def _daily_stats(self) -> dict[str, Any]:
        trades, pnl = self.store.get_daily_stats()
        # Same priority as RiskEngine.effective_capital: explicit override
        # wins, otherwise live broker cash, otherwise 0.
        cap = self.risk.effective_capital()
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

        # Seed each watchlist symbol's candle aggregator from the broker's
        # historical-candle API so the brain doesn't sit in ``warmup`` for
        # 25 minutes after every restart. We deliberately do this BEFORE
        # the first poll_once so cycle 1 already has ≥5 5m bars and ≥2 15m
        # bars in memory; signals can fire immediately. Failures are
        # non-fatal — the loop falls back to live-tick warmup.
        if s.bot_warmup_from_history:
            try:
                seeded = await self.scanner.warmup_from_history(
                    api,
                    lookback_5m_minutes=int(s.bot_warmup_lookback_5m_min),
                    lookback_15m_minutes=int(s.bot_warmup_lookback_15m_min),
                    lookback_1m_minutes=int(s.bot_warmup_lookback_1m_min),
                    max_concurrent=int(s.bot_warmup_concurrency),
                )
                self._warmup_seeded = int(seeded or 0)
                wl = self.scanner.active_watchlist()
                for ex, items in (wl or {}).items():
                    for it in items:
                        tok = str(it.get("token", "")).strip()
                        if tok:
                            self._warmed_keys.add(f"{ex.upper()}:{tok}")
            except Exception as e:  # noqa: BLE001 — never block the loop on warmup
                log.warning("auto_trader_warmup_failed", error=str(e))
                self._warmup_seeded = 0

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
                    # Push live broker cash into RiskEngine so when
                    # RISK_CAPITAL_RUPEES=0 the sizing math uses real cash.
                    self.risk.set_broker_cash(available)
                    # Capital actually deployable for sizing this cycle.
                    # In dry-run with override, this is the override amount; in
                    # live (or dry-run without override), this is the live cash.
                    deployable = self._deployable_cash(available)

                    hits = await self.scanner.poll_once(api, available_funds=deployable)
                    self.last_scanner = hits

                    # Mark every open paper position to market using the freshly
                    # fetched LTPs. Closures (stop/target/timeout) update
                    # daily_stats_mode['dryrun'] inside the paper trader.
                    paper_closures = self.paper.mark_and_close(self.scanner.latest_prices())
                    for ev in paper_closures:
                        self.decisions.add(
                            Decision(
                                ts=DecisionLog.now_iso(),
                                name=ev.tradingsymbol,
                                exchange="-",
                                token="-",
                                signal="MODE",
                                reason=f"paper_close_{ev.exit_reason}: pnl ₹{ev.realized_pnl:+.2f}",
                                last_price=ev.exit_price,
                                quantity=ev.qty,
                                lots=0,
                                capital_used=ev.entry_price * ev.qty,
                                side=ev.side,
                                placed=False,
                                dry_run=True,
                            )
                        )
                        # Feed the post-loss cooldown timer.
                        self.risk.record_close(realized_pnl=float(ev.realized_pnl or 0.0))

                    # Adopt long broker positions opened directly on the
                    # Angel One platform (mobile / web) so the bot can manage
                    # their SL / TP / max-hold exactly like its own trades.
                    # Adoption is a no-op in dry-run / paper modes — we never
                    # touch real money from a sim. Also detects user-side
                    # manual closes and books the realized P&L into live stats.
                    if self._runtime_trading_enabled and s.bot_adopt_external_positions:
                        try:
                            adoption_events = self.live_exits.reconcile_external_positions(
                                (positions or {}).get("rows") or [],
                                master=self.master,
                                product_types={
                                    p.strip().upper()
                                    for p in (s.bot_adopt_product_types or "").split(",")
                                    if p.strip()
                                },
                                sl_pct=s.paper_stop_loss_pct,
                                tp_pct=s.paper_take_profit_pct,
                                max_hold_minutes=s.paper_max_hold_minutes,
                                default_variety=s.bot_default_variety,
                            )
                        except Exception as e:  # noqa: BLE001
                            adoption_events = []
                            log.warning("live_exit_adopt_reconcile_failed", error=str(e))
                        for ae in adoption_events:
                            if ae.kind == "adopted":
                                reason = (
                                    f"adopted_external {ae.tradingsymbol} "
                                    f"qty {ae.qty} @ ₹{ae.entry_price:.2f}"
                                )
                                placed = True
                                price = ae.entry_price
                                pnl_chunk = 0.0
                            elif ae.kind == "qty_resync":
                                prev = ae.extra.get("prev_qty")
                                reason = (
                                    f"adopted_resync {ae.tradingsymbol} qty "
                                    f"{prev}→{ae.qty}"
                                )
                                placed = False
                                price = ae.entry_price
                                pnl_chunk = 0.0
                            else:  # external_close
                                reason = (
                                    f"external_close {ae.tradingsymbol}: "
                                    f"pnl ₹{ae.realized_pnl or 0.0:+.2f}"
                                )
                                placed = True
                                price = ae.exit_price or ae.entry_price
                                pnl_chunk = float(ae.realized_pnl or 0.0)
                                self.risk.record_close(realized_pnl=pnl_chunk)
                            self.decisions.add(
                                Decision(
                                    ts=DecisionLog.now_iso(),
                                    name=ae.tradingsymbol,
                                    exchange=ae.exchange,
                                    token=ae.symboltoken,
                                    signal="MODE",
                                    reason=reason,
                                    last_price=price,
                                    quantity=ae.qty,
                                    lots=int(ae.extra.get("lots") or 0),
                                    capital_used=ae.entry_price * ae.qty,
                                    side=ae.side,
                                    placed=placed,
                                    dry_run=False,
                                    extra={
                                        "adoption": {
                                            "kind": ae.kind,
                                            "plan_id": ae.plan_id,
                                            **ae.extra,
                                        }
                                    },
                                )
                            )

                    # Live-position exit manager: enforces premium SL / TP /
                    # max-hold for every position the bot opened in live mode.
                    # Always runs (even in dry-run) so a session that flipped
                    # back to dry-run with positions still open still gets
                    # them managed. Each closure also feeds the post-loss
                    # cooldown.
                    try:
                        live_closures = await self.live_exits.mark_and_close(api)
                    except Exception as e:  # noqa: BLE001 — never crash the loop
                        live_closures = []
                        log.warning("live_exit_mark_and_close_failed", error=str(e))
                    for lev in live_closures:
                        src_tag = f" ({lev.source})" if lev.source and lev.source != "bot" else ""
                        self.decisions.add(
                            Decision(
                                ts=DecisionLog.now_iso(),
                                name=lev.tradingsymbol,
                                exchange="-",
                                token="-",
                                signal="MODE",
                                reason=(
                                    f"live_close_{lev.exit_reason}{src_tag}: "
                                    f"pnl ₹{lev.realized_pnl:+.2f}"
                                ),
                                last_price=lev.exit_price,
                                quantity=lev.qty,
                                lots=0,
                                capital_used=lev.entry_price * lev.qty,
                                side=lev.side,
                                placed=True,
                                dry_run=False,
                                broker_order_id=lev.close_order_id,
                                extra={
                                    "live_exit": True,
                                    "exit_reason": lev.exit_reason,
                                    "source": lev.source,
                                },
                            )
                        )
                        self.risk.record_close(realized_pnl=float(lev.realized_pnl or 0.0))

                    self._record_scan_summary(hits, positions, available, deployable)

                    # NEW: rank → take TOP-N → process each independently. The
                    # max-concurrent + per-hour caps inside _consider_trade
                    # naturally short-circuit further attempts when full.
                    candidates = self._select_top_candidates(
                        hits, positions, n=s.llm_top_n_candidates, deployable=deployable
                    )
                    if not candidates:
                        # Still log a NO_TRADE skip so the dashboard doesn't go silent.
                        await self._consider_trade(api, None, deployable)
                    else:
                        for cand in candidates:
                            # Recompute open count after each placement so the
                            # gate inside _consider_trade sees fresh state.
                            self.risk.set_open_count(self._current_open_count())
                            if self.risk.state.open_position_count >= s.bot_max_concurrent_positions:
                                break
                            await self._consider_trade(api, cand, deployable)
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

    # Hits with kind=="OPTION" come from the universe builder (live premium for
    # the dashboard) but the brain on premium movements is noisy — we let the
    # brain on the *underlying* drive trade decisions instead.
    _SIGNAL_SOURCE_KINDS: frozenset[str] = frozenset({"INDEX", "EQUITY", "COMMODITY"})

    def _current_open_count(self) -> int:
        """Live = broker open positions; dry-run = open paper positions."""
        if self._runtime_trading_enabled:
            return int((self.last_positions or {}).get("open_positions", 0) or 0)
        return int(self.paper.open_positions_summary().get("open_positions", 0) or 0)

    def _select_top_candidates(
        self,
        hits: list[ScannerHit],
        positions: dict[str, Any],
        *,
        n: int = 3,
        deployable: float | None = None,
    ) -> list[ScannerHit]:
        """Return up to N best-ranked candidates that the brain has signalled
        BUY_CALL / BUY_PUT on, in score order.

        Cheap filters (kind, signal exists, score floor, lot-fit for cash
        instruments) are applied here. INDEX candidates whose resolved
        ATM CE / PE 1-lot premium is already above ``deployable`` cash are
        also dropped — without this the brain keeps proposing
        permanently-unaffordable indexes (e.g. NIFTYNXT50 @ ₹72k/lot when
        the user has ₹12k) and the decision log fills with identical
        ``need_more_capital`` rows every scan cycle. Heavier gates (market
        hours, risk, LLM) are applied per-candidate inside _consider_trade.
        """
        if not hits:
            self._last_index_unaffordable = 0
            return []
        s = self.settings
        live_open = int(positions.get("open_positions", 0) or 0)
        paper_open = int(self.paper.open_positions_summary().get("open_positions", 0))
        open_now = live_open if self._runtime_trading_enabled else paper_open
        if open_now >= s.bot_max_concurrent_positions:
            self._last_index_unaffordable = 0
            return []

        min_score = max(s.strategy_min_score, s.bot_min_signal_strength)
        keep: list[ScannerHit] = []
        index_unaffordable = 0
        for h in hits:
            if h.kind not in self._SIGNAL_SOURCE_KINDS:
                continue
            if h.last_price is None or h.last_price <= 0:
                continue
            if h.score < min_score:
                continue
            if h.signal_side not in ("BUY_CALL", "BUY_PUT"):
                continue
            # Hidden by the affordability gate — keep it in the scanner cache
            # for premium lookup but do not propose it as a candidate.
            if not h.is_affordable and h.kind != "INDEX":
                continue
            if h.kind != "INDEX":
                if not h.lot_size or not h.affordable_lots or h.affordable_lots < 1:
                    continue
                if not h.in_trade_value_range:
                    continue
            else:
                # INDEX → resolve to its ATM CE / PE and drop the candidate
                # if even one lot of the option is permanently unaffordable.
                # Tolerant of resolve failures: if we can't price the option
                # yet, fall through and let _consider_trade record a clean
                # skip (resolve / no_execution_price) instead of swallowing
                # it silently.
                if deployable is not None and deployable > 0:
                    exec_inst, exec_lot, exec_price, _why = self._resolve_executable(
                        h, h.signal_side
                    )
                    if (
                        exec_inst is not None
                        and exec_price is not None
                        and exec_price > 0
                        and exec_lot
                    ):
                        if exec_price * exec_lot > float(deployable):
                            index_unaffordable += 1
                            continue
            keep.append(h)

        keep.sort(key=lambda h: h.score, reverse=True)
        # Cap the slate. The runtime breaks out early once max-concurrent fills.
        slots = max(1, min(int(n or 1), s.bot_max_concurrent_positions))
        self._last_index_unaffordable = index_unaffordable
        return keep[:slots]

    # Kept for backwards-compat with any external callers (tests etc.)
    def _pick_candidate(self, hits: list[ScannerHit], positions: dict[str, Any]) -> ScannerHit | None:
        cands = self._select_top_candidates(hits, positions, n=1)
        return cands[0] if cands else None

    # ------------------------------------------------------------------
    # Execution-instrument resolution (signal → tradeable instrument)
    # ------------------------------------------------------------------

    def _resolve_executable(
        self, hit: ScannerHit, signal: str
    ) -> tuple[Instrument | None, int, float | None, str]:
        """Given a brain signal on an underlying, return:
            (instrument_to_trade, lot_size, last_price, reason)

        * INDEX  → resolve nearest-expiry ATM CE (BUY_CALL) or PE (BUY_PUT)
                   from the master, look up its premium from the scanner.
        * EQUITY / COMMODITY → trade the underlying itself; only BUY_CALL is
                   supported in cash market (no shorting).
        * OPTION (rare — only if the option was the picked candidate) →
                   trade as-is (BUY for both signals = long premium).

        Returns (None, 0, None, reason) when resolution fails — caller should
        record the reason as a skip and stay flat.
        """
        # OPTION as primary candidate: just trade it.
        if hit.kind == "OPTION":
            inst = Instrument(exchange=hit.exchange, tradingsymbol=hit.name, symboltoken=hit.token)
            return inst, hit.lot_size or 1, hit.last_price, "ok"

        if hit.kind == "INDEX":
            if self.master is None:
                return None, 0, None, "master_not_loaded"
            spot = float(hit.last_price or 0.0)
            if spot <= 0:
                return None, 0, None, "no_index_spot"
            try:
                chain = self.master.atm_options(hit.name, spot)
            except Exception as e:  # noqa: BLE001
                return None, 0, None, f"atm_lookup_error:{e}"
            rows = chain.get("rows") or []
            if not rows:
                return None, 0, None, "no_atm_chain"
            row0 = rows[0]
            side_key = "ce" if signal == "BUY_CALL" else "pe"
            inst = row0.get(side_key)
            if inst is None:
                return None, 0, None, f"no_{side_key}_in_chain (strike={row0.get('strike')})"
            premium = self._scanner_premium_for(inst.exchange, inst.symboltoken)
            return inst, inst.lot_size or 1, premium, "ok"

        if hit.kind == "EQUITY":
            if signal == "BUY_PUT":
                return None, 0, None, "stock_short_unsupported_in_cash"
            inst = Instrument(exchange=hit.exchange, tradingsymbol=hit.name, symboltoken=hit.token)
            return inst, hit.lot_size or 1, hit.last_price, "ok"

        if hit.kind == "COMMODITY":
            if signal == "BUY_PUT":
                return None, 0, None, "commodity_short_unsupported_yet"
            inst = Instrument(exchange=hit.exchange, tradingsymbol=hit.name, symboltoken=hit.token)
            return inst, hit.lot_size or 1, hit.last_price, "ok"

        return None, 0, None, f"unknown_kind:{hit.kind}"

    def _scanner_premium_for(self, exchange: str, symboltoken: str) -> float | None:
        """Look up the option's last polled premium from the scanner cache."""
        ex = (exchange or "").upper()
        tok = str(symboltoken or "")
        for h in self.scanner.last_hits:
            if h.exchange.upper() == ex and str(h.token) == tok and h.last_price:
                return float(h.last_price)
        return None

    def _live_exit_price_lookup(self, exchange: str, symboltoken: str) -> float | None:
        """Price callback the LiveExitManager uses to mark positions to market.

        Order of preference:
          1. Scanner cache — fresh, sub-second, no extra API call.
          2. Broker positions row (``self.last_positions``) — broker reports
             ``ltp`` for everything you hold so this works even when the
             instrument has fallen out of the scanner watchlist (e.g. an ATM
             option whose strike moved away from the current spot).

        Returns ``None`` only when neither source has a price; the manager
        treats that as "skip mark this cycle, only max-hold can still fire".
        """
        ex = (exchange or "").upper()
        tok = str(symboltoken or "")
        cached = self._scanner_premium_for(ex, tok)
        if cached is not None:
            return cached
        rows = (self.last_positions or {}).get("rows") or []
        for r in rows:
            r_ex = str(r.get("exchange") or "").upper()
            r_tok = str(r.get("symboltoken") or "")
            if r_ex == ex and r_tok == tok:
                ltp = r.get("ltp") or r.get("last_price")
                try:
                    f = float(ltp) if ltp is not None else 0.0
                except (TypeError, ValueError):
                    f = 0.0
                if f > 0:
                    return f
        return None

    async def _run_llm_filter(
        self,
        *,
        hit: ScannerHit,
        exec_inst: Instrument,
        signal: str,
        side: str,
        exec_price: float,
        lot_size: int,
        chosen_lots: int,
        capital_used: float,
        deployable: float,
    ) -> LlmDecision:
        """Build a sanitized market context and ask the LLM for a YES/NO/AVOID.

        Never raises — converts any error to a structured LlmDecision per the
        configured fail-open / fail-closed policy.
        """
        # NOTE: do not include broker tokens, JWTs, account IDs, or anything
        # the model doesn't strictly need to make a risk call.
        ctx: dict[str, Any] = {
            "mode": self.mode,
            "now_utc": datetime.now(UTC).isoformat(),
            "underlying": {
                "name": hit.name,
                "kind": hit.kind,
                "spot": hit.last_price,
                "change_pct": hit.change_pct,
                "candles_1m": hit.candles_1m,
                "candles_5m": hit.candles_5m,
                "candles_15m": hit.candles_15m,
            },
            "brain": {
                "score": hit.score,
                "score_breakdown": hit.score_breakdown,
                "signal": hit.signal_side,
                "signal_reason": hit.signal_reason,
                "confidence": hit.signal_confidence,
                "checks": hit.checks,
            },
            "execution": {
                "underlying": hit.name,
                "option_symbol": exec_inst.tradingsymbol,  # safe: public symbol
                "option_side": side,
                "option_premium": exec_price,
                "lot_size": lot_size,
                "lots": chosen_lots,
                "capital_used": capital_used,
                "deployable_cash": deployable,
                "capital_pct": (capital_used / deployable) if deployable else None,
            },
        }
        proposed = (
            f"{signal} {exec_inst.tradingsymbol} "
            f"@ ₹{exec_price:.2f} × {chosen_lots} lots × {lot_size}"
        )
        try:
            return await llm_filter_setup(
                market_context=ctx,
                proposed_signal=proposed,
                settings=self.settings,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("llm_filter_unexpected_error", error=str(e))
            # Apply fail-closed policy explicitly.
            if self.settings.llm_filter_fail_closed:
                return LlmDecision(
                    verdict="AVOID", allowed=False,
                    reason=f"unexpected_error:{type(e).__name__} (fail-closed)",
                    source="fail_closed",
                )
            return LlmDecision(
                verdict="YES", allowed=True,
                reason=f"unexpected_error:{type(e).__name__} (fail-open)",
                source="error",
            )

    async def _run_llm_classifier(
        self,
        *,
        hit: ScannerHit,
        exec_inst: Instrument,
        signal: str,
        side: str,
        exec_price: float,
        lot_size: int,
        chosen_lots: int,
        capital_used: float,
        deployable: float,
    ) -> LlmClassification:
        """Classifier replacement for the YES/NO/AVOID veto.

        Same sanitization + timeout + fail-closed semantics as the veto, but
        returns {decision, confidence, type} which the runtime then thresholds
        against LLM_DECISION_THRESHOLD.
        """
        # Pull pattern + structure from the brain so the LLM ranks the *setup*
        # rather than guessing it.
        brain_pattern = "other"
        structure: dict[str, Any] = {}
        for h in self.scanner.last_hits:
            if h.exchange == hit.exchange and h.token == hit.token:
                # ScannerHit.to_dict already includes the brain blob via
                # signal_side/reason. We reach into the cached BrainOutput too.
                structure = (h.diagnostics or {}).get("score_inputs", {}) or {}
                # The pattern was stamped onto Signal — runtime exposes it
                # via the scanner cache: structure_components has it indirectly.
                comps = structure.get("structure_components") or {}
                if comps.get("breakout", 0) >= max(comps.get("pullback", 0), comps.get("continuation", 0)):
                    brain_pattern = "breakout"
                elif comps.get("pullback", 0) >= comps.get("continuation", 0):
                    brain_pattern = "pullback"
                elif comps.get("continuation", 0) > 0:
                    brain_pattern = "continuation"
                break

        ctx: dict[str, Any] = {
            "mode": self.mode,
            "now_utc": datetime.now(UTC).isoformat(),
            "underlying": {
                "name": hit.name,
                "kind": hit.kind,
                "spot": hit.last_price,
                "change_pct": hit.change_pct,
                "candles_1m": hit.candles_1m,
                "candles_5m": hit.candles_5m,
                "candles_15m": hit.candles_15m,
            },
            "brain": {
                "score": hit.score,
                "score_breakdown": hit.score_breakdown,
                "signal": hit.signal_side,
                "signal_reason": hit.signal_reason,
                "confidence": hit.signal_confidence,
                "checks": hit.checks,
                "pattern": brain_pattern,
                "structure": structure,
            },
            "execution": {
                "underlying": hit.name,
                "option_symbol": exec_inst.tradingsymbol,
                "option_side": side,
                "option_premium": exec_price,
                "lot_size": lot_size,
                "lots": chosen_lots,
                "capital_used": capital_used,
                "deployable_cash": deployable,
                "capital_pct": (capital_used / deployable) if deployable else None,
            },
        }
        proposed = (
            f"{signal} {exec_inst.tradingsymbol} "
            f"@ ₹{exec_price:.2f} × {chosen_lots} lots × {lot_size}"
        )
        try:
            return await llm_classify_setup(
                market_context=ctx,
                proposed_signal=proposed,
                proposed_pattern=brain_pattern,
                settings=self.settings,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("llm_classifier_unexpected_error", error=str(e))
            if self.settings.llm_filter_fail_closed:
                return LlmClassification(
                    decision="SKIP", confidence=0.0, pattern_type="other",
                    reason=f"unexpected_error:{type(e).__name__} (fail-closed)",
                    source="fail_closed",
                )
            return LlmClassification(
                decision="TAKE", confidence=0.5, pattern_type="other",
                reason=f"unexpected_error:{type(e).__name__} (fail-open)",
                source="error",
            )

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
            "hidden_unaffordable": int(getattr(self.scanner, "last_hidden_unaffordable", 0)),
            "index_unaffordable": int(self._last_index_unaffordable),
        }

    async def _consider_trade(self, api: SmartApiClient, hit: ScannerHit | None, deployable: float) -> None:
        s = self.settings
        if hit is None:
            scan_reason = (self.last_scan_summary or {}).get("reason", "no_candidate")
            self._record_skip(hit=None, signal="NO_TRADE", reason=f"no_candidate ({scan_reason})", price=None)
            return
        signal = hit.signal_side
        reason = hit.signal_reason
        if signal == "NO_TRADE":
            self._record_skip(hit=hit, signal=signal, reason=reason, price=hit.last_price)
            return

        # Honor user kind toggles even if a stale candidate slipped through.
        kind_for_check = (hit.kind or "").upper()
        # The hit kind for indices is "INDEX" but the actual trade is an OPTION,
        # so we gate INDEX hits on the OPTION toggle.
        gate_kind = "OPTION" if kind_for_check == "INDEX" else kind_for_check
        if gate_kind in self.kind_enabled and not self.kind_enabled[gate_kind]:
            self._record_skip(
                hit=hit, signal=signal,
                reason=f"kind_disabled:{gate_kind}",
                price=hit.last_price,
            )
            return

        # Hard market-hours gate. We refuse to even attempt placement outside
        # the corresponding session — Angel will reject it but we save the
        # round-trip and the rate-limit budget.
        mkt = kind_market_status(gate_kind)
        if not mkt.is_open:
            opens = mkt.opens_at_label or "next session"
            self._record_skip(
                hit=hit, signal=signal,
                reason=f"market_closed:{mkt.label} reopens {opens}",
                price=hit.last_price,
            )
            return

        # ---- Resolve the *executable* instrument from the brain's signal ----
        exec_inst, exec_lot_size, exec_price, why = self._resolve_executable(hit, signal)
        if exec_inst is None:
            self._record_skip(hit=hit, signal=signal, reason=f"resolve:{why}", price=hit.last_price)
            return
        if not exec_price or exec_price <= 0:
            self._record_skip(
                hit=hit, signal=signal,
                reason=f"no_execution_price for {exec_inst.tradingsymbol}",
                price=hit.last_price,
            )
            return
        lot_size = exec_lot_size or 1
        notional_per_lot = exec_price * lot_size

        # ---- Lot-value range guard against the EXECUTION instrument ----
        min_tv = s.strategy_min_trade_value or 0.0
        max_tv = s.strategy_max_trade_value or 0.0
        if min_tv > 0 and notional_per_lot < min_tv:
            self._record_skip(
                hit=hit, signal=signal,
                reason=f"option_lot_value_below_min:₹{notional_per_lot:.0f}<₹{min_tv:.0f}",
                price=exec_price,
            )
            return
        if max_tv > 0 and notional_per_lot > max_tv:
            self._record_skip(
                hit=hit, signal=signal,
                reason=f"option_lot_value_above_max:₹{notional_per_lot:.0f}>₹{max_tv:.0f}",
                price=exec_price,
            )
            return

        # ---- Funds check (deployable cash vs option lot notional) ----
        affordable_lots = int(max(0, deployable // notional_per_lot)) if notional_per_lot > 0 else 0
        if affordable_lots < 1:
            short = max(0.0, notional_per_lot - deployable)
            self._record_skip(
                hit=hit, signal=signal,
                reason=(
                    f"need_more_capital:₹{short:.0f}_for_1_lot of "
                    f"{exec_inst.tradingsymbol} (lot ₹{notional_per_lot:.0f})"
                ),
                price=exec_price,
            )
            return

        # ---- Risk gate (uses option premium as the entry, paper SL pct) ----
        nominal_stop = (
            exec_price * (1 - s.paper_stop_loss_pct)
            if signal == "BUY_CALL"
            else exec_price * (1 + s.paper_stop_loss_pct)
        )
        decision = self.risk.evaluate_new_trade(entry=exec_price, stop=nominal_stop, lot_size=lot_size)
        if not decision.allowed:
            self._record_skip(hit=hit, signal=signal, reason=f"risk:{decision.reason}", price=exec_price)
            return

        risk_lots = decision.quantity // lot_size
        chosen_lots = max(0, min(risk_lots, affordable_lots))
        if chosen_lots < 1:
            self._record_skip(hit=hit, signal=signal, reason="zero_lots_after_funds_cap", price=exec_price)
            return
        chosen_qty = chosen_lots * lot_size
        capital_used = exec_price * chosen_qty
        side = "CE" if signal == "BUY_CALL" else "PE"

        # ------------------------------------------------------------------
        # LLM CLASSIFIER — primary decision-quality filter (5m pipeline).
        # Returns {decision, confidence, type}. We require:
        #   decision == TAKE  AND  confidence >= LLM_DECISION_THRESHOLD.
        # When the LLM is disabled or no key is set, this short-circuits to
        # TAKE@1.0 so the trade flows through. Fail-closed/open is honored.
        # ------------------------------------------------------------------
        llm_dec = await self._run_llm_classifier(
            hit=hit, exec_inst=exec_inst, signal=signal, side=side,
            exec_price=exec_price, lot_size=lot_size, chosen_lots=chosen_lots,
            capital_used=capital_used, deployable=deployable,
        )
        threshold = float(s.llm_decision_threshold)
        if not llm_dec.passes(threshold):
            self._record_skip(
                hit=hit, signal=signal,
                reason=(
                    f"llm:{llm_dec.decision} conf={llm_dec.confidence:.2f}"
                    f"<{threshold:.2f} — {llm_dec.reason}"
                ),
                price=exec_price,
                extra={"llm": llm_dec.to_dict(), "exec_symbol": exec_inst.tradingsymbol},
            )
            return

        # ------------------------------------------------------------------
        # DRY-RUN: open a paper position on the EXECUTABLE instrument
        # ------------------------------------------------------------------
        if not self._runtime_trading_enabled:
            if not self.paper.has_capacity():
                self._record_skip(hit=hit, signal=signal, reason="paper_book_full", price=exec_price)
                return
            try:
                pid = self.paper.open(
                    PaperOpenRequest(
                        exchange=exec_inst.exchange,
                        symboltoken=exec_inst.symboltoken,
                        tradingsymbol=exec_inst.tradingsymbol,
                        kind=hit.kind,            # source kind for grouping
                        signal=signal,
                        side=side,
                        entry_price=exec_price,
                        lots=chosen_lots,
                        lot_size=lot_size,
                        capital_at_open=deployable,
                        reason=f"{reason} ({hit.name})",
                    )
                )
            except Exception as e:  # noqa: BLE001
                self._record_skip(hit=hit, signal=signal, reason=f"paper_open_error:{e}", price=exec_price)
                return
            paper_payload = {
                "tradingsymbol": exec_inst.tradingsymbol,
                "exchange": exec_inst.exchange,
                "symboltoken": exec_inst.symboltoken,
                "transactiontype": "BUY",
                "variety": s.bot_default_variety,
                "quantity": chosen_qty,
                "ordertype": "MARKET",
                "producttype": s.bot_default_product,
                "paper_id": pid,
                "underlying": hit.name,
            }
            self.store.log_order(
                paper_payload,
                broker_order_id=f"PAPER-{pid}",
                status="placed",
                lifecycle_status="executed",
                placed_by_bot=True,
                intent="open",
                mode="dryrun",
            )
            self._record_decision(
                hit=hit, signal=signal,
                reason=f"paper_open {exec_inst.tradingsymbol} ({reason})",
                price=exec_price, qty=chosen_qty, lots=chosen_lots,
                capital=capital_used, side=side,
                placed=True, dry_run=True, broker_order_id=f"PAPER-{pid}",
                extra={"llm": llm_dec.to_dict(), "underlying": hit.name},
            )
            # Update risk-engine entry count for the per-hour cap.
            self.risk.record_entry()
            return

        # ------------------------------------------------------------------
        # LIVE: place real broker order on the executable instrument
        # ------------------------------------------------------------------
        payload = build_order_payload(
            exec_inst,
            variety=s.bot_default_variety,
            transactiontype="BUY",
            ordertype="MARKET",
            producttype=s.bot_default_product,
            quantity=chosen_qty,
        )
        try:
            validate_order_payload(payload)
        except ValueError as e:
            self._record_skip(hit=hit, signal=signal, reason=f"invalid_payload:{e}", price=exec_price)
            return
        if not self._dup_guard.check_and_remember(payload):
            self._record_skip(hit=hit, signal=signal, reason="duplicate_order_window", price=exec_price)
            return
        try:
            resp = await api.place_order(payload)
        except Exception as e:
            self.last_error = str(e)
            self._record_skip(hit=hit, signal=signal, reason=f"place_order_error:{e}", price=exec_price)
            return
        oid = extract_place_order_id(resp) if isinstance(resp, dict) else None
        if oid:
            self.store.log_order(
                payload, oid, status="placed", lifecycle_status="placed",
                placed_by_bot=True, intent="open", mode="live",
            )
            # Register the exit plan immediately. The LiveExitManager will
            # back-fill the actual fill price + recompute SL/TP from it on
            # the next reconcile, so the plan is honest even if the open
            # slips. Registration is idempotent on broker_order_id.
            try:
                self.live_exits.register_open(
                    open_order_id=str(oid),
                    exchange=exec_inst.exchange,
                    symboltoken=exec_inst.symboltoken,
                    tradingsymbol=exec_inst.tradingsymbol,
                    kind=hit.kind,
                    side=side,
                    signal=signal,
                    underlying=hit.name,
                    qty=chosen_qty,
                    lots=chosen_lots,
                    lot_size=lot_size,
                    planned_entry=exec_price,
                    product=s.bot_default_product,
                    variety=s.bot_default_variety,
                )
            except Exception as e:  # noqa: BLE001 — never block the trade on plan persistence
                log.warning("live_exit_register_failed", error=str(e), broker_order_id=oid)
        self._record_decision(
            hit=hit, signal=signal,
            reason=f"placed {exec_inst.tradingsymbol}",
            price=exec_price, qty=chosen_qty, lots=chosen_lots,
            capital=capital_used, side=side,
            placed=bool(oid), dry_run=False, broker_order_id=oid,
            extra={"resp": _redact(resp), "underlying": hit.name, "llm": llm_dec.to_dict()},
        )
        if oid:
            self.risk.record_entry()

    # Skip reasons that are *stable* per (instrument, side) — i.e. they will
    # almost certainly fire again on every cycle until something the user
    # controls changes (cash balance, watchlist, score threshold). We collapse
    # repeated identical rows to ONE every 5 minutes so the decision log
    # doesn't fill up with the same line every 5 seconds.
    _DEDUPE_REASON_PREFIXES: tuple[str, ...] = (
        "need_more_capital",
        "option_lot_value_below_min",
        "option_lot_value_above_max",
        "kind_disabled",
        "market_closed",
        "duplicate_order_window",
        "no_execution_price",
        "resolve:",
    )
    _DEDUPE_WINDOW_S: float = 300.0

    def _record_skip(
        self,
        *,
        hit: ScannerHit | None,
        signal: str,
        reason: str,
        price: float | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self._should_dedupe_skip(hit, signal, reason):
            return
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
                extra=extra or {},
            )
        )

    def _should_dedupe_skip(
        self,
        hit: ScannerHit | None,
        signal: str,
        reason: str,
    ) -> bool:
        """Return True if this skip is a repeat of a recent one we already
        logged for the same (instrument, signal, reason-family).

        Only "stable" reason families dedupe — transient ones (warmup,
        risk, llm) always log so the user sees them flip.
        """
        family: str | None = None
        for pref in self._DEDUPE_REASON_PREFIXES:
            if reason.startswith(pref):
                family = pref.rstrip(":")
                break
        if family is None:
            return False
        token = hit.token if hit else "-"
        key = f"{token}|{signal}|{family}"
        now = datetime.now(UTC)
        prev_iso = self._last_skip_at.get(key)
        if prev_iso:
            try:
                prev = datetime.fromisoformat(prev_iso)
                if (now - prev).total_seconds() < self._DEDUPE_WINDOW_S:
                    return True
            except ValueError:
                pass
        self._last_skip_at[key] = now.isoformat()
        return False

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
                placed_by_bot=True, intent="close", mode="live",
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
