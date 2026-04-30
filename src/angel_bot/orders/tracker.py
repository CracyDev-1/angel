from __future__ import annotations

import re
from typing import Any

import structlog

from angel_bot.smart_client import SmartApiClient
from angel_bot.state.store import StateStore

log = structlog.get_logger(__name__)


def _to_int(x: Any) -> int:
    try:
        return int(float(str(x)))
    except (TypeError, ValueError):
        return 0


def _to_float(x: Any) -> float | None:
    try:
        v = float(str(x))
        return v
    except (TypeError, ValueError):
        return None


def normalize_order_lifecycle(row: dict[str, Any]) -> tuple[str, str, int, int, float | None]:
    """
    Map Angel order book row → (lifecycle, broker_status, filled, pending, avg_price).
    """
    broker_status = str(
        row.get("orderstatus") or row.get("orderStatus") or row.get("status") or ""
    ).strip()
    low = broker_status.lower()

    qty = _to_int(row.get("quantity") or row.get("qty"))
    filled = _to_int(row.get("filledshares") or row.get("filledquantity"))
    unfilled = _to_int(row.get("unfilledshares") or row.get("unfilledquantity"))
    pending = unfilled if unfilled > 0 else max(0, qty - filled)

    avg_price = _to_float(row.get("averageprice") or row.get("averagePrice") or row.get("fillprice"))

    if "reject" in low:
        return ("rejected", broker_status, filled, pending, avg_price)
    if "cancel" in low:
        return ("cancelled", broker_status, filled, pending, avg_price)
    if "complete" in low or "fully" in low or low == "closed":
        return ("executed", broker_status, filled, pending, avg_price)
    if "open" in low or "trigger" in low or "pending" in low or "validation" in low:
        if filled > 0 and pending > 0:
            return ("partial", broker_status, filled, pending, avg_price)
        return ("placed", broker_status, filled, pending, avg_price)

    return ("placed", broker_status, filled, pending, avg_price)


def _order_id(row: dict[str, Any]) -> str | None:
    oid = row.get("orderid") or row.get("orderId") or row.get("uniqueorderid") or row.get("uniqueOrderId")
    if oid is None:
        return None
    s = str(oid).strip()
    return s or None


class OrderTracker:
    """Reconcile broker order book into local lifecycle state."""

    def __init__(self, store: StateStore):
        self.store = store

    async def reconcile_once(self, api: SmartApiClient) -> int:
        resp = await api.order_book()
        if not resp.get("status"):
            log.warning("order_book_failed", body=resp)
            return 0
        data = resp.get("data")
        if not isinstance(data, list):
            return 0
        n = 0
        for row in data:
            if not isinstance(row, dict):
                continue
            oid = _order_id(row)
            if not oid:
                continue
            life, bstatus, filled, pending, avg = normalize_order_lifecycle(row)
            self.store.upsert_broker_order(
                broker_order_id=oid,
                lifecycle_status=life,
                broker_status=bstatus,
                filled_qty=filled,
                pending_qty=pending,
                avg_price=avg,
                raw_row=row,
            )
            n += 1
        log.info("orders_reconciled", count=n)
        return n


def extract_place_order_id(response: dict[str, Any]) -> str | None:
    """Best-effort parse of placeOrder response."""
    if not response.get("status"):
        return None
    data = response.get("data")
    if isinstance(data, str) and data.strip():
        if re.fullmatch(r"[A-Za-z0-9-]+", data.strip()):
            return data.strip()
    if isinstance(data, dict):
        for k in ("orderid", "orderId", "uniqueorderid", "uniqueOrderId"):
            v = data.get(k)
            if v:
                return str(v).strip()
    return None
