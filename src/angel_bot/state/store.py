from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


class StateStore:
    """SQLite-backed orders, positions, daily stats."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def _table_columns(self, con: sqlite3.Connection, table: str) -> set[str]:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r["name"]) for r in rows}

    def _init_schema(self) -> None:
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  broker_order_id TEXT,
                  payload_json TEXT,
                  status TEXT,
                  created_at TEXT NOT NULL,
                  lifecycle_status TEXT,
                  broker_status TEXT,
                  filled_qty INTEGER,
                  pending_qty INTEGER,
                  avg_price REAL,
                  raw_last_json TEXT,
                  updated_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_broker_id
                  ON orders(broker_order_id) WHERE broker_order_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS positions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  symbol TEXT NOT NULL,
                  side TEXT NOT NULL,
                  qty INTEGER NOT NULL,
                  avg_price REAL,
                  opened_at TEXT NOT NULL,
                  closed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS daily_stats (
                  day TEXT PRIMARY KEY,
                  trades INTEGER NOT NULL,
                  pnl REAL NOT NULL
                );

                -- Daily P&L per mode (live vs dryrun) so the dashboard can
                -- show separate ledgers.
                CREATE TABLE IF NOT EXISTS daily_stats_mode (
                  day TEXT NOT NULL,
                  mode TEXT NOT NULL,    -- 'live' | 'dryrun'
                  trades INTEGER NOT NULL,
                  pnl REAL NOT NULL,
                  PRIMARY KEY (day, mode)
                );

                -- Paper (dry-run) positions and their lifecycle. Mark-to-market
                -- is computed in Python from incoming LTPs; SQLite is authoritative.
                CREATE TABLE IF NOT EXISTS paper_positions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  exchange TEXT NOT NULL,
                  symboltoken TEXT NOT NULL,
                  tradingsymbol TEXT NOT NULL,
                  kind TEXT,            -- EQUITY | INDEX | COMMODITY
                  side TEXT NOT NULL,   -- CE | PE  (synthetic option side)
                  signal TEXT NOT NULL, -- BUY_CALL | BUY_PUT
                  lots INTEGER NOT NULL,
                  lot_size INTEGER NOT NULL,
                  qty INTEGER NOT NULL,
                  entry_price REAL NOT NULL,
                  stop_price REAL,
                  target_price REAL,
                  capital_used REAL NOT NULL,
                  capital_at_open REAL,
                  opened_at TEXT NOT NULL,
                  last_price REAL,
                  last_marked_at TEXT,
                  closed_at TEXT,
                  exit_price REAL,
                  exit_reason TEXT,     -- 'stop' | 'target' | 'manual' | 'session_end'
                  realized_pnl REAL,
                  reason_at_open TEXT,
                  max_hold_minutes INTEGER,
                  initial_stop_price REAL,
                  peak_premium REAL
                );
                CREATE INDEX IF NOT EXISTS idx_paper_open ON paper_positions(closed_at);

                -- Live exit plans: one row per LIVE position the bot opens.
                -- Mirrors paper SL/TP/max-hold, but for real broker
                -- positions. The exit manager iterates open plans every
                -- cycle, checks LTP from the scanner / broker positions,
                -- and fires a market reverse order when SL/TP/max-hold
                -- triggers. Persisted so a process restart inside a live
                -- session doesn't forget about positions it should be
                -- managing.
                CREATE TABLE IF NOT EXISTS live_exit_plans (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  open_order_id TEXT NOT NULL UNIQUE,
                  exchange TEXT NOT NULL,
                  symboltoken TEXT NOT NULL,
                  tradingsymbol TEXT NOT NULL,
                  kind TEXT,                -- INDEX | EQUITY | COMMODITY (signal source)
                  side TEXT NOT NULL,       -- CE | PE | LONG (cash buy)
                  signal TEXT NOT NULL,     -- BUY_CALL | BUY_PUT
                  underlying TEXT,
                  qty INTEGER NOT NULL,
                  lots INTEGER NOT NULL,
                  lot_size INTEGER NOT NULL,
                  planned_entry REAL NOT NULL,
                  fill_price REAL,          -- avg fill from order book (NULL until reconciled)
                  filled_at TEXT,           -- ISO time the open filled
                  stop_price REAL NOT NULL,
                  target_price REAL NOT NULL,
                  max_hold_minutes INTEGER NOT NULL,
                  initial_stop_price REAL,
                  peak_premium REAL,
                  product TEXT NOT NULL,
                  variety TEXT NOT NULL,
                  opened_at TEXT NOT NULL,  -- when the plan was created
                  close_order_id TEXT,      -- broker order id of the square-off
                  closed_at TEXT,
                  exit_price REAL,
                  exit_reason TEXT,         -- stop | target | session_end | manual | kill
                  realized_pnl REAL
                );
                CREATE INDEX IF NOT EXISTS idx_live_exit_open
                  ON live_exit_plans(closed_at);

                -- Legacy table: rolling entry timestamps (historically used for a
                -- per-hour trade cap; retained for DB compatibility).
                CREATE TABLE IF NOT EXISTS risk_entries (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  entered_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_risk_entries_at
                  ON risk_entries(entered_at);
                CREATE TABLE IF NOT EXISTS risk_state (
                  id INTEGER PRIMARY KEY CHECK (id = 1),
                  last_loss_at TEXT
                );
                """
            )
            cols = self._table_columns(con, "orders")
            migrations = [
                ("lifecycle_status", "ALTER TABLE orders ADD COLUMN lifecycle_status TEXT"),
                ("broker_status", "ALTER TABLE orders ADD COLUMN broker_status TEXT"),
                ("filled_qty", "ALTER TABLE orders ADD COLUMN filled_qty INTEGER"),
                ("pending_qty", "ALTER TABLE orders ADD COLUMN pending_qty INTEGER"),
                ("avg_price", "ALTER TABLE orders ADD COLUMN avg_price REAL"),
                ("raw_last_json", "ALTER TABLE orders ADD COLUMN raw_last_json TEXT"),
                ("updated_at", "ALTER TABLE orders ADD COLUMN updated_at TEXT"),
                ("placed_by_bot", "ALTER TABLE orders ADD COLUMN placed_by_bot INTEGER DEFAULT 0"),
                ("intent", "ALTER TABLE orders ADD COLUMN intent TEXT"),  # "open" | "close"
                ("tradingsymbol", "ALTER TABLE orders ADD COLUMN tradingsymbol TEXT"),
                ("exchange", "ALTER TABLE orders ADD COLUMN exchange TEXT"),
                ("symboltoken", "ALTER TABLE orders ADD COLUMN symboltoken TEXT"),
                ("transactiontype", "ALTER TABLE orders ADD COLUMN transactiontype TEXT"),
                ("variety", "ALTER TABLE orders ADD COLUMN variety TEXT"),
                # Marks live vs dryrun. NULL is treated as 'live' for old rows.
                ("mode", "ALTER TABLE orders ADD COLUMN mode TEXT DEFAULT 'live'"),
            ]
            for name, ddl in migrations:
                if name not in cols:
                    con.execute(ddl)

            # ----- live_exit_plans migrations (additive, idempotent) -----
            # ``source`` distinguishes plans the bot opened ('bot') from
            # positions opened directly on the Angel One platform that the
            # bot adopted for management ('adopted'). Older rows get 'bot'
            # by default so existing decisions / dashboards still make sense.
            plan_cols = self._table_columns(con, "live_exit_plans")
            plan_migrations = [
                ("source", "ALTER TABLE live_exit_plans ADD COLUMN source TEXT DEFAULT 'bot'"),
                ("last_seen_qty", "ALTER TABLE live_exit_plans ADD COLUMN last_seen_qty INTEGER"),
                # ``pnl_long_only_v1`` is a marker flag — non-NULL means
                # we already recomputed realized_pnl with the long-only
                # formula. Without this, the back-fill below would run
                # every startup and double-flip rows.
                ("pnl_long_only_v1", "ALTER TABLE live_exit_plans ADD COLUMN pnl_long_only_v1 INTEGER"),
                (
                    "initial_stop_price",
                    "ALTER TABLE live_exit_plans ADD COLUMN initial_stop_price REAL",
                ),
                (
                    "peak_premium",
                    "ALTER TABLE live_exit_plans ADD COLUMN peak_premium REAL",
                ),
            ]
            for name, ddl in plan_migrations:
                if name not in plan_cols:
                    con.execute(ddl)

            paper_cols = self._table_columns(con, "paper_positions")
            paper_migrations = [
                (
                    "max_hold_minutes",
                    "ALTER TABLE paper_positions ADD COLUMN max_hold_minutes INTEGER",
                ),
                (
                    "initial_stop_price",
                    "ALTER TABLE paper_positions ADD COLUMN initial_stop_price REAL",
                ),
                (
                    "peak_premium",
                    "ALTER TABLE paper_positions ADD COLUMN peak_premium REAL",
                ),
            ]
            for name, ddl in paper_migrations:
                if name not in paper_cols:
                    con.execute(ddl)

            # Back-fill trailing-stop columns for open rows (idempotent).
            try:
                con.execute(
                    """
                    UPDATE paper_positions
                    SET initial_stop_price = COALESCE(initial_stop_price, stop_price),
                        peak_premium = COALESCE(peak_premium, entry_price)
                    WHERE closed_at IS NULL
                      AND (initial_stop_price IS NULL OR peak_premium IS NULL)
                    """
                )
                con.execute(
                    """
                    UPDATE live_exit_plans
                    SET initial_stop_price = COALESCE(initial_stop_price, stop_price),
                        peak_premium = COALESCE(
                            peak_premium, COALESCE(fill_price, planned_entry)
                        )
                    WHERE closed_at IS NULL
                      AND (initial_stop_price IS NULL OR peak_premium IS NULL)
                    """
                )
            except Exception:  # noqa: BLE001 — best-effort on odd schemas
                pass

            # ----- One-shot back-fill: long-only PnL for every closed plan -----
            # Earlier builds of the exit manager treated a PE BUY as a
            # short and inverted the realized P&L sign. The dashboard's
            # "Closed today" panel now reads directly from this table,
            # so anything stamped by the buggy code would still display
            # the wrong sign. We rewrite each closed row with the
            # corrected formula  (exit - entry) * qty  exactly once.
            for row in con.execute(
                """
                SELECT id, qty, fill_price, planned_entry, exit_price, realized_pnl
                FROM live_exit_plans
                WHERE closed_at IS NOT NULL
                  AND (pnl_long_only_v1 IS NULL OR pnl_long_only_v1 = 0)
                """
            ).fetchall():
                qty = int(row["qty"] or 0)
                entry = float(row["fill_price"] or row["planned_entry"] or 0.0)
                exit_p = float(row["exit_price"] or 0.0)
                if qty <= 0 or entry <= 0 or exit_p <= 0:
                    # Insufficient data to recompute — leave the stored
                    # PnL alone but mark so we don't keep retrying.
                    con.execute(
                        "UPDATE live_exit_plans SET pnl_long_only_v1 = 1 WHERE id = ?",
                        (int(row["id"]),),
                    )
                    continue
                corrected = (exit_p - entry) * qty
                con.execute(
                    "UPDATE live_exit_plans SET realized_pnl = ?, pnl_long_only_v1 = 1 WHERE id = ?",
                    (float(corrected), int(row["id"])),
                )

    def log_order(
        self,
        payload: dict[str, Any],
        broker_order_id: str | None,
        status: str,
        *,
        lifecycle_status: str | None = None,
        placed_by_bot: bool = False,
        intent: str | None = None,
        mode: str = "live",
    ) -> None:
        now = datetime.now(UTC).isoformat()
        ls = lifecycle_status or status
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO orders (
                  broker_order_id, payload_json, status, created_at,
                  lifecycle_status, updated_at,
                  placed_by_bot, intent,
                  tradingsymbol, exchange, symboltoken,
                  transactiontype, variety, mode
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    broker_order_id,
                    json.dumps(payload, separators=(",", ":")),
                    status,
                    now,
                    ls,
                    now,
                    1 if placed_by_bot else 0,
                    intent,
                    payload.get("tradingsymbol"),
                    payload.get("exchange"),
                    payload.get("symboltoken"),
                    payload.get("transactiontype"),
                    payload.get("variety"),
                    (mode or "live").lower(),
                ),
            )

    def upsert_broker_order(
        self,
        *,
        broker_order_id: str,
        lifecycle_status: str,
        broker_status: str,
        filled_qty: int,
        pending_qty: int,
        avg_price: float | None,
        raw_row: dict[str, Any],
        payload_json: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        raw_s = json.dumps(raw_row, separators=(",", ":"), default=str)
        with self._connect() as con:
            row = con.execute(
                "SELECT id FROM orders WHERE broker_order_id = ?",
                (broker_order_id,),
            ).fetchone()
            if row is None:
                con.execute(
                    """
                    INSERT INTO orders (
                      broker_order_id, payload_json, status, created_at,
                      lifecycle_status, broker_status, filled_qty, pending_qty,
                      avg_price, raw_last_json, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        broker_order_id,
                        payload_json or "{}",
                        lifecycle_status,
                        now,
                        lifecycle_status,
                        broker_status,
                        filled_qty,
                        pending_qty,
                        avg_price,
                        raw_s,
                        now,
                    ),
                )
            else:
                con.execute(
                    """
                    UPDATE orders SET
                      lifecycle_status = ?,
                      broker_status = ?,
                      filled_qty = ?,
                      pending_qty = ?,
                      avg_price = ?,
                      raw_last_json = ?,
                      updated_at = ?,
                      status = ?
                    WHERE broker_order_id = ?
                    """,
                    (
                        lifecycle_status,
                        broker_status,
                        filled_qty,
                        pending_qty,
                        avg_price,
                        raw_s,
                        now,
                        lifecycle_status,
                        broker_order_id,
                    ),
                )

    def set_daily_stats(self, day: date, trades: int, pnl: float) -> None:
        d = day.isoformat()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO daily_stats (day, trades, pnl) VALUES (?,?,?)
                ON CONFLICT(day) DO UPDATE SET trades = excluded.trades, pnl = excluded.pnl
                """,
                (d, trades, pnl),
            )

    def get_daily_stats(self, day: date | None = None) -> tuple[int, float]:
        d = (day or datetime.now(UTC).date()).isoformat()
        with self._connect() as con:
            row = con.execute("SELECT trades, pnl FROM daily_stats WHERE day = ?", (d,)).fetchone()
            if not row:
                return (0, 0.0)
            return (int(row["trades"]), float(row["pnl"]))

    def recent_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
            return [dict(r) for r in rows]

    def bot_orders_today(self) -> list[dict[str, Any]]:
        """All orders placed by the bot since UTC midnight today."""
        start = datetime.now(UTC).date().isoformat()
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT * FROM orders
                WHERE placed_by_bot = 1 AND created_at >= ?
                ORDER BY id DESC
                """,
                (start,),
            ).fetchall()
            return [dict(r) for r in rows]

    def pending_bot_orders(self) -> list[dict[str, Any]]:
        """Bot-placed orders that are still open (not filled / cancelled / rejected)."""
        terminal = ("executed", "complete", "cancelled", "rejected")
        with self._connect() as con:
            rows = con.execute(
                f"""
                SELECT * FROM orders
                WHERE placed_by_bot = 1
                  AND broker_order_id IS NOT NULL
                  AND COALESCE(LOWER(lifecycle_status), '') NOT IN ({",".join("?" * len(terminal))})
                ORDER BY id DESC
                """,
                terminal,
            ).fetchall()
            return [dict(r) for r in rows]

    def all_daily_stats(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute("SELECT day, trades, pnl FROM daily_stats ORDER BY day DESC").fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Per-mode (live | dryrun) daily stats
    # ------------------------------------------------------------------

    def add_mode_pnl(self, mode: str, pnl_delta: float, trades_delta: int = 1) -> None:
        """Increment today's per-mode realized P&L and trade count."""
        d = datetime.now(UTC).date().isoformat()
        m = (mode or "live").lower()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO daily_stats_mode (day, mode, trades, pnl) VALUES (?,?,?,?)
                ON CONFLICT(day, mode) DO UPDATE SET
                  trades = trades + excluded.trades,
                  pnl = pnl + excluded.pnl
                """,
                (d, m, int(trades_delta), float(pnl_delta)),
            )

    def get_mode_daily_stats(self, mode: str, day: date | None = None) -> tuple[int, float]:
        d = (day or datetime.now(UTC).date()).isoformat()
        m = (mode or "live").lower()
        with self._connect() as con:
            row = con.execute(
                "SELECT trades, pnl FROM daily_stats_mode WHERE day = ? AND mode = ?",
                (d, m),
            ).fetchone()
            if not row:
                return (0, 0.0)
            return (int(row["trades"]), float(row["pnl"]))

    def all_mode_daily_stats(self, mode: str) -> list[dict[str, Any]]:
        m = (mode or "live").lower()
        with self._connect() as con:
            rows = con.execute(
                "SELECT day, trades, pnl FROM daily_stats_mode WHERE mode = ? ORDER BY day DESC",
                (m,),
            ).fetchall()
            return [dict(r) for r in rows]

    def reset_mode(self, mode: str) -> None:
        """Wipe paper positions + per-mode daily stats + per-mode orders.

        Only supports 'dryrun' to avoid accidentally nuking live history.
        """
        m = (mode or "").lower()
        if m != "dryrun":
            raise ValueError("reset_mode only supports 'dryrun'")
        with self._connect() as con:
            con.execute("DELETE FROM paper_positions")
            con.execute("DELETE FROM daily_stats_mode WHERE mode = 'dryrun'")
            con.execute("DELETE FROM orders WHERE mode = 'dryrun'")

    def recent_orders_by_mode(self, mode: str, limit: int = 200) -> list[dict[str, Any]]:
        m = (mode or "live").lower()
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM orders WHERE mode = ? ORDER BY id DESC LIMIT ?",
                (m, int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Paper (dry-run) positions
    # ------------------------------------------------------------------

    def open_paper_position(self, p: dict[str, Any]) -> int:
        now = datetime.now(UTC).isoformat()
        with self._connect() as con:
            cur = con.execute(
                """
                INSERT INTO paper_positions (
                  exchange, symboltoken, tradingsymbol, kind, side, signal,
                  lots, lot_size, qty, entry_price, stop_price, target_price,
                  capital_used, capital_at_open, opened_at,
                  last_price, last_marked_at, reason_at_open,
                  max_hold_minutes,
                  initial_stop_price, peak_premium
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    p["exchange"],
                    p["symboltoken"],
                    p["tradingsymbol"],
                    p.get("kind"),
                    p["side"],
                    p["signal"],
                    int(p["lots"]),
                    int(p["lot_size"]),
                    int(p["qty"]),
                    float(p["entry_price"]),
                    p.get("stop_price"),
                    p.get("target_price"),
                    float(p["capital_used"]),
                    p.get("capital_at_open"),
                    p.get("opened_at") or now,
                    float(p["entry_price"]),
                    now,
                    p.get("reason_at_open"),
                    p.get("max_hold_minutes"),
                    p.get("initial_stop_price", p.get("stop_price")),
                    p.get("peak_premium", p.get("entry_price")),
                ),
            )
            return int(cur.lastrowid or 0)

    def list_open_paper_positions(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM paper_positions WHERE closed_at IS NULL ORDER BY id DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def list_recent_paper_positions(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM paper_positions ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
            return [dict(r) for r in rows]

    def update_paper_mark(self, pid: int, last_price: float) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as con:
            con.execute(
                "UPDATE paper_positions SET last_price = ?, last_marked_at = ? WHERE id = ? AND closed_at IS NULL",
                (float(last_price), now, int(pid)),
            )

    def update_paper_trailing_stop(
        self, pid: int, *, peak_premium: float, stop_price: float
    ) -> None:
        """Persist ratcheted peak and working stop for open paper positions."""
        with self._connect() as con:
            con.execute(
                """
                UPDATE paper_positions
                SET peak_premium = ?, stop_price = ?
                WHERE id = ? AND closed_at IS NULL
                """,
                (float(peak_premium), float(stop_price), int(pid)),
            )

    def close_paper_position(
        self,
        pid: int,
        *,
        exit_price: float,
        exit_reason: str,
        realized_pnl: float,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as con:
            con.execute(
                """
                UPDATE paper_positions
                SET closed_at = ?, exit_price = ?, exit_reason = ?,
                    realized_pnl = ?, last_price = ?, last_marked_at = ?
                WHERE id = ?
                """,
                (now, float(exit_price), exit_reason, float(realized_pnl), float(exit_price), now, int(pid)),
            )

    # ------------------------------------------------------------------
    # Live exit plans (live-mode equivalent of paper_positions)
    # ------------------------------------------------------------------

    def create_live_exit_plan(self, p: dict[str, Any]) -> int:
        """Persist a new exit plan when the bot places a LIVE open order.

        Idempotent on ``open_order_id`` — re-registering the same broker order id
        is a no-op (returns the existing plan id). This protects against a race
        where the runtime restarts mid-cycle and tries to re-register a plan
        whose open order is already in the DB.
        """
        now = datetime.now(UTC).isoformat()
        with self._connect() as con:
            existing = con.execute(
                "SELECT id FROM live_exit_plans WHERE open_order_id = ?",
                (p["open_order_id"],),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            source = str(p.get("source") or "bot").lower()
            fill_price = p.get("fill_price")
            filled_at = p.get("filled_at")
            cur = con.execute(
                """
                INSERT INTO live_exit_plans (
                  open_order_id, exchange, symboltoken, tradingsymbol, kind,
                  side, signal, underlying,
                  qty, lots, lot_size,
                  planned_entry, fill_price, filled_at,
                  stop_price, target_price, max_hold_minutes,
                  initial_stop_price, peak_premium,
                  product, variety, opened_at, source, last_seen_qty
                ) VALUES (?,?,?,?,?, ?,?,?, ?,?,?, ?,?,?, ?,?,?, ?,?,?, ?,?,?, ?)
                """,
                (
                    str(p["open_order_id"]),
                    str(p["exchange"]).upper(),
                    str(p["symboltoken"]),
                    str(p["tradingsymbol"]),
                    p.get("kind"),
                    str(p["side"]).upper(),
                    str(p["signal"]),
                    p.get("underlying"),
                    int(p["qty"]),
                    int(p["lots"]),
                    int(p["lot_size"]),
                    float(p["planned_entry"]),
                    float(fill_price) if fill_price is not None else None,
                    str(filled_at) if filled_at else None,
                    float(p["stop_price"]),
                    float(p["target_price"]),
                    int(p["max_hold_minutes"]),
                    float(p.get("initial_stop_price", p["stop_price"])),
                    float(
                        p.get("peak_premium")
                        if p.get("peak_premium") is not None
                        else p["planned_entry"]
                    ),
                    str(p["product"]),
                    str(p["variety"]),
                    p.get("opened_at") or now,
                    source,
                    int(p["qty"]),
                ),
            )
            return int(cur.lastrowid or 0)

    def list_open_live_exit_plans(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM live_exit_plans WHERE closed_at IS NULL ORDER BY id DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def list_closed_live_plans_since(
        self, since_iso: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Closed live exit plans (bot- or user-initiated) since ``since_iso``.

        This is the canonical source for the dashboard's "Closed today"
        panel — backed by SQLite so it survives restarts and never falls
        out of an in-memory log buffer the way the decision stream does.
        ``since_iso`` should be the ISO-formatted start of the trading
        day in IST (Angel's reporting timezone).
        """
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT * FROM live_exit_plans
                WHERE closed_at IS NOT NULL AND closed_at >= ?
                ORDER BY closed_at DESC
                LIMIT ?
                """,
                (str(since_iso), int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_closed_live_plans(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """All closed live exit plans (newest first), for analytics / dashboards."""
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT * FROM live_exit_plans
                WHERE closed_at IS NOT NULL
                ORDER BY closed_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_closed_paper_positions(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Closed paper positions (newest first), for analytics / dashboards."""
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT * FROM paper_positions
                WHERE closed_at IS NOT NULL
                ORDER BY closed_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]

    def closed_live_pnl_since(self, since_iso: str) -> tuple[int, float]:
        """Return (count, summed realized PnL) for closed plans since
        ``since_iso``. Used to make the dashboard's "PnL today" tile
        consistent with the closed-trade list — both come from this
        single SQL aggregate now."""
        with self._connect() as con:
            row = con.execute(
                """
                SELECT COUNT(*) AS n, COALESCE(SUM(realized_pnl), 0.0) AS pnl
                FROM live_exit_plans
                WHERE closed_at IS NOT NULL AND closed_at >= ?
                """,
                (str(since_iso),),
            ).fetchone()
            if row is None:
                return (0, 0.0)
            return (int(row["n"] or 0), float(row["pnl"] or 0.0))

    def find_open_live_exit_plan_by_token(
        self, *, exchange: str, symboltoken: str
    ) -> dict[str, Any] | None:
        """Return the open plan that holds (exchange, symboltoken) or None.

        Used by the adoption flow so the same broker position is never
        managed twice — once a plan exists for a token (whether bot-opened
        or adopted) we update it in place instead of creating another row.
        """
        with self._connect() as con:
            row = con.execute(
                """
                SELECT * FROM live_exit_plans
                WHERE closed_at IS NULL
                  AND UPPER(exchange) = ?
                  AND symboltoken = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (str(exchange).upper(), str(symboltoken)),
            ).fetchone()
            return dict(row) if row is not None else None

    def update_live_exit_plan_qty(
        self,
        plan_id: int,
        *,
        qty: int,
        lots: int,
        last_seen_qty: int | None = None,
    ) -> None:
        """Resync qty/lots after a partial manual exit on the broker."""
        seen = qty if last_seen_qty is None else last_seen_qty
        with self._connect() as con:
            con.execute(
                """
                UPDATE live_exit_plans
                SET qty = ?, lots = ?, last_seen_qty = ?
                WHERE id = ? AND closed_at IS NULL
                """,
                (int(qty), int(lots), int(seen), int(plan_id)),
            )

    def update_live_exit_plan_seen(self, plan_id: int, *, last_seen_qty: int) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE live_exit_plans SET last_seen_qty = ? WHERE id = ?",
                (int(last_seen_qty), int(plan_id)),
            )

    def update_live_exit_trailing_stop(
        self, plan_id: int, *, peak_premium: float, stop_price: float
    ) -> None:
        """Persist ratcheted peak and working stop for open live exit plans."""
        with self._connect() as con:
            con.execute(
                """
                UPDATE live_exit_plans
                SET peak_premium = ?, stop_price = ?
                WHERE id = ? AND closed_at IS NULL
                """,
                (float(peak_premium), float(stop_price), int(plan_id)),
            )

    def update_live_exit_plan_fill(
        self, *, open_order_id: str, fill_price: float, filled_at: str | None = None
    ) -> None:
        when = filled_at or datetime.now(UTC).isoformat()
        with self._connect() as con:
            con.execute(
                """
                UPDATE live_exit_plans
                SET fill_price = ?, filled_at = COALESCE(filled_at, ?)
                WHERE open_order_id = ? AND closed_at IS NULL
                """,
                (float(fill_price), when, str(open_order_id)),
            )

    def close_live_exit_plan(
        self,
        plan_id: int,
        *,
        exit_price: float,
        exit_reason: str,
        realized_pnl: float,
        close_order_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as con:
            con.execute(
                """
                UPDATE live_exit_plans
                SET closed_at = ?, exit_price = ?, exit_reason = ?,
                    realized_pnl = ?, close_order_id = ?
                WHERE id = ? AND closed_at IS NULL
                """,
                (
                    now,
                    float(exit_price),
                    str(exit_reason),
                    float(realized_pnl),
                    close_order_id,
                    int(plan_id),
                ),
            )

    # ------------------------------------------------------------------
    # Persistent fragments of RiskState
    # ------------------------------------------------------------------

    def add_risk_entry(self, when_iso: str) -> None:
        """Append a UTC-ISO timestamp marking a successful trade entry."""
        with self._connect() as con:
            con.execute("INSERT INTO risk_entries (entered_at) VALUES (?)", (str(when_iso),))

    def trim_risk_entries(self, before_iso: str) -> int:
        """Delete risk_entries older than ``before_iso``. Returns rows removed."""
        with self._connect() as con:
            cur = con.execute(
                "DELETE FROM risk_entries WHERE entered_at < ?", (str(before_iso),)
            )
            return int(cur.rowcount or 0)

    def list_recent_risk_entries(self, since_iso: str) -> list[str]:
        """Return all entry timestamps at or after ``since_iso``, oldest first."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT entered_at FROM risk_entries WHERE entered_at >= ? ORDER BY entered_at ASC",
                (str(since_iso),),
            ).fetchall()
            return [str(r["entered_at"]) for r in rows]

    def get_risk_state(self) -> dict[str, Any]:
        """Snapshot of the persisted RiskState fragment (rolling entry timestamps)."""
        with self._connect() as con:
            entries_rows = con.execute(
                "SELECT entered_at FROM risk_entries ORDER BY entered_at ASC"
            ).fetchall()
        return {
            "recent_entries": [str(r["entered_at"]) for r in entries_rows],
        }
