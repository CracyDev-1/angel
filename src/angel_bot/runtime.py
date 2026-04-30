from __future__ import annotations

import asyncio
import queue

import structlog

from angel_bot.auth.session import AngelHttpError, AngelSession
from angel_bot.config import Settings, get_settings
from angel_bot.market_data.ws_binary import parse_ws_subscriptions
from angel_bot.market_data.ws_feed import AngelWebSocketFeed
from angel_bot.smart_client import SmartApiClient

log = structlog.get_logger(__name__)


class TradingRuntime:
    """Single live Angel session + optional bot task (owned by dashboard / long-running process)."""

    _instance: TradingRuntime | None = None

    def __init__(self) -> None:
        self.settings: Settings = get_settings()
        self.session: AngelSession | None = None
        self._bot_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_error: str | None = None

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

    async def connect_with_totp(self, totp: str) -> dict:
        """Login with a one-time TOTP from the dashboard (not stored in .env)."""
        self.last_error = None
        code = (totp or "").strip()
        if len(code) != 6 or not code.isdigit():
            raise ValueError("Enter the current 6-digit code from your authenticator app.")

        if self.session is None:
            self.session = AngelSession(self.settings)

        self.session.set_runtime_totp(code)
        try:
            await self.session.ensure_login(force=True)
        except AngelHttpError as e:
            self.last_error = str(e)
            raise
        except ValueError as e:
            self.last_error = str(e)
            raise

        prof = await self.session.get_profile()
        ok = bool(prof.get("status"))
        if not ok:
            self.last_error = str(prof.get("message") or prof)
        return {"status": ok, "profile_message": prof.get("message"), "clientcode": prof.get("data", {}).get("clientcode")}

    async def disconnect(self) -> None:
        await self.stop_bot()
        if self.session:
            await self.session.aclose()
            self.session = None

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
        self._bot_task = asyncio.create_task(self._bot_loop(), name="angel-bot-loop")

    async def _bot_loop(self) -> None:
        assert self.session is not None
        s = self.settings
        subs = parse_ws_subscriptions(s.ws_subscriptions)
        try:
            if subs and self.session.feed_token:
                await self._ws_loop(subs)
            else:
                await self._poll_loop()
        except asyncio.CancelledError:
            log.info("bot_cancelled")
            raise
        except Exception as e:
            self.last_error = str(e)
            log.exception("bot_loop_error")
            raise

    async def _ws_loop(self, subs: list) -> None:
        assert self.session is not None
        s = self.settings
        q: queue.Queue = queue.Queue(maxsize=50_000)
        feed = AngelWebSocketFeed(
            jwt=self.session.jwt or "",
            api_key=s.angel_api_key.get_secret_value(),
            client_code=s.angel_client_code,
            feed_token=self.session.feed_token or "",
            token_list=subs,
            mode=s.ws_feed_mode,
            out_queue=q,
        )
        feed.start()
        log.info("bot_ws_started", subscriptions=subs)
        try:
            while not self._stop.is_set():
                drained = 0
                while drained < 200:
                    try:
                        tick = q.get_nowait()
                    except queue.Empty:
                        break
                    log.info("tick", **{k: tick.get(k) for k in ("token", "last_traded_price", "exchange_type") if isinstance(tick, dict)})
                    drained += 1
                await asyncio.sleep(0.05)
        finally:
            feed.stop()
            log.info("bot_ws_stopped")

    async def _poll_loop(self) -> None:
        assert self.session is not None
        s = self.settings
        api = SmartApiClient(self.session, s)
        tokens = s.ltp_exchange_tokens()
        log.info("bot_poll_started", interval_s=s.ltp_poll_interval_s)
        while not self._stop.is_set():
            try:
                resp = await api.get_ltp(tokens)
                log.info("ltp_poll", ok=resp.get("status"))
            except Exception as e:
                log.warning("ltp_poll_error", error=str(e))
            for _ in range(max(1, int(s.ltp_poll_interval_s / 0.25))):
                if self._stop.is_set():
                    break
                await asyncio.sleep(0.25)
        log.info("bot_poll_stopped")

    async def shutdown(self) -> None:
        await self.disconnect()
