import asyncio
import time
from typing import Dict, List, Optional, Tuple
from enum import Enum
from dataclasses import dataclass

from src.utils.logger import setup_logger
from src.execution.position_manager import PositionManager
from src.execution.exchange_api_client import LiveExchangeHub

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
        logger.critical(f"🚨 EMERGENCY EXIT TRIGGERED: {reason.value} - {details}")

        report = EmergencyExitReport(
            reason=reason,
            triggered_at=time.time(),
            details=details,
        )

        try:
            # Get all positions
            all_positions = self.position_manager.get_all_positions()
            if not all_positions:
                logger.info("No positions to exit")
                report.completed_at = time.time()
                report.success = True
                self.exit_reports.append(report)
                return report

            # Exit each symbol's positions
            for symbol in all_positions:
                await self._exit_symbol(symbol, reason, report)

            report.completed_at = time.time()
            report.success = all(o.status == "filled" for o in report.orders)

            logger.info(f"Emergency exit completed: {report.success}")
            self.exit_reports.append(report)

            return report

        except Exception as e:
            logger.error(f"Emergency exit error: {e}")
            report.details = f"Error: {e}"
            report.completed_at = time.time()
            self.exit_reports.append(report)
            return report

        finally:
            self.is_exiting = False

    async def _exit_symbol(self, symbol: str, reason: EmergencyReason, report: EmergencyExitReport):
        """Exit all positions for a single symbol."""
        positions = self.position_manager.get_symbol_positions(symbol)

        logger.info(f"Exiting {len(positions)} positions for {symbol}")

        for exchange, position in positions.items():
            exit_order = await self._exit_position(
                symbol, exchange, position.quantity, position.side, reason
            )
            if exit_order:
                report.orders.append(exit_order)

    async def _exit_position(self, symbol: str, exchange: str, quantity: float,
                            side: str, reason: EmergencyReason) -> Optional[ExitOrder]:
        """Exit a single position."""
        try:
            # Determine exit side (opposite of entry)
            if side == "LONG":
                exit_side = "SELL"
            else:
                exit_side = "BUY"

            logger.info(f"[{symbol}] Emergency exit on {exchange}: {exit_side} {quantity}")

            # Place market order
            if exchange == "Bybit":
                order = await self.exchange_hub.bybit.place_order(
                    symbol, exit_side, quantity, order_type="Market"
                )
            else:  # Binance
                order = await self.exchange_hub.binance.place_order(
                    symbol, exit_side, quantity, order_type="MARKET"
                )

            if order:
                exit_order = ExitOrder(
                    symbol=symbol,
                    exchange=exchange,
                    side=exit_side,
                    quantity=quantity,
                    order_id=order.get("orderId"),
                    status="submitted",
                    reason=reason,
                    timestamp=time.time(),
                )
                logger.info(f"[{symbol}] Exit order submitted: {exit_order.order_id}")
                return exit_order
            else:
                logger.error(f"[{symbol}] Failed to place exit order on {exchange}")
                exit_order = ExitOrder(
                    symbol=symbol,
                    exchange=exchange,
                    side=exit_side,
                    quantity=quantity,
                    status="failed",
                    reason=reason,
                    timestamp=time.time(),
                )
                return exit_order

        except Exception as e:
            logger.error(f"[{symbol}] Exit error on {exchange}: {e}")
            return None

    async def wait_for_exits(self, timeout_s: float = 30) -> bool:
        """
        Wait for all pending exit orders to complete.

        Args:
            timeout_s: Max time to wait

        Returns:
            True if all filled, False if timeout
        """
        start = time.time()

        while time.time() - start < timeout_s:
            if not self.exit_reports:
                return False

            latest_report = self.exit_reports[-1]
            if not latest_report.orders:
                return True

            pending = [o for o in latest_report.orders if o.status != "filled"]
            if not pending:
                return True

            # Check status
            for order in pending:
                await self._check_exit_order_status(order)

            await asyncio.sleep(1)

        logger.warning(f"Exit wait timeout after {timeout_s}s")
        return False

    async def _check_exit_order_status(self, order: ExitOrder):
        """Check status of an exit order."""
        try:
            if order.exchange == "Bybit":
                status = await self.exchange_hub.bybit.get_order_status(
                    order.symbol, order.order_id
                )
            else:
                status = await self.exchange_hub.binance.get_order_status(
                    order.symbol, order.order_id
                )

            if status:
                if status.get("status") == "Filled" or status.get("status") == "FILLED":
                    order.status = "filled"
                    order.exit_price = float(status.get("avgPrice", 0))
                elif status.get("status") == "Cancelled" or status.get("status") == "CANCELED":
                    order.status = "failed"

        except Exception as e:
            logger.error(f"Failed to check order status: {e}")

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
            status_icon = "✓" if order.status == "filled" else "✗" if order.status == "failed" else "⏳"
            print(f"  {status_icon} {order.symbol} on {order.exchange}: "
                  f"{order.side} {order.quantity} @ ${order.exit_price or 'N/A'}")

        if report.total_realized_pnl:
            print(f"\nRealized P&L:   ${report.total_realized_pnl:.2f}")
        if report.total_funding_earned:
            print(f"Funding Earned: ${report.total_funding_earned:.2f}")

        print("=" * 80 + "\n")
