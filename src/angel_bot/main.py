from __future__ import annotations

import argparse
import asyncio
import json
import queue
import sys

import structlog

from angel_bot.auth.session import AngelSession, totp_configured_in_env
from angel_bot.config import get_settings
from angel_bot.logging_config import configure_logging
from angel_bot.market_data.ws_binary import parse_ws_subscriptions
from angel_bot.market_data.ws_feed import AngelWebSocketFeed
from angel_bot.orders.tracker import OrderTracker
from angel_bot.risk.engine import RiskEngine
from angel_bot.smart_client import SmartApiClient
from angel_bot.state.store import StateStore


async def cmd_profile() -> int:
    settings = get_settings()
    if not totp_configured_in_env(settings):
        print(
            "No TOTP in .env (ANGEL_TOTP / ANGEL_TOTP_SECRET). "
            "Run the dashboard and enter the 6-digit code from your authenticator: "
            "`python -m angel_bot.main dashboard`",
            file=sys.stderr,
        )
        return 2
    session = AngelSession()
    try:
        await session.ensure_login()
        prof = await session.get_profile()
        print(json.dumps(prof, indent=2, default=str))
        return 0 if prof.get("status") else 1
    finally:
        await session.aclose()


async def cmd_poll_ltp() -> int:
    settings = get_settings()
    if not totp_configured_in_env(settings):
        print(
            "No TOTP in .env. Start the dashboard and connect with your authenticator code: "
            "`python -m angel_bot.main dashboard`",
            file=sys.stderr,
        )
        return 2
    session = AngelSession()
    try:
        await session.ensure_login()
        api = SmartApiClient(session)
        tokens = settings.ltp_exchange_tokens()
        resp = await api.get_ltp(tokens)
        print(json.dumps(resp, indent=2, default=str))
        return 0 if resp.get("status") else 1
    finally:
        await session.aclose()


async def cmd_ws_feed() -> int:
    settings = get_settings()
    if not totp_configured_in_env(settings):
        print(
            "No TOTP in .env. Start the dashboard and connect first: "
            "`python -m angel_bot.main dashboard`",
            file=sys.stderr,
        )
        return 2
    token_list = parse_ws_subscriptions(settings.ws_subscriptions)
    if not token_list:
        print("Set WS_SUBSCRIPTIONS in .env (see .env.example).", file=sys.stderr)
        return 2

    session = AngelSession()
    await session.ensure_login()
    if not session.jwt or not session.feed_token:
        print("Missing JWT or feedToken after login.", file=sys.stderr)
        await session.aclose()
        return 1

    q: queue.Queue = queue.Queue(maxsize=50_000)
    feed = AngelWebSocketFeed(
        jwt=session.jwt,
        api_key=settings.angel_api_key.get_secret_value(),
        client_code=settings.angel_client_code,
        feed_token=session.feed_token or "",
        token_list=token_list,
        mode=settings.ws_feed_mode,
        out_queue=q,
    )
    feed.start()
    log = structlog.get_logger(__name__)
    log.info("ws_feed_started", subscriptions=token_list, mode=settings.ws_feed_mode)
    try:
        while True:
            await asyncio.sleep(0.05)
            drained = 0
            while drained < 500:
                try:
                    tick = q.get_nowait()
                except queue.Empty:
                    break
                print(json.dumps(tick, default=str))
                drained += 1
    except KeyboardInterrupt:
        return 0
    finally:
        feed.stop()
        await session.aclose()


async def cmd_orders_sync() -> int:
    settings = get_settings()
    if not totp_configured_in_env(settings):
        print(
            "No TOTP in .env. Start the dashboard and connect first: "
            "`python -m angel_bot.main dashboard`",
            file=sys.stderr,
        )
        return 2
    store = StateStore(settings.state_sqlite_path)
    session = AngelSession()
    try:
        await session.ensure_login()
        api = SmartApiClient(session)
        tracker = OrderTracker(store)
        n = await tracker.reconcile_once(api)
        risk = RiskEngine(settings)
        risk.sync_from_store(store)
        print(json.dumps({"reconciled_orders": n, "risk_daily": risk.state.__dict__}, indent=2))
        return 0
    finally:
        await session.aclose()


def run_dashboard() -> None:
    import uvicorn

    from angel_bot.dashboard.app import create_app

    s = get_settings()
    configure_logging(json_logs=s.log_format == "json")
    app = create_app()
    print(
        f"Open the dashboard: http://{s.dashboard_host}:{s.dashboard_port}/\n"
        "Enter the 6-digit TOTP from your authenticator, click Connect, then Start bot.",
        flush=True,
    )
    uvicorn.run(app, host=s.dashboard_host, port=s.dashboard_port, log_level="info")


def run() -> None:
    parser = argparse.ArgumentParser(description="Angel One SmartAPI trading pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("dashboard", help="Web UI: enter TOTP at runtime, connect to Angel One, start/stop bot")
    sub.add_parser("profile", help="Login and print getProfile (needs TOTP in .env or use dashboard)")
    sub.add_parser("poll-ltp", help="Fetch LTP (needs TOTP in .env or use dashboard)")
    sub.add_parser("ws-feed", help="WebSocket ticks (needs TOTP in .env or use dashboard)")
    sub.add_parser("orders-sync", help="Order book sync (needs TOTP in .env or use dashboard)")

    args = parser.parse_args()
    if args.cmd == "dashboard":
        run_dashboard()
        return

    settings = get_settings()
    configure_logging(json_logs=settings.log_format == "json")
    log = structlog.get_logger(__name__)

    if args.cmd == "profile":
        sys.exit(asyncio.run(cmd_profile()))
    if args.cmd == "poll-ltp":
        sys.exit(asyncio.run(cmd_poll_ltp()))
    if args.cmd == "ws-feed":
        sys.exit(asyncio.run(cmd_ws_feed()))
    if args.cmd == "orders-sync":
        sys.exit(asyncio.run(cmd_orders_sync()))
    log.error("unknown_command", cmd=args.cmd)
    sys.exit(2)


if __name__ == "__main__":
    run()
