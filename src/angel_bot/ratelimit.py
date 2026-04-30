"""Process-wide rate limiter for Angel One SmartAPI calls.

Angel publishes hard per-endpoint quotas (per-second / per-minute / per-hour) on
https://smartapi.angelone.in/docs/RateLimit. Hitting those returns
HTTP 403 "Access denied because of exceeding rate limit" and (worse) often
trips a longer cool-down on Angel's side. This module is a *client-side*
guardrail that ensures we never knowingly send a request that would breach a
window — we wait first.

Design
------
* Sliding-window counters per endpoint, per window (sec / min / hour).
* A separate "combined" group `orders` enforces the 9/sec aggregate cap that
  Angel imposes across placeOrder + modifyOrder + cancelOrder.
* Reservation pattern: when `acquire()` finds a window full, it computes the
  earliest free moment, *reserves* a slot at that moment in every relevant
  bucket while still under a single asyncio.Lock, then sleeps. Concurrent
  callers see the reservation immediately and queue behind it without
  thundering on the broker.
* `safety_factor` (0..1, default 0.9) lets us run a touch under the
  documented cap so we leave headroom for clock skew, in-flight retries,
  the watchdog, etc. We never round below 1.
* `note_rate_limited(path, retry_after_s)` is called when the broker still
  responds with a 403 / "rate limit" message — it stuffs synthetic
  reservations to back the offending bucket off for `retry_after_s` seconds.

This is intentionally dependency-free (only stdlib + asyncio).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Endpoint catalogue (matches the table on https://smartapi.angelone.in/docs/RateLimit)
# Tuple per window: (limit, window_seconds). Use None for "Not Applicable" in docs.
# ---------------------------------------------------------------------------

ENDPOINT_LIMITS: dict[str, list[tuple[int, float]]] = {
    # Auth
    "/rest/auth/angelbroking/user/v1/loginByPassword": [(1, 1.0)],
    "/rest/auth/angelbroking/jwt/v1/generateTokens": [(1, 1.0), (1000, 3600.0)],
    # User
    "/rest/secure/angelbroking/user/v1/getProfile": [(3, 1.0), (1000, 3600.0)],
    "/rest/secure/angelbroking/user/v1/logout": [(1, 1.0)],
    "/rest/secure/angelbroking/user/v1/getRMS": [(2, 1.0)],
    # Orders (also share the `orders` combined group below)
    "/rest/secure/angelbroking/order/v1/placeOrder": [(9, 1.0), (500, 60.0), (1000, 3600.0)],
    "/rest/secure/angelbroking/order/v1/modifyOrder": [(9, 1.0), (500, 60.0), (1000, 3600.0)],
    "/rest/secure/angelbroking/order/v1/cancelOrder": [(9, 1.0), (500, 60.0), (1000, 3600.0)],
    # Reads
    "/rest/secure/angelbroking/order/v1/getOrderBook": [(1, 1.0)],
    "/rest/secure/angelbroking/order/v1/getLtpData": [(10, 1.0), (500, 60.0), (5000, 3600.0)],
    "/rest/secure/angelbroking/order/v1/getPosition": [(1, 1.0)],
    "/rest/secure/angelbroking/order/v1/getTradeBook": [(1, 1.0)],
    "/rest/secure/angelbroking/order/v1/convertPosition": [(10, 1.0), (500, 60.0), (5000, 3600.0)],
    "/rest/secure/angelbroking/order/v1/searchScrip": [(1, 1.0)],
    # Portfolio
    "/rest/secure/angelbroking/portfolio/v1/getHolding": [(1, 1.0)],
    "/rest/secure/angelbroking/portfolio/v1/getAllHolding": [(1, 1.0)],
    # Quotes / margin / GTT
    "/rest/secure/angelbroking/market/v1/quote": [(10, 1.0), (500, 60.0), (5000, 3600.0)],
    "/rest/secure/angelbroking/market/v1/quote/": [(10, 1.0), (500, 60.0), (5000, 3600.0)],
    "/rest/secure/angelbroking/margin/v1/batch": [(10, 1.0), (500, 60.0), (5000, 3600.0)],
    "/rest/secure/angelbroking/gtt/v1/createRule": [(9, 1.0), (500, 60.0), (5000, 3600.0)],
    "/rest/secure/angelbroking/gtt/v1/modifyRule": [(9, 1.0), (500, 60.0), (5000, 3600.0)],
    "/rest/secure/angelbroking/gtt/v1/cancelRule": [(9, 1.0), (500, 60.0), (5000, 3600.0)],
    "/rest/secure/angelbroking/gtt/v1/ruleDetails": [(10, 1.0), (500, 60.0), (5000, 3600.0)],
    "/rest/secure/angelbroking/gtt/v1/ruleList": [(10, 1.0), (500, 60.0), (5000, 3600.0)],
    # Historical / option greeks
    "/rest/secure/angelbroking/historical/v1/getCandleData": [(3, 1.0), (180, 60.0), (5000, 3600.0)],
    "/rest/secure/angelbroking/marketData/v1/optionGreek": [(1, 1.0)],
}

# Endpoints that share a combined per-second cap (docs: "combined request
# count must not exceed 9 requests per second" across order APIs).
ORDER_GROUP_LIMITS: list[tuple[int, float]] = [(9, 1.0)]

ORDER_PATHS: frozenset[str] = frozenset(
    {
        "/rest/secure/angelbroking/order/v1/placeOrder",
        "/rest/secure/angelbroking/order/v1/modifyOrder",
        "/rest/secure/angelbroking/order/v1/cancelOrder",
    }
)


def _scaled(limit: int, safety_factor: float) -> int:
    """Apply safety headroom, never below 1."""
    if safety_factor >= 1.0 or limit <= 1:
        return limit
    eff = int(limit * max(0.0, min(1.0, safety_factor)))
    return max(1, eff)


@dataclass
class _Bucket:
    limit: int
    window: float
    name: str
    times: deque[float] = field(default_factory=deque)

    def purge(self, now: float) -> None:
        cutoff = now - self.window
        while self.times and self.times[0] <= cutoff:
            self.times.popleft()

    def earliest_free(self, now: float) -> float:
        """Return the timestamp at which this bucket has room (>=1 free slot)."""
        self.purge(now)
        if len(self.times) < self.limit:
            return now
        # When the oldest reservation falls out of the window, a slot opens.
        return self.times[0] + self.window

    def reserve(self, at: float) -> None:
        self.times.append(at)


class RateLimiter:
    """Process-wide async rate limiter. Safe to share across tasks."""

    def __init__(
        self,
        *,
        endpoint_limits: dict[str, list[tuple[int, float]]] | None = None,
        order_group_limits: list[tuple[int, float]] | None = None,
        safety_factor: float = 0.9,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.safety_factor = safety_factor
        self._lock = asyncio.Lock()

        src = endpoint_limits if endpoint_limits is not None else ENDPOINT_LIMITS
        self._endpoint_buckets: dict[str, list[_Bucket]] = {}
        for path, limits in src.items():
            self._endpoint_buckets[path] = [
                _Bucket(limit=_scaled(lim, safety_factor), window=win, name=f"{path}:{int(win)}s")
                for (lim, win) in limits
            ]

        og = order_group_limits if order_group_limits is not None else ORDER_GROUP_LIMITS
        self._group_buckets: dict[str, list[_Bucket]] = {
            "orders": [
                _Bucket(limit=_scaled(lim, safety_factor), window=win, name=f"group:orders:{int(win)}s")
                for (lim, win) in og
            ]
        }

        self._last_wait_s: float = 0.0
        self._total_waits: int = 0
        self._total_calls: int = 0

    # ------------------------------------------------------------------
    # public stats (used by the dashboard /api/snapshot)
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, object]:
        now = time.monotonic()
        per_endpoint: dict[str, list[dict[str, object]]] = {}
        for path, buckets in self._endpoint_buckets.items():
            rows: list[dict[str, object]] = []
            for b in buckets:
                b.purge(now)
                rows.append(
                    {"window_s": b.window, "limit": b.limit, "in_window": len(b.times)}
                )
            per_endpoint[path] = rows
        groups: dict[str, list[dict[str, object]]] = {}
        for grp, buckets in self._group_buckets.items():
            rows = []
            for b in buckets:
                b.purge(now)
                rows.append(
                    {"window_s": b.window, "limit": b.limit, "in_window": len(b.times)}
                )
            groups[grp] = rows
        return {
            "enabled": self.enabled,
            "safety_factor": self.safety_factor,
            "calls_total": self._total_calls,
            "waits_total": self._total_waits,
            "last_wait_s": round(self._last_wait_s, 4),
            "endpoints": per_endpoint,
            "groups": groups,
        }

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------
    def register(self, path: str, limits: Iterable[tuple[int, float]]) -> None:
        """Add a custom endpoint at runtime (e.g., for tests or new APIs)."""
        self._endpoint_buckets[path] = [
            _Bucket(limit=_scaled(lim, self.safety_factor), window=win, name=f"{path}:{int(win)}s")
            for (lim, win) in limits
        ]

    # ------------------------------------------------------------------
    # acquire / release
    # ------------------------------------------------------------------
    async def acquire(self, path: str, *, group: str | None = None) -> float:
        """Block until it is safe to send a request to `path` without breaching limits.

        Returns the actual seconds it slept (0 if no wait was required).
        Unknown paths are not throttled (logged once per path).
        """
        if not self.enabled:
            self._total_calls += 1
            return 0.0

        # Auto-attach the orders group when calling order endpoints.
        if group is None and path in ORDER_PATHS:
            group = "orders"

        wait_s = 0.0
        async with self._lock:
            now = time.monotonic()
            wait_until = now

            ep_buckets = self._endpoint_buckets.get(path)
            grp_buckets = self._group_buckets.get(group) if group else None

            if ep_buckets is None and grp_buckets is None:
                self._total_calls += 1
                self._maybe_warn_unknown(path)
                return 0.0

            for b in ep_buckets or ():
                t = b.earliest_free(now)
                if t > wait_until:
                    wait_until = t
            for b in grp_buckets or ():
                t = b.earliest_free(now)
                if t > wait_until:
                    wait_until = t

            for b in ep_buckets or ():
                b.reserve(wait_until)
            for b in grp_buckets or ():
                b.reserve(wait_until)

            wait_s = max(0.0, wait_until - now)
            self._last_wait_s = wait_s
            self._total_calls += 1
            if wait_s > 0:
                self._total_waits += 1

        if wait_s > 0:
            log.info("rate_limit_wait", path=path, group=group, sleep_s=round(wait_s, 3))
            await asyncio.sleep(wait_s)
        return wait_s

    def note_rate_limited(self, path: str, *, retry_after_s: float = 1.0) -> None:
        """Force a backoff after the broker explicitly rejected us.

        Stuffs the *narrowest* (per-second) bucket of the offending endpoint
        and the orders group with reservations spaced `retry_after_s` apart so
        further calls naturally yield. This is a best-effort cool-down.
        """
        if not self.enabled:
            return
        retry_after_s = max(0.05, float(retry_after_s))
        now = time.monotonic()
        target_until = now + retry_after_s

        def _stuff(buckets: list[_Bucket]) -> None:
            if not buckets:
                return
            # Choose the bucket with the smallest window (per-sec preferably).
            b = min(buckets, key=lambda x: x.window)
            b.purge(now)
            # Push enough synthetic reservations to fill the bucket and keep it
            # full until target_until. We add (limit - len) entries at target_until.
            need = max(0, b.limit - len(b.times))
            for _ in range(need):
                b.times.append(target_until)

        _stuff(self._endpoint_buckets.get(path, []))
        if path in ORDER_PATHS:
            _stuff(self._group_buckets.get("orders", []))
        log.warning("rate_limit_backoff", path=path, sleep_s=round(retry_after_s, 3))

    # ------------------------------------------------------------------
    _warned_unknown: set[str] = set()

    def _maybe_warn_unknown(self, path: str) -> None:
        if path in self._warned_unknown:
            return
        self._warned_unknown.add(path)
        log.info("rate_limit_unknown_endpoint", path=path)


# ---------------------------------------------------------------------------
# Process singleton
# ---------------------------------------------------------------------------
_global_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _global_limiter
    if _global_limiter is None:
        # Late import to avoid circular dep with config/Settings during tests.
        try:
            from angel_bot.config import get_settings

            s = get_settings()
            _global_limiter = RateLimiter(
                enabled=bool(getattr(s, "rate_limit_enabled", True)),
                safety_factor=float(getattr(s, "rate_limit_safety_factor", 0.9)),
            )
        except Exception:  # noqa: BLE001 — fall back to defaults
            _global_limiter = RateLimiter()
    return _global_limiter


def reset_rate_limiter() -> None:
    """Tests / dashboard use this to drop accumulated state."""
    global _global_limiter
    _global_limiter = None


# ---------------------------------------------------------------------------
# Helpers for callers
# ---------------------------------------------------------------------------

def looks_rate_limited(*, status_code: int | None, body: object) -> bool:
    """Heuristic — Angel sometimes returns HTTP 403 with a JSON body explaining
    the breach, sometimes returns 200 with status:false + a message. Either way
    the message contains 'rate limit' / 'exceeding'. Treat as backoff-worthy.
    """
    if status_code == 429:
        return True
    msg = ""
    err = ""
    if isinstance(body, dict):
        msg = str(body.get("message") or body.get("errormessage") or "").lower()
        err = str(body.get("errorcode") or "").lower()
    if status_code == 403 and ("rate" in msg or "exceed" in msg or "denied" in msg):
        return True
    if "rate limit" in msg or "exceeding rate" in msg or "too many requests" in msg:
        return True
    # Angel's documented rate-limit error code in some payloads.
    if err in {"ab1004", "ag8002", "ag8003"}:
        return True
    return False
