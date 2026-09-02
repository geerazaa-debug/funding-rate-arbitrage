from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional


@dataclass
class TradeRecord:
    """Database record for a completed trade."""
    id: Optional[int] = None
    symbol: str = ""
    entry_time: datetime = None
    exit_time: datetime = None
    entry_price: float = 0.0
    exit_price: float = 0.0
    quantity: float = 0.0
    bybit_entry_price: float = 0.0
    binance_entry_price: float = 0.0
    bybit_exit_price: float = 0.0
    binance_exit_price: float = 0.0
    realized_pnl: float = 0.0
    funding_earned: float = 0.0
    total_pnl: float = 0.0
    duration_seconds: int = 0
    entry_differential: float = 0.0
    exit_differential: float = 0.0

    def to_dict(self):
        return asdict(self)


@dataclass
class PositionRecord:
    """Database record for an open position."""
    id: Optional[int] = None
    symbol: str = ""
    exchange: str = ""
    side: str = ""
    quantity: float = 0.0
    entry_price: float = 0.0
    entry_time: datetime = None
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    entry_differential: float = 0.0
    last_update: datetime = None

    def to_dict(self):
        return asdict(self)


@dataclass
class FundingRecord:
    """Database record for funding payment."""
    id: Optional[int] = None
    symbol: str = ""
    exchange: str = ""
    timestamp: datetime = None
    funding_rate: float = 0.0
    position_size: float = 0.0
    amount: float = 0.0

    def to_dict(self):
        return asdict(self)


@dataclass
class SlippageRecord:
    """Database record for trade slippage."""
    id: Optional[int] = None
    symbol: str = ""
    exchange: str = ""
    timestamp: datetime = None
    expected_price: float = 0.0
    actual_price: float = 0.0
    quantity: float = 0.0
    side: str = ""
    slippage_bps: float = 0.0
    slippage_usd: float = 0.0

    def to_dict(self):
        return asdict(self)


@dataclass
class MetricsSnapshot:
    """Database record for performance metrics snapshot."""
    id: Optional[int] = None
    timestamp: datetime = None
    capital: float = 0.0
    total_exposure: float = 0.0
    open_positions: int = 0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    total_pnl: float = 0.0
    return_pct: float = 0.0
    win_rate: float = 0.0
    avg_trade_pnl: float = 0.0
    daily_loss: float = 0.0
    max_drawdown: float = 0.0

    def to_dict(self):
        return asdict(self)


@dataclass
class AlertRecord:
    """Database record for alerts sent."""
    id: Optional[int] = None
    timestamp: datetime = None
    alert_type: str = ""
    alert_level: str = ""
    title: str = ""
    message: str = ""
    services: str = ""  # Comma-separated list

    def to_dict(self):
        return asdict(self)
