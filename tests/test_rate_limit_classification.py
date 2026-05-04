"""Tests for rate-limit detection and the auth/rate-limit dichotomy.

Background: the production trace showed Angel returning HTTP 403 with a
plain-text body ``Access denied because of exceeding access rate``. The
old classifier failed to spot that as a rate-limit (only checked dict
bodies) AND treated 403 as auth, which triggered a JWT refresh on every
burst. The refresh itself eats rate budget → cascading thrash. These
tests pin the corrected behaviour:

* Plain-text "Access denied because of exceeding access rate" is
  rate-limited.
* Empty-body 403 is rate-limited (Angel's gateway sometimes drops the
  body but the meaning is still "you tripped the per-second limit").
* 403 with a real auth-shaped body is still auth-retryable.
* Bare 401 is auth-retryable; bare 403 is NOT (it's caught by the rate-
  limit retry path first).
* SmartApiClient._parse turns a non-JSON 403 body into an AngelHttpError
  whose ``body`` field carries the raw text, so the upstream
  rate-limit classifier can match it.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from angel_bot.auth.session import AngelHttpError
from angel_bot.ratelimit import looks_rate_limited, reset_rate_limiter
from angel_bot.smart_client import SmartApiClient


@pytest.fixture(autouse=True)
def _reset_limiter() -> None:
    reset_rate_limiter()


@pytest.fixture
def client() -> SmartApiClient:
    return SmartApiClient.__new__(SmartApiClient)


# ---------------------------------------------------------------------------
# looks_rate_limited
# ---------------------------------------------------------------------------


def test_plain_text_access_denied_is_rate_limited() -> None:
    assert looks_rate_limited(
        status_code=403,
        body="Access denied because of exceeding access rate",
    )


def test_dict_body_with_rate_limit_message_is_rate_limited() -> None:
    assert looks_rate_limited(
        status_code=403,
        body={"status": False, "message": "Rate limit exceeded"},
    )


def test_empty_403_treated_as_rate_limited() -> None:
    """Angel's gateway sometimes drops the explanatory text on a 403 burst.
    Treat it as rate-limit so we back off rather than refresh JWT."""
    assert looks_rate_limited(status_code=403, body=None)
    assert looks_rate_limited(status_code=403, body={})
    assert looks_rate_limited(status_code=403, body="")


def test_429_is_rate_limited() -> None:
    assert looks_rate_limited(status_code=429, body=None)


def test_auth_shaped_403_is_not_rate_limited() -> None:
    """A 403 with a real auth-shaped JSON body must NOT be misclassified."""
    body = {"status": False, "errorcode": "AG8001", "message": "Invalid Token"}
    assert looks_rate_limited(status_code=403, body=body) is False


def test_400_with_unrelated_message_is_not_rate_limited() -> None:
    assert looks_rate_limited(
        status_code=400,
        body={"status": False, "message": "symboltoken not in master"},
    ) is False


def test_bytes_body_is_handled() -> None:
    assert looks_rate_limited(
        status_code=403,
        body=b"Access denied because of exceeding access rate",
    )


# ---------------------------------------------------------------------------
# SmartApiClient._auth_retryable — bare 403 is no longer auth.
# ---------------------------------------------------------------------------


def test_bare_403_is_not_auth_retryable(client: SmartApiClient) -> None:
    """Used to be: any 403 → JWT refresh. Now: only 401, or message-shaped
    auth bodies. Plain rate-limit 403s skip the refresh path entirely."""
    e = AngelHttpError("forbidden", status_code=403, body=None)
    assert client._auth_retryable(e) is False


def test_bare_401_is_still_auth_retryable(client: SmartApiClient) -> None:
    e = AngelHttpError("unauthorized", status_code=401, body=None)
    assert client._auth_retryable(e) is True


def test_403_with_invalid_token_message_is_auth_retryable(client: SmartApiClient) -> None:
    """Auth-shaped 403s are still classified correctly via message inspection."""
    e = AngelHttpError(
        "Invalid Token",
        status_code=403,
        body={"status": False, "errorcode": "AG8001", "message": "Invalid Token"},
    )
    assert client._auth_retryable(e) is True


def test_403_with_rate_limit_message_is_not_auth(client: SmartApiClient) -> None:
    e = AngelHttpError(
        "Access denied because of exceeding access rate",
        status_code=403,
        body="Access denied because of exceeding access rate",
    )
    assert client._auth_retryable(e) is False
    # And the rate-limit classifier *does* match it:
    assert client._rate_limit_retryable(e) is True


# ---------------------------------------------------------------------------
# _parse: non-JSON 403 must preserve the raw body so upstream can classify it.
# ---------------------------------------------------------------------------


def _text_resp(text: str, *, status_code: int) -> httpx.Response:
    req = httpx.Request("POST", "https://example/getCandleData")
    return httpx.Response(
        status_code=status_code,
        text=text,
        headers={"content-type": "text/plain"},
        request=req,
    )


def test_parse_non_json_403_includes_text_body(client: SmartApiClient) -> None:
    """The upstream auth-vs-rate-limit classifier needs the message to
    classify a 403 correctly. Before this fix the AngelHttpError had
    body=None and we always took the auth path."""
    r = _text_resp("Access denied because of exceeding access rate", status_code=403)
    with pytest.raises(AngelHttpError) as exc:
        client._parse(r, "/rest/secure/angelbroking/historical/v1/getCandleData")
    err = exc.value
    assert err.status_code == 403
    assert isinstance(err.body, str)
    assert "exceeding access rate" in err.body.lower()
    # Rate-limit retry path must catch it now.
    assert client._rate_limit_retryable(err) is True
    assert client._auth_retryable(err) is False


def test_parse_empty_403_promoted_to_rate_limit(client: SmartApiClient) -> None:
    """An empty 403 body (no JSON, no text) becomes a rate-limit error so
    the auth path doesn't refresh JWT on every gateway drop."""
    req = httpx.Request("POST", "https://example/getCandleData")
    r = httpx.Response(status_code=403, text="", request=req)
    with pytest.raises(AngelHttpError) as exc:
        client._parse(r, "/rest/secure/angelbroking/historical/v1/getCandleData")
    err = exc.value
    assert err.status_code == 403
    assert client._rate_limit_retryable(err) is True
    assert client._auth_retryable(err) is False


def test_parse_400_empty_body_still_uses_diagnostic_message(client: SmartApiClient) -> None:
    """Empty 400 bodies are NOT rate-limit — they're still surfaced with
    the placeOrder diagnostic hint."""
    req = httpx.Request("POST", "https://example/placeOrder")
    r = httpx.Response(status_code=400, text="", request=req)
    with pytest.raises(AngelHttpError) as exc:
        client._parse(r, "/rest/secure/angelbroking/order/v1/placeOrder")
    err = exc.value
    assert err.status_code == 400
    assert "verify symboltoken" in str(err).lower()
    assert client._rate_limit_retryable(err) is False


# ---------------------------------------------------------------------------
# SmartApiClient.get_single_ltp — fallback used by runtime when scanner
# cache hasn't priced an option yet.
# ---------------------------------------------------------------------------


def test_get_single_ltp_uses_ltp_endpoint(client: SmartApiClient) -> None:
    import asyncio

    sent: dict[str, Any] = {}

    class _FakeSession:
        async def ensure_login(self) -> None:
            return None

    client.session = _FakeSession()  # type: ignore[assignment]

    async def _fake_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        sent["path"] = path
        sent["body"] = body
        return {"status": True, "data": {"ltp": 123.45}}

    client._post_with_auth_retry = _fake_post  # type: ignore[assignment]

    asyncio.run(
        client.get_single_ltp(
            exchange="NFO",
            tradingsymbol="NIFTY05MAY2624250CE",
            symboltoken="42424",
        )
    )
    assert sent["path"].endswith("/getLtpData")
    assert sent["body"] == {
        "exchange": "NFO",
        "tradingsymbol": "NIFTY05MAY2624250CE",
        "symboltoken": "42424",
    }
