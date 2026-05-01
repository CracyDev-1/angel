"""Regression tests for the smart-client auth-retry classifier.

The hot bug we caught in production: AB1004 ("Tokens max limit exceeded")
was being classified as an auth error because the substring "token" appears
in its message. That triggered a JWT refresh + immediate retry of the same
oversized payload, masking the real cap-exceeded error. These tests pin
the classifier down so it never regresses.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from angel_bot.auth.session import AngelHttpError
from angel_bot.smart_client import SmartApiClient


class _FakeSession:
    """Minimal stand-in for AngelSession; SmartApiClient only touches `.jwt`
    and `.refresh_tokens()` from the auth-retry path, neither of which is
    exercised by the methods we test here."""

    jwt = "fake-jwt"
    settings = None  # never accessed by the classifier helpers


@pytest.fixture
def client() -> SmartApiClient:
    return SmartApiClient.__new__(SmartApiClient)  # bypass __init__ — pure helpers


# ---------------------------------------------------------------------------
# _auth_retryable: AB1004 must NOT be treated as auth.
# ---------------------------------------------------------------------------


def test_ab1004_tokens_max_limit_is_not_auth_retryable(client: SmartApiClient) -> None:
    e = AngelHttpError(
        "Tokens max limit exceeded",
        status_code=403,  # Angel actually returns 403 for AB1004 in some envs
        body={"status": False, "errorcode": "AB1004", "message": "Tokens max limit exceeded"},
    )
    # 403 alone is auth-retryable; clear that path so we test the body classifier.
    e.status_code = 200
    assert client._auth_retryable(e) is False


@pytest.mark.parametrize(
    "code,msg",
    [
        ("AG8001", "Invalid Token"),
        ("AG8002", "Token Expired"),
        ("AG8003", "Token Mismatch"),
        ("AB1010", "Invalid Refresh Token"),
        ("AB1011", "Invalid Refresh Token"),
    ],
)
def test_documented_auth_codes_are_retryable(
    client: SmartApiClient, code: str, msg: str
) -> None:
    e = AngelHttpError(msg, status_code=200, body={"status": False, "errorcode": code, "message": msg})
    assert client._auth_retryable(e) is True


def test_message_only_invalid_token_is_retryable(client: SmartApiClient) -> None:
    e = AngelHttpError(
        "Invalid Token", status_code=200,
        body={"status": False, "message": "Invalid Token"},
    )
    assert client._auth_retryable(e) is True


# ---------------------------------------------------------------------------
# _parse: AB1004 must surface as a normal AngelHttpError, NOT raised as 401.
# ---------------------------------------------------------------------------


def _resp(payload: dict[str, Any], *, status_code: int = 200) -> httpx.Response:
    req = httpx.Request("POST", "https://example/quote/")
    return httpx.Response(status_code=status_code, json=payload, request=req)


def test_parse_does_not_promote_ab1004_to_401(client: SmartApiClient) -> None:
    payload = {
        "status": False,
        "errorcode": "AB1004",
        "message": "Tokens max limit exceeded",
        "data": None,
    }
    out = client._parse(_resp(payload, status_code=200), "/quote/")
    # status:false bodies that aren't auth-shaped must be returned to the
    # caller (e.g. get_ltp's batch loop) without being re-raised as 401.
    # That's what was missing: previously _parse mistook this for an auth
    # error, kicked off a JWT refresh, and retried the same payload —
    # which immediately failed with the same AB1004.
    assert out == payload


def test_parse_promotes_real_auth_failure_to_401(client: SmartApiClient) -> None:
    payload = {
        "status": False,
        "errorcode": "AG8002",
        "message": "Token Expired",
    }
    with pytest.raises(AngelHttpError) as excinfo:
        client._parse(_resp(payload, status_code=200), "/getRMS")
    assert excinfo.value.status_code == 401


def test_parse_promotes_message_only_invalid_token(client: SmartApiClient) -> None:
    payload = {"status": False, "message": "Invalid Token"}
    with pytest.raises(AngelHttpError) as excinfo:
        client._parse(_resp(payload, status_code=200), "/getRMS")
    assert excinfo.value.status_code == 401
