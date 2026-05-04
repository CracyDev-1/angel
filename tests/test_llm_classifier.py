"""Tests for the LLM classifier path used by the new 5m pipeline."""
from __future__ import annotations

import json
import os

import httpx
import pytest

from angel_bot.config import Settings
from angel_bot.llm.filter import LlmClassification, llm_classify_setup


def _settings(**overrides) -> Settings:
    base = dict(
        ANGEL_API_KEY="k",
        ANGEL_CLIENT_CODE="c",
        ANGEL_PIN="p",
        OPENAI_API_KEY="sk-test",
        LLM_FILTER_ENABLED="true",
        LLM_FILTER_FAIL_CLOSED="true",
        LLM_DECISION_THRESHOLD="0.65",
    )
    base.update(overrides)
    for k, v in base.items():
        os.environ[k] = str(v)
    return Settings()


def _fake_openai_client(content: str) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"choices": [{"message": {"content": content}}]}
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_classifier_take_high_confidence():
    s = _settings()
    client = _fake_openai_client(
        json.dumps({"decision": "TAKE", "confidence": 0.81, "type": "breakout", "reason": "clean push"})
    )
    res = await llm_classify_setup(
        market_context={"x": 1}, proposed_signal="BUY_CALL NIFTY",
        proposed_pattern="breakout", settings=s, client=client,
    )
    assert isinstance(res, LlmClassification)
    assert res.decision == "TAKE"
    assert 0.80 <= res.confidence <= 0.82
    assert res.pattern_type == "breakout"
    assert res.passes(0.65)
    assert not res.passes(0.85)


@pytest.mark.asyncio
async def test_classifier_skip_blocks_even_with_high_confidence():
    s = _settings()
    client = _fake_openai_client(
        json.dumps({"decision": "SKIP", "confidence": 0.95, "type": "pullback", "reason": "exhausted"})
    )
    res = await llm_classify_setup(
        market_context={}, proposed_signal="BUY_PUT BANKNIFTY",
        proposed_pattern="pullback", settings=s, client=client,
    )
    assert res.decision == "SKIP"
    assert not res.allowed
    assert not res.passes(0.65)


@pytest.mark.asyncio
async def test_classifier_unknown_type_falls_back_to_other():
    s = _settings()
    client = _fake_openai_client(
        json.dumps({"decision": "TAKE", "confidence": 0.7, "type": "scalp", "reason": "x"})
    )
    res = await llm_classify_setup(
        market_context={}, proposed_signal="x",
        proposed_pattern="breakout", settings=s, client=client,
    )
    assert res.pattern_type == "other"


@pytest.mark.asyncio
async def test_classifier_clamps_confidence_into_unit_range():
    s = _settings()
    client = _fake_openai_client(
        json.dumps({"decision": "TAKE", "confidence": 9.9, "type": "breakout", "reason": ""})
    )
    res = await llm_classify_setup(
        market_context={}, proposed_signal="x",
        proposed_pattern="breakout", settings=s, client=client,
    )
    assert res.confidence == 1.0


@pytest.mark.asyncio
async def test_classifier_fail_closed_on_http_error():
    s = _settings(LLM_FILTER_FAIL_CLOSED="true")

    def boom(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    res = await llm_classify_setup(
        market_context={}, proposed_signal="x",
        proposed_pattern="other", settings=s, client=client,
    )
    assert res.decision == "SKIP"
    assert res.source == "fail_closed"


@pytest.mark.asyncio
async def test_classifier_fail_open_on_http_error_returns_low_confidence():
    s = _settings(LLM_FILTER_FAIL_CLOSED="false")

    def boom(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    res = await llm_classify_setup(
        market_context={}, proposed_signal="x",
        proposed_pattern="other", settings=s, client=client,
    )
    # Fail-open: TAKE with conf=0.5 — gets blocked by default 0.65 threshold.
    assert res.decision == "TAKE"
    assert res.confidence < 0.65
    assert not res.passes(0.65)


@pytest.mark.asyncio
async def test_classifier_disabled_short_circuits_to_take_full_confidence():
    s = _settings(LLM_FILTER_ENABLED="false")
    res = await llm_classify_setup(
        market_context={}, proposed_signal="x",
        proposed_pattern="breakout", settings=s,
    )
    assert res.decision == "TAKE"
    assert res.confidence == 1.0
    assert res.source == "disabled"


@pytest.mark.asyncio
async def test_classifier_no_key_short_circuits():
    s = _settings(OPENAI_API_KEY="")
    res = await llm_classify_setup(
        market_context={}, proposed_signal="x",
        proposed_pattern="breakout", settings=s,
    )
    assert res.decision == "TAKE"
    assert res.source == "no_key"


@pytest.mark.asyncio
async def test_classifier_take_includes_clamped_exit_params():
    s = _settings()
    client = _fake_openai_client(
        json.dumps(
            {
                "decision": "TAKE",
                "confidence": 0.8,
                "type": "breakout",
                "reason": "ok",
                "stop_loss_pct": 0.5,
                "take_profit_pct": 0.001,
                "max_hold_minutes": 999,
            }
        )
    )
    res = await llm_classify_setup(
        market_context={}, proposed_signal="x",
        proposed_pattern="breakout", settings=s, client=client,
    )
    assert res.stop_loss_pct == pytest.approx(float(s.llm_exit_sl_pct_max))
    assert res.take_profit_pct == pytest.approx(float(s.llm_exit_tp_pct_min))
    assert res.max_hold_minutes == int(s.llm_exit_hold_max)


@pytest.mark.asyncio
async def test_classifier_skip_ignores_exit_param_keys():
    s = _settings()
    client = _fake_openai_client(
        json.dumps(
            {
                "decision": "SKIP",
                "confidence": 0.9,
                "type": "breakout",
                "reason": "x",
                "stop_loss_pct": 0.02,
                "take_profit_pct": 0.1,
                "max_hold_minutes": 60,
            }
        )
    )
    res = await llm_classify_setup(
        market_context={}, proposed_signal="x",
        proposed_pattern="breakout", settings=s, client=client,
    )
    assert res.decision == "SKIP"
    assert res.stop_loss_pct is None
    assert res.take_profit_pct is None
    assert res.max_hold_minutes is None
