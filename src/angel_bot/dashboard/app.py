from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from angel_bot.auth.session import AngelHttpError
from angel_bot.config import get_settings
from angel_bot.runtime import TradingRuntime

log = structlog.get_logger(__name__)


def _check_dashboard_token(x_dashboard_token: str | None) -> None:
    s = get_settings()
    expected = (s.dashboard_token.get_secret_value() if s.dashboard_token else "").strip()
    if not expected:
        return
    if (x_dashboard_token or "").strip() != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Dashboard-Token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    rt = TradingRuntime.instance()
    s = get_settings()
    # Always try to load the instrument master at startup so dynamic
    # universe + ATM resolution work from the very first scanner cycle.
    try:
        master_res = await rt.ensure_master(force_download=False)
        log.info(
            "instrument_master_ready",
            ok=master_res.get("ok"),
            instruments=(master_res.get("status") or {}).get("instruments"),
            source=(master_res.get("status") or {}).get("source"),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("instrument_master_startup_failed", error=str(e))
    if s.auto_connect_on_startup and rt.auto_mode:
        try:
            res = await rt.auto_connect()
            log.info("auto_connect_result", ok=bool(res and res.get("status")))
            if rt.connected() and s.bot_autostart:
                await rt.start_bot()
                log.info("bot_autostarted")
        except Exception as e:  # noqa: BLE001
            log.warning("auto_connect_startup_failed", error=str(e))
    yield
    await rt.shutdown()


def _frontend_dist() -> Path | None:
    here = Path(__file__).resolve().parent.parent.parent.parent  # project root
    candidate = here / "frontend" / "dist"
    if candidate.exists() and (candidate / "index.html").exists():
        return candidate
    return None


def create_app() -> FastAPI:
    app = FastAPI(title="Angel One dashboard", lifespan=lifespan)

    s = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/status")
    async def api_status(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        rt = TradingRuntime.instance()
        return {
            "connected": rt.connected(),
            "bot_running": rt.bot_running(),
            "last_error": rt.last_error,
            "clientcode": rt.connected_clientcode,
            "trading_enabled": rt.trading_enabled,
            "auto_mode": rt.auto_mode,
        }

    @app.get("/api/snapshot")
    async def api_snapshot(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        return TradingRuntime.instance().snapshot()

    @app.get("/api/funds")
    async def api_funds(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        rt = TradingRuntime.instance()
        if not rt.connected():
            raise HTTPException(status_code=400, detail="Not connected")
        return await rt.refresh_funds()

    @app.get("/api/positions")
    async def api_positions(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        rt = TradingRuntime.instance()
        if not rt.connected():
            raise HTTPException(status_code=400, detail="Not connected")
        return await rt.refresh_positions()

    @app.get("/api/scanner")
    async def api_scanner(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        rt = TradingRuntime.instance()
        return [h.to_dict() for h in rt.last_scanner]

    @app.get("/api/decisions")
    async def api_decisions(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        return [d.to_dict() for d in TradingRuntime.instance().decisions.recent(120)]

    @app.get("/api/orders")
    async def api_orders(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        rt = TradingRuntime.instance()
        rows = rt.store.recent_orders(80)
        from angel_bot.broker_models import summarize_orders_for_ui
        return summarize_orders_for_ui(rows)

    @app.get("/api/stats")
    async def api_stats(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        rt = TradingRuntime.instance()
        return rt._daily_stats()  # noqa: SLF001 — controlled internal accessor

    @app.get("/api/config")
    async def api_config(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        cs = get_settings()
        return {
            "trading_enabled": cs.trading_enabled,
            "loop_interval_s": cs.bot_loop_interval_s,
            "max_concurrent_positions": cs.bot_max_concurrent_positions,
            "use_capital_pct": cs.bot_use_capital_pct,
            "min_signal_strength": cs.bot_min_signal_strength,
            "default_product": cs.bot_default_product,
            "default_variety": cs.bot_default_variety,
            "watchlist": cs.scanner_watchlist(),
            "risk": {
                "capital_rupees": cs.risk_capital_rupees,
                "per_trade_pct": cs.risk_per_trade_pct,
                "max_daily_loss_pct": cs.risk_max_daily_loss_pct,
                "max_trades_per_day": cs.risk_max_trades_per_day,
                "one_position_at_a_time": cs.risk_one_position_at_a_time,
            },
        }

    @app.post("/api/connect")
    async def api_connect(
        request: Request,
        x_dashboard_token: str | None = Header(default=None),
    ) -> Any:
        _check_dashboard_token(x_dashboard_token)
        body = await request.json()
        totp = str(body.get("totp", ""))
        rt = TradingRuntime.instance()
        try:
            return JSONResponse(await rt.connect_with_totp(totp))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except AngelHttpError as e:
            log.warning("connect_failed", error=str(e))
            raise HTTPException(status_code=401, detail=str(e)) from e

    @app.post("/api/bot/start")
    async def bot_start(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        rt = TradingRuntime.instance()
        try:
            await rt.start_bot()
            return {"started": True, "trading_enabled": rt.trading_enabled}
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/bot/stop")
    async def bot_stop(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        await TradingRuntime.instance().stop_bot()
        return {"stopped": True}

    @app.post("/api/trading/enable")
    async def trading_enable(
        request: Request,
        x_dashboard_token: str | None = Header(default=None),
    ) -> Any:
        """Flip the bot from dry-run → live placement at runtime.

        Body MUST include {"confirm": "I_UNDERSTAND_LIVE_TRADING"} so a UI bug
        cannot accidentally turn live mode on. The bot still respects every
        risk cap from .env (RISK_*) and refuses live index option trades.
        """
        _check_dashboard_token(x_dashboard_token)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if str(body.get("confirm", "")).strip() != "I_UNDERSTAND_LIVE_TRADING":
            raise HTTPException(
                status_code=400,
                detail='Live trading requires confirmation. Send {"confirm":"I_UNDERSTAND_LIVE_TRADING"}.',
            )
        return TradingRuntime.instance().set_trading_enabled(True)

    @app.post("/api/trading/disable")
    async def trading_disable(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        return TradingRuntime.instance().set_trading_enabled(False)

    @app.get("/api/history")
    async def api_history(
        mode: str = "live",
        x_dashboard_token: str | None = Header(default=None),
    ) -> Any:
        _check_dashboard_token(x_dashboard_token)
        return TradingRuntime.instance().history(orders_limit=200, mode=mode)

    @app.post("/api/dryrun/capital")
    async def api_set_dryrun_capital(
        request: Request,
        x_dashboard_token: str | None = Header(default=None),
    ) -> Any:
        """Set the capital the dry-run sim should use for sizing.

        Body: {"amount": <float ₹, 0 = use real broker cash>}.
        """
        _check_dashboard_token(x_dashboard_token)
        try:
            body = await request.json()
        except Exception:
            body = {}
        amount = body.get("amount")
        if amount is None:
            raise HTTPException(status_code=400, detail="missing 'amount'")
        try:
            amt = float(amount)
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"invalid amount: {e}") from e
        if amt < 0:
            raise HTTPException(status_code=400, detail="amount must be >= 0")
        return TradingRuntime.instance().set_dryrun_capital(amt)

    @app.get("/api/dryrun/paper")
    async def api_paper_positions(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        rt = TradingRuntime.instance()
        return {
            "open": rt.paper.open_positions_summary(),
            "today": rt.paper.today_summary(),
        }

    @app.post("/api/dryrun/reset")
    async def api_reset_paper(
        request: Request,
        x_dashboard_token: str | None = Header(default=None),
    ) -> Any:
        """Wipe paper book, dry-run daily P&L and dry-run order history.
        Body: {"confirm": "RESET_DRYRUN"}."""
        _check_dashboard_token(x_dashboard_token)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if str(body.get("confirm", "")).strip() != "RESET_DRYRUN":
            raise HTTPException(
                status_code=400,
                detail='Send {"confirm":"RESET_DRYRUN"} to wipe paper history.',
            )
        return TradingRuntime.instance().reset_paper()

    @app.post("/api/dryrun/paper/close")
    async def api_paper_close(
        request: Request,
        x_dashboard_token: str | None = Header(default=None),
    ) -> Any:
        """Close a single open paper position at its last marked price.
        Body: {"id": <paper_id>}."""
        _check_dashboard_token(x_dashboard_token)
        try:
            body = await request.json()
        except Exception:
            body = {}
        pid = body.get("id")
        if pid is None:
            raise HTTPException(status_code=400, detail="missing 'id'")
        try:
            pid_int = int(pid)
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"invalid id: {e}") from e
        return TradingRuntime.instance().close_paper_position(pid_int)

    @app.post("/api/kill-switch")
    async def api_kill_switch(
        request: Request,
        x_dashboard_token: str | None = Header(default=None),
    ) -> Any:
        """Stop the bot, flip to dry-run, optionally cancel pending bot orders and
        square-off open positions. Body: {"confirm": "STOP_EVERYTHING",
        "cancel_pending": true, "square_off": true}.
        """
        _check_dashboard_token(x_dashboard_token)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if str(body.get("confirm", "")).strip() != "STOP_EVERYTHING":
            raise HTTPException(
                status_code=400,
                detail='Kill switch requires confirmation. Send {"confirm":"STOP_EVERYTHING"}.',
            )
        cancel_pending = bool(body.get("cancel_pending", True))
        square_off = bool(body.get("square_off", True))
        rt = TradingRuntime.instance()
        try:
            return await rt.kill_switch(cancel_pending=cancel_pending, square_off=square_off)
        except Exception as e:  # noqa: BLE001 — surface to UI
            log.exception("kill_switch_error")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @app.post("/api/positions/close")
    async def api_close_position(
        request: Request,
        x_dashboard_token: str | None = Header(default=None),
    ) -> Any:
        """Square-off ONE position. Body must include tradingsymbol, exchange,
        symboltoken, net_qty (signed). Optional: producttype.
        """
        _check_dashboard_token(x_dashboard_token)
        try:
            body = await request.json()
        except Exception:
            body = {}
        required = ("tradingsymbol", "exchange", "symboltoken", "net_qty")
        missing = [k for k in required if body.get(k) in (None, "")]
        if missing:
            raise HTTPException(status_code=400, detail=f"missing fields: {missing}")
        rt = TradingRuntime.instance()
        try:
            return await rt.close_position(
                tradingsymbol=str(body["tradingsymbol"]),
                exchange=str(body["exchange"]),
                symboltoken=str(body["symboltoken"]),
                net_qty=int(body["net_qty"]),
                producttype=body.get("producttype"),
            )
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:  # noqa: BLE001
            log.exception("close_position_error")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @app.get("/api/instruments/status")
    async def api_instruments_status(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        rt = TradingRuntime.instance()
        st = rt.master_status
        return {
            "loaded": rt.master is not None,
            "instruments": (len(rt.master) if rt.master else 0),
            "status": (st.__dict__ if st else None),
        }

    @app.post("/api/instruments/refresh")
    async def api_instruments_refresh(
        request: Request,
        x_dashboard_token: str | None = Header(default=None),
    ) -> Any:
        """(Re)download the Angel scrip master and rebuild the dynamic universe.
        Body: optional {"force": true} to bypass the cache freshness check."""
        _check_dashboard_token(x_dashboard_token)
        try:
            body = await request.json()
        except Exception:
            body = {}
        force = bool(body.get("force", False))
        return await TradingRuntime.instance().ensure_master(force_download=force)

    @app.get("/api/instruments/search")
    async def api_instruments_search(
        q: str,
        exchange: str | None = None,
        kind: str | None = None,
        limit: int = 25,
        x_dashboard_token: str | None = Header(default=None),
    ) -> Any:
        _check_dashboard_token(x_dashboard_token)
        return TradingRuntime.instance().search_instruments(
            q, exchange=exchange, kind=kind, limit=int(limit)
        )

    @app.get("/api/universe")
    async def api_universe_get(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        return TradingRuntime.instance().universe_state()

    @app.post("/api/universe")
    async def api_universe_set(
        request: Request,
        x_dashboard_token: str | None = Header(default=None),
    ) -> Any:
        """Replace the live universe spec at runtime.
        Body: {"indices": [...], "stocks": [...], "commodities": [...],
               "atm_for": [...], "atm_offsets": [-1,0,1]}."""
        _check_dashboard_token(x_dashboard_token)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        return TradingRuntime.instance().set_universe_spec(body)

    @app.post("/api/universe/kinds")
    async def api_universe_kinds(
        request: Request,
        x_dashboard_token: str | None = Header(default=None),
    ) -> Any:
        """Toggle which instrument categories the bot watches and trades.
        Body: {"INDEX": true|false, "EQUITY": true|false,
               "COMMODITY": true|false, "OPTION": true|false}.
        Disabled kinds are dropped from the watchlist (no API polls) and
        cannot be traded until re-enabled."""
        _check_dashboard_token(x_dashboard_token)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        return TradingRuntime.instance().set_kind_enabled(body)

    @app.post("/api/disconnect")
    async def disconnect(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        await TradingRuntime.instance().disconnect()
        return {"disconnected": True}

    dist = _frontend_dist()
    if dist is not None:
        assets = dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/", response_class=HTMLResponse)
        async def index() -> FileResponse:
            return FileResponse(str(dist / "index.html"))

        @app.get("/{full_path:path}", response_class=HTMLResponse)
        async def spa_fallback(full_path: str) -> FileResponse:
            target = dist / full_path
            if target.exists() and target.is_file():
                return FileResponse(str(target))
            return FileResponse(str(dist / "index.html"))
    else:
        @app.get("/", response_class=HTMLResponse)
        async def index() -> HTMLResponse:
            return HTMLResponse(
                "<html><body style='font-family:system-ui;padding:2rem'>"
                "<h2>Angel One dashboard</h2>"
                "<p>The React frontend is not built. From the project root:</p>"
                "<pre>cd frontend &amp;&amp; yarn install &amp;&amp; yarn build</pre>"
                "<p>Or run dev mode:</p>"
                "<pre>cd frontend &amp;&amp; yarn install &amp;&amp; yarn dev</pre>"
                "<p>Then open <a href='http://localhost:5173/'>http://localhost:5173/</a> "
                "(the dev server proxies /api to this backend).</p>"
                "</body></html>"
            )

    return app
