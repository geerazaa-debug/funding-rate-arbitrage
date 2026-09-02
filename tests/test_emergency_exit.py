"""Regression tests for the emergency exit path.

Every test here pins behaviour that was previously broken in a way that would
have cost capital: inverted close sides, exits reported as successful while
positions stayed open, and duplicate market orders on re-trigger.
"""
import pytest
from unittest.mock import AsyncMock, Mock

from src.execution.emergency_exit import (
    EmergencyExitHandler, EmergencyReason, ExitOrder, EmergencyExitReport,
)
from src.execution.order_executor import OrderSide
from src.execution.position_manager import PositionManager


def make_hub(bybit_result=None, binance_result=None,
             bybit_exc=None, binance_exc=None):
    """Build an exchange hub whose place_order is fully controlled."""
    hub = Mock()
    hub.bybit = Mock()
    hub.binance = Mock()
    hub.bybit.place_order = AsyncMock(
        side_effect=bybit_exc, return_value=bybit_result
    )
    hub.binance.place_order = AsyncMock(
        side_effect=binance_exc, return_value=binance_result
    )
    return hub


class TestExitSideMapping:
    """A LONG must be closed with SELL. Getting this backwards doubles the position."""

    def test_long_enum_maps_to_sell(self):
        assert EmergencyExitHandler._exit_side_for(OrderSide.LONG) is OrderSide.SHORT
        assert EmergencyExitHandler._exit_side_for(OrderSide.LONG).value == "SELL"

    def test_short_enum_maps_to_buy(self):
        assert EmergencyExitHandler._exit_side_for(OrderSide.SHORT) is OrderSide.LONG
        assert EmergencyExitHandler._exit_side_for(OrderSide.SHORT).value == "BUY"

    @pytest.mark.parametrize("raw", ["LONG", "long", "BUY", "OrderSide.LONG"])
    def test_long_strings_map_to_sell(self, raw):
        """String callers must not fall through to the wrong branch."""
        assert EmergencyExitHandler._exit_side_for(raw).value == "SELL"

    @pytest.mark.parametrize("raw", ["SHORT", "short", "SELL", "OrderSide.SHORT"])
    def test_short_strings_map_to_buy(self, raw):
        assert EmergencyExitHandler._exit_side_for(raw).value == "BUY"

    def test_unrecognised_side_raises(self):
        with pytest.raises(ValueError):
            EmergencyExitHandler._exit_side_for("SIDEWAYS")


class TestExitOrderSubmission:
    async def test_long_position_submits_sell_with_reduce_only(self):
        """The regression that would have doubled a long during an emergency."""
        pm = PositionManager()
        pm.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        hub = make_hub(bybit_result={"orderId": "b1"})

        handler = EmergencyExitHandler(pm, hub)
        report = await handler.trigger_exit(EmergencyReason.MANUAL_TRIGGER)

        hub.bybit.place_order.assert_awaited_once()
        _, kwargs = hub.bybit.place_order.call_args
        args, _ = hub.bybit.place_order.call_args
        assert args[1] == "SELL", "LONG must be closed with SELL"
        assert kwargs["reduce_only"] is True, "exit must never open new exposure"
        assert report.success is True

    async def test_short_position_submits_buy(self):
        pm = PositionManager()
        pm.add_position("Binance", "BTCUSDT", OrderSide.SHORT, 1.0, 45000.0)
        hub = make_hub(binance_result={"orderId": "n1"})

        handler = EmergencyExitHandler(pm, hub)
        await handler.trigger_exit(EmergencyReason.MANUAL_TRIGGER)

        args, kwargs = hub.binance.place_order.call_args
        assert args[1] == "BUY", "SHORT must be closed with BUY"
        assert kwargs["reduce_only"] is True


class TestFailedExitHandling:
    async def test_all_legs_raising_reports_failure(self):
        """`all([])` used to make a total failure look like success."""
        pm = PositionManager()
        pm.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        pm.add_position("Binance", "BTCUSDT", OrderSide.SHORT, 1.0, 45000.0)
        hub = make_hub(
            bybit_exc=ConnectionError("exchange down"),
            binance_exc=ConnectionError("exchange down"),
        )

        handler = EmergencyExitHandler(pm, hub)
        report = await handler.trigger_exit(EmergencyReason.CONNECTION_LOST)

        assert report.success is False
        assert report.expected_legs == 2
        assert len(report.orders) == 2, "a raising leg must still be recorded"
        assert all(o.status == "failed" for o in report.orders)
        assert all(o.error for o in report.orders)

    async def test_rejected_order_reports_failure(self):
        """A None return from the exchange means rejected, not absent."""
        pm = PositionManager()
        pm.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        hub = make_hub(bybit_result=None)

        handler = EmergencyExitHandler(pm, hub)
        report = await handler.trigger_exit(EmergencyReason.EXCHANGE_ERROR)

        assert report.success is False
        assert report.orders[0].status == "failed"

    async def test_partial_failure_reports_failure(self):
        """One good leg does not make the exit a success."""
        pm = PositionManager()
        pm.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        pm.add_position("Binance", "BTCUSDT", OrderSide.SHORT, 1.0, 45000.0)
        hub = make_hub(
            bybit_result={"orderId": "b1"},
            binance_exc=RuntimeError("rejected"),
        )

        handler = EmergencyExitHandler(pm, hub)
        report = await handler.trigger_exit(EmergencyReason.CIRCUIT_BREAKER)

        assert report.success is False
        statuses = sorted(o.status for o in report.orders)
        assert statuses == ["failed", "submitted"]

    async def test_no_positions_is_a_real_success(self):
        pm = PositionManager()
        handler = EmergencyExitHandler(pm, make_hub())
        report = await handler.trigger_exit(EmergencyReason.MANUAL_TRIGGER)

        assert report.success is True
        assert report.expected_legs == 0

    def test_empty_orders_against_expected_legs_is_failure(self):
        """Directly pins the `all([])` trap."""
        report = EmergencyExitReport(
            reason=EmergencyReason.MANUAL_TRIGGER, triggered_at=0.0
        )
        report.expected_legs = 2
        report.orders = []
        assert EmergencyExitHandler._evaluate_success(report) is False


class TestPositionCleanup:
    async def test_submitted_leg_is_released_from_tracking(self):
        pm = PositionManager()
        pm.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        hub = make_hub(bybit_result={"orderId": "b1"})

        handler = EmergencyExitHandler(pm, hub)
        await handler.trigger_exit(EmergencyReason.MANUAL_TRIGGER)

        assert pm.get_position("BTCUSDT", "Bybit") is None
        assert len(pm.trades) == 1, "exit must record a Trade"

    async def test_failed_leg_stays_tracked_for_retry(self):
        pm = PositionManager()
        pm.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        hub = make_hub(bybit_exc=ConnectionError("down"))

        handler = EmergencyExitHandler(pm, hub)
        await handler.trigger_exit(EmergencyReason.MANUAL_TRIGGER)

        assert pm.get_position("BTCUSDT", "Bybit") is not None, \
            "a leg that never reached the exchange must remain tracked"

    async def test_retrigger_does_not_duplicate_orders(self):
        """Re-triggering used to fire a second market order for a closed leg."""
        pm = PositionManager()
        pm.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        hub = make_hub(bybit_result={"orderId": "b1"})

        handler = EmergencyExitHandler(pm, hub)
        await handler.trigger_exit(EmergencyReason.MANUAL_TRIGGER)
        await handler.trigger_exit(EmergencyReason.MANUAL_TRIGGER)

        assert hub.bybit.place_order.await_count == 1


class TestWaitForExits:
    async def test_failed_legs_are_never_polled(self):
        """Polling a failed leg with order_id=None spammed errors for the full timeout."""
        pm = PositionManager()
        handler = EmergencyExitHandler(pm, make_hub())
        report = EmergencyExitReport(
            reason=EmergencyReason.MANUAL_TRIGGER, triggered_at=0.0
        )
        report.expected_legs = 1
        report.orders = [ExitOrder(
            symbol="BTCUSDT", exchange="Bybit", side="SELL",
            quantity=1.0, order_id=None, status="failed",
        )]
        handler.exit_reports.append(report)
        handler._check_exit_order_status = AsyncMock()

        result = await handler.wait_for_exits(timeout_s=5)

        assert result is False
        handler._check_exit_order_status.assert_not_awaited()

    async def test_bybit_orderstatus_field_is_read(self):
        """Bybit reports 'orderStatus'; reading only 'status' left orders stuck."""
        pm = PositionManager()
        hub = make_hub()
        hub.bybit.get_order_status = AsyncMock(
            return_value={"orderStatus": "Filled", "avgPrice": "45010.5"}
        )
        handler = EmergencyExitHandler(pm, hub)
        order = ExitOrder(
            symbol="BTCUSDT", exchange="Bybit", side="SELL",
            quantity=1.0, order_id="b1", status="submitted",
        )

        await handler._check_exit_order_status(order)

        assert order.status == "filled"
        assert order.exit_price == pytest.approx(45010.5)

    async def test_binance_status_field_is_read(self):
        pm = PositionManager()
        hub = make_hub()
        hub.binance.get_order_status = AsyncMock(
            return_value={"status": "FILLED", "avgPrice": "45010.5"}
        )
        handler = EmergencyExitHandler(pm, hub)
        order = ExitOrder(
            symbol="BTCUSDT", exchange="Binance", side="BUY",
            quantity=1.0, order_id="n1", status="submitted",
        )

        await handler._check_exit_order_status(order)

        assert order.status == "filled"

    async def test_rejected_order_is_marked_failed(self):
        pm = PositionManager()
        hub = make_hub()
        hub.bybit.get_order_status = AsyncMock(return_value={"orderStatus": "Rejected"})
        handler = EmergencyExitHandler(pm, hub)
        order = ExitOrder(
            symbol="BTCUSDT", exchange="Bybit", side="SELL",
            quantity=1.0, order_id="b1", status="submitted",
        )

        await handler._check_exit_order_status(order)

        assert order.status == "failed"
