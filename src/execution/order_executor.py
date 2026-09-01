import asyncio
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Dict, List
from enum import Enum

from src.utils.logger import setup_logger

logger = setup_logger("order_executor")


class OrderSide(Enum):
    """Order side (long or short)."""
    LONG = "BUY"
    SHORT = "SELL"


class OrderStatus(Enum):
    """Order lifecycle status."""
    PENDING = "pending"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class Order:
    """Represents a single order."""
    order_id: str
    exchange: str
    symbol: str
    side: OrderSide
    quantity: float
    price: Optional[float]  # None for market orders
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: float = 0.0
    filled_price: float = 0.0
    timestamp: float = field(default_factory=time.time)
    execution_time_ms: Optional[float] = None

    def __repr__(self):
        return (f"Order({self.exchange} {self.side.value} {self.quantity} {self.symbol} "
                f"@ {self.price}, status={self.status.value})")


@dataclass
class DualExchangeOrder:
    """Represents a coordinated pair of orders (one per exchange)."""
    arb_id: str
    bybit_order: Order
    binance_order: Order
    status: str = "pending"  # pending, partial, filled, failed, rollback
    timestamp: float = field(default_factory=time.time)

    def __repr__(self):
        return (f"DualOrder({self.arb_id[:8]}... "
                f"Bybit={self.bybit_order.status.value} "
                f"Binance={self.binance_order.status.value})")


class ExchangeExecutor(ABC):
    """Base executor for exchange-specific order placement."""

    def __init__(self, exchange: str):
        self.exchange = exchange
        self.pending_orders: Dict[str, Order] = {}

    @abstractmethod
    async def place_order(self, symbol: str, side: OrderSide, quantity: float,
                         price: Optional[float] = None) -> Optional[Order]:
        """Place order on exchange. Returns Order if successful, None if failed."""
        pass

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel order. Returns True if successful."""
        pass

    @abstractmethod
    async def get_order_status(self, order_id: str) -> Optional[Order]:
        """Fetch current order status from exchange."""
        pass

    async def _simulate_execution(self, order: Order, execution_latency_ms: float = 100) -> Order:
        """Simulate order execution (for paper trading)."""
        await asyncio.sleep(execution_latency_ms / 1000)
        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.execution_time_ms = execution_latency_ms
        return order


class BybitExecutor(ExchangeExecutor):
    """Bybit order executor (paper trading for now)."""

    def __init__(self):
        super().__init__("Bybit")

    async def place_order(self, symbol: str, side: OrderSide, quantity: float,
                         price: Optional[float] = None) -> Optional[Order]:
        """Place market order on Bybit (paper trading)."""
        try:
            order = Order(
                order_id=f"bybit_{uuid.uuid4().hex[:12]}",
                exchange="Bybit",
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
            )

            logger.info(f"[Bybit] Placing {side.value} order: {quantity} {symbol}")

            # Paper trading: simulate execution
            order = await self._simulate_execution(order, execution_latency_ms=150)

            self.pending_orders[order.order_id] = order
            logger.info(f"[Bybit] Order filled: {order.order_id}")

            return order
        except Exception as e:
            logger.error(f"[Bybit] Failed to place order: {e}")
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel order on Bybit."""
        try:
            if order_id in self.pending_orders:
                order = self.pending_orders[order_id]
                if order.status != OrderStatus.FILLED:
                    order.status = OrderStatus.CANCELLED
                    logger.info(f"[Bybit] Order cancelled: {order_id}")
                    return True
            return False
        except Exception as e:
            logger.error(f"[Bybit] Failed to cancel order: {e}")
            return False

    async def get_order_status(self, order_id: str) -> Optional[Order]:
        """Get order status from Bybit."""
        return self.pending_orders.get(order_id)


class BinanceExecutor(ExchangeExecutor):
    """Binance order executor (paper trading for now)."""

    def __init__(self):
        super().__init__("Binance")

    async def place_order(self, symbol: str, side: OrderSide, quantity: float,
                         price: Optional[float] = None) -> Optional[Order]:
        """Place market order on Binance (paper trading)."""
        try:
            order = Order(
                order_id=f"binance_{uuid.uuid4().hex[:12]}",
                exchange="Binance",
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
            )

            logger.info(f"[Binance] Placing {side.value} order: {quantity} {symbol}")

            # Paper trading: simulate execution with slight delay variance
            order = await self._simulate_execution(order, execution_latency_ms=200)

            self.pending_orders[order.order_id] = order
            logger.info(f"[Binance] Order filled: {order.order_id}")

            return order
        except Exception as e:
            logger.error(f"[Binance] Failed to place order: {e}")
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel order on Binance."""
        try:
            if order_id in self.pending_orders:
                order = self.pending_orders[order_id]
                if order.status != OrderStatus.FILLED:
                    order.status = OrderStatus.CANCELLED
                    logger.info(f"[Binance] Order cancelled: {order_id}")
                    return True
            return False
        except Exception as e:
            logger.error(f"[Binance] Failed to cancel order: {e}")
            return False

    async def get_order_status(self, order_id: str) -> Optional[Order]:
        """Get order status from Binance."""
        return self.pending_orders.get(order_id)


class OrderExecutor:
    """Coordinates dual-exchange order placement for arbitrage."""

    def __init__(self):
        self.bybit = BybitExecutor()
        self.binance = BinanceExecutor()
        self.dual_orders: Dict[str, DualExchangeOrder] = {}
        self.failed_orders: List[DualExchangeOrder] = []

    async def place_dual_order(self, symbol: str, bybit_side: OrderSide, binance_side: OrderSide,
                              quantity: float, timeout_ms: float = 5000) -> Optional[DualExchangeOrder]:
        """
        Place simultaneous orders on both exchanges (atomic operation).
        If one fails, rollback the other.

        Args:
            symbol: Trading pair (e.g., "BTCUSDT")
            bybit_side: LONG or SHORT
            binance_side: LONG or SHORT
            quantity: Amount to trade
            timeout_ms: Max time to wait for both orders

        Returns:
            DualExchangeOrder if both succeed, None if either fails
        """
        arb_id = f"arb_{uuid.uuid4().hex[:12]}"
        logger.info(f"[{arb_id}] Placing dual order: {symbol} "
                   f"({bybit_side.value}/{binance_side.value}) x{quantity}")

        try:
            # Place both orders concurrently
            bybit_task = asyncio.create_task(
                self.bybit.place_order(symbol, bybit_side, quantity)
            )
            binance_task = asyncio.create_task(
                self.binance.place_order(symbol, binance_side, quantity)
            )

            # Wait for both with timeout
            bybit_order, binance_order = await asyncio.wait_for(
                asyncio.gather(bybit_task, binance_task),
                timeout=timeout_ms / 1000
            )

            # Check if both succeeded
            if bybit_order and binance_order:
                dual_order = DualExchangeOrder(
                    arb_id=arb_id,
                    bybit_order=bybit_order,
                    binance_order=binance_order,
                    status="filled"
                )
                self.dual_orders[arb_id] = dual_order
                logger.info(f"[{arb_id}] DUAL ORDER FILLED ✓")
                return dual_order
            else:
                # One failed, rollback the other
                logger.error(f"[{arb_id}] One order failed, rolling back")
                if bybit_order:
                    await self.bybit.cancel_order(bybit_order.order_id)
                if binance_order:
                    await self.binance.cancel_order(binance_order.order_id)

                failed_order = DualExchangeOrder(
                    arb_id=arb_id,
                    bybit_order=bybit_order or Order(
                        order_id="cancelled",
                        exchange="Bybit",
                        symbol=symbol,
                        side=bybit_side,
                        quantity=quantity,
                        price=None,
                        status=OrderStatus.FAILED
                    ),
                    binance_order=binance_order or Order(
                        order_id="cancelled",
                        exchange="Binance",
                        symbol=symbol,
                        side=binance_side,
                        quantity=quantity,
                        price=None,
                        status=OrderStatus.FAILED
                    ),
                    status="failed"
                )
                self.failed_orders.append(failed_order)
                return None

        except asyncio.TimeoutError:
            logger.error(f"[{arb_id}] Dual order timeout, rolling back")
            return None
        except Exception as e:
            logger.error(f"[{arb_id}] Unexpected error: {e}")
            return None

    async def close_dual_position(self, dual_order: DualExchangeOrder) -> bool:
        """Close both positions from a dual order (exit arbitrage)."""
        try:
            arb_id = dual_order.arb_id
            logger.info(f"[{arb_id}] Closing dual position")

            # Close with opposite sides
            bybit_close_side = OrderSide.SHORT if dual_order.bybit_order.side == OrderSide.LONG else OrderSide.LONG
            binance_close_side = OrderSide.SHORT if dual_order.binance_order.side == OrderSide.LONG else OrderSide.LONG

            bybit_close = await self.bybit.place_order(
                dual_order.bybit_order.symbol,
                bybit_close_side,
                dual_order.bybit_order.filled_quantity
            )
            binance_close = await self.binance.place_order(
                dual_order.binance_order.symbol,
                binance_close_side,
                dual_order.binance_order.filled_quantity
            )

            if bybit_close and binance_close:
                logger.info(f"[{arb_id}] Dual position CLOSED ✓")
                return True
            else:
                logger.error(f"[{arb_id}] Failed to close position")
                return False

        except Exception as e:
            logger.error(f"Error closing position: {e}")
            return False

    def get_order_history(self, limit: int = 100) -> Dict:
        """Get recent order history."""
        return {
            "successful": list(self.dual_orders.values())[-limit:],
            "failed": list(self.failed_orders)[-limit:],
        }
