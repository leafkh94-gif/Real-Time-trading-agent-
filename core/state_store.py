"""
SQLite-backed persistence for daily PnL, trade counts, and open position cache.
Survives process restarts — daily totals accumulate correctly across crashes.
"""
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import NamedTuple

DB_PATH = Path("state/bot.db")


class DailyStats(NamedTuple):
    pnl: float
    trades: int


class StateStore:
    def __init__(self, db_path: Path = DB_PATH):
        db_path.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS daily (
                day     TEXT PRIMARY KEY,
                pnl     REAL    NOT NULL DEFAULT 0,
                trades  INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS open_positions (
                position_id TEXT PRIMARY KEY,
                symbol      TEXT NOT NULL,
                lots        REAL NOT NULL,
                direction   TEXT NOT NULL,
                open_price  REAL NOT NULL,
                opened_at   TEXT NOT NULL
            );
        """)

    # ── Daily stats ──────────────────────────────────────────────────────────

    def _today(self) -> str:
        # UTC so the trading-day boundary is deterministic regardless of host timezone,
        # and consistent with the kill-switch timestamps and the 00:05-UTC S3 backup.
        return datetime.now(timezone.utc).date().isoformat()

    def get_today(self) -> DailyStats:
        cur = self.conn.execute(
            "SELECT pnl, trades FROM daily WHERE day = ?", (self._today(),)
        )
        row = cur.fetchone()
        return DailyStats(row[0], row[1]) if row else DailyStats(0.0, 0)

    def add_pnl(self, amount: float) -> None:
        self.conn.execute(
            "INSERT INTO daily(day, pnl, trades) VALUES(?, ?, 0)"
            " ON CONFLICT(day) DO UPDATE SET pnl = pnl + ?",
            (self._today(), amount, amount),
        )

    def add_trade(self) -> None:
        self.conn.execute(
            "INSERT INTO daily(day, pnl, trades) VALUES(?, 0, 1)"
            " ON CONFLICT(day) DO UPDATE SET trades = trades + 1",
            (self._today(),),
        )

    # ── Open positions ────────────────────────────────────────────────────────

    def add_position(
        self,
        position_id: str,
        symbol: str,
        lots: float,
        direction: str,
        open_price: float,
        opened_at: str,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO open_positions"
            "(position_id, symbol, lots, direction, open_price, opened_at)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (position_id, symbol, lots, direction, open_price, opened_at),
        )

    def remove_position(self, position_id: str) -> None:
        self.conn.execute(
            "DELETE FROM open_positions WHERE position_id = ?", (position_id,)
        )

    def count_open_positions(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM open_positions")
        return cur.fetchone()[0]

    def close(self) -> None:
        self.conn.close()


state_store = StateStore()
