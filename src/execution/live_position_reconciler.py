import asyncio
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from src.utils.logger import setup_logger
from src.execution.position_manager import PositionManager
from src.execution.exchange_api_client import LiveExchangeHub

logger = setup_logger("position_reconciler")


class ReconciliationStatus(Enum):
    """Status of position reconciliation."""
    SYNCED = "synced"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class PositionMismatch:
    """Represents a mismatch between tracked and actual position."""
    symbol: str
    exchange: str
    tracked_qty: float
    actual_qty: float
    tracked_entry_price: float
    actual_entry_price: float
    difference_pct: float
    severity: str  # "info", "warning", "critical"


class LivePositionReconciler:
    """Reconciles tracked positions with live exchange state."""

    def __init__(self, position_manager: PositionManager, exchange_hub: LiveExchangeHub):
        self.position_manager = position_manager
        self.exchange_hub = exchange_hub

        self.mismatches: List[PositionMismatch] = []
        self.last_sync_time: Dict[str, float] = {}
        self.sync_errors: List[str] = []

        logger.info("Initialized position reconciler")

    async def reconcile_all(self, symbols: List[str]) -> ReconciliationStatus:
        """
        Reconcile all tracked positions against live exchange state.

        Returns overall reconciliation status.
        """
        logger.info(f"Starting reconciliation for {len(symbols)} symbols")
        self.mismatches = []

        status = ReconciliationStatus.SYNCED

        for symbol in symbols:
            symbol_status = await self._reconcile_symbol(symbol)
            if symbol_status == ReconciliationStatus.CRITICAL:
                status = ReconciliationStatus.CRITICAL
            elif symbol_status == ReconciliationStatus.WARNING and status != ReconciliationStatus.CRITICAL:
                status = ReconciliationStatus.WARNING

        logger.info(f"Reconciliation complete: {status.value} "
                   f"({len(self.mismatches)} mismatches found)")

        return status

    async def _reconcile_symbol(self, symbol: str) -> ReconciliationStatus:
        """Reconcile a single symbol across both exchanges."""
        status = ReconciliationStatus.SYNCED

        # Get tracked positions
        tracked = self.position_manager.get_symbol_positions(symbol)

        # Get actual positions from exchanges
        bybit_actual = await self._get_live_position(symbol, "Bybit")
        binance_actual = await self._get_live_position(symbol, "Binance")

        # Check Bybit
        if "Bybit" in tracked:
            tracked_pos = tracked["Bybit"]
            if bybit_actual:
                mismatch = self._check_position_match(
                    symbol, "Bybit", tracked_pos, bybit_actual
                )
                if mismatch:
                    self.mismatches.append(mismatch)
                    if mismatch.severity == "critical":
                        status = ReconciliationStatus.CRITICAL
                    elif mismatch.severity == "warning":
                        status = ReconciliationStatus.WARNING
            else:
                # No position found on exchange but we tracked one
                mismatch = PositionMismatch(
                    symbol=symbol,
                    exchange="Bybit",
                    tracked_qty=tracked_pos.quantity,
                    actual_qty=0,
                    tracked_entry_price=tracked_pos.entry_price,
                    actual_entry_price=0,
                    difference_pct=100.0,
                    severity="critical",
                )
                self.mismatches.append(mismatch)
                status = ReconciliationStatus.CRITICAL
                logger.error(f"[{symbol}] Position missing on Bybit!")

        # Check Binance
        if "Binance" in tracked:
            tracked_pos = tracked["Binance"]
            if binance_actual:
                mismatch = self._check_position_match(
                    symbol, "Binance", tracked_pos, binance_actual
                )
                if mismatch:
                    self.mismatches.append(mismatch)
                    if mismatch.severity == "critical":
                        status = ReconciliationStatus.CRITICAL
                    elif mismatch.severity == "warning":
                        status = ReconciliationStatus.WARNING
            else:
                # No position found on exchange but we tracked one
                mismatch = PositionMismatch(
                    symbol=symbol,
                    exchange="Binance",
                    tracked_qty=tracked_pos.quantity,
                    actual_qty=0,
                    tracked_entry_price=tracked_pos.entry_price,
                    actual_entry_price=0,
                    difference_pct=100.0,
                    severity="critical",
                )
                self.mismatches.append(mismatch)
                status = ReconciliationStatus.CRITICAL
                logger.error(f"[{symbol}] Position missing on Binance!")

        return status

    async def _get_live_position(self, symbol: str, exchange: str) -> Optional[Dict]:
        """Fetch live position from exchange."""
        try:
            if exchange == "Bybit":
                positions = await self.exchange_hub.bybit.get_positions(symbol)
            else:  # Binance
                positions = await self.exchange_hub.binance.get_positions(symbol)

            if positions and len(positions) > 0:
                return positions[0]
            return None

        except Exception as e:
            logger.error(f"[{exchange}] Failed to fetch position: {e}")
            self.sync_errors.append(f"{exchange}: {e}")
            return None

    def _check_position_match(self, symbol: str, exchange: str, tracked: any,
                             actual: Dict) -> Optional[PositionMismatch]:
        """Compare tracked vs actual position."""
        # Extract values from exchange-specific response formats
        if exchange == "Bybit":
            actual_qty = float(actual.get("size", 0))
            actual_entry_price = float(actual.get("avgPrice", 0))
        else:  # Binance
            actual_qty = float(actual.get("positionAmt", 0))
            actual_entry_price = float(actual.get("entryPrice", 0))

        tracked_qty = tracked.quantity
        tracked_entry_price = tracked.entry_price

        # Calculate difference percentage
        if tracked_qty != 0:
            qty_diff_pct = abs(actual_qty - tracked_qty) / abs(tracked_qty) * 100
        else:
            qty_diff_pct = 0 if actual_qty == 0 else 100

        # Determine severity
        severity = "info"
        if qty_diff_pct > 10:
            severity = "critical"
        elif qty_diff_pct > 1:
            severity = "warning"

        # Price mismatch
        if tracked_entry_price != 0 and actual_entry_price != 0:
            price_diff_pct = abs(actual_entry_price - tracked_entry_price) / tracked_entry_price * 100
            if price_diff_pct > 5:
                severity = "critical"
            elif price_diff_pct > 1:
                severity = "warning"

        if severity != "info":
            mismatch = PositionMismatch(
                symbol=symbol,
                exchange=exchange,
                tracked_qty=tracked_qty,
                actual_qty=actual_qty,
                tracked_entry_price=tracked_entry_price,
                actual_entry_price=actual_entry_price,
                difference_pct=qty_diff_pct,
                severity=severity,
            )

            if severity == "warning":
                logger.warning(f"[{symbol}] Position mismatch on {exchange}: "
                              f"tracked={tracked_qty}, actual={actual_qty} ({qty_diff_pct:.1f}%)")
            else:
                logger.error(f"[{symbol}] CRITICAL mismatch on {exchange}: "
                            f"tracked={tracked_qty}, actual={actual_qty} ({qty_diff_pct:.1f}%)")

            return mismatch

        return None

    def get_mismatches(self, severity: Optional[str] = None) -> List[PositionMismatch]:
        """Get recorded mismatches, optionally filtered by severity."""
        if severity:
            return [m for m in self.mismatches if m.severity == severity]
        return self.mismatches.copy()

    def get_sync_status(self) -> Dict:
        """Get reconciliation status summary."""
        critical = [m for m in self.mismatches if m.severity == "critical"]
        warnings = [m for m in self.mismatches if m.severity == "warning"]

        return {
            "status": ReconciliationStatus.CRITICAL if critical else (
                ReconciliationStatus.WARNING if warnings else ReconciliationStatus.SYNCED
            ),
            "critical_mismatches": len(critical),
            "warnings": len(warnings),
            "sync_errors": len(self.sync_errors),
            "last_sync": max(self.last_sync_time.values()) if self.last_sync_time else 0,
        }

    def print_reconciliation_report(self):
        """Print reconciliation report."""
        status = self.get_sync_status()

        print("\n" + "=" * 80)
        print("POSITION RECONCILIATION REPORT")
        print("=" * 80)
        print(f"Status: {status['status'].value.upper()}")
        print(f"Critical Mismatches: {status['critical_mismatches']}")
        print(f"Warnings: {status['warnings']}")
        print(f"Sync Errors: {status['sync_errors']}")

        if self.mismatches:
            print("\nDetailed Mismatches:")
            for mismatch in self.mismatches:
                print(f"  [{mismatch.severity.upper()}] {mismatch.symbol} on {mismatch.exchange}:")
                print(f"    Tracked:  {mismatch.tracked_qty} @ ${mismatch.tracked_entry_price:.2f}")
                print(f"    Actual:   {mismatch.actual_qty} @ ${mismatch.actual_entry_price:.2f}")
                print(f"    Δ:        {mismatch.difference_pct:.1f}%")

        if self.sync_errors:
            print("\nSync Errors:")
            for error in self.sync_errors[-5:]:
                print(f"  - {error}")

        print("=" * 80 + "\n")
