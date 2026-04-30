from __future__ import annotations

import json
import re
from typing import Any, Literal

import httpx
import structlog

from angel_bot.config import Settings, get_settings

log = structlog.get_logger(__name__)

LLMVerdict = Literal["YES", "NO", "AVOID"]


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError("LLM did not return JSON object")
    return json.loads(m.group(0))


async def llm_filter_setup(
    *,
    market_context: dict[str, Any],
    proposed_signal: str,
    settings: Settings | None = None,
) -> LLMVerdict:
    """
    Optional filter only. Returns YES / NO / AVOID from strict JSON.
    Does not place orders. Never include API keys or broker tokens in `market_context`.
    """
    settings = settings or get_settings()
    key = settings.openai_api_key
    if not key:
        log.info("llm_filter_skipped", reason="no OPENAI_API_KEY")
        return "YES"

    system = (
        "You are a risk filter for an existing rule-based trade setup. "
        "You do NOT decide entries alone. Reply with ONE JSON object only: "
        '{"verdict":"YES"|"NO"|"AVOID","reason":"short text"}. '
        "Use NO or AVOID when context is ambiguous, stale, or late/choppy."
    )
    user = json.dumps({"proposed_signal": proposed_signal, "market": market_context})

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {key.get_secret_value()}",
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.openai_model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()
    content = data["choices"][0]["message"]["content"]
    parsed = _extract_json_object(content)
    verdict = parsed.get("verdict", "AVOID")
    if verdict not in ("YES", "NO", "AVOID"):
        return "AVOID"
    return verdict  # type: ignore[return-value]
