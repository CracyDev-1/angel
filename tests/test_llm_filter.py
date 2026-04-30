"""Tests for the optional OpenAI risk-filter.

We mock httpx with a thin fake transport so no network calls happen.
The filter must be:
  * pass-through when no API key / disabled
  * strict (only YES allows the trade)
  * fail-closed by default, fail-open when configured
  * never leak secrets in the outgoing payload
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from angel_bot.config import Settings
from angel_bot.llm.filter import (
    LlmDecision,
    llm_filter_setup,
    sanitize_context,
)


def _settings(
    *,
    key: str | None = "sk-test",
    enabled: bool = True,
    fail_closed: bool = True,
    timeout: float = 2.0,
    model: str = "gpt-4o-mini",
) -> Settings:
    return Settings(
        ANGEL_API_KEY="api",
        ANGEL_CLIENT_CODE="C1",
        ANGEL_PIN="1234",
        OPENAI_API_KEY=key,
        OPENAI_MODEL=model,
        LLM_FILTER_ENABLED=str(enabled).lower(),
        LLM_FILTER_FAIL_CLOSED=str(fail_closed).lower(),
        LLM_FILTER_TIMEOUT_S=timeout,
    )


def _mock_client(responder) -> httpx.AsyncClient:
    """Return an httpx.AsyncClient whose transport calls `responder(request) -> Response`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return responder(request)

    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_passthrough_when_no_api_key() -> None:
    s = _settings(key=None)
    dec = await llm_filter_setup(market_context={"foo": 1}, proposed_signal="BUY_CALL", settings=s)
    assert isinstance(dec, LlmDecision)
    assert dec.allowed is True
    assert dec.verdict == "YES"
    assert dec.source == "no_key"


@pytest.mark.asyncio
async def test_passthrough_when_disabled() -> None:
    s = _settings(enabled=False)
    dec = await llm_filter_setup(market_context={"foo": 1}, proposed_signal="BUY_CALL", settings=s)
    assert dec.allowed is True
    assert dec.source == "disabled"


@pytest.mark.asyncio
async def test_yes_verdict_allows_trade() -> None:
    s = _settings()

    def respond(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"verdict": "YES", "reason": "trend aligned"})}}
                ]
            },
        )

    async with _mock_client(respond) as client:
        dec = await llm_filter_setup(
            market_context={"x": 1}, proposed_signal="BUY_CALL", settings=s, client=client
        )
    assert dec.allowed is True
    assert dec.verdict == "YES"
    assert "trend" in dec.reason
    assert dec.source == "openai"


@pytest.mark.asyncio
async def test_no_verdict_vetoes_trade() -> None:
    s = _settings()

    def respond(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"verdict": "NO", "reason": "against 15m trend"}'}}
                ]
            },
        )

    async with _mock_client(respond) as client:
        dec = await llm_filter_setup(
            market_context={"x": 1}, proposed_signal="BUY_CALL", settings=s, client=client
        )
    assert dec.allowed is False
    assert dec.verdict == "NO"


@pytest.mark.asyncio
async def test_avoid_verdict_vetoes_trade() -> None:
    s = _settings()

    def respond(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"verdict":"AVOID","reason":"choppy"}'}}]},
        )

    async with _mock_client(respond) as client:
        dec = await llm_filter_setup(
            market_context={"x": 1}, proposed_signal="BUY_PUT", settings=s, client=client
        )
    assert dec.allowed is False
    assert dec.verdict == "AVOID"


@pytest.mark.asyncio
async def test_unknown_verdict_becomes_avoid() -> None:
    s = _settings()

    def respond(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"verdict":"MAYBE","reason":"hmm"}'}}]},
        )

    async with _mock_client(respond) as client:
        dec = await llm_filter_setup(
            market_context={"x": 1}, proposed_signal="BUY_CALL", settings=s, client=client
        )
    assert dec.allowed is False
    assert dec.verdict == "AVOID"


@pytest.mark.asyncio
async def test_non_json_reply_fails_closed() -> None:
    s = _settings(fail_closed=True)

    def respond(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "definitely not json"}}]})

    async with _mock_client(respond) as client:
        dec = await llm_filter_setup(
            market_context={"x": 1}, proposed_signal="BUY_CALL", settings=s, client=client
        )
    assert dec.allowed is False
    assert dec.source == "fail_closed"
    assert "non_json_reply" in dec.reason


@pytest.mark.asyncio
async def test_http_error_fails_closed() -> None:
    s = _settings(fail_closed=True)

    def respond(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    async with _mock_client(respond) as client:
        dec = await llm_filter_setup(
            market_context={"x": 1}, proposed_signal="BUY_CALL", settings=s, client=client
        )
    assert dec.allowed is False
    assert dec.source == "fail_closed"


@pytest.mark.asyncio
async def test_http_error_fails_open_when_configured() -> None:
    s = _settings(fail_closed=False)

    def respond(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "x"})

    async with _mock_client(respond) as client:
        dec = await llm_filter_setup(
            market_context={"x": 1}, proposed_signal="BUY_CALL", settings=s, client=client
        )
    assert dec.allowed is True
    assert dec.source == "error"


@pytest.mark.asyncio
async def test_request_does_not_leak_secrets() -> None:
    """The outgoing payload must not include API keys / JWTs / broker tokens."""
    s = _settings()
    captured: dict[str, Any] = {}

    def respond(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content.decode())
        captured["headers"] = dict(req.headers)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"verdict":"YES","reason":"ok"}'}}]},
        )

    poisoned_ctx = {
        "underlying": {
            "name": "NIFTY",
            "spot": 24500,
            "symboltoken": "99926000",  # MUST be stripped
            "token": "99926000",        # MUST be stripped
        },
        "secrets": {
            "ANGEL_PIN": "9999",        # MUST be stripped
            "OPENAI_API_KEY": "sk-x",   # MUST be stripped
            "jwt": "Bearer abcdef",     # MUST be stripped
            "Authorization": "Bearer x",
        },
        "brain": {"score": 0.7},
    }
    async with _mock_client(respond) as client:
        await llm_filter_setup(
            market_context=poisoned_ctx,
            proposed_signal="BUY_CALL NIFTY24500CE",
            settings=s,
            client=client,
        )

    body = captured["body"]
    user_msg = body["messages"][1]["content"]
    # None of the secret strings should appear in the user message.
    for needle in ("9999", "sk-x", "Bearer abcdef", "Bearer x", "99926000"):
        assert needle not in user_msg, f"Secret leaked into LLM payload: {needle}"
    # Auth header is allowed (that's how we authenticate to OpenAI itself).
    assert captured["headers"].get("authorization", "").startswith("Bearer ")


def test_sanitize_context_strips_known_keys() -> None:
    raw = {
        "ok": "keep",
        "ANGEL_PIN": "1234",
        "nested": {"jwt": "x", "okay": "keep"},
        "list": [{"token": "drop", "name": "keep"}],
    }
    cleaned = sanitize_context(raw)
    assert cleaned == {
        "ok": "keep",
        "nested": {"okay": "keep"},
        "list": [{"name": "keep"}],
    }
