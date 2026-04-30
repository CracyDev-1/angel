"""Unit tests for the Angel rate limiter.

We deliberately use very small windows so the suite runs fast (<1 s).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from angel_bot.ratelimit import (
    ENDPOINT_LIMITS,
    ORDER_PATHS,
    RateLimiter,
    looks_rate_limited,
)


PLACE = "/rest/secure/angelbroking/order/v1/placeOrder"
CANCEL = "/rest/secure/angelbroking/order/v1/cancelOrder"
LTP = "/rest/secure/angelbroking/order/v1/getLtpData"


def _make_limiter(safety: float = 1.0) -> RateLimiter:
    # Use the real Angel limits so the test asserts real behaviour, but
    # keep safety_factor=1.0 so we test the documented caps verbatim.
    return RateLimiter(safety_factor=safety, enabled=True)


@pytest.mark.asyncio
async def test_per_second_cap_blocks_after_limit():
    """LTP cap is 10/sec — burst 10 instantly, the 11th must wait ~1s."""
    rl = _make_limiter()
    start = time.monotonic()
    for _ in range(10):
        await rl.acquire(LTP)
    burst_elapsed = time.monotonic() - start
    assert burst_elapsed < 0.2, f"burst should be free, took {burst_elapsed:.3f}s"

    t0 = time.monotonic()
    await rl.acquire(LTP)
    waited = time.monotonic() - t0
    # 11th call has to wait until the oldest of the 10 ages out → ~1s.
    assert 0.7 <= waited <= 1.4, f"expected ~1s wait, got {waited:.3f}s"


@pytest.mark.asyncio
async def test_combined_order_group_caps_at_9_per_sec():
    """Place + Cancel share a 9/sec budget. Confirm the 10th call across both
    waits, even though no single endpoint has hit its own per-sec cap."""
    rl = _make_limiter()
    for _ in range(5):
        await rl.acquire(PLACE)
    for _ in range(4):
        await rl.acquire(CANCEL)
    # Group is now full (5+4 = 9). The next call must wait ~1s.
    t0 = time.monotonic()
    await rl.acquire(CANCEL)
    waited = time.monotonic() - t0
    assert waited >= 0.7, f"combined cap should have forced a wait, got {waited:.3f}s"


@pytest.mark.asyncio
async def test_disabled_limiter_does_not_block():
    rl = RateLimiter(enabled=False)
    t0 = time.monotonic()
    for _ in range(50):
        await rl.acquire(LTP)
    assert time.monotonic() - t0 < 0.2


@pytest.mark.asyncio
async def test_unknown_path_is_not_throttled():
    rl = _make_limiter()
    t0 = time.monotonic()
    for _ in range(20):
        await rl.acquire("/rest/totally/made/up/path")
    assert time.monotonic() - t0 < 0.2


@pytest.mark.asyncio
async def test_concurrent_acquires_are_serialised_by_window():
    """Fan out 11 LTP calls concurrently and confirm at least one of them
    actually slept (proving the limiter survives task contention)."""
    rl = _make_limiter()

    async def one() -> float:
        return await rl.acquire(LTP)

    waits = await asyncio.gather(*[one() for _ in range(11)])
    assert max(waits) >= 0.7, f"at least one task should have waited; waits={waits}"
    assert sum(1 for w in waits if w == 0) >= 10, "first ten should have been free"


@pytest.mark.asyncio
async def test_safety_factor_shrinks_effective_limit():
    """At 0.5 safety the LTP cap of 10 becomes 5. The 6th must wait."""
    rl = RateLimiter(safety_factor=0.5, enabled=True)
    for _ in range(5):
        await rl.acquire(LTP)
    t0 = time.monotonic()
    await rl.acquire(LTP)
    waited = time.monotonic() - t0
    assert waited >= 0.7, f"safety_factor=0.5 should have triggered wait, got {waited:.3f}s"


@pytest.mark.asyncio
async def test_note_rate_limited_forces_backoff():
    """When the broker tells us we're over the limit, subsequent calls back off."""
    rl = _make_limiter()
    rl.note_rate_limited(LTP, retry_after_s=0.4)
    t0 = time.monotonic()
    await rl.acquire(LTP)
    waited = time.monotonic() - t0
    assert waited >= 0.3, f"forced backoff should sleep ~0.4s, got {waited:.3f}s"


def test_looks_rate_limited_detection():
    assert looks_rate_limited(status_code=429, body=None)
    assert looks_rate_limited(status_code=403, body={"message": "exceeding rate limit"})
    assert looks_rate_limited(status_code=200, body={"status": False, "message": "Rate limit exceeded"})
    assert looks_rate_limited(status_code=200, body={"errorcode": "AB1004"})
    assert not looks_rate_limited(status_code=200, body={"status": True, "data": {}})
    assert not looks_rate_limited(status_code=401, body={"message": "Invalid token"})


def test_every_documented_endpoint_is_registered():
    """Sanity: the catalogue covers every path our client and session module use."""
    must_have = {
        "/rest/auth/angelbroking/user/v1/loginByPassword",
        "/rest/auth/angelbroking/jwt/v1/generateTokens",
        "/rest/secure/angelbroking/user/v1/getProfile",
        "/rest/secure/angelbroking/user/v1/getRMS",
        "/rest/secure/angelbroking/order/v1/placeOrder",
        "/rest/secure/angelbroking/order/v1/cancelOrder",
        "/rest/secure/angelbroking/order/v1/getOrderBook",
        "/rest/secure/angelbroking/order/v1/getLtpData",
        "/rest/secure/angelbroking/order/v1/getPosition",
        "/rest/secure/angelbroking/order/v1/getTradeBook",
        "/rest/secure/angelbroking/portfolio/v1/getHolding",
    }
    assert must_have.issubset(set(ENDPOINT_LIMITS.keys()))
    assert ORDER_PATHS.issubset(set(ENDPOINT_LIMITS.keys()))


def test_stats_exposes_buckets():
    rl = _make_limiter()
    s = rl.stats()
    assert s["enabled"] is True
    assert "endpoints" in s
    assert "groups" in s
    assert LTP in s["endpoints"]
    rows = s["endpoints"][LTP]
    # LTP has 3 windows (sec/min/hour)
    assert len(rows) == 3
