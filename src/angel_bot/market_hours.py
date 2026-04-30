"""Market hours per instrument category (NSE Equity / NSE Index Options / MCX).

We deliberately keep this self-contained — no external holiday API, just
weekday + per-segment session windows in IST. The bot uses this to gate
trade placement so it never tries to order outside session, and the UI
uses it to dim the corresponding category card.

Segment windows (IST):
  NSE Equity / Options : 09:15 → 15:30, Mon-Fri
  MCX (non-agri)       : 09:00 → 23:30, Mon-Fri (full session, US-DST window)
  MCX agri             : 09:00 → 17:00, Mon-Fri (we don't currently scan these)

Holidays (Republic Day, Diwali, etc.) are not modeled — the bot will see
zero LTP returns from Angel on those days and naturally idle.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

# Asia/Kolkata is UTC+5:30 with no DST — a fixed offset is exact and avoids a
# zoneinfo import / tzdata dependency surprise on minimal containers.
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class Session:
    label: str
    open_h: int
    open_m: int
    close_h: int
    close_m: int

    def open_time(self) -> time:
        return time(self.open_h, self.open_m)

    def close_time(self) -> time:
        return time(self.close_h, self.close_m)


SESSIONS: dict[str, Session] = {
    # Stock cash market
    "EQUITY":    Session("NSE Equity", 9, 15, 15, 30),
    # Index spot (we trade options on these — gated separately as OPTION below)
    "INDEX":     Session("NSE Index", 9, 15, 15, 30),
    # NIFTY/BANKNIFTY ATM CE/PE — same NSE F&O hours
    "OPTION":    Session("NSE F&O", 9, 15, 15, 30),
    # MCX non-agri energy/metals (CRUDE/GOLD/SILVER/NATURALGAS)
    "COMMODITY": Session("MCX Energy/Metals", 9, 0, 23, 30),
}


@dataclass
class MarketStatus:
    kind: str
    label: str            # e.g. "NSE Equity"
    is_open: bool
    is_weekend: bool
    opens_at_iso: str | None     # next open in IST as "YYYY-MM-DDTHH:MM:SS+05:30"
    closes_at_iso: str | None    # current/next close in IST
    opens_at_label: str | None   # e.g. "Mon 09:15 IST" or "09:15 IST"
    closes_at_label: str | None  # e.g. "15:30 IST"
    reason: str           # short human reason: "open", "weekend", "before_open", "after_close"

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _now_ist(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(IST)
    if now.tzinfo is None:
        # treat naive as UTC then convert
        return now.replace(tzinfo=timezone.utc).astimezone(IST)
    return now.astimezone(IST)


def _next_weekday(d: datetime) -> datetime:
    """Move forward until a weekday (Mon-Fri)."""
    while d.weekday() >= 5:
        d = d + timedelta(days=1)
    return d


def _label_time(dt: datetime, *, with_day: bool) -> str:
    if with_day:
        return dt.strftime("%a %H:%M IST")
    return dt.strftime("%H:%M IST")


def kind_market_status(kind: str, now: datetime | None = None) -> MarketStatus:
    """Resolve open/closed state for one instrument kind."""
    k = (kind or "").upper()
    sess = SESSIONS.get(k)
    if sess is None:
        # Unknown kind — treat as open so we don't silently block anything new.
        return MarketStatus(
            kind=k, label=k or "Unknown", is_open=True, is_weekend=False,
            opens_at_iso=None, closes_at_iso=None,
            opens_at_label=None, closes_at_label=None, reason="unknown_kind",
        )

    cur = _now_ist(now)
    today = cur.date()
    open_dt = datetime.combine(today, sess.open_time(), tzinfo=IST)
    close_dt = datetime.combine(today, sess.close_time(), tzinfo=IST)
    weekday = cur.weekday()  # Mon=0 ... Sun=6
    is_weekend = weekday >= 5

    if is_weekend:
        nxt = _next_weekday(cur + timedelta(days=1))
        nxt_open = datetime.combine(nxt.date(), sess.open_time(), tzinfo=IST)
        nxt_close = datetime.combine(nxt.date(), sess.close_time(), tzinfo=IST)
        return MarketStatus(
            kind=k, label=sess.label, is_open=False, is_weekend=True,
            opens_at_iso=nxt_open.isoformat(),
            closes_at_iso=nxt_close.isoformat(),
            opens_at_label=_label_time(nxt_open, with_day=True),
            closes_at_label=_label_time(nxt_close, with_day=False),
            reason="weekend",
        )

    if cur < open_dt:
        return MarketStatus(
            kind=k, label=sess.label, is_open=False, is_weekend=False,
            opens_at_iso=open_dt.isoformat(),
            closes_at_iso=close_dt.isoformat(),
            opens_at_label=_label_time(open_dt, with_day=False),
            closes_at_label=_label_time(close_dt, with_day=False),
            reason="before_open",
        )

    if cur > close_dt:
        nxt = _next_weekday(cur + timedelta(days=1))
        nxt_open = datetime.combine(nxt.date(), sess.open_time(), tzinfo=IST)
        nxt_close = datetime.combine(nxt.date(), sess.close_time(), tzinfo=IST)
        with_day = nxt.date() != today
        return MarketStatus(
            kind=k, label=sess.label, is_open=False, is_weekend=False,
            opens_at_iso=nxt_open.isoformat(),
            closes_at_iso=nxt_close.isoformat(),
            opens_at_label=_label_time(nxt_open, with_day=with_day),
            closes_at_label=_label_time(nxt_close, with_day=with_day),
            reason="after_close",
        )

    return MarketStatus(
        kind=k, label=sess.label, is_open=True, is_weekend=False,
        opens_at_iso=open_dt.isoformat(),
        closes_at_iso=close_dt.isoformat(),
        opens_at_label=_label_time(open_dt, with_day=False),
        closes_at_label=_label_time(close_dt, with_day=False),
        reason="open",
    )


def all_market_status(now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Return status for every kind the bot understands."""
    return {k: kind_market_status(k, now=now).to_dict() for k in SESSIONS}
