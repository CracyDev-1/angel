from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import structlog

from angel_bot.instruments.master import Instrument

log = structlog.get_logger(__name__)


class DuplicateOrderGuard:
    """Prevent accidental duplicate submits within a short window."""

    def __init__(self, ttl_s: float = 30.0):
        self.ttl_s = ttl_s
        self._seen: dict[str, float] = {}

    def _key(self, payload: dict[str, Any]) -> str:
        stable = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(stable.encode()).hexdigest()

    def check_and_remember(self, payload: dict[str, Any]) -> bool:
        now = time.monotonic()
        self._seen = {k: t for k, t in self._seen.items() if now - t < self.ttl_s}
        k = self._key(payload)
        if k in self._seen:
            return False
        self._seen[k] = now
        return True


def build_order_payload(
    inst: Instrument,
    *,
    variety: str,
    transactiontype: str,
    ordertype: str,
    producttype: str,
    quantity: int,
    price: str = "0",
    duration: str = "DAY",
    squareoff: str = "0",
    stoploss: str = "0",
) -> dict[str, Any]:
    return {
        "variety": variety,
        "tradingsymbol": inst.tradingsymbol,
        "symboltoken": inst.symboltoken,
        "transactiontype": transactiontype,
        "exchange": inst.exchange,
        "ordertype": ordertype,
        "producttype": producttype,
        "duration": duration,
        "price": price,
        "squareoff": squareoff,
        "stoploss": stoploss,
        "quantity": str(int(quantity)),
    }


def validate_order_payload(p: dict[str, Any]) -> None:
    required = [
        "variety",
        "tradingsymbol",
        "symboltoken",
        "transactiontype",
        "exchange",
        "ordertype",
        "producttype",
        "duration",
        "quantity",
    ]
    missing = [k for k in required if not p.get(k)]
    if missing:
        raise ValueError(f"order missing fields: {missing}")
