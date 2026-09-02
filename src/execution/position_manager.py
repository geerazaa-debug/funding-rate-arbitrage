import time
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple
from collections import defaultdict

from src.utils.logger import setup_logger
from src.execution.order_executor import DualExchangeOrder, OrderSide

logger = setup_logger("position_manager")


@dataclass
class Position:
    """Represents an open position on an exchange."""
    exchange: str
    symbol: str
    side: OrderSide
    quantity: float
    entry_price: float
    entry_time: float
    cost_basis: float = field(init=False)
    current_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None

    def __post_init__(self):
        self.cost_basis = self.quantity * self.entry_price

    def update_price(self, current_price: float):
        """Update position with current market price."""
        self.current_price = current_price
        if self.side == OrderSide.LONG:
            self.unrealized_pnl = (current_price - self.entry_price) * self.quantity
        else:  # SHORT
            self.unrealized_pnl = (self.entry_price - current_price) * self.quantity

    def __repr__(self):
        pnl_str = f"${self.unrealized_pnl:.2f}" if self.unrealized_pnl is not None else "N/A"
        return (f"Position({self.exchange} {self.side.value} {self.quantity} {self.symbol} "
                f"@ {self.entry_price:.2f}, PnL={pnl_str})")


@dataclass
class Trade:
    """Represents a completed trade (open + close)."""
    trade_id: str
    symbol: str
    entry_time: float
    exit_time: Optional[float]
    entry_price: float
    exit_price: Optional[float]
    quantity: float
    realized_pnl: Optional[float] = None
    duration_seconds: Optional[float] = None
    funding_earned: float = 0.0

    def finalize(self, exit_price: float, exit_time: float, funding_earned: float = 0.0):
        """Finalize trade with exit information."""
        self.exit_price = exit_price
        self.exit_time = exit_time
        self.realized_pnl = (exit_price - self.entry_price) * self.quantity + funding_earned
        self.duration_seconds = exit_time - self.entry_time
        self.funding_earned = funding_earned

    def __repr__(self):
        pnl_str = f"${self.realized_pnl:.2f}" if self.realized_pnl else "N/A"
        return f"Trade({self.symbol} {self.quantity} {self.entry_price:.2f}->{self.exit_price}, PnL={pnl_str})"


class PositionManager:
    """Tracks all open positions and trades across both exchanges."""

    def __init__(self):
        # Current positions: symbol -> {exchange -> Position}
        self.positions: Dict[str, Dict[str, Position]] = defaultdict(dict)

        # Completed trades
        self.trades: List[Trade] = []

        # Dual order tracking
        self.dual_positions: Dict[str, DualExchangeOrder] = {}

        logger.info("Initialized position manager")

    @staticmethod
    def _coerce_side(side) -> OrderSide:
        """
        Normalise a side to OrderSide.

        Callers have historically passed both the enum and bare strings ("LONG").
        Storing whichever arrived made `position.side == OrderSide.LONG` silently
        false for string callers, which inverted close-side decisions downstream.
        Normalising at the boundary makes that class of bug impossible.
        """
        if isinstance(side, OrderSide):
            return side
        normalized = str(side).strip().upper()
        if normalized in ("LONG", "BUY", "ORDERSIDE.LONG"):
            return OrderSide.LONG
        if normalized in ("SHORT", "SELL", "ORDERSIDE.SHORT"):
            return OrderSide.SHORT
        raise ValueError(f"Unrecognised position side: {side!r}")

    def add_position(self, exchange: str, symbol: str, side: OrderSide,
                    quantity: float, entry_price: float) -> Position:
        """Create and track a new position."""
        position = Position(
            exchange=exchange,
            symbol=symbol,
            side=self._coerce_side(side),
            quantity=quantity,
            entry_price=entry_price,
            entry_time=time.time(),
        )

        self.positions[symbol][exchange] = position
        logger.info(f"[{symbol}] New position on {exchange}: {position}")
        return position

    def close_position(self, symbol: str, exchange: str, exit_price: float,
                      funding_earned: float = 0.0) -> Optional[Trade]:
        """Close a position and record the trade."""
        if symbol not in self.positions or exchange not in self.positions[symbol]:
            logger.error(f"Position not found: {symbol} on {exchange}")
            return None

        position = self.positions[symbol].pop(exchange)
        exit_time = time.time()

        trade = Trade(
            trade_id=f"trade_{symbol}_{int(exit_time * 1000)}",
            symbol=symbol,
            entry_time=position.entry_time,
            exit_time=None,
            entry_price=position.entry_price,
            exit_price=None,
            quantity=position.quantity,
        )

        trade.finalize(exit_price, exit_time, funding_earned)
        self.trades.append(trade)

        logger.info(f"[{symbol}] Position closed on {exchange}: {trade}")
        return trade

    def register_dual_order(self, dual_order: DualExchangeOrder):
        """Register a dual-exchange order."""
        self.dual_positions[dual_order.arb_id] = dual_order

        # Add positions for both exchanges
        self.add_position(
            "Bybit",
            dual_order.bybit_order.symbol,
            dual_order.bybit_order.side,
            dual_order.bybit_order.filled_quantity,
            dual_order.bybit_order.filled_price if dual_order.bybit_order.filled_price else 0
        )

        self.add_position(
            "Binance",
            dual_order.binance_order.symbol,
            dual_order.binance_order.side,
            dual_order.binance_order.filled_quantity,
            dual_order.binance_order.filled_price if dual_order.binance_order.filled_price else 0
        )

    def close_dual_order(self, arb_id: str, exit_prices: Dict[str, float],
                        funding_earned: float = 0.0) -> Tuple[Optional[Trade], Optional[Trade]]:
        """Close both positions from a dual order."""
        if arb_id not in self.dual_positions:
            logger.error(f"Dual order not found: {arb_id}")
            return None, None

        dual_order = self.dual_positions.pop(arb_id)
        symbol = dual_order.bybit_order.symbol

        bybit_trade = self.close_position(
            symbol, "Bybit",
            exit_prices.get("bybit", 0),
            funding_earned / 2
        )

        binance_trade = self.close_position(
            symbol, "Binance",
            exit_prices.get("binance", 0),
            funding_earned / 2
        )

        logger.info(f"[{arb_id}] Dual position closed: Bybit={bybit_trade}, Binance={binance_trade}")
        return bybit_trade, binance_trade

    def update_prices(self, symbol: str, prices: Dict[str, float]):
        """Update prices for all positions of a symbol."""
        if symbol in self.positions:
            for exchange, price in prices.items():
                if exchange in self.positions[symbol]:
                    self.positions[symbol][exchange].update_price(price)

    def get_position(self, symbol: str, exchange: str) -> Optional[Position]:
        """Get specific position."""
        return self.positions.get(symbol, {}).get(exchange)

    def get_symbol_positions(self, symbol: str) -> Dict[str, Position]:
        """Get all positions for a symbol."""
        return self.positions.get(symbol, {})

    def get_all_positions(self) -> Dict[str, Dict[str, Position]]:
        """Get all open positions."""
        return dict(self.positions)

    def get_net_delta(self, symbol: str) -> float:
        """
        Get net delta for a symbol (should be ~0 for delta-neutral).
        Positive = net long, Negative = net short
        """
        if symbol not in self.positions:
            return 0.0

        net = 0.0
        for position in self.positions[symbol].values():
            if position.side == OrderSide.LONG:
                net += position.quantity
            else:
                net -= position.quantity

        return net

    def get_total_exposure(self, symbol: str) -> float:
        """Get total notional exposure for a symbol."""
        if symbol not in self.positions:
            return 0.0

        total = 0.0
        for position in self.positions[symbol].values():
            total += abs(position.cost_basis)

        return total

    def get_unrealized_pnl(self, symbol: str) -> float:
        """Get total unrealized P&L for a symbol."""
        if symbol not in self.positions:
            return 0.0

        total = 0.0
        for position in self.positions[symbol].values():
            if position.unrealized_pnl is not None:
                total += position.unrealized_pnl

        return total

    def get_realized_pnl(self, limit: Optional[int] = None) -> float:
        """Get total realized P&L from closed trades."""
        trades = self.trades[-limit:] if limit else self.trades
        return sum(t.realized_pnl for t in trades if t.realized_pnl)

    def get_trade_statistics(self) -> Dict:
        """Calculate trade statistics."""
        if not self.trades:
            return {
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate": 0.0,
                "avg_pnl": 0.0,
                "total_realized_pnl": 0.0,
                "avg_duration_s": 0.0,
            }

        winning = sum(1 for t in self.trades if t.realized_pnl and t.realized_pnl > 0)
        losing = sum(1 for t in self.trades if t.realized_pnl and t.realized_pnl <= 0)
        total_pnl = sum(t.realized_pnl for t in self.trades if t.realized_pnl)
        durations = [t.duration_seconds for t in self.trades if t.duration_seconds]

        return {
            "total_trades": len(self.trades),
            "winning_trades": winning,
            "losing_trades": losing,
            "win_rate": winning / len(self.trades) if self.trades else 0.0,
            "avg_pnl": total_pnl / len(self.trades) if self.trades else 0.0,
            "total_realized_pnl": total_pnl,
            "avg_duration_s": sum(durations) / len(durations) if durations else 0.0,
        }

    def __repr__(self):
        symbol_count = len(self.positions)
        position_count = sum(len(pos) for pos in self.positions.values())
        return f"PositionManager({position_count} positions across {symbol_count} symbols, {len(self.trades)} trades)"
