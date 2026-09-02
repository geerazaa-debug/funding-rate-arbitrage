import pytest
import time
from unittest.mock import Mock, AsyncMock, patch
from collections import defaultdict

from src.execution.exchange_api_client import BybitLiveClient, BinanceLiveClient, LiveExchangeHub
from src.execution.funding_accumulator import FundingAccumulator, FundingAccrual, FundingRecord
from src.execution.live_position_reconciler import LivePositionReconciler, ReconciliationStatus
from src.execution.slippage_tracker import SlippageTracker, SlippageRecord
from src.execution.position_manager import PositionManager


class TestBybitLiveClient:
    @pytest.fixture
    def client(self):
        return BybitLiveClient("test_key", "test_secret", testnet=True)

    @pytest.mark.asyncio
    async def test_place_order(self, client):
        """Test order placement."""
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {
                "retCode": 0,
                "result": {"orderId": "12345"}
            }

            order = await client.place_order("BTCUSDT", "BUY", 1.0)

            assert order is not None
            assert order["orderId"] == "12345"
            mock_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_order(self, client):
        """Test order cancellation."""
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {
                "retCode": 0,
                "result": {"orderId": "12345"}
            }

            result = await client.cancel_order("BTCUSDT", "12345")

            assert result is not None

    @pytest.mark.asyncio
    async def test_get_positions(self, client):
        """Test fetching positions."""
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "side": "Buy",
                            "size": "1.0",
                            "avgPrice": "45000",
                        }
                    ]
                }
            }

            positions = await client.get_positions("BTCUSDT")

            assert len(positions) == 1
            assert positions[0]["symbol"] == "BTCUSDT"


class TestBinanceLiveClient:
    @pytest.fixture
    def client(self):
        return BinanceLiveClient("test_key", "test_secret", testnet=True)

    @pytest.mark.asyncio
    async def test_place_order(self, client):
        """Test order placement."""
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {
                "orderId": 12345,
                "symbol": "BTCUSDT",
                "side": "BUY",
                "quantity": "1.0"
            }

            order = await client.place_order("BTCUSDT", "BUY", 1.0)

            assert order is not None
            assert order["orderId"] == 12345


class TestFundingAccumulator:
    @pytest.fixture
    def accumulator(self):
        return FundingAccumulator()

    def test_start_accrual(self, accumulator):
        """Test starting funding accrual."""
        accrual = accumulator.start_accrual("BTCUSDT", "Bybit", 1.0, 0.00015)

        assert accrual.symbol == "BTCUSDT"
        assert accrual.position_size == 1.0
        assert accrual.accumulated_amount == 0.0

    def test_apply_funding(self, accumulator):
        """Test applying funding payment."""
        accumulator.start_accrual("BTCUSDT", "Bybit", 1.0, 0.00015)

        # Apply funding
        accrual = accumulator.apply_funding("BTCUSDT", "Bybit", 0.00015, time.time())

        assert accrual is not None
        assert accrual.accumulated_amount == pytest.approx(1.0 * 0.00015)

    def test_close_accrual(self, accumulator):
        """Test closing accrual."""
        accumulator.start_accrual("BTCUSDT", "Bybit", 1.0, 0.00015)
        accumulator.apply_funding("BTCUSDT", "Bybit", 0.00015, time.time())

        closed = accumulator.close_accrual("BTCUSDT", "Bybit")

        assert closed is not None
        assert closed.accumulated_amount > 0

    def test_get_total_funding(self, accumulator):
        """Test total funding calculation."""
        accumulator.start_accrual("BTCUSDT", "Bybit", 1.0, 0.00015)
        accumulator.apply_funding("BTCUSDT", "Bybit", 0.00015, time.time())

        total = accumulator.get_total_funding("BTCUSDT")

        assert total > 0

    def test_funding_stats(self, accumulator):
        """Test funding statistics."""
        accumulator.start_accrual("BTCUSDT", "Bybit", 1.0, 0.00015)
        accumulator.apply_funding("BTCUSDT", "Bybit", 0.00015, time.time())

        stats = accumulator.get_funding_stats("BTCUSDT")

        assert stats["total_earned"] > 0
        assert stats["total_payments"] == 1


class TestLivePositionReconciler:
    @pytest.mark.asyncio
    async def test_reconciliation_synced(self):
        """Test successful reconciliation."""
        position_manager = PositionManager()
        exchange_hub = LiveExchangeHub("key", "secret", "key", "secret")

        reconciler = LivePositionReconciler(position_manager, exchange_hub)

        # Mock exchange responses
        with patch.object(reconciler, "_get_live_position", new_callable=AsyncMock) as mock_get:
            # `side` included: a real Bybit position response always carries it,
            # and the reconciler compares direction as well as size.
            mock_get.return_value = {
                "size": "1.0",
                "side": "Buy",
                "avgPrice": "45000"
            }

            # Add a tracked position
            position_manager.add_position("Bybit", "BTCUSDT", "LONG", 1.0, 45000.0)

            # Reconcile
            status = await reconciler._reconcile_symbol("BTCUSDT")

            assert status == ReconciliationStatus.SYNCED

    @pytest.mark.asyncio
    async def test_reconciliation_mismatch(self):
        """Test reconciliation with mismatch."""
        position_manager = PositionManager()
        exchange_hub = LiveExchangeHub("key", "secret", "key", "secret")

        reconciler = LivePositionReconciler(position_manager, exchange_hub)

        # Mock exchange responses
        with patch.object(reconciler, "_get_live_position", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {
                "size": "0.5",  # Different size
                "avgPrice": "45000"
            }

            # Add a tracked position
            position_manager.add_position("Bybit", "BTCUSDT", "LONG", 1.0, 45000.0)

            # Reconcile. tracked=1.0 vs actual=0.5 is a 50% divergence, which is
            # well past the 10% critical threshold - a half-missing hedge leg is
            # not a warning.
            status = await reconciler._reconcile_symbol("BTCUSDT")

            assert status == ReconciliationStatus.CRITICAL
            assert len(reconciler.mismatches) > 0
            assert reconciler.mismatches[0].severity == "critical"
            assert reconciler.mismatches[0].difference_pct == pytest.approx(50.0)


class TestSlippageTracker:
    @pytest.fixture
    def tracker(self):
        return SlippageTracker()

    def test_record_slippage(self, tracker):
        """Test recording slippage."""
        record = tracker.record_slippage(
            "BTCUSDT", "Bybit",
            expected_price=45000.0,
            actual_price=45010.0,  # 10 bps slippage
            quantity=1.0,
            side="BUY"
        )

        assert record.slippage_bps == pytest.approx(2.22, abs=0.01)

    def test_slippage_stats(self, tracker):
        """Test slippage statistics."""
        tracker.record_slippage("BTCUSDT", "Bybit", 45000.0, 45010.0, 1.0, "BUY")
        tracker.record_slippage("BTCUSDT", "Bybit", 45000.0, 44990.0, 1.0, "SELL")

        stats = tracker.get_slippage_stats()

        assert stats["count"] == 2
        assert stats["avg_slippage_bps"] > 0

    def test_worst_slippage(self, tracker):
        """Test finding worst slippage."""
        tracker.record_slippage("BTCUSDT", "Bybit", 45000.0, 45100.0, 1.0, "BUY")
        tracker.record_slippage("BTCUSDT", "Bybit", 45000.0, 45010.0, 1.0, "BUY")

        worst = tracker.get_worst_slippage(1)

        assert len(worst) == 1
        assert worst[0].slippage_bps > 20

    def test_exchange_comparison(self, tracker):
        """Test exchange slippage comparison."""
        tracker.record_slippage("BTCUSDT", "Bybit", 45000.0, 45010.0, 1.0, "BUY")
        tracker.record_slippage("BTCUSDT", "Binance", 45000.0, 45005.0, 1.0, "BUY")

        comparison = tracker.get_exchange_comparison()

        assert "Bybit" in comparison
        assert "Binance" in comparison
        # Bybit should have higher slippage
        assert comparison["Bybit"]["avg_slippage_bps"] > comparison["Binance"]["avg_slippage_bps"]


class TestLiveExchangeHub:
    @pytest.mark.asyncio
    async def test_initialization(self):
        """Test hub initialization."""
        hub = LiveExchangeHub("key", "secret", "key", "secret", testnet=True)

        assert hub.bybit is not None
        assert hub.binance is not None
        assert hub.testnet is True

        await hub.close()

    @pytest.mark.asyncio
    async def test_dual_order_execution(self):
        """Test dual order placement."""
        hub = LiveExchangeHub("key", "secret", "key", "secret", testnet=True)

        with patch.object(hub.bybit, "place_order", new_callable=AsyncMock) as bybit_mock:
            with patch.object(hub.binance, "place_order", new_callable=AsyncMock) as binance_mock:
                bybit_mock.return_value = {"orderId": "123"}
                binance_mock.return_value = {"orderId": "456"}

                result = await hub.place_dual_order(
                    "BTCUSDT", "BUY", "SELL", 1.0
                )

                assert result is not None
                assert result["bybit"]["orderId"] == "123"
                assert result["binance"]["orderId"] == "456"

        await hub.close()
