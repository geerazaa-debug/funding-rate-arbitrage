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

        # Positions table
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        """Insert an open position record."""
        cursor = self.connection.cursor()
        data = position.to_dict()
        data.pop('id', None)

        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?" for _ in data])
        query = f"INSERT INTO positions ({cols}) VALUES ({placeholders})"

        cursor.execute(query, tuple(data.values()))
        self.connection.commit()

        logger.debug(f"Position inserted: {position.symbol} on {position.exchange}")
        return cursor.lastrowid

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

        if exchange:
            cursor.execute(
                "SELECT AVG(slippage_bps) as avg_bps, MAX(slippage_bps) as max_bps, "
                "SUM(slippage_usd) as total_usd, COUNT(*) as count FROM slippage WHERE exchange = ?",
                (exchange,)
            )
        else:
            cursor.execute(
                "SELECT AVG(slippage_bps) as avg_bps, MAX(slippage_bps) as max_bps, "
                "SUM(slippage_usd) as total_usd, COUNT(*) as count FROM slippage"
            )

        result = cursor.fetchone()
        return dict(result) if result else {}

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

        cursor.execute("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) as losing_trades,
                SUM(total_pnl) as total_pnl,
                AVG(total_pnl) as avg_pnl,
                MAX(total_pnl) as max_pnl,
                MIN(total_pnl) as min_pnl,
                AVG(duration_seconds) as avg_duration_s
            FROM trades
            WHERE exit_time IS NOT NULL
        """)

        result = cursor.fetchone()
        return dict(result) if result else {}

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
