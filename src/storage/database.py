import sqlite3
from datetime import datetime
from typing import List, Optional, Dict
from pathlib import Path

from src.utils.logger import setup_logger
from src.storage.models import (
    TradeRecord, PositionRecord, FundingRecord, SlippageRecord,
    MetricsSnapshot, AlertRecord
)

logger = setup_logger("database")


class Database:
    """SQLite database for persistent storage of bot data."""

    def __init__(self, db_path: str = "data/bot.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection: Optional[sqlite3.Connection] = None

        logger.info(f"Database initialized at {self.db_path}")

    def connect(self):
        """Connect to database and initialize tables."""
        self.connection = sqlite3.connect(str(self.db_path))
        self.connection.row_factory = sqlite3.Row
        self._create_tables()
        logger.info("Database connected")

    def disconnect(self):
        """Disconnect from database."""
        if self.connection:
            self.connection.close()
            logger.info("Database disconnected")

    def _create_tables(self):
        """Create all tables if they don't exist."""
        cursor = self.connection.cursor()

        # Trades table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                entry_time TIMESTAMP,
                exit_time TIMESTAMP,
                entry_price REAL,
                exit_price REAL,
                quantity REAL,
                bybit_entry_price REAL,
                binance_entry_price REAL,
                bybit_exit_price REAL,
                binance_exit_price REAL,
                realized_pnl REAL,
                funding_earned REAL,
                total_pnl REAL,
                duration_seconds INTEGER,
                entry_differential REAL,
                exit_differential REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Positions table.
        #
        # UNIQUE(symbol, exchange) makes this a live snapshot of open positions
        # rather than an append-only log. Without it, repeated snapshots and
        # re-opened positions accumulated duplicate rows and closed positions were
        # never removed, so get_open_positions() reported phantom exposure -
        # dangerous for anything reconciling state after a restart.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                side TEXT,
                quantity REAL,
                entry_price REAL,
                entry_time TIMESTAMP,
                current_price REAL,
                unrealized_pnl REAL,
                entry_differential REAL,
                last_update TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(symbol, exchange)
            )
        """)

        # Funding table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS funding (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                timestamp TIMESTAMP,
                funding_rate REAL,
                position_size REAL,
                amount REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Slippage table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS slippage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                timestamp TIMESTAMP,
                expected_price REAL,
                actual_price REAL,
                quantity REAL,
                side TEXT,
                slippage_bps REAL,
                slippage_usd REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Metrics table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP,
                capital REAL,
                total_exposure REAL,
                open_positions INTEGER,
                unrealized_pnl REAL,
                realized_pnl REAL,
                total_pnl REAL,
                return_pct REAL,
                win_rate REAL,
                avg_trade_pnl REAL,
                daily_loss REAL,
                max_drawdown REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Alerts table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP,
                alert_type TEXT,
                alert_level TEXT,
                title TEXT,
                message TEXT,
                services TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self.connection.commit()
        logger.info("Database tables initialized")

    def insert_trade(self, trade: TradeRecord) -> int:
        """Insert a completed trade record."""
        cursor = self.connection.cursor()
        data = trade.to_dict()
        data.pop('id', None)  # Remove id if present

        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?" for _ in data])
        query = f"INSERT INTO trades ({cols}) VALUES ({placeholders})"

        cursor.execute(query, tuple(data.values()))
        self.connection.commit()

        logger.debug(f"Trade inserted: {trade.symbol}")
        return cursor.lastrowid

    def insert_position(self, position: PositionRecord) -> int:
        """
        Upsert an open position.

        INSERT OR REPLACE keyed on the table's UNIQUE(symbol, exchange): re-writing
        the same leg updates it in place instead of appending a duplicate row.
        """
        cursor = self.connection.cursor()
        data = position.to_dict()
        data.pop('id', None)

        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?" for _ in data])
        query = f"INSERT OR REPLACE INTO positions ({cols}) VALUES ({placeholders})"

        cursor.execute(query, tuple(data.values()))
        self.connection.commit()

        logger.debug(f"Position upserted: {position.symbol} on {position.exchange}")
        return cursor.lastrowid

    def delete_position(self, symbol: str, exchange: str) -> bool:
        """
        Remove a position once it is closed.

        Must be called on close, otherwise the row lingers and is reported as
        open exposure that no longer exists.
        """
        cursor = self.connection.cursor()
        cursor.execute(
            "DELETE FROM positions WHERE symbol = ? AND exchange = ?",
            (symbol, exchange),
        )
        self.connection.commit()

        if cursor.rowcount:
            logger.debug(f"Position removed: {symbol} on {exchange}")
            return True
        return False

    def insert_funding(self, funding: FundingRecord) -> int:
        """Insert a funding payment record."""
        cursor = self.connection.cursor()
        data = funding.to_dict()
        data.pop('id', None)

        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?" for _ in data])
        query = f"INSERT INTO funding ({cols}) VALUES ({placeholders})"

        cursor.execute(query, tuple(data.values()))
        self.connection.commit()

        logger.debug(f"Funding recorded: {funding.symbol} on {funding.exchange} +${funding.amount:.2f}")
        return cursor.lastrowid

    def insert_slippage(self, slippage: SlippageRecord) -> int:
        """Insert a slippage record."""
        cursor = self.connection.cursor()
        data = slippage.to_dict()
        data.pop('id', None)

        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?" for _ in data])
        query = f"INSERT INTO slippage ({cols}) VALUES ({placeholders})"

        cursor.execute(query, tuple(data.values()))
        self.connection.commit()

        return cursor.lastrowid

    def insert_metrics(self, metrics: MetricsSnapshot) -> int:
        """Insert a metrics snapshot."""
        cursor = self.connection.cursor()
        data = metrics.to_dict()
        data.pop('id', None)

        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?" for _ in data])
        query = f"INSERT INTO metrics ({cols}) VALUES ({placeholders})"

        cursor.execute(query, tuple(data.values()))
        self.connection.commit()

        return cursor.lastrowid

    def insert_alert(self, alert: AlertRecord) -> int:
        """Insert an alert record."""
        cursor = self.connection.cursor()
        data = alert.to_dict()
        data.pop('id', None)

        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?" for _ in data])
        query = f"INSERT INTO alerts ({cols}) VALUES ({placeholders})"

        cursor.execute(query, tuple(data.values()))
        self.connection.commit()

        return cursor.lastrowid

    def get_trades(self, symbol: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """Get trade history."""
        cursor = self.connection.cursor()

        if symbol:
            cursor.execute(
                "SELECT * FROM trades WHERE symbol = ? ORDER BY exit_time DESC LIMIT ?",
                (symbol, limit)
            )
        else:
            cursor.execute("SELECT * FROM trades ORDER BY exit_time DESC LIMIT ?", (limit,))

        return [dict(row) for row in cursor.fetchall()]

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Dict]:
        """Get open positions."""
        cursor = self.connection.cursor()

        if symbol:
            cursor.execute(
                "SELECT * FROM positions WHERE symbol = ? ORDER BY entry_time DESC",
                (symbol,)
            )
        else:
            cursor.execute("SELECT * FROM positions ORDER BY entry_time DESC")

        return [dict(row) for row in cursor.fetchall()]

    def get_funding_history(self, symbol: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """Get funding payment history."""
        cursor = self.connection.cursor()

        if symbol:
            cursor.execute(
                "SELECT * FROM funding WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?",
                (symbol, limit)
            )
        else:
            cursor.execute("SELECT * FROM funding ORDER BY timestamp DESC LIMIT ?", (limit,))

        return [dict(row) for row in cursor.fetchall()]

    def get_slippage_stats(self, exchange: Optional[str] = None) -> Dict:
        """Get slippage statistics."""
        cursor = self.connection.cursor()

        aggregates = (
            "COALESCE(AVG(slippage_bps), 0.0) as avg_bps, "
            "COALESCE(MAX(slippage_bps), 0.0) as max_bps, "
            "COALESCE(SUM(slippage_usd), 0.0) as total_usd, "
            "COUNT(*) as count"
        )

        if exchange:
            cursor.execute(
                f"SELECT {aggregates} FROM slippage WHERE exchange = ?", (exchange,)
            )
        else:
            cursor.execute(f"SELECT {aggregates} FROM slippage")

        return dict(cursor.fetchone() or {})

    def get_metrics_history(self, hours: int = 24, interval_minutes: int = 60) -> List[Dict]:
        """Get metrics history."""
        cursor = self.connection.cursor()

        query = """
            SELECT * FROM metrics
            WHERE created_at >= datetime('now', '-' || ? || ' hours')
            ORDER BY timestamp DESC
        """

        cursor.execute(query, (hours,))
        return [dict(row) for row in cursor.fetchall()]

    def get_trade_statistics(self) -> Dict:
        """Get overall trade statistics."""
        cursor = self.connection.cursor()

        # COALESCE throughout: on an empty table SUM/AVG/MAX/MIN return NULL,
        # which reached callers as None and raised TypeError the moment anyone
        # formatted it (f"${stats['total_pnl']:.2f}").
        cursor.execute("""
            SELECT
                COUNT(*) as total_trades,
                COALESCE(SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END), 0) as winning_trades,
                COALESCE(SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END), 0) as losing_trades,
                COALESCE(SUM(total_pnl), 0.0) as total_pnl,
                COALESCE(AVG(total_pnl), 0.0) as avg_pnl,
                COALESCE(MAX(total_pnl), 0.0) as max_pnl,
                COALESCE(MIN(total_pnl), 0.0) as min_pnl,
                COALESCE(AVG(duration_seconds), 0.0) as avg_duration_s
            FROM trades
            WHERE exit_time IS NOT NULL
        """)

        result = dict(cursor.fetchone() or {})
        total = result.get("total_trades", 0)
        result["win_rate"] = (result["winning_trades"] / total) if total else 0.0
        return result

    def cleanup_old_data(self, days: int = 90):
        """Delete old data to manage database size."""
        cursor = self.connection.cursor()

        cursor.execute(
            "DELETE FROM trades WHERE exit_time < datetime('now', '-' || ? || ' days')",
            (days,)
        )
        cursor.execute(
            "DELETE FROM funding WHERE timestamp < datetime('now', '-' || ? || ' days')",
            (days,)
        )
        cursor.execute(
            "DELETE FROM metrics WHERE timestamp < datetime('now', '-' || ? || ' days')",
            (days,)
        )

        self.connection.commit()
        logger.info(f"Cleaned up data older than {days} days")
