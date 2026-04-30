"""Parse Smart Stream v2 binary ticks (LTP / quote base fields)."""

from __future__ import annotations

import struct
from typing import Any


def _parse_token_field(blob: bytes) -> str:
    out = bytearray()
    for b in blob:
        if b == 0:
            break
        out.append(b)
    return out.decode("ascii", errors="replace")


def parse_ws_tick_binary(binary_data: bytes) -> dict[str, Any]:
    """
    Decode minimum LTP frame (subscription_mode=1) and shared prefix for higher modes.
    Prices are typically in paise (divide by 100) for NSE cash/FO — adjust if your segment differs.
    """
    if len(binary_data) < 51:
        raise ValueError(f"packet too short: {len(binary_data)} bytes")
    sub_mode = struct.unpack_from("<B", binary_data, 0)[0]
    ex_type = struct.unpack_from("<B", binary_data, 1)[0]
    token = _parse_token_field(binary_data[2:27])
    sequence_number = struct.unpack_from("<q", binary_data, 27)[0]
    exchange_timestamp = struct.unpack_from("<q", binary_data, 35)[0]
    last_traded_price_raw = struct.unpack_from("<q", binary_data, 43)[0]
    last_traded_price = last_traded_price_raw / 100.0
    return {
        "subscription_mode": sub_mode,
        "exchange_type": ex_type,
        "token": token,
        "sequence_number": sequence_number,
        "exchange_timestamp": exchange_timestamp,
        "last_traded_price_raw": last_traded_price_raw,
        "last_traded_price": last_traded_price,
    }


def parse_ws_subscriptions(spec: str) -> list[dict[str, Any]]:
    """
    Parse `WS_SUBSCRIPTIONS` env:
      `exchangeType:token,token|exchangeType:token`
    Example: `1:99926000|2:12345,67890`
    """
    spec = spec.strip()
    if not spec:
        return []
    groups: list[dict[str, Any]] = []
    for group in spec.split("|"):
        g = group.strip()
        if not g:
            continue
        left, _, right = g.partition(":")
        et = int(left.strip())
        tokens = [t.strip() for t in right.split(",") if t.strip()]
        if not tokens:
            continue
        groups.append({"exchangeType": et, "tokens": tokens})
    return groups
