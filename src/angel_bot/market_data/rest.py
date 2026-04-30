from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from angel_bot.smart_client import SmartApiClient

log = structlog.get_logger(__name__)


class LtpPoller:
    """Phase-1 polling for LTP (5–10s cadence)."""

    def __init__(
        self,
        client: SmartApiClient,
        exchange_tokens: dict[str, list[str]],
        interval_s: float = 7.5,
    ):
        self.client = client
        self.exchange_tokens = exchange_tokens
        self.interval_s = interval_s

    async def fetch_once(self) -> dict[str, Any]:
        return await self.client.get_ltp(self.exchange_tokens)

    async def run_loop(self, on_tick: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        while True:
            try:
                data = await self.fetch_once()
                await on_tick(data)
            except Exception:
                log.exception("ltp_poll_error")
            await asyncio.sleep(self.interval_s)
