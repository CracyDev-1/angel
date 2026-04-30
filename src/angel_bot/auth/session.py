from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from angel_bot.config import Settings, get_settings

log = structlog.get_logger(__name__)

LOGIN_PATH = "/rest/auth/angelbroking/user/v1/loginByPassword"
REFRESH_PATH = "/rest/auth/angelbroking/jwt/v1/generateTokens"
PROFILE_PATH = "/rest/secure/angelbroking/user/v1/getProfile"


class AngelHttpError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _public_headers(settings: Settings, *, with_auth: bool = False, jwt: str | None = None) -> dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": settings.angel_client_local_ip,
        "X-ClientPublicIP": settings.angel_client_public_ip,
        "X-MACAddress": settings.angel_mac_address,
        "X-PrivateKey": settings.angel_api_key.get_secret_value(),
    }
    if with_auth and jwt:
        token = jwt if jwt.startswith("Bearer ") else f"Bearer {jwt}"
        h["Authorization"] = token
    return h


def resolve_totp_from_settings(settings: Settings) -> str:
    if settings.angel_totp and settings.angel_totp.get_secret_value().strip():
        return settings.angel_totp.get_secret_value().strip()
    if settings.angel_totp_secret and settings.angel_totp_secret.get_secret_value().strip():
        import pyotp

        return pyotp.TOTP(settings.angel_totp_secret.get_secret_value().strip()).now()
    raise ValueError(
        "No TOTP configured. Either set ANGEL_TOTP / ANGEL_TOTP_SECRET in .env, "
        "or start the dashboard and enter the 6-digit code at runtime: "
        "`python -m angel_bot.main dashboard`"
    )


def totp_configured_in_env(settings: Settings) -> bool:
    """True only when env TOTP can actually be used to log in.

    A static ANGEL_TOTP is one-shot (only useful for a single login), so it
    does NOT count as "auto mode" — only a valid base32 ANGEL_TOTP_SECRET does.
    """
    secret = (
        settings.angel_totp_secret.get_secret_value().strip()
        if settings.angel_totp_secret
        else ""
    )
    if not secret:
        return False
    try:
        import pyotp

        pyotp.TOTP(secret).now()
        return True
    except Exception as e:  # noqa: BLE001 — invalid secret should not crash startup
        log.warning("invalid_angel_totp_secret", error=str(e))
        return False


class AngelSession:
    """JWT + refresh + feedToken with refresh and bounded retries."""

    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None):
        self.settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=self.settings.angel_base_url, timeout=30.0)
        self.jwt: str | None = None
        self.refresh_token: str | None = None
        self.feed_token: str | None = None
        self._runtime_totp: str | None = None

    def set_runtime_totp(self, code: str | None) -> None:
        """One-shot TOTP from the dashboard (cleared after login attempt)."""
        c = (code or "").strip()
        self._runtime_totp = c or None

    def clear_runtime_totp(self) -> None:
        self._runtime_totp = None

    def _totp_for_login(self) -> str:
        if self._runtime_totp:
            return self._runtime_totp
        return resolve_totp_from_settings(self.settings)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def login(self) -> dict[str, Any]:
        body = {
            "clientcode": self.settings.angel_client_code,
            "password": self.settings.angel_pin.get_secret_value(),
            "totp": self._totp_for_login(),
        }
        return await self._post_json(LOGIN_PATH, body, auth=False)

    async def refresh(self) -> dict[str, Any]:
        if not self.refresh_token:
            raise AngelHttpError("No refresh token; login first.")
        body = {"refreshToken": self.refresh_token}
        return await self._post_json(REFRESH_PATH, body, auth=True, jwt=self.jwt)

    async def get_profile(self) -> dict[str, Any]:
        if not self.refresh_token:
            raise AngelHttpError("No refresh token; login first.")
        params = {"refreshToken": self.refresh_token}
        return await self._get_json(PROFILE_PATH, params=params, jwt=self.jwt)

    def apply_login_payload(self, data: dict[str, Any]) -> None:
        self.jwt = data.get("jwtToken")
        self.refresh_token = data.get("refreshToken")
        self.feed_token = data.get("feedToken")

    async def ensure_login(self, *, force: bool = False) -> None:
        if self.jwt and self.refresh_token and not force:
            return
        # loginByPassword consumes a one-time TOTP — DO NOT retry on broker auth
        # rejections (status:false / 4xx); only retry on transport hiccups.
        try:
            resp = await self._login_with_transport_retries()
        finally:
            # consume the runtime code so it can't be reused by accident.
            self.clear_runtime_totp()
        if not resp.get("status"):
            raise AngelHttpError("Login failed", body=resp)
        self.apply_login_payload(resp["data"])

    async def _login_with_transport_retries(
        self, attempts: int = 3, base_delay: float = 0.5
    ) -> dict[str, Any]:
        last: Exception | None = None
        for i in range(attempts):
            try:
                return await self.login()
            except httpx.TransportError as e:
                last = e
                delay = base_delay * (2**i)
                log.warning("login_transport_retry", attempt=i + 1, error=str(e), sleep_s=delay)
                await asyncio.sleep(delay)
        assert last is not None
        raise last

    async def refresh_tokens(self) -> None:
        """Call generateTokens; on failure fall back to full login (e.g. session expired at midnight)."""
        try:
            resp = await self._with_retries(self.refresh)
            if resp.get("status") and isinstance(resp.get("data"), dict):
                self.apply_login_payload(resp["data"])
                return
        except AngelHttpError as e:
            log.warning("token_refresh_failed", error=str(e))
        await self.ensure_login(force=True)

    async def _with_retries(self, fn, attempts: int = 4, base_delay: float = 0.5):
        last: Exception | None = None
        for i in range(attempts):
            try:
                return await fn()
            except (httpx.TransportError, AngelHttpError) as e:
                last = e
                delay = base_delay * (2**i)
                log.warning("retry", attempt=i + 1, error=str(e), sleep_s=delay)
                await asyncio.sleep(delay)
        assert last is not None
        raise last

    async def _post_json(
        self,
        path: str,
        json: dict[str, Any],
        *,
        auth: bool,
        jwt: str | None = None,
    ) -> dict[str, Any]:
        headers = _public_headers(self.settings, with_auth=auth, jwt=jwt)
        r = await self._client.post(path, json=json, headers=headers)
        try:
            payload = r.json()
        except Exception as exc:
            raise AngelHttpError(f"Non-JSON response: {r.text[:500]}", status_code=r.status_code) from exc
        if r.status_code >= 400:
            raise AngelHttpError(
                f"HTTP {r.status_code} for {path}",
                status_code=r.status_code,
                body=payload,
            )
        return payload

    async def _get_json(
        self,
        path: str,
        *,
        params: dict[str, Any],
        jwt: str | None,
    ) -> dict[str, Any]:
        headers = _public_headers(self.settings, with_auth=True, jwt=jwt)
        r = await self._client.get(path, params=params, headers=headers)
        try:
            payload = r.json()
        except Exception as exc:
            raise AngelHttpError(f"Non-JSON response: {r.text[:500]}", status_code=r.status_code) from exc
        if r.status_code >= 400:
            raise AngelHttpError(
                f"HTTP {r.status_code} for {path}",
                status_code=r.status_code,
                body=payload,
            )
        return payload
