from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from angel_bot.auth.session import AngelHttpError
from angel_bot.config import get_settings
from angel_bot.runtime import TradingRuntime

log = structlog.get_logger(__name__)

PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Angel One — connect & bot</title>
  <style>
    :root { font-family: system-ui, sans-serif; background: #0f1419; color: #e7ecf3; }
    body { max-width: 520px; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.25rem; font-weight: 600; }
    p.muted { color: #8b98a5; font-size: 0.9rem; }
    label { display: block; margin-top: 1rem; font-size: 0.85rem; color: #a8b3c0; }
    input { width: 100%; box-sizing: border-box; margin-top: 0.35rem; padding: 0.6rem 0.75rem;
      border-radius: 8px; border: 1px solid #2a3444; background: #151c24; color: #e7ecf3; font-size: 1.1rem; letter-spacing: 0.2em; }
    button { margin-top: 1rem; margin-right: 0.5rem; padding: 0.55rem 1rem; border-radius: 8px; border: none;
      cursor: pointer; font-weight: 600; }
    .primary { background: #3b82f6; color: white; }
    .danger { background: #374151; color: #e7ecf3; }
    #status { margin-top: 1.25rem; padding: 0.75rem 1rem; border-radius: 8px; background: #151c24; border: 1px solid #2a3444;
      font-size: 0.9rem; white-space: pre-wrap; }
    .err { color: #f87171; }
    .ok { color: #4ade80; }
  </style>
</head>
<body>
  <h1>Angel One SmartAPI</h1>
  <p class="muted">Open your Angel One / authenticator app, copy the <strong>current 6-digit TOTP</strong>, paste below, then connect.
  JWT is not shown here. After a successful connect, start the bot (WebSocket if <code>WS_SUBSCRIPTIONS</code> is set, otherwise LTP polling).</p>
  <form id="f">
    <label for="totp">TOTP (6 digits)</label>
    <input id="totp" name="totp" inputmode="numeric" pattern="[0-9]*" maxlength="6" autocomplete="one-time-code" placeholder="000000"/>
    <button type="button" class="primary" id="btnConnect">Connect</button>
    <button type="button" class="primary" id="btnStart" disabled>Start bot</button>
    <button type="button" class="danger" id="btnStop" disabled>Stop bot</button>
    <button type="button" class="danger" id="btnDisconnect" disabled>Disconnect</button>
  </form>
  <div id="status" class="muted">Not connected.</div>
  <script>
    const el = (id) => document.getElementById(id);
    const status = el("status");
    const token = %TOKEN_JSON%;
    function hdr() {
      const h = {"Content-Type": "application/json"};
      if (token) h["X-Dashboard-Token"] = token;
      return h;
    }
    async function refresh() {
      const r = await fetch("/api/status", { headers: hdr() });
      const j = await r.json();
      el("btnStart").disabled = !j.connected || j.bot_running;
      el("btnStop").disabled = !j.bot_running;
      el("btnDisconnect").disabled = !j.connected;
      let lines = ["Connected: " + j.connected, "Bot running: " + j.bot_running];
      if (j.last_error) lines.push("Last error: " + j.last_error);
      status.textContent = lines.join(String.fromCharCode(10));
      status.className = j.last_error ? "err" : (j.connected ? "ok" : "muted");
    }
    el("btnConnect").onclick = async () => {
      const totp = el("totp").value.trim();
      status.textContent = "Connecting…";
      const r = await fetch("/api/connect", { method: "POST", headers: hdr(), body: JSON.stringify({ totp }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { status.textContent = j.detail || r.statusText; status.className = "err"; return; }
      status.textContent = JSON.stringify(j, null, 2);
      status.className = j.status ? "ok" : "err";
      await refresh();
    };
    el("btnStart").onclick = async () => {
      const r = await fetch("/api/bot/start", { method: "POST", headers: hdr() });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { status.textContent = j.detail || r.statusText; status.className = "err"; return; }
      await refresh();
    };
    el("btnStop").onclick = async () => {
      await fetch("/api/bot/stop", { method: "POST", headers: hdr() });
      await refresh();
    };
    el("btnDisconnect").onclick = async () => {
      await fetch("/api/disconnect", { method: "POST", headers: hdr() });
      el("totp").value = "";
      await refresh();
    };
    refresh().catch(() => {});
    setInterval(() => refresh().catch(() => {}), 5000);
  </script>
</body>
</html>
"""


def _check_dashboard_token(x_dashboard_token: str | None) -> None:
    s = get_settings()
    expected = (s.dashboard_token.get_secret_value() if s.dashboard_token else "").strip()
    if not expected:
        return
    if (x_dashboard_token or "").strip() != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Dashboard-Token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await TradingRuntime.instance().shutdown()


def create_app() -> FastAPI:
    app = FastAPI(title="Angel One dashboard", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        s = get_settings()
        tok = s.dashboard_token.get_secret_value() if s.dashboard_token else ""
        token_js = json.dumps(tok)
        return HTMLResponse(PAGE.replace("%TOKEN_JSON%", token_js))

    @app.get("/api/status")
    async def api_status(x_dashboard_token: str | None = Header(default=None)) -> Any:
        _check_dashboard_token(x_dashboard_token)
        rt = TradingRuntime.instance()
        return {
            "connected": rt.connected(),
            "bot_running": rt.bot_running(),
            "last_error": rt.last_error,
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
            out = await rt.connect_with_totp(totp)
            return JSONResponse(out)
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

    return app
