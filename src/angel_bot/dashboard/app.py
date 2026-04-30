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
            "trading_enabled": rt.settings.trading_enabled,
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
            return {"started": True}
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/bot/stop")
    async def bot_stop(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        await TradingRuntime.instance().stop_bot()
        return {"stopped": True}

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
