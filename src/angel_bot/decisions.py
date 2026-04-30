from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class Decision:
    ts: str
    name: str
    exchange: str
    token: str
    signal: str  # BUY_CALL, BUY_PUT, NO_TRADE
    reason: str
    last_price: float | None
    quantity: int
    lots: int
    capital_used: float
    side: str  # CE / PE / "-"
    placed: bool
    dry_run: bool
    broker_order_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DecisionLog:
    def __init__(self, capacity: int = 500):
        self._buf: deque[Decision] = deque(maxlen=capacity)

    def add(self, d: Decision) -> None:
        self._buf.append(d)

    def recent(self, limit: int = 100) -> list[Decision]:
        items = list(self._buf)[-limit:]
        items.reverse()
        return items

    @staticmethod
    def now_iso() -> str:
        return datetime.now(UTC).isoformat()
