"""Tests for reconcile-and-flatten on partial dual-order failure.

Market orders cannot be cancelled once placed, so a partial failure must be
unwound by trading back out. The previous implementation called cancel_order,
which no-ops on a filled order and left naked directional exposure that nothing
tracked - the exact risk a delta-neutral strategy exists to avoid.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock

from src.execution.order_executor import (
    OrderExecutor, Order, OrderSide, OrderStatus,
)


def make_order(exchange: str, side: OrderSide, qty: float = 1.0) -> Order:
    return Order(
        order_id=f"{exchange.lower()}_1",
        exchange=exchange,
        symbol="BTCUSDT",
        side=side,
        quantity=qty,
        price=None,
        status=OrderStatus.FILLED,
        filled_quantity=qty,
    )


class TestBothLegsSucceed:
    async def test_returns_dual_order(self):
        ex = OrderExecutor()
        result = await ex.place_dual_order("BTCUSDT", OrderSide.LONG, OrderSide.SHORT, 1.0)

        assert result is not None
        assert result.status == "filled"
        assert not ex.naked_positions


class TestPartialFailureFlattens:
    async def test_binance_failure_flattens_bybit_leg(self):
        """The core regression: a live leg must be traded out, not 'cancelled'."""
        ex = OrderExecutor()
        ex.binance.place_order = AsyncMock(return_value=None)
        ex.bybit.place_order = AsyncMock(
            return_value=make_order("Bybit", OrderSide.LONG)
        )

        result = await ex.place_dual_order("BTCUSDT", OrderSide.LONG, OrderSide.SHORT, 1.0)

        assert result is None
        assert ex.bybit.place_order.await_count == 2, "open, then flatten"
        flatten_args, _ = ex.bybit.place_order.call_args
        assert flatten_args[1] is OrderSide.SHORT, "a LONG is flattened with SHORT"
        assert not ex.naked_positions, "successful flatten leaves no naked exposure"

    async def test_bybit_failure_flattens_binance_leg(self):
        ex = OrderExecutor()
        ex.bybit.place_order = AsyncMock(return_value=None)
        ex.binance.place_order = AsyncMock(
            return_value=make_order("Binance", OrderSide.SHORT)
        )

        result = await ex.place_dual_order("BTCUSDT", OrderSide.LONG, OrderSide.SHORT, 1.0)

        assert result is None
        assert ex.binance.place_order.await_count == 2
        flatten_args, _ = ex.binance.place_order.call_args
        assert flatten_args[1] is OrderSide.LONG, "a SHORT is flattened with LONG"

    async def test_raising_leg_still_flattens_the_other(self):
        """An exception must not orphan the sibling leg."""
        ex = OrderExecutor()
        ex.binance.place_order = AsyncMock(side_effect=ConnectionError("down"))
        ex.bybit.place_order = AsyncMock(
            return_value=make_order("Bybit", OrderSide.LONG)
        )

        result = await ex.place_dual_order("BTCUSDT", OrderSide.LONG, OrderSide.SHORT, 1.0)

        assert result is None
        assert ex.bybit.place_order.await_count == 2

    async def test_flatten_uses_actual_filled_quantity(self):
        """Flatten the size that filled, not the size requested."""
        ex = OrderExecutor()
        partial = make_order("Bybit", OrderSide.LONG, qty=1.0)
        partial.filled_quantity = 0.4
        ex.bybit.place_order = AsyncMock(return_value=partial)
        ex.binance.place_order = AsyncMock(return_value=None)

        await ex.place_dual_order("BTCUSDT", OrderSide.LONG, OrderSide.SHORT, 1.0)

        flatten_args, _ = ex.bybit.place_order.call_args
        assert flatten_args[2] == 0.4


class TestFlattenFailure:
    async def test_failed_flatten_is_recorded_as_naked(self):
        """If we cannot trade out, that must be loud and recorded - not swallowed."""
        ex = OrderExecutor()
        ex.binance.place_order = AsyncMock(return_value=None)
        ex.bybit.place_order = AsyncMock(
            side_effect=[make_order("Bybit", OrderSide.LONG), None]
        )

        result = await ex.place_dual_order("BTCUSDT", OrderSide.LONG, OrderSide.SHORT, 1.0)

        assert result is None
        assert len(ex.naked_positions) == 1
        assert ex.naked_positions[0].status == "naked"

    async def test_raising_flatten_is_recorded_as_naked(self):
        ex = OrderExecutor()
        ex.binance.place_order = AsyncMock(return_value=None)
        ex.bybit.place_order = AsyncMock(
            side_effect=[make_order("Bybit", OrderSide.LONG), RuntimeError("boom")]
        )

        await ex.place_dual_order("BTCUSDT", OrderSide.LONG, OrderSide.SHORT, 1.0)

        assert len(ex.naked_positions) == 1


class TestBothLegsFail:
    async def test_no_flatten_attempted_when_book_is_flat(self):
        ex = OrderExecutor()
        ex.bybit.place_order = AsyncMock(return_value=None)
        ex.binance.place_order = AsyncMock(return_value=None)

        result = await ex.place_dual_order("BTCUSDT", OrderSide.LONG, OrderSide.SHORT, 1.0)

        assert result is None
        assert not ex.naked_positions, "nothing filled, so nothing is naked"
        assert ex.bybit.place_order.await_count == 1, "no pointless flatten"
        assert len(ex.failed_orders) == 1
        assert ex.failed_orders[0].status == "failed"


class TestTimeout:
    async def test_slow_leg_resolving_in_grace_period_is_flattened(self):
        """A leg completing just after the timeout must not become an untracked fill."""
        ex = OrderExecutor()

        async def slow_bybit(*_a, **_k):
            await asyncio.sleep(0.2)
            return make_order("Bybit", OrderSide.LONG)

        ex.bybit.place_order = AsyncMock(side_effect=slow_bybit)
        ex.binance.place_order = AsyncMock(return_value=None)

        result = await ex.place_dual_order(
            "BTCUSDT", OrderSide.LONG, OrderSide.SHORT, 1.0, timeout_ms=50
        )

        assert result is None
        # Resolved inside the grace window, found filled, then flattened.
        assert ex.bybit.place_order.await_count == 2
        assert not ex.naked_positions

    async def test_unresolved_leg_is_escalated_not_assumed_flat(self):
        """
        A leg that never resolves may still be live on the exchange. Treating it
        as flat would hide real exposure, so it must be flagged for reconciliation.
        """
        ex = OrderExecutor()
        ex.GRACE_PERIOD_S = 0.05

        async def hanging_bybit(*_a, **_k):
            await asyncio.sleep(30)

        ex.bybit.place_order = AsyncMock(side_effect=hanging_bybit)
        ex.binance.place_order = AsyncMock(return_value=None)

        result = await ex.place_dual_order(
            "BTCUSDT", OrderSide.LONG, OrderSide.SHORT, 1.0, timeout_ms=50
        )

        assert result is None
        assert len(ex.naked_positions) == 1, \
            "an unresolved leg must be escalated, never silently assumed flat"
        assert ex.naked_positions[0].status == "naked"
