"""Tests for reverse-direction reconciliation (orphan detection).

The reconciler previously only walked tracked positions, so a position living on
an exchange that the bot did not know about was structurally invisible - which
is exactly the state a partially-failed dual order leaves behind.
"""
import pytest
from unittest.mock import AsyncMock, Mock

from src.execution.live_position_reconciler import (
    LivePositionReconciler, ReconciliationStatus, OrphanPosition,
)
from src.execution.order_executor import OrderSide
from src.execution.position_manager import PositionManager


def make_hub(bybit_positions=None, binance_positions=None,
             bybit_exc=None, binance_exc=None):
    hub = Mock()
    hub.bybit = Mock()
    hub.binance = Mock()
    hub.bybit.get_positions = AsyncMock(
        side_effect=bybit_exc, return_value=bybit_positions
    )
    hub.binance.get_positions = AsyncMock(
        side_effect=binance_exc, return_value=binance_positions
    )
    return hub


BYBIT_LIVE = [{"symbol": "BTCUSDT", "size": "1.0", "side": "Buy", "avgPrice": "45000"}]
BINANCE_LIVE = [{"symbol": "BTCUSDT", "positionAmt": "-1.0", "entryPrice": "45000"}]


class TestOrphanDetection:
    async def test_untracked_exchange_position_is_flagged(self):
        """The core gap: a live position nothing tracks must surface."""
        pm = PositionManager()  # tracking nothing
        hub = make_hub(bybit_positions=BYBIT_LIVE, binance_positions=[])

        rec = LivePositionReconciler(pm, hub)
        status = await rec.reconcile_all([])

        assert status is ReconciliationStatus.CRITICAL
        assert len(rec.orphans) == 1
        orphan = rec.orphans[0]
        assert orphan.symbol == "BTCUSDT"
        assert orphan.exchange == "Bybit"
        assert orphan.quantity == 1.0

    async def test_tracked_position_is_not_an_orphan(self):
        pm = PositionManager()
        pm.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        hub = make_hub(bybit_positions=BYBIT_LIVE, binance_positions=[])

        rec = LivePositionReconciler(pm, hub)
        await rec.reconcile_all(["BTCUSDT"])

        assert rec.orphans == []

    async def test_orphan_found_on_unconfigured_symbol(self):
        """
        The scan must not be scoped to the configured symbol list - a position on
        a symbol we never traded is precisely what we would otherwise miss.
        """
        pm = PositionManager()
        hub = make_hub(
            bybit_positions=[
                {"symbol": "DOGEUSDT", "size": "500", "side": "Sell", "avgPrice": "0.12"}
            ],
            binance_positions=[],
        )

        rec = LivePositionReconciler(pm, hub)
        status = await rec.reconcile_all(["BTCUSDT", "ETHUSDT"])

        assert status is ReconciliationStatus.CRITICAL
        assert rec.orphans[0].symbol == "DOGEUSDT"

    async def test_orphans_detected_on_both_exchanges(self):
        pm = PositionManager()
        hub = make_hub(bybit_positions=BYBIT_LIVE, binance_positions=BINANCE_LIVE)

        rec = LivePositionReconciler(pm, hub)
        await rec.reconcile_all([])

        exchanges = sorted(o.exchange for o in rec.orphans)
        assert exchanges == ["Binance", "Bybit"]

    async def test_partial_fill_scenario_is_caught(self):
        """
        End-to-end shape of the bug this exists for: the Bybit leg filled, the
        Binance leg did not, nothing was registered, and the book is net long.
        """
        pm = PositionManager()
        hub = make_hub(bybit_positions=BYBIT_LIVE, binance_positions=[])

        rec = LivePositionReconciler(pm, hub)
        status = await rec.reconcile_all(["BTCUSDT"])

        assert status is ReconciliationStatus.CRITICAL
        assert rec.get_sync_status()["orphans"] == 1


class TestNormalisation:
    async def test_zero_size_entries_are_ignored(self):
        """Binance returns a row per touched symbol, mostly with positionAmt 0."""
        pm = PositionManager()
        hub = make_hub(
            bybit_positions=[],
            binance_positions=[
                {"symbol": "BTCUSDT", "positionAmt": "0", "entryPrice": "0"},
                {"symbol": "ETHUSDT", "positionAmt": "0.0", "entryPrice": "0"},
            ],
        )

        rec = LivePositionReconciler(pm, hub)
        status = await rec.reconcile_all([])

        assert rec.orphans == []
        assert status is ReconciliationStatus.SYNCED

    async def test_binance_negative_amount_is_a_short(self):
        pm = PositionManager()
        hub = make_hub(bybit_positions=[], binance_positions=BINANCE_LIVE)

        rec = LivePositionReconciler(pm, hub)
        await rec.reconcile_all([])

        orphan = rec.orphans[0]
        assert orphan.side == "SELL"
        assert orphan.quantity == 1.0, "quantity is reported as magnitude"

    async def test_unparseable_entry_does_not_abort_the_scan(self):
        pm = PositionManager()
        hub = make_hub(
            bybit_positions=[
                {"symbol": "BAD", "size": "not-a-number", "avgPrice": "0"},
                {"symbol": "BTCUSDT", "size": "1.0", "side": "Buy", "avgPrice": "45000"},
            ],
            binance_positions=[],
        )

        rec = LivePositionReconciler(pm, hub)
        await rec.reconcile_all([])

        assert [o.symbol for o in rec.orphans] == ["BTCUSDT"]


class TestScanFailure:
    async def test_failed_query_is_not_read_as_no_orphans(self):
        """A fetch failure must not be silently equivalent to a clean scan."""
        pm = PositionManager()
        hub = make_hub(bybit_exc=ConnectionError("down"), binance_positions=[])

        rec = LivePositionReconciler(pm, hub)
        await rec.reconcile_all([])

        assert rec.orphans == []
        assert rec.sync_errors, "the failure must be recorded, not swallowed"

    async def test_none_response_is_treated_as_failure(self):
        pm = PositionManager()
        hub = make_hub(bybit_positions=None, binance_positions=[])

        rec = LivePositionReconciler(pm, hub)
        await rec.reconcile_all([])

        assert rec.orphans == []


class TestBinanceSignHandling:
    """
    Binance encodes direction in the sign of positionAmt; Bybit uses an unsigned
    size plus a side field. Comparing a signed value against a tracked magnitude
    made every correctly-hedged short read as a 200% mismatch - and every
    arbitrage has a short leg, so this fired CRITICAL on every cycle.
    """

    async def test_correctly_hedged_short_is_not_a_mismatch(self):
        pm = PositionManager()
        pm.add_position("Binance", "BTCUSDT", OrderSide.SHORT, 1.0, 45000.0)
        hub = make_hub(bybit_positions=[], binance_positions=BINANCE_LIVE)

        rec = LivePositionReconciler(pm, hub)
        await rec._reconcile_symbol("BTCUSDT")

        assert rec.mismatches == [], \
            "a short of the right size in the right direction is not a mismatch"

    async def test_wrong_direction_is_critical(self):
        """Right size, wrong way round: double the exposure, not a hedge."""
        pm = PositionManager()
        pm.add_position("Binance", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        hub = make_hub(bybit_positions=[], binance_positions=BINANCE_LIVE)

        rec = LivePositionReconciler(pm, hub)
        status = await rec._reconcile_symbol("BTCUSDT")

        assert status is ReconciliationStatus.CRITICAL
        assert rec.mismatches[0].severity == "critical"

    async def test_bybit_side_field_is_compared(self):
        pm = PositionManager()
        pm.add_position("Bybit", "BTCUSDT", OrderSide.SHORT, 1.0, 45000.0)
        # Exchange reports a LONG of the same size.
        hub = make_hub(bybit_positions=BYBIT_LIVE, binance_positions=[])

        rec = LivePositionReconciler(pm, hub)
        status = await rec._reconcile_symbol("BTCUSDT")

        assert status is ReconciliationStatus.CRITICAL

    async def test_missing_side_field_does_not_manufacture_a_mismatch(self):
        """
        An absent direction is unknown, not opposite. Treating it as opposite
        would fire a false critical on every cycle.
        """
        pm = PositionManager()
        pm.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        hub = make_hub(
            bybit_positions=[{"symbol": "BTCUSDT", "size": "1.0", "avgPrice": "45000"}],
            binance_positions=[],
        )

        rec = LivePositionReconciler(pm, hub)
        status = await rec._reconcile_symbol("BTCUSDT")

        assert status is ReconciliationStatus.SYNCED
        assert rec.mismatches == []


class TestCleanState:
    async def test_no_positions_anywhere_is_synced(self):
        pm = PositionManager()
        hub = make_hub(bybit_positions=[], binance_positions=[])

        rec = LivePositionReconciler(pm, hub)
        status = await rec.reconcile_all(["BTCUSDT"])

        assert status is ReconciliationStatus.SYNCED
        assert rec.get_sync_status()["orphans"] == 0

    async def test_orphans_cleared_between_runs(self):
        pm = PositionManager()
        hub = make_hub(bybit_positions=BYBIT_LIVE, binance_positions=[])
        rec = LivePositionReconciler(pm, hub)

        await rec.reconcile_all([])
        assert len(rec.orphans) == 1

        hub.bybit.get_positions = AsyncMock(return_value=[])
        await rec.reconcile_all([])
        assert rec.orphans == [], "stale orphans must not persist across runs"
