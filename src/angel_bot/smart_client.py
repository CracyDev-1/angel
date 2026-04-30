from __future__ import annotations

from typing import Any

import httpx
import structlog

from angel_bot.auth.session import AngelHttpError, AngelSession, _public_headers
from angel_bot.config import Settings, get_settings

log = structlog.get_logger(__name__)

LTP_PATH = "/rest/secure/angelbroking/order/v1/getLtpData"
PLACE_ORDER_PATH = "/rest/secure/angelbroking/order/v1/placeOrder"
CANCEL_ORDER_PATH = "/rest/secure/angelbroking/order/v1/cancelOrder"
ORDER_BOOK_PATH = "/rest/secure/angelbroking/order/v1/getOrderBook"
TRADE_BOOK_PATH = "/rest/secure/angelbroking/order/v1/getTradeBook"
RMS_PATH = "/rest/secure/angelbroking/user/v1/getRMS"
POSITION_PATH = "/rest/secure/angelbroking/order/v1/getPosition"
HOLDING_PATH = "/rest/secure/angelbroking/portfolio/v1/getHolding"


class SmartApiClient:
    """Authenticated REST calls (LTP, orders, books) with JWT refresh on auth failures."""

    def __init__(self, session: AngelSession, settings: Settings | None = None):
        self.session = session
        self.settings = settings or get_settings()

    def _auth_retryable(self, e: AngelHttpError) -> bool:
        if e.status_code in (401, 403):
            return True
        body = e.body
        if isinstance(body, dict):
            msg = str(body.get("message", "")).lower()
            if "invalid" in msg and "token" in msg:
                return True
            if "expired" in msg and "token" in msg:
                return True
            err = str(body.get("errorcode", "")).lower()
            if "ag" in err or "token" in err:
                return True
        return False

    async def get_ltp(self, exchange_tokens: dict[str, list[str]]) -> dict[str, Any]:
        await self.session.ensure_login()
        body = {"exchangeTokens": exchange_tokens}
        return await self._post_with_auth_retry(LTP_PATH, body)

    async def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        await self.session.ensure_login()
        return await self._post_with_auth_retry(PLACE_ORDER_PATH, order)

    async def cancel_order(self, *, variety: str, orderid: str) -> dict[str, Any]:
        await self.session.ensure_login()
        body = {"variety": variety, "orderid": orderid}
        return await self._post_with_auth_retry(CANCEL_ORDER_PATH, body)

    async def order_book(self) -> dict[str, Any]:
        await self.session.ensure_login()
        return await self._get_with_auth_retry(ORDER_BOOK_PATH)

    async def trade_book(self) -> dict[str, Any]:
        await self.session.ensure_login()
        return await self._get_with_auth_retry(TRADE_BOOK_PATH)

    async def get_rms(self) -> dict[str, Any]:
        await self.session.ensure_login()
        return await self._get_with_auth_retry(RMS_PATH)

    async def get_position(self) -> dict[str, Any]:
        await self.session.ensure_login()
        return await self._get_with_auth_retry(POSITION_PATH)

    async def get_holding(self) -> dict[str, Any]:
        await self.session.ensure_login()
        return await self._get_with_auth_retry(HOLDING_PATH)

    async def _post_with_auth_retry(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._secure_post(path, json)
        except AngelHttpError as e:
            if self._auth_retryable(e):
                log.info("smartapi_refresh_after_auth_error", path=path)
                await self.session.refresh_tokens()
                return await self._secure_post(path, json)
            raise

    async def _get_with_auth_retry(self, path: str) -> dict[str, Any]:
        try:
            return await self._secure_get(path)
        except AngelHttpError as e:
            if self._auth_retryable(e):
                log.info("smartapi_refresh_after_auth_error", path=path)
                await self.session.refresh_tokens()
                return await self._secure_get(path)
            raise

    async def _secure_post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        if not self.session.jwt:
            raise AngelHttpError("Missing JWT")
        headers = _public_headers(self.settings, with_auth=True, jwt=self.session.jwt)
        r = await self.session._client.post(path, json=json, headers=headers)
        return self._parse(r, path)

    async def _secure_get(self, path: str) -> dict[str, Any]:
        if not self.session.jwt:
            raise AngelHttpError("Missing JWT")
        headers = _public_headers(self.settings, with_auth=True, jwt=self.session.jwt)
        r = await self.session._client.get(path, headers=headers)
        return self._parse(r, path)

    def _parse(self, r: httpx.Response, path: str) -> dict[str, Any]:
        try:
            payload: dict[str, Any] = r.json()
        except Exception as exc:
            raise AngelHttpError(f"Non-JSON response: {r.text[:500]}", status_code=r.status_code) from exc
        if r.status_code >= 400:
            raise AngelHttpError(f"HTTP {r.status_code} for {path}", status_code=r.status_code, body=payload)
        if isinstance(payload, dict) and payload.get("status") is False:
            msg = str(payload.get("message", ""))
            if any(x in msg.lower() for x in ("token", "invalid", "expired", "unauthorized")):
                raise AngelHttpError(msg, status_code=401, body=payload)
        return payload
