import asyncio
import time
from typing import Dict, List, Optional, Tuple
from enum import Enum
from dataclasses import dataclass

from src.utils.logger import setup_logger
from src.execution.position_manager import PositionManager
from src.execution.exchange_api_client import LiveExchangeHub
from src.execution.order_executor import OrderSide

logger = setup_logger("emergency_exit")


class EmergencyReason(Enum):
    """Reasons for emergency exit."""
    MANUAL_TRIGGER = "manual_trigger"
    CIRCUIT_BREAKER = "circuit_breaker"
    CONNECTION_LOST = "connection_lost"
    EXCHANGE_ERROR = "exchange_error"
    POSITION_MISMATCH = "position_mismatch"
    MARKET_HALT = "market_halt"
    CRITICAL_ERROR = "critical_error"


@dataclass
class ExitOrder:
    """Represents a single position exit order."""
    symbol: str
    exchange: str
    side: str  # LONG -> SELL, SHORT -> BUY
    quantity: float
    order_id: Optional[str] = None
    status: str = "pending"  # pending, submitted, filled, failed
    reason: Optional[EmergencyReason] = None
    timestamp: float = 0.0
    exit_price: Optional[float] = None
    error: Optional[str] = None


@dataclass
class EmergencyExitReport:
    """Report on emergency exit execution."""
    reason: EmergencyReason
    triggered_at: float
    completed_at: Optional[float] = None
    orders: List[ExitOrder] = None
    total_realized_pnl: float = 0.0
    total_funding_earned: float = 0.0
    total_loss: float = 0.0
    success: bool = False
    details: str = ""
    # Legs present when the exit was triggered. Guards against an empty orders
    # list being read as a vacuous success.
    expected_legs: int = 0
    # Set by wait_for_exits once fills are confirmed; distinct from `success`,
    # which only reports whether every leg was accepted by the exchange.
    all_filled: bool = False

    def __post_init__(self):
        if self.orders is None:
            self.orders = []


class EmergencyExitHandler:
    """Handles emergency position exits when critical conditions detected."""

    def __init__(self, position_manager: PositionManager, exchange_hub: LiveExchangeHub):
        self.position_manager = position_manager
        self.exchange_hub = exchange_hub

        self.exit_reports: List[EmergencyExitReport] = []
        self.is_exiting = False

        logger.info("Initialized emergency exit handler")

    async def trigger_exit(self, reason: EmergencyReason, details: str = "") -> Optional[EmergencyExitReport]:
        """
        Trigger emergency exit of all positions.

        Args:
            reason: Why exit is being triggered
            details: Additional details about the emergency

        Returns:
            EmergencyExitReport with exit details
        """
        if self.is_exiting:
            logger.warning("Exit already in progress, skipping")
            return None

        self.is_exiting = True
        logger.critical(f"EMERGENCY EXIT TRIGGERED: {reason.value} - {details}")

        report = EmergencyExitReport(
            reason=reason,
            triggered_at=time.time(),
            details=details,
        )

        try:
            # Snapshot the legs up front. Position state is mutated as legs are
            # closed, so we must not iterate the live structure, and we need the
            # expected count to tell "nothing to do" apart from "everything blew up".
            legs = [
                (symbol, exchange, position)
                for symbol, exchanges in self.position_manager.get_all_positions().items()
                for exchange, position in exchanges.items()
            ]
            report.expected_legs = len(legs)

            if not legs:
                logger.info("No positions to exit")
                report.completed_at = time.time()
                report.success = True
                self.exit_reports.append(report)
                return report

            logger.info(f"Exiting {len(legs)} position legs")

            for symbol, exchange, position in legs:
                exit_order = await self._exit_position(
                    symbol, exchange, position.quantity, position.side, reason
                )
                report.orders.append(exit_order)

            report.completed_at = time.time()
            report.success = self._evaluate_success(report)

            if report.success:
                logger.info(f"Emergency exit: all {report.expected_legs} legs submitted")
            else:
                failed = [o for o in report.orders if o.status == "failed"]
                logger.critical(
                    f"EMERGENCY EXIT INCOMPLETE: {len(failed)}/{report.expected_legs} "
                    f"legs failed to submit - MANUAL INTERVENTION REQUIRED"
                )
            self.exit_reports.append(report)

            return report

        except Exception as e:
            logger.critical(f"Emergency exit error: {e}", exc_info=True)
            report.details = f"{report.details} | Error: {e}".strip(" |")
            report.completed_at = time.time()
            report.success = False
            self.exit_reports.append(report)
            return report

        finally:
            self.is_exiting = False

    @staticmethod
    def _evaluate_success(report: "EmergencyExitReport") -> bool:
        """
        An exit succeeds only when every expected leg produced a submitted order.

        Deliberately does NOT require fills: fills resolve asynchronously via
        wait_for_exits, so requiring them here would report failure on the happy
        path. An empty order list against a non-zero expected_legs is a failure,
        never a vacuous success.
        """
        if report.expected_legs == 0:
            return True
        if len(report.orders) != report.expected_legs:
            return False
        return all(o.status != "failed" for o in report.orders)

    @staticmethod
    def _exit_side_for(side) -> OrderSide:
        """
        Map an open position's side to the side that CLOSES it.

        LONG -> SELL (OrderSide.SHORT), SHORT -> BUY (OrderSide.LONG).
        Accepts the OrderSide enum or its string name so a caller passing "LONG"
        cannot silently fall through to the wrong branch.
        """
        if isinstance(side, OrderSide):
            return OrderSide.SHORT if side is OrderSide.LONG else OrderSide.LONG
        normalized = str(side).strip().upper()
        if normalized in ("LONG", "BUY", "ORDERSIDE.LONG"):
            return OrderSide.SHORT
        if normalized in ("SHORT", "SELL", "ORDERSIDE.SHORT"):
            return OrderSide.LONG
        raise ValueError(f"Unrecognised position side: {side!r}")

    async def _exit_position(self, symbol: str, exchange: str, quantity: float,
                            side, reason: EmergencyReason) -> ExitOrder:
        """
        Exit a single position leg.

        Always returns an ExitOrder - never None and never swallows an exception
        into a missing record, because a dropped leg would make the exit look
        complete when a position is still open.
        """
        try:
            exit_side = self._exit_side_for(side)
        except ValueError as e:
            logger.critical(f"[{symbol}] Cannot determine exit side on {exchange}: {e}")
            return ExitOrder(
                symbol=symbol, exchange=exchange, side="UNKNOWN", quantity=quantity,
                status="failed", reason=reason, timestamp=time.time(),
                error=str(e),
            )

        logger.info(f"[{symbol}] Emergency exit on {exchange}: "
                    f"{exit_side.value} {quantity} (position was {side})")

        try:
            # reduce_only guarantees the order can only shrink an existing position.
            # Without it a stale quantity would open fresh exposure the wrong way.
            if exchange == "Bybit":
                order = await self.exchange_hub.bybit.place_order(
                    symbol, exit_side.value, quantity,
                    order_type="Market", reduce_only=True
                )
            else:
                order = await self.exchange_hub.binance.place_order(
                    symbol, exit_side.value, quantity,
                    order_type="MARKET", reduce_only=True
                )
        except Exception as e:
            logger.critical(f"[{symbol}] Exit order raised on {exchange}: {e}", exc_info=True)
            return ExitOrder(
                symbol=symbol, exchange=exchange, side=exit_side.value, quantity=quantity,
                status="failed", reason=reason, timestamp=time.time(),
                error=repr(e),
            )

        if not order:
            logger.critical(f"[{symbol}] Exit order REJECTED on {exchange} - position still open")
            return ExitOrder(
                symbol=symbol, exchange=exchange, side=exit_side.value, quantity=quantity,
                status="failed", reason=reason, timestamp=time.time(),
                error="exchange returned no order",
            )

        exit_order = ExitOrder(
            symbol=symbol, exchange=exchange, side=exit_side.value, quantity=quantity,
            order_id=str(order.get("orderId")) if order.get("orderId") is not None else None,
            status="submitted", reason=reason, timestamp=time.time(),
        )
        logger.info(f"[{symbol}] Exit order submitted on {exchange}: {exit_order.order_id}")

        # Drop the leg from tracked state only once the exchange has accepted the
        # close. A failed leg stays tracked so a retry still sees it; a submitted
        # leg is released so a re-trigger cannot fire a duplicate market order.
        self._release_tracked_position(symbol, exchange)

        return exit_order

    def _release_tracked_position(self, symbol: str, exchange: str):
        """Remove a closed leg from PositionManager, recording the Trade."""
        try:
            position = self.position_manager.get_position(symbol, exchange)
            if position is None:
                return
            exit_price = position.current_price or position.entry_price
            self.position_manager.close_position(symbol, exchange, exit_price)
        except Exception as e:
            # Never let bookkeeping failure abort an in-flight emergency exit.
            logger.error(f"[{symbol}] Failed to release tracked position on {exchange}: {e}")

    async def wait_for_exits(self, timeout_s: float = 30) -> bool:
        """
        Poll submitted exit orders until they fill or the timeout expires.

        Returns True when every submitted order is filled. Legs that failed to
        submit are never waited on - they can never fill - and are reported by
        trigger_exit instead.
        """
        start = time.time()

        if not self.exit_reports:
            return False

        report = self.exit_reports[-1]

        while True:
            waitable = [
                o for o in report.orders
                if o.status == "submitted" and o.order_id is not None
            ]
            if not waitable:
                # Either everything filled, or nothing was ever submittable.
                filled = [o for o in report.orders if o.status == "filled"]
                report.all_filled = bool(filled) and len(filled) == report.expected_legs
                return report.all_filled

            if time.time() - start >= timeout_s:
                logger.critical(
                    f"Exit wait timeout after {timeout_s}s with "
                    f"{len(waitable)} leg(s) unconfirmed - VERIFY POSITIONS MANUALLY"
                )
                return False

            for order in waitable:
                await self._check_exit_order_status(order)

            await asyncio.sleep(1)

    async def _check_exit_order_status(self, order: ExitOrder):
        """
        Refresh one exit order's status from the exchange.

        Bybit reports the field as 'orderStatus'; Binance as 'status'. Both are
        checked because a missing field previously left every order stuck in
        'submitted' until the timeout expired.
        """
        if order.order_id is None:
            return

        try:
            if order.exchange == "Bybit":
                status = await self.exchange_hub.bybit.get_order_status(
                    order.symbol, order.order_id
                )
            else:
                status = await self.exchange_hub.binance.get_order_status(
                    order.symbol, order.order_id
                )
        except Exception as e:
            logger.error(f"[{order.symbol}] Failed to check order status: {e}")
            return

        if not status:
            return

        raw = status.get("orderStatus") or status.get("status") or ""
        state = str(raw).strip().upper()

        if state == "FILLED":
            order.status = "filled"
            price = status.get("avgPrice") or status.get("averagePrice") or 0
            try:
                order.exit_price = float(price)
            except (TypeError, ValueError):
                order.exit_price = None
        elif state in ("CANCELLED", "CANCELED", "REJECTED", "EXPIRED"):
            order.status = "failed"
            order.error = f"exchange reported {state}"
            logger.critical(
                f"[{order.symbol}] Exit order {order.order_id} {state} on "
                f"{order.exchange} - position may still be open"
            )

    def get_exit_history(self, limit: int = 10) -> List[EmergencyExitReport]:
        """Get recent exit reports."""
        return self.exit_reports[-limit:]

    def print_exit_summary(self, report: Optional[EmergencyExitReport] = None):
        """Print exit report summary."""
        if report is None and self.exit_reports:
            report = self.exit_reports[-1]

        if not report:
            print("No exit reports")
            return

        print("\n" + "=" * 80)
        print("EMERGENCY EXIT REPORT")
        print("=" * 80)
        print(f"Reason:         {report.reason.value}")
        print(f"Triggered:      {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(report.triggered_at))}")
        if report.completed_at:
            duration = report.completed_at - report.triggered_at
            print(f"Duration:       {duration:.1f}s")
        print(f"Status:         {'SUCCESS' if report.success else 'PARTIAL/FAILED'}")
        print(f"Details:        {report.details}")

        print(f"\nOrders ({len(report.orders)}):")
        for order in report.orders:
            status_icon = "[OK]" if order.status == "filled" else "[FAIL]" if order.status == "failed" else "[PENDING]"
            print(f"  {status_icon} {order.symbol} on {order.exchange}: "
                  f"{order.side} {order.quantity} @ ${order.exit_price or 'N/A'}")

        if report.total_realized_pnl:
            print(f"\nRealized P&L:   ${report.total_realized_pnl:.2f}")
        if report.total_funding_earned:
            print(f"Funding Earned: ${report.total_funding_earned:.2f}")

        print("=" * 80 + "\n")
