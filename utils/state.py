"""
utils/state.py — SQLite persistence layer.

Stores trade history, daily statistics, and open position tracking.
All monetary values are stored in cents (integers) to avoid float rounding.
"""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Optional

import config
from utils.logger import get_logger

logger = get_logger(__name__)

_lock = threading.Lock()


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def _db():
    with _lock:
        conn = _get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker              TEXT NOT NULL,
                side                TEXT NOT NULL,       -- 'yes' or 'no'
                action              TEXT NOT NULL,       -- 'buy' or 'sell'
                contracts           INTEGER NOT NULL,
                price_cents         INTEGER NOT NULL,
                total_cost_cents    INTEGER NOT NULL,
                order_id            TEXT,
                status              TEXT DEFAULT 'open', -- open/closed/cancelled
                close_reason        TEXT,                -- profit_target/stop_loss/approaching_resolution/manual
                opened_at           TEXT DEFAULT (datetime('now')),
                closed_at           TEXT,
                realized_pnl_cents  INTEGER,
                ai_estimate_cents   INTEGER,
                edge_cents          INTEGER
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                date                    TEXT PRIMARY KEY,   -- YYYY-MM-DD
                total_spent_cents       INTEGER DEFAULT 0,
                realized_pnl_cents      INTEGER DEFAULT 0,
                trades_placed           INTEGER DEFAULT 0,
                kill_switch_tripped     INTEGER DEFAULT 0   -- SQLite has no BOOLEAN
            );

            CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
        """)
    logger.info("Database initialized", extra={"db_path": str(config.DB_PATH)})


# ------------------------------------------------------------------ #
# Daily stats helpers
# ------------------------------------------------------------------ #

def _today() -> str:
    return date.today().isoformat()


def _ensure_today() -> None:
    """Insert a row for today if it doesn't exist."""
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO daily_stats (date) VALUES (?)", (_today(),)
        )


def get_daily_stats() -> dict:
    _ensure_today()
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM daily_stats WHERE date = ?", (_today(),)
        ).fetchone()
        return dict(row) if row else {}


def add_daily_spend(cents: int) -> None:
    _ensure_today()
    with _db() as conn:
        conn.execute(
            "UPDATE daily_stats SET total_spent_cents = total_spent_cents + ?, "
            "trades_placed = trades_placed + 1 WHERE date = ?",
            (cents, _today()),
        )


def add_daily_pnl(cents: int) -> None:
    """Record realized P&L (positive = profit, negative = loss)."""
    _ensure_today()
    with _db() as conn:
        conn.execute(
            "UPDATE daily_stats SET realized_pnl_cents = realized_pnl_cents + ? "
            "WHERE date = ?",
            (cents, _today()),
        )


def set_kill_switch(tripped: bool) -> None:
    _ensure_today()
    with _db() as conn:
        conn.execute(
            "UPDATE daily_stats SET kill_switch_tripped = ? WHERE date = ?",
            (1 if tripped else 0, _today()),
        )


def is_kill_switch_tripped() -> bool:
    stats = get_daily_stats()
    return bool(stats.get("kill_switch_tripped", 0))


# ------------------------------------------------------------------ #
# Trade record helpers
# ------------------------------------------------------------------ #

def record_open_trade(
    ticker: str,
    side: str,
    contracts: int,
    price_cents: int,
    total_cost_cents: int,
    order_id: Optional[str] = None,
    ai_estimate_cents: Optional[int] = None,
    edge_cents: Optional[int] = None,
) -> int:
    """Insert a new open trade. Returns the row id."""
    with _db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO trades
                (ticker, side, action, contracts, price_cents, total_cost_cents,
                 order_id, status, ai_estimate_cents, edge_cents)
            VALUES (?, ?, 'buy', ?, ?, ?, ?, 'open', ?, ?)
            """,
            (ticker, side, contracts, price_cents, total_cost_cents,
             order_id, ai_estimate_cents, edge_cents),
        )
        return cursor.lastrowid


def record_close_trade(
    trade_id: int,
    close_reason: str,
    realized_pnl_cents: int,
    order_id: Optional[str] = None,
) -> None:
    closed_at = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        conn.execute(
            """
            UPDATE trades SET
                status = 'closed',
                close_reason = ?,
                realized_pnl_cents = ?,
                closed_at = ?,
                order_id = COALESCE(?, order_id)
            WHERE id = ?
            """,
            (close_reason, realized_pnl_cents, closed_at, order_id, trade_id),
        )
    add_daily_pnl(realized_pnl_cents)


def get_open_trades() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = 'open' ORDER BY opened_at"
        ).fetchall()
        return [dict(r) for r in rows]


def get_open_trade_by_ticker(ticker: str) -> Optional[dict]:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE ticker = ? AND status = 'open' LIMIT 1",
            (ticker,),
        ).fetchone()
        return dict(row) if row else None


def count_open_trades() -> int:
    with _db() as conn:
        result = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status = 'open'"
        ).fetchone()
        return result[0] if result else 0
