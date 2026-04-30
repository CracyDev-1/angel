from __future__ import annotations

from typing import Any


def _f(x: Any) -> float | None:
    if x is None:
        return None
    s = str(x).strip()
    if not s or s.lower() in ("none", "null", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _i(x: Any) -> int:
    v = _f(x)
    return int(v) if v is not None else 0


def normalize_rms(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Best-effort normalization of getRMS response into a small UI-friendly shape.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {
            "available_cash": 0.0,
            "net": 0.0,
            "utilised_margin": 0.0,
            "available_margin": 0.0,
            "raw": data,
        }
    available = (
        _f(data.get("availablecash"))
        or _f(data.get("availableCash"))
        or _f(data.get("net"))
        or 0.0
    )
    util = (
        _f(data.get("utiliseddebits"))
        or _f(data.get("utilisedmargin"))
        or _f(data.get("utilisedMargin"))
        or 0.0
    )
    return {
        "available_cash": available,
        "net": _f(data.get("net")) or 0.0,
        "utilised_margin": util,
        "available_margin": _f(data.get("availableintradaypayin")) or available,
        "raw": data,
    }


def _classify_option(symbol: str) -> str:
    s = (symbol or "").upper().rstrip()
    if s.endswith("CE"):
        return "CE"
    if s.endswith("PE"):
        return "PE"
    return "-"


def normalize_positions(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize getPosition response. Splits CE / PE buckets and computes capital used.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    rows: list[dict[str, Any]] = data if isinstance(data, list) else []
    out_rows: list[dict[str, Any]] = []
    capital_ce = 0.0
    capital_pe = 0.0
    pnl_total = 0.0
    open_positions = 0

    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("tradingsymbol") or r.get("symbolname") or r.get("symbol") or "").strip()
        side = _classify_option(sym)
        net_qty = _i(r.get("netqty") or r.get("netQty") or r.get("buyqty", 0))
        buy_qty = _i(r.get("buyqty"))
        sell_qty = _i(r.get("sellqty"))
        buy_avg = _f(r.get("totalbuyavgprice") or r.get("buyavgprice"))
        sell_avg = _f(r.get("totalsellavgprice") or r.get("sellavgprice"))
        ltp = _f(r.get("ltp") or r.get("lastprice"))
        pnl = _f(r.get("pnl") or r.get("netvalue"))
        if pnl is None and ltp is not None and buy_avg is not None:
            pnl = (ltp - buy_avg) * (net_qty or buy_qty)
        capital_used = 0.0
        if buy_qty and buy_avg:
            capital_used = buy_qty * buy_avg
        if side == "CE":
            capital_ce += capital_used
        elif side == "PE":
            capital_pe += capital_used
        if (net_qty or 0) != 0:
            open_positions += 1
        if pnl is not None:
            pnl_total += pnl

        out_rows.append(
            {
                "tradingsymbol": sym,
                "exchange": str(r.get("exchange") or "").upper(),
                "symboltoken": str(r.get("symboltoken") or r.get("symbolToken") or ""),
                "side": side,
                "net_qty": net_qty,
                "buy_qty": buy_qty,
                "sell_qty": sell_qty,
                "buy_avg": buy_avg,
                "sell_avg": sell_avg,
                "ltp": ltp,
                "capital_used": capital_used,
                "pnl": pnl,
                "producttype": r.get("producttype") or r.get("productType"),
            }
        )

    return {
        "rows": out_rows,
        "open_positions": open_positions,
        "capital_used_ce": capital_ce,
        "capital_used_pe": capital_pe,
        "capital_used_total": capital_ce + capital_pe,
        "pnl_total": pnl_total,
    }


def summarize_orders_for_ui(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        out.append(
            {
                "id": r.get("broker_order_id") or r.get("id"),
                "lifecycle": r.get("lifecycle_status") or r.get("status"),
                "broker_status": r.get("broker_status"),
                "filled_qty": r.get("filled_qty"),
                "pending_qty": r.get("pending_qty"),
                "avg_price": r.get("avg_price"),
                "updated_at": r.get("updated_at") or r.get("created_at"),
                "raw": r,
            }
        )
    return out
