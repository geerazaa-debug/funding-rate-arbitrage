"""Tests for the single authoritative view of unhedged exposure.

Directional risk previously lived in three unconnected places: the executor's
naked_positions, the reconciler's orphans, and nothing at all for a tracked
position that lost its hedge. get_unhedged_exposure() merges them.
"""
import pytest
from unittest.mock import AsyncMock, Mock

from src.execution.live_position_reconciler import (
    LivePositionReconciler, ReconciliationStatus, UnhedgedExposure,
)
from src.execution.order_executor import OrderExecutor, Order, OrderSide, OrderStatus
from src.execution.position_manager import PositionManager


def make_hub(bybit_positions=None, binance_positions=None):
    hub = Mock()
    hub.bybit = Mock()
    hub.binance = Mock()
    hub.bybit.get_positions = AsyncMock(return_value=bybit_positions or [])
    hub.binance.get_positions = AsyncMock(return_value=binance_positions or [])
    return hub


def make_order(exchange: str, side: OrderSide, qty: float = 1.0) -> Order:
    return Order(
        order_id=f"{exchange.lower()}_1", exchange=exchange, symbol="BTCUSDT",
        side=side, quantity=qty, price=45000.0,
        status=OrderStatus.FILLED, filled_quantity=qty,
    )


class TestFailedFlattenRegistersPosition:
    async def test_failed_flatten_becomes_tracked(self):
        """A leg that filled and could not be closed is a known live position."""
        pm = PositionManager()
        ex = OrderExecutor(position_manager=pm)
        ex.binance.place_order = AsyncMock(return_value=None)
        ex.bybit.place_order = AsyncMock(
            side_effect=[make_order("Bybit", OrderSide.LONG), None]
        )

        await ex.place_dual_order("BTCUSDT", OrderSide.LONG, OrderSide.SHORT, 1.0)

        position = pm.get_position("BTCUSDT", "Bybit")
        assert position is not None, "an un-closable filled leg must be tracked"
        assert position.side is OrderSide.LONG
        assert position.quantity == 1.0
        assert len(ex.naked_positions) == 1, "provenance is still recorded"

    async def test_successful_flatten_registers_nothing(self):
        pm = PositionManager()
        ex = OrderExecutor(position_manager=pm)
        ex.binance.place_order = AsyncMock(return_value=None)
        ex.bybit.place_order = AsyncMock(
            side_effect=[
                make_order("Bybit", OrderSide.LONG),
                make_order("Bybit", OrderSide.SHORT),
            ]
        )

        await ex.place_dual_order("BTCUSDT", OrderSide.LONG, OrderSide.SHORT, 1.0)

        assert pm.get_all_positions() == {} or not pm.get_position("BTCUSDT", "Bybit")

    async def test_unresolved_leg_registers_no_phantom(self):
        """
        An unresolved leg is unknown, not known-live. Inventing a position here
        would create phantom exposure; the reverse pass discovers the truth.
        """
        import asyncio
        pm = PositionManager()
        ex = OrderExecutor(position_manager=pm)
        ex.GRACE_PERIOD_S = 0.05

        async def hanging(*_a, **_k):
            await asyncio.sleep(30)

        ex.bybit.place_order = AsyncMock(side_effect=hanging)
        ex.binance.place_order = AsyncMock(return_value=None)

        await ex.place_dual_order(
            "BTCUSDT", OrderSide.LONG, OrderSide.SHORT, 1.0, timeout_ms=50
        )

        assert pm.get_position("BTCUSDT", "Bybit") is None, \
            "must not invent a position we are not sure exists"

    async def test_works_without_position_manager(self):
        """The wiring is optional; existing callers must still work."""
        ex = OrderExecutor()
        ex.binance.place_order = AsyncMock(return_value=None)
        ex.bybit.place_order = AsyncMock(
            side_effect=[make_order("Bybit", OrderSide.LONG), None]
        )

        await ex.place_dual_order("BTCUSDT", OrderSide.LONG, OrderSide.SHORT, 1.0)

        assert len(ex.naked_positions) == 1


class TestDeltaDetection:
    async def test_tracked_naked_position_is_flagged(self):
        """
        The gap the delta pass closes: tracked and actual agree, so the forward
        pass says SYNCED, but the position has no hedge.
        """
        pm = PositionManager()
        pm.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        hub = make_hub(
            bybit_positions=[
                {"symbol": "BTCUSDT", "size": "1.0", "side": "Buy", "avgPrice": "45000"}
            ]
        )

        rec = LivePositionReconciler(pm, hub)
        status = await rec.reconcile_all(["BTCUSDT"])

        assert status is ReconciliationStatus.CRITICAL
        assert len(rec.unhedged) == 1
        assert rec.unhedged[0].source == "delta"

    async def test_balanced_legs_are_not_flagged(self):
        pm = PositionManager()
        pm.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        pm.add_position("Binance", "BTCUSDT", OrderSide.SHORT, 1.0, 45000.0)
        hub = make_hub(
            bybit_positions=[
                {"symbol": "BTCUSDT", "size": "1.0", "side": "Buy", "avgPrice": "45000"}
            ],
            binance_positions=[
                {"symbol": "BTCUSDT", "positionAmt": "-1.0", "entryPrice": "45000"}
            ],
        )

        rec = LivePositionReconciler(pm, hub)
        status = await rec.reconcile_all(["BTCUSDT"])

        assert rec.unhedged == []
        assert status is ReconciliationStatus.SYNCED

    async def test_mismatched_sizes_are_flagged(self):
        """Both legs present but unequal still leaves net exposure."""
        pm = PositionManager()
        pm.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        pm.add_position("Binance", "BTCUSDT", OrderSide.SHORT, 0.6, 45000.0)
        hub = make_hub()

        rec = LivePositionReconciler(pm, hub)
        await rec.reconcile_all([])

        assert rec.unhedged, "0.4 net long is unhedged"


class TestUnifiedView:
    async def test_merges_both_sources(self):
        pm = PositionManager()
        # Tracked but unhedged.
        pm.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        # Untracked on the other exchange, different symbol.
        hub = make_hub(
            bybit_positions=[
                {"symbol": "BTCUSDT", "size": "1.0", "side": "Buy", "avgPrice": "45000"},
                {"symbol": "DOGEUSDT", "size": "500", "side": "Sell", "avgPrice": "0.12"},
            ]
        )

        rec = LivePositionReconciler(pm, hub)
        await rec.reconcile_all(["BTCUSDT"])

        exposure = rec.get_unhedged_exposure()
        sources = sorted(e.source for e in exposure)
        assert sources == ["delta", "orphan"]
        assert {e.symbol for e in exposure} == {"BTCUSDT", "DOGEUSDT"}

    async def test_sources_are_disjoint(self):
        """A tracked position must never be counted as both orphan and delta."""
        pm = PositionManager()
        pm.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        hub = make_hub(
            bybit_positions=[
                {"symbol": "BTCUSDT", "size": "1.0", "side": "Buy", "avgPrice": "45000"}
            ]
        )

        rec = LivePositionReconciler(pm, hub)
        await rec.reconcile_all(["BTCUSDT"])

        exposure = rec.get_unhedged_exposure()
        assert len(exposure) == 1
        assert exposure[0].source == "delta"

    async def test_clean_book_reports_no_exposure(self):
        pm = PositionManager()
        rec = LivePositionReconciler(pm, make_hub())
        status = await rec.reconcile_all(["BTCUSDT"])

        assert rec.get_unhedged_exposure() == []
        assert status is ReconciliationStatus.SYNCED

    async def test_status_counts_are_consistent(self):
        pm = PositionManager()
        pm.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        hub = make_hub(
            bybit_positions=[
                {"symbol": "BTCUSDT", "size": "1.0", "side": "Buy", "avgPrice": "45000"},
                {"symbol": "DOGEUSDT", "size": "500", "side": "Sell", "avgPrice": "0.12"},
            ]
        )

        rec = LivePositionReconciler(pm, hub)
        await rec.reconcile_all(["BTCUSDT"])
        status = rec.get_sync_status()

        assert status["total_unhedged_exposure"] == \
            status["orphans"] + status["unhedged"]
        assert status["status"] is ReconciliationStatus.CRITICAL


class TestEndToEnd:
    async def test_failed_flatten_surfaces_in_unified_view(self):
        """
        The full chain: a partial fill that cannot be flattened is registered by
        the executor, then reported by the reconciler as one entry - with
        provenance pointing back at the failed flatten.
        """
        pm = PositionManager()
        ex = OrderExecutor(position_manager=pm)
        ex.binance.place_order = AsyncMock(return_value=None)
        ex.bybit.place_order = AsyncMock(
            side_effect=[make_order("Bybit", OrderSide.LONG), None]
        )

        await ex.place_dual_order("BTCUSDT", OrderSide.LONG, OrderSide.SHORT, 1.0)

        hub = make_hub(
            bybit_positions=[
                {"symbol": "BTCUSDT", "size": "1.0", "side": "Buy", "avgPrice": "45000"}
            ]
        )
        rec = LivePositionReconciler(pm, hub, order_executor=ex)
        status = await rec.reconcile_all(["BTCUSDT"])

        assert status is ReconciliationStatus.CRITICAL

        exposure = rec.get_unhedged_exposure()
        assert len(exposure) == 1
        assert exposure[0].symbol == "BTCUSDT"
        assert exposure[0].source == "delta"
        assert "failed flatten" in exposure[0].detail, \
            "provenance should point back at the executor"

    async def test_emergency_exit_can_now_see_it(self):
        """
        The practical payoff: because the position is tracked, the emergency exit
        handler - which iterates tracked positions - will attempt to close it.
        """
        from src.execution.emergency_exit import EmergencyExitHandler, EmergencyReason

        pm = PositionManager()
        ex = OrderExecutor(position_manager=pm)
        ex.binance.place_order = AsyncMock(return_value=None)
        ex.bybit.place_order = AsyncMock(
            side_effect=[make_order("Bybit", OrderSide.LONG), None]
        )
        await ex.place_dual_order("BTCUSDT", OrderSide.LONG, OrderSide.SHORT, 1.0)

        exit_hub = Mock()
        exit_hub.bybit = Mock()
        exit_hub.binance = Mock()
        exit_hub.bybit.place_order = AsyncMock(return_value={"orderId": "x1"})
        exit_hub.binance.place_order = AsyncMock(return_value={"orderId": "x2"})

        handler = EmergencyExitHandler(pm, exit_hub)
        report = await handler.trigger_exit(EmergencyReason.MANUAL_TRIGGER)

        assert report.expected_legs == 1, "the unhedged leg is visible to exit"
        args, kwargs = exit_hub.bybit.place_order.call_args
        assert args[1] == "SELL", "and is closed with the correct side"
        assert kwargs["reduce_only"] is True
