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
        # Legs that filled and could not be flattened. Non-empty means real,
        # unhedged directional exposure that needs manual attention.
        self.naked_positions: List[DualExchangeOrder] = []

    # Sentinel for a leg whose outcome could not be determined. Distinct from
    # None (a clean failure) because an unknown leg may be live on the exchange.
    UNKNOWN = object()

    # Extra time allowed for a leg that missed the timeout to report its outcome,
    # before it is declared unresolved and escalated for reconciliation.
    GRACE_PERIOD_S = 3.0

    @staticmethod
    def _opposite(side: OrderSide) -> OrderSide:
        """The side that closes a position opened with `side`."""
        return OrderSide.SHORT if side is OrderSide.LONG else OrderSide.LONG

    async def _flatten_leg(self, executor: "ExchangeExecutor", arb_id: str, symbol: str,
                           order: Order) -> bool:
        """
        Close a leg that filled while its counterpart did not.

        A market order cannot be cancelled - it has already executed - so the only
        way to undo it is to trade out of it. The previous implementation called
        cancel_order here, which no-ops on a FILLED order and silently left a
        naked directional position that nothing tracked.
        """
        close_side = self._opposite(order.side)
        filled = order.filled_quantity or order.quantity

        logger.warning(
            f"[{arb_id}] Flattening orphaned {order.exchange} leg: "
            f"{close_side.value} {filled} {symbol}"
        )
        try:
            closing = await executor.place_order(symbol, close_side, filled)
        except Exception as e:
            logger.critical(
                f"[{arb_id}] FLATTEN RAISED on {order.exchange}: {e} - "
                f"NAKED {order.side.value} {filled} {symbol} REQUIRES MANUAL CLOSE",
                exc_info=True,
            )
            return False

        if not closing:
            logger.critical(
                f"[{arb_id}] FLATTEN REJECTED on {order.exchange} - "
                f"NAKED {order.side.value} {filled} {symbol} REQUIRES MANUAL CLOSE"
            )
            return False

        logger.info(f"[{arb_id}] {order.exchange} leg flattened: {closing.order_id}")
        return True

    async def place_dual_order(self, symbol: str, bybit_side: OrderSide, binance_side: OrderSide,
                              quantity: float, timeout_ms: float = 5000) -> Optional[DualExchangeOrder]:
        """
        Open both legs of an arbitrage concurrently.

        These are market orders, so true atomicity is not available: a leg that
        reaches the exchange fills immediately and cannot be recalled. When one
        leg succeeds and the other does not, the successful leg is therefore
        FLATTENED - traded back out - rather than "cancelled". If the flatten
        itself fails the position is genuinely naked, and that is logged at
        CRITICAL and recorded on the report rather than being swallowed.

        Returns a DualExchangeOrder only when both legs opened. Otherwise returns
        None, having attempted to leave the book flat.
        """
        arb_id = f"arb_{uuid.uuid4().hex[:12]}"
        logger.info(f"[{arb_id}] Placing dual order: {symbol} "
                   f"({bybit_side.value}/{binance_side.value}) x{quantity}")

        bybit_task = asyncio.create_task(self.bybit.place_order(symbol, bybit_side, quantity))
        binance_task = asyncio.create_task(self.binance.place_order(symbol, binance_side, quantity))

        # asyncio.wait (not wait_for) because it leaves unfinished tasks running.
        # Once a place_order request is on the wire the exchange may fill it;
        # cancelling the coroutine does not recall the order, so abandoning a slow
        # leg would create exactly the untracked fill we are trying to prevent.
        _, pending = await asyncio.wait(
            {bybit_task, binance_task}, timeout=timeout_ms / 1000
        )

        if pending:
            logger.error(
                f"[{arb_id}] {len(pending)} leg(s) still open after {timeout_ms}ms; "
                f"waiting {self.GRACE_PERIOD_S}s to learn the outcome"
            )
            _, still_pending = await asyncio.wait(pending, timeout=self.GRACE_PERIOD_S)
            for task in still_pending:
                task.cancel()

        def unwrap(task: asyncio.Task, exchange: str):
            """Return the Order, None for a clean failure, or UNKNOWN if unresolved."""
            if not task.done() or task.cancelled():
                logger.critical(
                    f"[{arb_id}] {exchange} leg UNRESOLVED - an order may be live "
                    f"on the exchange. Reconciliation required."
                )
                return self.UNKNOWN
            exc = task.exception()
            if exc is not None:
                logger.error(f"[{arb_id}] {exchange} leg raised: {exc!r}")
                return None
            return task.result()

        bybit_order = unwrap(bybit_task, "Bybit")
        binance_order = unwrap(binance_task, "Binance")

        # An unresolved leg is not a flat leg. Flatten whatever is known-live and
        # surface the unknown one for reconciliation instead of assuming it away.
        if self.UNKNOWN in (bybit_order, binance_order):
            known_bybit = None if bybit_order is self.UNKNOWN else bybit_order
            known_binance = None if binance_order is self.UNKNOWN else binance_order

            if known_bybit:
                await self._flatten_leg(self.bybit, arb_id, symbol, known_bybit)
            if known_binance:
                await self._flatten_leg(self.binance, arb_id, symbol, known_binance)

            self._record_failure(arb_id, symbol, bybit_side, binance_side, quantity,
                                 known_bybit, known_binance, naked=True)
            return None

        if bybit_order and binance_order:
            dual_order = DualExchangeOrder(
                arb_id=arb_id,
                bybit_order=bybit_order,
                binance_order=binance_order,
                status="filled",
            )
            self.dual_orders[arb_id] = dual_order
            logger.info(f"[{arb_id}] DUAL ORDER FILLED [OK]")
            return dual_order

        if not bybit_order and not binance_order:
            logger.error(f"[{arb_id}] Both legs failed; book is flat, nothing to unwind")
            self._record_failure(arb_id, symbol, bybit_side, binance_side, quantity,
                                 bybit_order, binance_order, naked=False)
            return None

        # Exactly one leg is live. Trade out of it.
        if bybit_order:
            flattened = await self._flatten_leg(self.bybit, arb_id, symbol, bybit_order)
        else:
            flattened = await self._flatten_leg(self.binance, arb_id, symbol, binance_order)

        self._record_failure(arb_id, symbol, bybit_side, binance_side, quantity,
                             bybit_order, binance_order, naked=not flattened)
        return None

    def _record_failure(self, arb_id: str, symbol: str, bybit_side: OrderSide,
                        binance_side: OrderSide, quantity: float,
                        bybit_order: Optional[Order], binance_order: Optional[Order],
                        naked: bool):
        """Record a failed dual order, flagging any position left open."""
        def placeholder(exchange: str, side: OrderSide) -> Order:
            return Order(
                order_id="not_placed",
                exchange=exchange,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=None,
                status=OrderStatus.FAILED,
            )

        failed = DualExchangeOrder(
            arb_id=arb_id,
            bybit_order=bybit_order or placeholder("Bybit", bybit_side),
            binance_order=binance_order or placeholder("Binance", binance_side),
            status="naked" if naked else "failed",
        )
        self.failed_orders.append(failed)
        if naked:
            self.naked_positions.append(failed)

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
                logger.info(f"[{arb_id}] Dual position CLOSED [OK]")
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
