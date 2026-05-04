"""Optional OpenAI risk-filter for trade attempts.

The LLM does **not** decide trades. It only acts as a veto gate AFTER the
rule-based brain (`strategy/brain.py`) has already produced a BUY_CALL or
BUY_PUT signal AND after all funds/risk checks have passed.

Flow (in `runtime._consider_trade`):

    brain → BUY_CALL on NIFTY
        ↓
    risk + funds + lot-fit checks pass
        ↓
    llm_filter_setup(market_context, "BUY_CALL @ NIFTY24500CE")
        ↓
    YES → place order
    NO / AVOID / error (and fail-closed) → skip, log the reason

Safety:
  * No API keys / JWTs / broker tokens / client codes are EVER sent.
  * Strict JSON output. Anything else → AVOID.
  * Configurable fail-open vs fail-closed for outages.
  * Hard timeout (default 8s) so a slow LLM never holds up the trade loop.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
import structlog

from angel_bot.config import Settings, get_settings

log = structlog.get_logger(__name__)

LLMVerdict = Literal["YES", "NO", "AVOID"]


@dataclass
class LlmDecision:
    """Structured result from the LLM filter."""

    verdict: LLMVerdict          # YES / NO / AVOID
    allowed: bool                # True iff verdict == "YES"
    reason: str                  # short text the model returned (or our fallback)
    source: str                  # "openai" / "disabled" / "no_key" / "error" / "fail_closed"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "allowed": self.allowed,
            "reason": self.reason,
            "source": self.source,
        }


def _extract_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError("LLM did not return a JSON object")
    return json.loads(m.group(0))


# Keys we will NEVER forward to the LLM, even if they appear in market_context.
_REDACT_KEYS: frozenset[str] = frozenset({
    "api_key", "apikey", "x-privatekey",
    "jwt", "jwtToken", "access_token", "refresh_token", "feed_token",
    "clientcode", "client_code", "ANGEL_PIN", "pin", "totp",
    "OPENAI_API_KEY", "openai_api_key", "Authorization",
    "symboltoken", "token",  # broker tokens — model doesn't need them
})


def sanitize_context(ctx: Any) -> Any:
    """Strip secrets / broker-specific identifiers from any nested dict/list."""
    if isinstance(ctx, dict):
        out: dict[str, Any] = {}
        for k, v in ctx.items():
            if k in _REDACT_KEYS:
                continue
            out[k] = sanitize_context(v)
        return out
    if isinstance(ctx, list):
        return [sanitize_context(x) for x in ctx]
    return ctx


def clamp_llm_exit_params(
    settings: Settings,
    *,
    stop_loss_pct: float | None,
    take_profit_pct: float | None,
    max_hold_minutes: int | None,
) -> tuple[float | None, float | None, int | None]:
    """Clamp model-suggested exits to configured bounds. None = omit override."""
    if not settings.llm_exit_params_enabled:
        return None, None, None
    out_sl: float | None = None
    out_tp: float | None = None
    out_mh: int | None = None
    if stop_loss_pct is not None:
        try:
            x = float(stop_loss_pct)
        except (TypeError, ValueError):
            x = settings.paper_stop_loss_pct
        lo = float(settings.llm_exit_sl_pct_min)
        hi = float(settings.llm_exit_sl_pct_max)
        out_sl = max(lo, min(hi, x))
    if take_profit_pct is not None:
        try:
            x = float(take_profit_pct)
        except (TypeError, ValueError):
            x = settings.paper_take_profit_pct
        lo = float(settings.llm_exit_tp_pct_min)
        hi = float(settings.llm_exit_tp_pct_max)
        out_tp = max(lo, min(hi, x))
    if max_hold_minutes is not None:
        try:
            m = int(max_hold_minutes)
        except (TypeError, ValueError):
            m = int(settings.paper_max_hold_minutes)
        lo = int(settings.llm_exit_hold_min)
        hi = int(settings.llm_exit_hold_max)
        out_mh = max(lo, min(hi, m))
    return out_sl, out_tp, out_mh


def _disabled(reason: str) -> LlmDecision:
    """Helper for short-circuit paths that should pass-through (no veto)."""
    return LlmDecision(verdict="YES", allowed=True, reason=reason, source="disabled")


async def llm_filter_setup(
    *,
    market_context: dict[str, Any],
    proposed_signal: str,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> LlmDecision:
    """Ask the LLM whether to allow a proposed trade.

    Returns an ``LlmDecision`` with ``allowed=True`` for YES; False otherwise.
    Designed to be a strict veto, not a generator. If no API key is configured
    or the filter is disabled, the trade is allowed unconditionally.
    """
    settings = settings or get_settings()

    if not settings.llm_filter_enabled:
        return _disabled("LLM_FILTER_ENABLED=false")

    key = settings.openai_api_key
    if not key:
        return LlmDecision(
            verdict="YES", allowed=True, reason="OPENAI_API_KEY not set", source="no_key"
        )

    safe_ctx = sanitize_context(market_context)

    system = (
        "You are a risk filter for an existing rule-based options/equity trade setup. "
        "The rule-based brain has already decided to take this trade. Your job is "
        "to look at the live market context and answer ONLY whether to ALLOW it. "
        "You do NOT generate new trades. Reply with EXACTLY ONE JSON object: "
        '{"verdict":"YES"|"NO"|"AVOID","reason":"<=120 chars"}. '
        "Use NO when momentum/trend is clearly against the proposed direction. "
        "Use AVOID when context is stale, choppy, late in the move, or ambiguous. "
        "Use YES otherwise."
    )
    user = json.dumps({"proposed_signal": proposed_signal, "market": safe_ctx}, default=str)

    body = {
        "model": settings.openai_model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {key.get_secret_value()}",
        "Content-Type": "application/json",
    }
    url = "https://api.openai.com/v1/chat/completions"
    timeout = max(1.0, float(settings.llm_filter_timeout_s))

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout)

    try:
        try:
            r = await client.post(url, headers=headers, json=body, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            log.warning("llm_filter_http_error", error=str(e))
            return _on_error(settings, f"http_error:{type(e).__name__}")

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            log.warning("llm_filter_bad_shape", error=str(e), raw=data)
            return _on_error(settings, "bad_response_shape")

        try:
            parsed = _extract_json_object(content)
        except (ValueError, json.JSONDecodeError) as e:
            log.warning("llm_filter_non_json", error=str(e), raw=content[:200])
            return _on_error(settings, "non_json_reply")

        verdict_raw = str(parsed.get("verdict", "AVOID")).strip().upper()
        if verdict_raw not in ("YES", "NO", "AVOID"):
            verdict_raw = "AVOID"
        reason = str(parsed.get("reason", "")).strip()[:200] or "no_reason"
        verdict: LLMVerdict = verdict_raw  # type: ignore[assignment]
        return LlmDecision(
            verdict=verdict,
            allowed=(verdict == "YES"),
            reason=reason,
            source="openai",
            raw={"model": settings.openai_model},
        )
    finally:
        if own_client:
            await client.aclose()


def _on_error(settings: Settings, reason: str) -> LlmDecision:
    """Apply the configured fail-open / fail-closed policy."""
    if settings.llm_filter_fail_closed:
        return LlmDecision(
            verdict="AVOID", allowed=False,
            reason=f"llm_unavailable:{reason} (fail-closed)",
            source="fail_closed",
        )
    return LlmDecision(
        verdict="YES", allowed=True,
        reason=f"llm_unavailable:{reason} (fail-open)",
        source="error",
    )


# ---------------------------------------------------------------------------
# CLASSIFIER MODE — used by the new 5m/multi-candidate pipeline.
# Output: { decision: TAKE|SKIP, confidence: 0..1, type: breakout|pullback|
# continuation|other, reason: <120 chars> }.  Same redaction + timeout +
# fail-closed semantics as the veto, but the bot reads `confidence` and
# compares it against LLM_DECISION_THRESHOLD before letting the trade through.
# ---------------------------------------------------------------------------

ClassifierDecision = Literal["TAKE", "SKIP"]
ClassifierType = Literal["breakout", "pullback", "continuation", "other"]


@dataclass
class LlmClassification:
    decision: ClassifierDecision
    confidence: float                    # 0.0 .. 1.0
    pattern_type: ClassifierType
    reason: str
    source: str                          # "openai" / "disabled" / "no_key" / "error" / "fail_closed"
    raw: dict[str, Any] = field(default_factory=dict)
    # Optional exit overrides (fractions of premium; minutes). Clamped in filter.py.
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    max_hold_minutes: int | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == "TAKE"

    def passes(self, threshold: float) -> bool:
        return self.allowed and self.confidence >= float(threshold)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "decision": self.decision,
            "confidence": round(self.confidence, 3),
            "type": self.pattern_type,
            "reason": self.reason,
            "source": self.source,
        }
        if self.stop_loss_pct is not None:
            d["stop_loss_pct"] = round(self.stop_loss_pct, 4)
        if self.take_profit_pct is not None:
            d["take_profit_pct"] = round(self.take_profit_pct, 4)
        if self.max_hold_minutes is not None:
            d["max_hold_minutes"] = int(self.max_hold_minutes)
        return d


def _classifier_disabled(reason: str) -> LlmClassification:
    """Bypass: confidence=1.0 so the threshold gate doesn't accidentally block."""
    return LlmClassification(
        decision="TAKE", confidence=1.0, pattern_type="other",
        reason=reason, source="disabled",
    )


def _classifier_on_error(settings: Settings, reason: str) -> LlmClassification:
    if settings.llm_filter_fail_closed:
        return LlmClassification(
            decision="SKIP", confidence=0.0, pattern_type="other",
            reason=f"llm_unavailable:{reason} (fail-closed)", source="fail_closed",
        )
    # Fail-open: pretend the LLM said TAKE with low confidence so the
    # threshold gate still has a chance to filter it.
    return LlmClassification(
        decision="TAKE", confidence=0.5, pattern_type="other",
        reason=f"llm_unavailable:{reason} (fail-open)", source="error",
    )


async def llm_classify_setup(
    *,
    market_context: dict[str, Any],
    proposed_signal: str,
    proposed_pattern: str,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> LlmClassification:
    """Classify a candidate trade as TAKE/SKIP with a confidence score.

    The rule-based brain still produces the candidate. The LLM is asked to
    grade its quality given the live structure and assign a probability that
    the setup will play out. The runtime then compares the confidence to
    LLM_DECISION_THRESHOLD before placing the order.
    """
    settings = settings or get_settings()

    if not settings.llm_filter_enabled:
        return _classifier_disabled("LLM_FILTER_ENABLED=false")

    key = settings.openai_api_key
    if not key:
        return LlmClassification(
            decision="TAKE", confidence=1.0, pattern_type="other",
            reason="OPENAI_API_KEY not set", source="no_key",
        )

    safe_ctx = sanitize_context(market_context)

    exit_instr = ""
    if settings.llm_exit_params_enabled:
        exit_instr = (
            " When decision is TAKE, include numeric fields stop_loss_pct, "
            "take_profit_pct, max_hold_minutes. They apply to LONG option premium "
            "(stop below entry, target above). Index options need room for normal "
            "premium noise — prefer stop_loss_pct roughly 0.08-0.15, take_profit_pct "
            "0.15-0.30, max_hold_minutes 15-30 for intraday; TP often ~2x SL after "
            "broker round-trip. Widen TP on clean trends, tighter in chop."
        )

    system = (
        "You are a probabilistic trade-quality classifier for an existing "
        "rule-based intraday options bot focused on 5-minute setups. The "
        "rule-based brain has already proposed a trade. Grade the proposed "
        "setup using only the structured market context provided. Reply "
        "with EXACTLY one JSON object: "
        '{"decision":"TAKE"|"SKIP","confidence":0.0..1.0,'
        '"type":"breakout"|"pullback"|"continuation"|"other",'
        '"reason":"<=120 chars"'
        + (
            ',"stop_loss_pct":<number>,"take_profit_pct":<number>,"max_hold_minutes":<integer>'
            if settings.llm_exit_params_enabled
            else ""
        )
        + "}. "
        "Use TAKE only when the structure is clean and confluent. "
        "Use SKIP when the move is exhausted, against the higher-timeframe bias, "
        "or the setup is ambiguous. confidence reflects your probability that "
        "the trade plays out within the next 1-3 5m bars."
        + exit_instr
    )
    user = json.dumps(
        {
            "proposed_signal": proposed_signal,
            "proposed_pattern": proposed_pattern,
            "market": safe_ctx,
        },
        default=str,
    )

    body = {
        "model": settings.openai_model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {key.get_secret_value()}",
        "Content-Type": "application/json",
    }
    url = "https://api.openai.com/v1/chat/completions"
    timeout = max(1.0, float(settings.llm_filter_timeout_s))

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout)

    try:
        try:
            r = await client.post(url, headers=headers, json=body, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            log.warning("llm_classifier_http_error", error=str(e))
            return _classifier_on_error(settings, f"http_error:{type(e).__name__}")

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            log.warning("llm_classifier_bad_shape", error=str(e), raw=data)
            return _classifier_on_error(settings, "bad_response_shape")

        try:
            parsed = _extract_json_object(content)
        except (ValueError, json.JSONDecodeError) as e:
            log.warning("llm_classifier_non_json", error=str(e), raw=content[:200])
            return _classifier_on_error(settings, "non_json_reply")

        decision_raw = str(parsed.get("decision", "SKIP")).strip().upper()
        if decision_raw not in ("TAKE", "SKIP"):
            decision_raw = "SKIP"
        type_raw = str(parsed.get("type", "other")).strip().lower()
        if type_raw not in ("breakout", "pullback", "continuation", "other"):
            type_raw = "other"
        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        reason = str(parsed.get("reason", "")).strip()[:200] or "no_reason"

        raw_sl = raw_tp = raw_mh = None
        if decision_raw == "TAKE" and settings.llm_exit_params_enabled:
            raw_sl = parsed.get("stop_loss_pct")
            raw_tp = parsed.get("take_profit_pct")
            raw_mh = parsed.get("max_hold_minutes")
        sl_c, tp_c, mh_c = clamp_llm_exit_params(
            settings,
            stop_loss_pct=raw_sl,
            take_profit_pct=raw_tp,
            max_hold_minutes=raw_mh,
        )

        return LlmClassification(
            decision=decision_raw,        # type: ignore[arg-type]
            confidence=confidence,
            pattern_type=type_raw,        # type: ignore[arg-type]
            reason=reason,
            source="openai",
            raw={"model": settings.openai_model},
            stop_loss_pct=sl_c,
            take_profit_pct=tp_c,
            max_hold_minutes=mh_c,
        )
    finally:
        if own_client:
            await client.aclose()
