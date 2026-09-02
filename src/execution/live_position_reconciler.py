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


@dataclass
class OrphanPosition:
    """
    A live position on an exchange that the bot is not tracking.

    Always critical: an untracked position is unhedged by definition, carries
    directional risk, and is invisible to every other safety mechanism -
    emergency exit iterates tracked positions, so it would not be closed.
    """
    symbol: str
    exchange: str
    quantity: float
    entry_price: float
    side: str

    def __repr__(self):
        return (f"OrphanPosition({self.exchange} {self.side} {self.quantity} "
                f"{self.symbol} @ {self.entry_price})")


class LivePositionReconciler:
    """Reconciles tracked positions with live exchange state."""

    def __init__(self, position_manager: PositionManager, exchange_hub: LiveExchangeHub):
        self.position_manager = position_manager
        self.exchange_hub = exchange_hub

        self.mismatches: List[PositionMismatch] = []
        self.orphans: List[OrphanPosition] = []
        self.last_sync_time: Dict[str, float] = {}
        self.sync_errors: List[str] = []

        logger.info("Initialized position reconciler")

    async def reconcile_all(self, symbols: List[str]) -> ReconciliationStatus:
        """
        Reconcile in both directions.

        Forward  (tracked -> exchange): a position we believe we hold is missing
                 or the wrong size.
        Reverse  (exchange -> tracked): a position exists on the exchange that we
                 are not tracking at all.

        The reverse pass is what catches a leg that filled while its counterpart
        failed, or anything opened outside the bot. Without it such a position is
        invisible to every downstream safety mechanism, since they all iterate
        tracked state.
        """
        logger.info(f"Starting reconciliation for {len(symbols)} symbols")
        self.mismatches = []
        self.orphans = []

        status = ReconciliationStatus.SYNCED

        for symbol in symbols:
            symbol_status = await self._reconcile_symbol(symbol)
            if symbol_status == ReconciliationStatus.CRITICAL:
                status = ReconciliationStatus.CRITICAL
            elif symbol_status == ReconciliationStatus.WARNING and status != ReconciliationStatus.CRITICAL:
                status = ReconciliationStatus.WARNING

        # Reverse pass. Deliberately not scoped to `symbols`: a position on a
        # symbol we were never configured for is exactly the kind of exposure
        # that must not be missed.
        if await self._detect_orphans():
            status = ReconciliationStatus.CRITICAL

        logger.info(
            f"Reconciliation complete: {status.value} "
            f"({len(self.mismatches)} mismatches, {len(self.orphans)} orphans)"
        )

        return status

    async def _detect_orphans(self) -> bool:
        """
        Find live positions the bot is not tracking.

        Returns True if any orphan was found.
        """
        found = False

        for exchange in ("Bybit", "Binance"):
            live = await self._get_all_live_positions(exchange)
            if live is None:
                # A failed query is not evidence of absence. Say so rather than
                # letting a silent fetch failure read as "no orphans".
                logger.error(
                    f"[{exchange}] Orphan scan INCOMPLETE - could not list positions"
                )
                continue

            for entry in live:
                symbol = entry["symbol"]
                if self.position_manager.get_position(symbol, exchange) is not None:
                    continue  # tracked; the forward pass owns this one

                orphan = OrphanPosition(
                    symbol=symbol,
                    exchange=exchange,
                    quantity=entry["quantity"],
                    entry_price=entry["entry_price"],
                    side=entry["side"],
                )
                self.orphans.append(orphan)
                found = True
                logger.critical(
                    f"[{symbol}] ORPHAN POSITION on {exchange}: "
                    f"{orphan.side} {orphan.quantity} @ {orphan.entry_price} "
                    f"is live but untracked - unhedged exposure"
                )

        return found

    async def _get_all_live_positions(self, exchange: str) -> Optional[List[Dict]]:
        """
        List every open position on an exchange, normalised.

        Returns None when the query fails, which is distinct from an empty list.
        Zero-size entries are dropped: Binance's positionRisk returns a row for
        every symbol the account has ever touched, most with positionAmt "0".
        """
        try:
            if exchange == "Bybit":
                raw = await self.exchange_hub.bybit.get_positions()
            else:
                raw = await self.exchange_hub.binance.get_positions()
        except Exception as e:
            logger.error(f"[{exchange}] Failed to list positions: {e}")
            self.sync_errors.append(f"{exchange}: {e}")
            return None

        if raw is None:
            return None

        normalised = []
        for entry in raw:
            try:
                if exchange == "Bybit":
                    quantity = float(entry.get("size") or 0)
                    side = str(entry.get("side") or "").upper()
                    entry_price = float(entry.get("avgPrice") or 0)
                else:
                    # Binance encodes direction in the sign of positionAmt.
                    amount = float(entry.get("positionAmt") or 0)
                    quantity = abs(amount)
                    side = "BUY" if amount > 0 else "SELL"
                    entry_price = float(entry.get("entryPrice") or 0)

                if quantity == 0:
                    continue

                symbol = entry.get("symbol")
                if not symbol:
                    continue

                normalised.append({
                    "symbol": symbol,
                    "quantity": quantity,
                    "side": side,
                    "entry_price": entry_price,
                })
            except (TypeError, ValueError) as e:
                logger.error(f"[{exchange}] Unparseable position entry {entry}: {e}")
                continue

        return normalised

    def get_orphans(self) -> List[OrphanPosition]:
        """Live positions the bot is not tracking."""
        return list(self.orphans)

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

        # An orphan is always critical - it is unhedged exposure that no other
        # safety mechanism can see.
        if critical or self.orphans:
            status = ReconciliationStatus.CRITICAL
        elif warnings:
            status = ReconciliationStatus.WARNING
        else:
            status = ReconciliationStatus.SYNCED

        return {
            "status": status,
            "critical_mismatches": len(critical),
            "warnings": len(warnings),
            "orphans": len(self.orphans),
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
        print(f"Orphan Positions: {status['orphans']}")
        print(f"Sync Errors: {status['sync_errors']}")

        if self.orphans:
            print("\nORPHAN POSITIONS (live on exchange, untracked by the bot):")
            for orphan in self.orphans:
                print(f"  [CRITICAL] {orphan.symbol} on {orphan.exchange}: "
                      f"{orphan.side} {orphan.quantity} @ ${orphan.entry_price:.2f}")
            print("  -> unhedged exposure; emergency exit will NOT close these")

        if self.mismatches:
            print("\nDetailed Mismatches:")
            for mismatch in self.mismatches:
                print(f"  [{mismatch.severity.upper()}] {mismatch.symbol} on {mismatch.exchange}:")
                print(f"    Tracked:  {mismatch.tracked_qty} @ ${mismatch.tracked_entry_price:.2f}")
                print(f"    Actual:   {mismatch.actual_qty} @ ${mismatch.actual_entry_price:.2f}")
                print(f"Delta:        {mismatch.difference_pct:.1f}%")

        if self.sync_errors:
            print("\nSync Errors:")
            for error in self.sync_errors[-5:]:
                print(f"  - {error}")

        print("=" * 80 + "\n")
