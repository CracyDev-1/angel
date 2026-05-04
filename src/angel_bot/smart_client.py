from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import structlog

from angel_bot.auth.session import AngelHttpError, AngelSession, _public_headers
from angel_bot.config import Settings, get_settings
from angel_bot.ratelimit import get_rate_limiter, looks_rate_limited

log = structlog.get_logger(__name__)

# Single-symbol LTP. Angel rejects batch bodies here.
LTP_PATH = "/rest/secure/angelbroking/order/v1/getLtpData"
# Batch market data ("Quote Service") — accepts {"mode": "LTP|OHLC|FULL",
# "exchangeTokens": {"NSE":[...], "NFO":[...], "MCX":[...]}}.
# IMPORTANT: trailing slash is mandatory. Angel's gateway 301-redirects
# `.../quote` → `.../quote/`, and httpx does NOT auto-follow POST redirects,
# so without the slash the response body is empty (we'd see "Non-JSON
# response: ''" in logs). Verified against Angel's official REST docs.
MARKET_DATA_PATH = "/rest/secure/angelbroking/market/v1/quote/"
PLACE_ORDER_PATH = "/rest/secure/angelbroking/order/v1/placeOrder"
CANCEL_ORDER_PATH = "/rest/secure/angelbroking/order/v1/cancelOrder"
ORDER_BOOK_PATH = "/rest/secure/angelbroking/order/v1/getOrderBook"
TRADE_BOOK_PATH = "/rest/secure/angelbroking/order/v1/getTradeBook"
RMS_PATH = "/rest/secure/angelbroking/user/v1/getRMS"
POSITION_PATH = "/rest/secure/angelbroking/order/v1/getPosition"
HOLDING_PATH = "/rest/secure/angelbroking/portfolio/v1/getHolding"
HISTORICAL_PATH = "/rest/secure/angelbroking/historical/v1/getCandleData"
# When Angel returns 403 on getCandleData, back the limiter off longer than
# for quote endpoints — the gateway shares a global budget.
HISTORICAL_RATE_LIMIT_COOLDOWN_S = 5.0
# Pause before the single retry in _after_rate_limit (first attempt already
# received note_rate_limited's synthetic reservations).
HISTORICAL_RETRY_PAUSE_S = 1.0

# Angel historical-candle ``interval`` values. Keys are the minute counts the
# bot already uses internally so callers can ask for a given step without
# memorising the broker's enum strings.
HIST_INTERVAL_BY_MINUTES: dict[int, str] = {
    1: "ONE_MINUTE",
    3: "THREE_MINUTE",
    5: "FIVE_MINUTE",
    10: "TEN_MINUTE",
    15: "FIFTEEN_MINUTE",
    30: "THIRTY_MINUTE",
    60: "ONE_HOUR",
}


class SmartApiClient:
    """Authenticated REST calls (LTP, orders, books) with JWT refresh on auth failures."""

    def __init__(self, session: AngelSession, settings: Settings | None = None):
        self.session = session
        self.settings = settings or get_settings()

    # Documented Angel auth-error codes; anything else (e.g. AB1004 "Tokens
    # max limit exceeded") must NOT trigger a JWT refresh-and-retry cycle.
    _AUTH_ERROR_CODES = frozenset({"AG8001", "AG8002", "AG8003", "AB1010", "AB1011"})

    def _auth_retryable(self, e: AngelHttpError) -> bool:
        # 401 is unambiguously auth. 403 used to be treated as auth too, but
        # Angel uses 403 for both "JWT bad" AND "you tripped the per-second
        # rate limit" — the latter is FAR more common and was flooding logs
        # with "smartapi_refresh_after_auth_error" + an immediate JWT
        # generateTokens call (which also costs rate budget). The
        # rate-limit retry path catches 403s with rate-limit phrases or
        # empty bodies, so by the time we get here a 403 is genuinely
        # auth-shaped only if its message looks like one.
        if e.status_code == 401:
            return True
        body = e.body
        if isinstance(body, dict):
            err = str(body.get("errorcode", "")).strip().upper()
            if err in self._AUTH_ERROR_CODES:
                return True
            msg = str(body.get("message", "")).lower()
            # Strict phrase match — substring-only checks (`"token" in msg`)
            # incorrectly classify AB1004's message ("Tokens max limit
            # exceeded") as auth, causing a useless refresh + retry of an
            # oversized payload.
            if "invalid token" in msg:
                return True
            if "token expired" in msg or "session expired" in msg:
                return True
            if "unauthorized" in msg:
                return True
        return False

    # Angel One caps the Quote (getMarketData) endpoint at ~50 tokens per
    # request (summed across all exchanges in the body) per their docs, but
    # in practice we've observed AB1004 ("Tokens max limit exceeded") even
    # below that — the per-account / per-burst cap appears to be tighter.
    # We intentionally chunk at 30 to give ourselves headroom; the cost is
    # ~5 small POSTs per scan instead of ~3 large ones, which the rate
    # limiter handles fine.
    MARKET_DATA_BATCH_LIMIT = 30

    async def get_ltp(
        self,
        exchange_tokens: dict[str, list[str]],
        *,
        mode: str = "LTP",
    ) -> dict[str, Any]:
        """Batch quote for many symbols across many exchanges.

        Hits Angel's *getMarketData* endpoint (NOT getLtpData, which is
        single-symbol only). Mode = "LTP" / "OHLC" / "FULL" — "LTP" is the
        cheapest and is what the scanner needs.

        Auto-chunks when the watchlist exceeds Angel's per-request cap of
        ``MARKET_DATA_BATCH_LIMIT`` tokens, then merges the responses so the
        caller sees a single uniform shape.
        """
        await self.session.ensure_login()
        chunks = self._split_exchange_tokens(exchange_tokens, self.MARKET_DATA_BATCH_LIMIT)
        if len(chunks) <= 1:
            body: dict[str, Any] = {"mode": mode, "exchangeTokens": chunks[0] if chunks else {}}
            return await self._post_with_auth_retry(MARKET_DATA_PATH, body)

        # Multi-batch: fire sequentially so the rate limiter sees them as
        # separate calls and applies headroom. Merge `data.fetched` /
        # `data.unfetched` as we go.
        merged_fetched: list[Any] = []
        merged_unfetched: list[Any] = []
        last_status: Any = True
        last_message: str = "SUCCESS"
        for chunk in chunks:
            body = {"mode": mode, "exchangeTokens": chunk}
            resp = await self._post_with_auth_retry(MARKET_DATA_PATH, body)
            if not isinstance(resp, dict):
                continue
            last_status = resp.get("status", last_status)
            last_message = str(resp.get("message", last_message) or last_message)
            data = resp.get("data") or {}
            if isinstance(data, dict):
                fetched = data.get("fetched") or []
                unfetched = data.get("unfetched") or []
                if isinstance(fetched, list):
                    merged_fetched.extend(fetched)
                if isinstance(unfetched, list):
                    merged_unfetched.extend(unfetched)
        return {
            "status": last_status,
            "message": last_message,
            "data": {"fetched": merged_fetched, "unfetched": merged_unfetched},
        }

    @staticmethod
    def _split_exchange_tokens(
        exchange_tokens: dict[str, list[str]],
        limit: int,
    ) -> list[dict[str, list[str]]]:
        """Chunk an ``exchangeTokens`` payload so each chunk has ≤ ``limit``
        total tokens (summed across exchanges)."""
        chunks: list[dict[str, list[str]]] = []
        current: dict[str, list[str]] = {}
        current_count = 0
        for ex, tokens in exchange_tokens.items():
            if not isinstance(tokens, list) or not tokens:
                continue
            for tok in tokens:
                if current_count >= limit:
                    chunks.append(current)
                    current = {}
                    current_count = 0
                current.setdefault(ex, []).append(str(tok))
                current_count += 1
        if current:
            chunks.append(current)
        return chunks

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

    async def get_single_ltp(
        self,
        *,
        exchange: str,
        tradingsymbol: str,
        symboltoken: str,
    ) -> dict[str, Any]:
        """Fetch LTP/OHLC for one instrument via Angel's getLtpData endpoint.

        Used as a fallback when the scanner cache hasn't priced an option
        the brain just resolved (e.g. an ATM strike that joined the
        watchlist a fraction of a second ago) — instead of skipping the
        trade with ``no_execution_price`` we make this single, cheap call
        and retry. Has its own rate-limit bucket (10/sec) so it doesn't
        compete with the scanner's batch quote.
        """
        await self.session.ensure_login()
        body = {
            "exchange": str(exchange).upper(),
            "tradingsymbol": str(tradingsymbol),
            "symboltoken": str(symboltoken),
        }
        return await self._post_with_auth_retry(LTP_PATH, body)

    async def get_candle_data(
        self,
        *,
        exchange: str,
        symboltoken: str,
        interval_minutes: int,
        fromdate: str,
        todate: str,
    ) -> dict[str, Any]:
        """Fetch historical OHLCV candles for one instrument.

        Wraps Angel's ``getCandleData``. ``fromdate`` / ``todate`` must be
        in ``YYYY-MM-DD HH:MM`` IST format per the broker spec. The response
        ``data`` is a list of ``[ts_iso, open, high, low, close, volume]``
        rows ordered oldest → newest. ``interval_minutes`` is mapped to one
        of Angel's named intervals (1/3/5/10/15/30/60 minute).
        """
        await self.session.ensure_login()
        interval = HIST_INTERVAL_BY_MINUTES.get(int(interval_minutes))
        if interval is None:
            raise ValueError(
                f"Unsupported historical interval: {interval_minutes}m (allowed: "
                f"{sorted(HIST_INTERVAL_BY_MINUTES)})"
            )
        body = {
            "exchange": str(exchange).upper(),
            "symboltoken": str(symboltoken),
            "interval": interval,
            "fromdate": fromdate,
            "todate": todate,
        }
        out = await self._post_with_auth_retry(HISTORICAL_PATH, body)
        # Extra spacing beyond the rate-limiter: ``getCandleData`` competes
        # with LTP/position/order traffic for Angel's *global* gateway budget.
        # A short pause after every successful call keeps 403 thrash down.
        gap = float(getattr(self.settings, "rate_limit_candle_min_gap_s", 0.0) or 0.0)
        if gap > 0:
            await asyncio.sleep(gap)
        return out

    async def _post_with_auth_retry(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._secure_post(path, json)
        except AngelHttpError as e:
            if self._rate_limit_retryable(e):
                return await self._after_rate_limit(lambda: self._secure_post(path, json), path)
            if self._auth_retryable(e):
                log.info("smartapi_refresh_after_auth_error", path=path)
                await self.session.refresh_tokens()
                return await self._secure_post(path, json)
            raise

    async def _get_with_auth_retry(self, path: str) -> dict[str, Any]:
        try:
            return await self._secure_get(path)
        except AngelHttpError as e:
            if self._rate_limit_retryable(e):
                return await self._after_rate_limit(lambda: self._secure_get(path), path)
            if self._auth_retryable(e):
                log.info("smartapi_refresh_after_auth_error", path=path)
                await self.session.refresh_tokens()
                return await self._secure_get(path)
            raise

    async def _after_rate_limit(self, fn, path: str) -> dict[str, Any]:
        """One bounded retry after a broker-side 403/rate-limit. The limiter
        already inserted a back-off; historical candles need extra slack vs
        other endpoints because Angel's gateway aggregates quotas globally."""
        await asyncio.sleep(
            HISTORICAL_RETRY_PAUSE_S if path == HISTORICAL_PATH else 0.12
        )
        log.info("smartapi_retry_after_rate_limit", path=path)
        return await fn()

    def _rate_limit_retryable(self, e: AngelHttpError) -> bool:
        return looks_rate_limited(status_code=e.status_code, body=e.body)

    async def _secure_post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        if not self.session.jwt:
            raise AngelHttpError("Missing JWT")
        headers = _public_headers(self.settings, with_auth=True, jwt=self.session.jwt)
        await get_rate_limiter().acquire(path)
        r = await self.session._client.post(path, json=json, headers=headers)
        return self._parse(r, path)

    async def _secure_get(self, path: str) -> dict[str, Any]:
        if not self.session.jwt:
            raise AngelHttpError("Missing JWT")
        headers = _public_headers(self.settings, with_auth=True, jwt=self.session.jwt)
        await get_rate_limiter().acquire(path)
        r = await self.session._client.get(path, headers=headers)
        return self._parse(r, path)

    def _parse(self, r: httpx.Response, path: str) -> dict[str, Any]:
        raw_text = (r.text or "").lstrip("\ufeff")
        text_stripped = raw_text.strip()

        if not text_stripped:
            payload: dict[str, Any] = {}
            if r.status_code >= 400:
                log.warning(
                    "smartapi_empty_error_body",
                    path=path,
                    status_code=r.status_code,
                    content_type=r.headers.get("content-type"),
                    final_url=str(r.url),
                )
                # An empty 403 from Angel's gateway is almost always a
                # rate-limit drop (especially on bursts to /historical or
                # /quote). Push the limiter so the next retry naturally
                # waits, and surface it as a rate-limited error so the
                # auth-retry path doesn't try to refresh JWT.
                if r.status_code == 403:
                    get_rate_limiter().note_rate_limited(
                        path,
                        retry_after_s=(
                            HISTORICAL_RATE_LIMIT_COOLDOWN_S
                            if path == HISTORICAL_PATH
                            else 1.5
                        ),
                    )
                    raise AngelHttpError(
                        f"Rate limited by broker (HTTP 403, empty body) for {path}",
                        status_code=r.status_code,
                        body="rate limit",
                    )
                raise AngelHttpError(
                    "Empty error body from broker (HTTP "
                    f"{r.status_code}) for {path} — often gateway rejection; "
                    "verify symboltoken matches tradingsymbol in the instrument "
                    "master, quantity is a lot multiple, and F&O product type "
                    "matches your account (INTRADAY vs CARRYFORWARD).",
                    status_code=r.status_code,
                    body=None,
                )
            if r.status_code < 400:
                log.warning(
                    "smartapi_empty_success_body",
                    path=path,
                    status_code=r.status_code,
                    final_url=str(r.url),
                )
        else:
            try:
                parsed: Any = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                rate_limited = looks_rate_limited(status_code=r.status_code, body=raw_text)
                if rate_limited:
                    get_rate_limiter().note_rate_limited(
                        path,
                        retry_after_s=(
                            HISTORICAL_RATE_LIMIT_COOLDOWN_S
                            if path == HISTORICAL_PATH
                            else 1.5
                        ),
                    )
                if rate_limited and path == HISTORICAL_PATH:
                    log.info(
                        "smartapi_historical_rate_limit_plain_text",
                        path=path,
                        status_code=r.status_code,
                        body_preview=raw_text[:300],
                        final_url=str(r.url),
                    )
                else:
                    log.warning(
                        "smartapi_non_json_response",
                        path=path,
                        status_code=r.status_code,
                        content_type=r.headers.get("content-type"),
                        body_preview=raw_text[:500],
                        final_url=str(r.url),
                    )
                # Pass the raw text through as ``body`` so the auth-vs-rate-
                # limit classifier upstream can match the message phrase
                # ("Access denied because of exceeding access rate") and
                # avoid a wasted JWT refresh.
                raise AngelHttpError(
                    f"Non-JSON response (HTTP {r.status_code}): {raw_text[:500]!r}",
                    status_code=r.status_code,
                    body=raw_text[:500],
                ) from exc
            if not isinstance(parsed, dict):
                log.warning(
                    "smartapi_non_object_json",
                    path=path,
                    status_code=r.status_code,
                    parsed_preview=repr(parsed)[:200],
                )
                raise AngelHttpError(
                    f"Expected JSON object from {path}, got {type(parsed).__name__}",
                    status_code=r.status_code,
                )
            payload = parsed

        if looks_rate_limited(status_code=r.status_code, body=payload):
            get_rate_limiter().note_rate_limited(
                path,
                retry_after_s=(
                    HISTORICAL_RATE_LIMIT_COOLDOWN_S
                    if path == HISTORICAL_PATH
                    else 1.5
                ),
            )
            raise AngelHttpError(
                f"Rate limited by broker for {path}",
                status_code=r.status_code,
                body=payload,
            )
        if r.status_code >= 400:
            detail = ""
            if isinstance(payload, dict):
                detail = str(
                    payload.get("message")
                    or payload.get("error")
                    or payload.get("errorMessage")
                    or "",
                ).strip()
            err = f"HTTP {r.status_code} for {path}"
            if detail:
                err = f"{err}: {detail}"
            raise AngelHttpError(err, status_code=r.status_code, body=payload)
        if isinstance(payload, dict) and payload.get("status") is False:
            msg = str(payload.get("message", ""))
            err = str(payload.get("errorcode", "")).strip().upper()
            # Auth-shaped errors per Angel docs:
            #   AG8001  Invalid Token
            #   AG8002  Token Expired
            #   AG8003  Token mismatch
            #   AB1010  Invalid Refresh Token
            # IMPORTANT: do NOT match the generic substring "token" in the
            # message — it accidentally catches AB1004 "Tokens max limit
            # exceeded" (the Quote endpoint cap), which is NOT auth.
            auth_codes = {"AG8001", "AG8002", "AG8003", "AB1010", "AB1011"}
            low = msg.lower()
            looks_auth = (
                err in auth_codes
                or "invalid token" in low
                or "token expired" in low
                or "session expired" in low
                or "unauthorized" in low
            )
            if looks_auth:
                raise AngelHttpError(msg, status_code=401, body=payload)
        return payload
