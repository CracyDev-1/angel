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


def normalize_positions(
    payload: dict[str, Any],
    *,
    fresh_prices: dict[tuple[str, str], float] | None = None,
) -> dict[str, Any]:
    """Normalize ``getPosition`` response into a UI-friendly shape.

    P&L resolution order (most-trustworthy first), per row:

      1. ``realised + unrealised`` when Angel splits them — matches the
         position-level breakdown in the Angel One app.
      2. Consolidated ``pnl`` on the row when R/U are absent — Angel's
         own net for that line (may include their fee/charge treatment).
         We prefer this *before* recomputing MTM so the dashboard total
         tracks the broker feed instead of our synthetic mark.
      3. ``mark_to_market`` from the freshest LTP (scanner cache first)
         when neither split nor ``pnl`` is available.

    We *never* fall back to ``netvalue``: that field is sell_amount –
    buy_amount, which for an open BUY position equals -buy_amount and
    has nothing to do with P&L. The previous implementation did this
    fallback and that's why bot's "live PnL" diverged wildly from the
    Angel One app — for a fresh fill where Angel hadn't populated
    ``pnl`` yet we'd display a "loss" equal to the entire buy notional.

    ``fresh_prices`` (optional): scanner LTP cache keyed by
    ``(exchange, symboltoken)`` so the runtime can pass a sub-second
    fresh price for every position and we don't depend on the broker's
    refresh cadence.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    rows: list[dict[str, Any]] = data if isinstance(data, list) else []
    fresh = fresh_prices or {}
    out_rows: list[dict[str, Any]] = []
    capital_ce = 0.0
    capital_pe = 0.0
    pnl_total = 0.0
    realized_total = 0.0
    unrealized_total = 0.0
    open_positions = 0

    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("tradingsymbol") or r.get("symbolname") or r.get("symbol") or "").strip()
        ex = str(r.get("exchange") or "").upper()
        tok = str(r.get("symboltoken") or r.get("symbolToken") or "")
        side = _classify_option(sym)
        net_qty = _i(r.get("netqty") or r.get("netQty") or r.get("buyqty", 0))
        buy_qty = _i(r.get("buyqty"))
        sell_qty = _i(r.get("sellqty"))
        buy_avg = _f(r.get("totalbuyavgprice") or r.get("buyavgprice"))
        sell_avg = _f(r.get("totalsellavgprice") or r.get("sellavgprice"))
        broker_ltp = _f(r.get("ltp") or r.get("lastprice"))
        scanner_ltp = fresh.get((ex, tok)) if ex and tok else None
        # Prefer the scanner cache for mark-to-market when available; it
        # was polled from the broker's market-data endpoint at most one
        # cycle ago, whereas getPosition's ``ltp`` is whatever Angel's
        # backend last cached against the position record.
        ltp = scanner_ltp if scanner_ltp is not None and scanner_ltp > 0 else broker_ltp

        realised = _f(r.get("realised") or r.get("realized"))
        unrealised = _f(r.get("unrealised") or r.get("unrealized"))
        broker_pnl = _f(r.get("pnl"))

        # Step 1: trust explicit realised + unrealised when present.
        if realised is not None or unrealised is not None:
            pnl = (realised or 0.0) + (unrealised or 0.0)
            pnl_source = "broker_realised_unrealised"
        # Step 2: Angel's consolidated row total — match their feed before
        # we invent an MTM number (which ignores broker-side adjustments).
        elif broker_pnl is not None:
            pnl = broker_pnl
            pnl_source = "broker_pnl"
            # Attribute to realised vs unrealised so header totals stay
            # consistent with pnl_total when Angel omits the split.
            if (net_qty or 0) != 0:
                realised = 0.0
                unrealised = broker_pnl
            else:
                realised = broker_pnl
                unrealised = 0.0
        # Step 3: mark to market with whatever LTP is freshest.
        elif ltp is not None and ltp > 0 and buy_avg is not None and (net_qty or buy_qty):
            qty_for_mtm = net_qty if net_qty != 0 else buy_qty
            pnl = (ltp - buy_avg) * qty_for_mtm
            # Synthetic split: any closed leg's realised P&L is
            # (sell_avg - buy_avg) × sell_qty; the remainder is
            # unrealised on the remaining qty. Keeps the dashboard
            # numbers tied back to ground truth.
            if sell_qty and sell_avg is not None and buy_avg is not None:
                realised = (sell_avg - buy_avg) * sell_qty
                unrealised = pnl - realised
            else:
                unrealised = pnl
                realised = 0.0
            pnl_source = "mark_to_market"
        else:
            pnl = None
            pnl_source = "unknown"

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
        if realised is not None:
            realized_total += realised
        if unrealised is not None:
            unrealized_total += unrealised

        out_rows.append(
            {
                "tradingsymbol": sym,
                "exchange": ex,
                "symboltoken": tok,
                "side": side,
                "net_qty": net_qty,
                "buy_qty": buy_qty,
                "sell_qty": sell_qty,
                "buy_avg": buy_avg,
                "sell_avg": sell_avg,
                "ltp": ltp,
                "broker_ltp": broker_ltp,
                "scanner_ltp": scanner_ltp,
                "capital_used": capital_used,
                "pnl": pnl,
                "realised_pnl": realised,
                "unrealised_pnl": unrealised,
                "pnl_source": pnl_source,
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
        "realized_pnl_total": realized_total,
        "unrealized_pnl_total": unrealized_total,
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
