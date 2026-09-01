import pytest
import asyncio
from unittest.mock import Mock, AsyncMock

from src.execution.order_executor import (
    OrderExecutor, Order, OrderSide, OrderStatus, DualExchangeOrder
)
from src.execution.position_manager import PositionManager, Position, Trade
from src.execution.paper_trader import PaperTrader, PaperTradingConfig
from src.risk.position_sizer import PositionSizer, SizingConfig, SizingStrategy
from src.risk.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitBreakerStatus
from src.risk.risk_monitor import RiskMonitor


class TestOrderExecutor:
    @pytest.mark.asyncio
    async def test_single_order_execution(self):
        """Test placing a single order."""
        executor = OrderExecutor()
        order = await executor.bybit.place_order("BTCUSDT", OrderSide.LONG, 1.0)

        assert order is not None
        assert order.exchange == "Bybit"
        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == 1.0

    @pytest.mark.asyncio
    async def test_dual_order_execution(self):
        """Test placing simultaneous orders on both exchanges."""
        executor = OrderExecutor()
        dual = await executor.place_dual_order(
            "BTCUSDT",
            OrderSide.LONG,
            OrderSide.SHORT,
            1.0
        )

        assert dual is not None
        assert dual.status == "filled"
        assert dual.bybit_order.status == OrderStatus.FILLED
        assert dual.binance_order.status == OrderStatus.FILLED

    @pytest.mark.asyncio
    async def test_dual_order_close(self):
        """Test closing a dual position."""
        executor = OrderExecutor()
        dual = await executor.place_dual_order(
            "BTCUSDT",
            OrderSide.LONG,
            OrderSide.SHORT,
            1.0
        )

        closed = await executor.close_dual_position(dual)
        assert closed is True


class TestPositionManager:
    @pytest.fixture
    def manager(self):
        return PositionManager()

    def test_add_position(self, manager):
        """Test adding a position."""
        pos = manager.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)

        assert pos.exchange == "Bybit"
        assert pos.quantity == 1.0
        assert pos.entry_price == 45000.0

    def test_close_position(self, manager):
        """Test closing a position."""
        manager.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        trade = manager.close_position("BTCUSDT", "Bybit", 46000.0)

        assert trade is not None
        assert trade.realized_pnl == 1000.0  # 1 BTC * (46k - 45k)

    def test_net_delta(self, manager):
        """Test net delta calculation."""
        manager.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        manager.add_position("Binance", "BTCUSDT", OrderSide.SHORT, 1.0, 45000.0)

        delta = manager.get_net_delta("BTCUSDT")
        assert delta == 0.0  # Delta-neutral

    def test_total_exposure(self, manager):
        """Test total exposure calculation."""
        manager.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        manager.add_position("Binance", "BTCUSDT", OrderSide.SHORT, 1.0, 45000.0)

        exposure = manager.get_total_exposure("BTCUSDT")
        assert exposure == 90000.0  # 45k + 45k

    def test_trade_statistics(self, manager):
        """Test trade statistics."""
        manager.add_position("Bybit", "BTCUSDT", OrderSide.LONG, 1.0, 45000.0)
        manager.close_position("BTCUSDT", "Bybit", 46000.0)  # +$1000

        manager.add_position("Bybit", "ETHUSDT", OrderSide.LONG, 10.0, 2000.0)
        manager.close_position("ETHUSDT", "Bybit", 1900.0)  # -$1000

        stats = manager.get_trade_statistics()
        assert stats["total_trades"] == 2
        assert stats["winning_trades"] == 1
        assert stats["losing_trades"] == 1
        assert stats["win_rate"] == 0.5


class TestPaperTrader:
    @pytest.fixture
    def trader(self):
        config = PaperTradingConfig(
            initial_capital=10000.0,
            min_differential_threshold=0.0001,
            max_position_usd=2000.0
        )
        return PaperTrader(config)

    @pytest.mark.asyncio
    async def test_trade_execution(self, trader):
        """Test paper trading execution."""
        from src.data.funding_rate_tracker import FundingRateDifferential

        diff = FundingRateDifferential(
            symbol="BTCUSDT",
            bybit_rate=0.00010,
            binance_rate=0.00030,
            differential=0.00020,
            timestamp=0
        )

        result = await trader.evaluate_and_trade("BTCUSDT", diff)
        assert result is True
        assert trader.trade_count == 1

    @pytest.mark.asyncio
    async def test_insufficient_differential(self, trader):
        """Test skipping when differential is too small."""
        from src.data.funding_rate_tracker import FundingRateDifferential

        diff = FundingRateDifferential(
            symbol="BTCUSDT",
            bybit_rate=0.00010,
            binance_rate=0.00011,  # Only 0.01% difference
            differential=0.00001,
            timestamp=0
        )

        result = await trader.evaluate_and_trade("BTCUSDT", diff)
        assert result is False
        assert trader.skip_count > 0

    def test_portfolio_stats(self, trader):
        """Test portfolio statistics."""
        stats = trader.get_portfolio_stats()

        assert stats["capital"] == 10000.0
        assert stats["return_pct"] == 0.0  # No trades yet


class TestPositionSizer:
    @pytest.fixture
    def sizer(self):
        config = SizingConfig(
            strategy=SizingStrategy.FIXED,
            base_size_usd=1000.0,
            max_size_usd=5000.0
        )
        return PositionSizer(config)

    def test_fixed_sizing(self, sizer):
        """Test fixed position sizing."""
        quantity = sizer.calculate_size(
            capital=10000.0,
            symbol="BTCUSDT",
            entry_price=45000.0
        )

        # Should be ~1000/45000 ≈ 0.022 BTC
        assert quantity > 0
        assert abs(quantity * 45000.0 - 1000.0) < 10  # Within $10

    def test_sizing_respects_max_exposure(self, sizer):
        """Test that sizing respects max exposure."""
        quantity = sizer.calculate_size(
            capital=10000.0,
            symbol="BTCUSDT",
            entry_price=45000.0,
            existing_exposure=9500.0
        )

        # Should be reduced to stay under max_total_exposure
        assert quantity * 45000.0 <= 500.0 + 100.0  # Min 100 + safety

    def test_volatility_adjusted_sizing(self):
        """Test volatility-adjusted sizing."""
        config = SizingConfig(
            strategy=SizingStrategy.VOLATILITY_ADJUSTED,
            base_size_usd=1000.0
        )
        sizer = PositionSizer(config)

        # High vol → smaller position
        high_vol_qty = sizer.calculate_size(
            capital=10000.0,
            symbol="BTCUSDT",
            entry_price=45000.0,
            volatility=0.05
        )

        # Low vol → larger position
        low_vol_qty = sizer.calculate_size(
            capital=10000.0,
            symbol="BTCUSDT",
            entry_price=45000.0,
            volatility=0.01
        )

        assert low_vol_qty > high_vol_qty


class TestCircuitBreaker:
    @pytest.fixture
    def breaker(self):
        config = CircuitBreakerConfig(
            max_daily_loss_usd=1000.0,
            max_drawdown_pct=10.0,
            consecutive_losses_limit=3
        )
        return CircuitBreaker(10000.0, config)

    def test_initialization(self, breaker):
        """Test circuit breaker initialization."""
        assert breaker.status == CircuitBreakerStatus.ACTIVE
        assert breaker.daily_loss == 0.0

    def test_daily_loss_breach(self, breaker):
        """Test daily loss limit breach."""
        allowed = breaker.record_trade(-500.0, 9500.0)
        assert allowed is True

        allowed = breaker.record_trade(-600.0, 8900.0)
        assert allowed is False
        assert breaker.status == CircuitBreakerStatus.HALTED

    def test_consecutive_losses(self, breaker):
        """Test consecutive loss detection."""
        breaker.record_trade(-100.0, 9900.0)
        breaker.record_trade(-100.0, 9800.0)
        allowed = breaker.record_trade(-100.0, 9700.0)

        # Should trigger after 3 consecutive losses
        assert allowed is False
        assert breaker.status == CircuitBreakerStatus.HALTED

    def test_recovery_after_timeout(self, breaker):
        """Test recovery after timeout."""
        breaker.record_trade(-2000.0, 8000.0)  # Breach
        assert breaker.status == CircuitBreakerStatus.HALTED

        # Simulate timeout
        breaker.last_halt_time = breaker.last_halt_time - (breaker.config.recovery_timeout_minutes * 60 + 1)
        allowed = breaker.check_trade_allowed(8000.0)

        assert allowed is True
        assert breaker.status == CircuitBreakerStatus.ACTIVE


class TestRiskMonitor:
    @pytest.mark.asyncio
    async def test_pre_trade_checks(self):
        """Test pre-trade risk checks."""
        position_manager = PositionManager()
        monitor = RiskMonitor(10000.0, position_manager)

        checks = await monitor.pre_trade_checks("BTCUSDT", 1.0, 45000.0)

        assert len(checks) > 0
        # All should pass for first trade
        assert all(c.passed for c in checks)

    def test_trade_recording(self):
        """Test recording trade results."""
        position_manager = PositionManager()
        monitor = RiskMonitor(10000.0, position_manager)

        monitor.record_trade_result("BTCUSDT", 100.0, 1.0, 3600)

        assert monitor.current_capital == 10100.0

    def test_risk_summary(self):
        """Test risk summary generation."""
        position_manager = PositionManager()
        monitor = RiskMonitor(10000.0, position_manager)

        summary = monitor.get_risk_summary()

        assert summary["current_capital"] == 10000.0
        assert summary["return_pct"] == 0.0
        assert summary["checks_passed"] >= 0
