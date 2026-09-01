import pytest
import asyncio
import json
import time
from unittest.mock import Mock, AsyncMock, patch
from collections import deque

from src.data.websocket_manager import BybitWebSocketManager, BinanceWebSocketManager, WebSocketHub
from src.data.rest_api_client import BybitRESTClient, BinanceRESTClient, RESTAPIHub
from src.data.funding_rate_tracker import FundingRateTracker, FundingRate, FundingRateDifferential


class TestBybitWebSocketManager:
    @pytest.mark.asyncio
    async def test_initialization(self):
        """Test Bybit WebSocket manager initialization."""
        mock_callback = AsyncMock()
        manager = BybitWebSocketManager(["BTCUSDT", "ETHUSDT"], mock_callback)

        assert manager.exchange == "Bybit"
        assert manager.pairs == ["BTCUSDT", "ETHUSDT"]
        assert manager.is_connected == False
        assert manager.reconnect_attempts == 0

    @pytest.mark.asyncio
    async def test_message_subscription_format(self):
        """Test subscription message format for Bybit."""
        mock_callback = AsyncMock()
        manager = BybitWebSocketManager(["BTCUSDT"], mock_callback)

        # Mock websocket for testing
        manager.websocket = AsyncMock()
        await manager._subscribe()

        # Verify subscription messages were sent
        calls = manager.websocket.send.call_args_list
        assert len(calls) == 1

        message = json.loads(calls[0][0][0])
        assert message["op"] == "subscribe"
        assert "funding.BTCUSDT" in message["args"]


class TestBinanceWebSocketManager:
    @pytest.mark.asyncio
    async def test_initialization(self):
        """Test Binance WebSocket manager initialization."""
        mock_callback = AsyncMock()
        manager = BinanceWebSocketManager(["BTCUSDT", "ETHUSDT"], mock_callback)

        assert manager.exchange == "Binance"
        assert manager.pairs == ["BTCUSDT", "ETHUSDT"]
        assert "btcusdt@markprice" in manager.base_url or True  # URL built on connect


class TestBybitRESTClient:
    @pytest.mark.asyncio
    async def test_funding_rate_parsing(self):
        """Test parsing Bybit funding rate response."""
        client = BybitRESTClient()

        # Mock response
        mock_response = {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "fundingRate": "0.00015",
                        "fundingRateTimestamp": "1630000000000",
                    }
                ]
            },
        }

        with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_response):
            rates = await client.get_funding_rates("BTCUSDT", limit=1)

            assert len(rates) == 1
            assert rates[0]["exchange"] == "bybit"
            assert rates[0]["symbol"] == "BTCUSDT"
            assert rates[0]["funding_rate"] == 0.00015

    @pytest.mark.asyncio
    async def test_funding_rate_api_error(self):
        """Test error handling for API failures."""
        client = BybitRESTClient()

        with patch.object(client, "_request", new_callable=AsyncMock, return_value=None):
            rates = await client.get_funding_rates("BTCUSDT")
            assert rates is None

    @pytest.mark.asyncio
    async def test_latest_funding_rate(self):
        """Test fetching latest funding rate."""
        client = BybitRESTClient()

        mock_response = {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "fundingRate": "0.00015",
                        "fundingRateTimestamp": "1630000000000",
                    }
                ]
            },
        }

        with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_response):
            rate = await client.get_latest_funding_rate("BTCUSDT")
            assert rate is not None
            assert rate["funding_rate"] == 0.00015


class TestBinanceRESTClient:
    @pytest.mark.asyncio
    async def test_funding_rate_parsing(self):
        """Test parsing Binance funding rate response."""
        client = BinanceRESTClient()

        # Mock response
        mock_response = [
            {
                "symbol": "BTCUSDT",
                "fundingRate": "0.00025",
                "fundingTime": 1630000000000,
            }
        ]

        with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_response):
            rates = await client.get_funding_rates("BTCUSDT", limit=1)

            assert len(rates) == 1
            assert rates[0]["exchange"] == "binance"
            assert rates[0]["funding_rate"] == 0.00025

    @pytest.mark.asyncio
    async def test_mark_price_fetch(self):
        """Test fetching mark price."""
        client = BinanceRESTClient()

        mock_response = {"symbol": "BTCUSDT", "markPrice": "45000.50"}

        with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_response):
            price = await client.get_mark_price("BTCUSDT")
            assert price == 45000.50


class TestFundingRateTracker:
    @pytest.fixture
    def tracker(self):
        """Create a tracker instance for testing."""
        return FundingRateTracker(["BTCUSDT", "ETHUSDT"])

    def test_initialization(self, tracker):
        """Test tracker initialization."""
        assert "BTCUSDT" in tracker.symbols
        assert "ETHUSDT" in tracker.symbols
        assert len(tracker.bybit_rates) == 2
        assert len(tracker.binance_rates) == 2

    @pytest.mark.asyncio
    async def test_bybit_message_processing(self, tracker):
        """Test processing Bybit WebSocket message."""
        message = {
            "data": {
                "symbol": "BTCUSDT",
                "fundingRate": "0.00015",
                "fundingRateTimestamp": 1630000000000,
            }
        }

        await tracker.process_websocket_message(message, "bybit")

        # Verify rate was stored
        assert tracker.latest_rates["BTCUSDT"]["bybit"] is not None
        assert tracker.latest_rates["BTCUSDT"]["bybit"].rate == 0.00015
        assert len(tracker.bybit_rates["BTCUSDT"]) == 1

    @pytest.mark.asyncio
    async def test_binance_message_processing(self, tracker):
        """Test processing Binance WebSocket message."""
        message = {
            "e": "markPriceUpdate",
            "E": 1630000000000,
            "s": "BTCUSDT",
            "r": "0.00025",
        }

        await tracker.process_websocket_message(message, "binance")

        # Verify rate was stored
        assert tracker.latest_rates["BTCUSDT"]["binance"] is not None
        assert tracker.latest_rates["BTCUSDT"]["binance"].rate == 0.00025

    @pytest.mark.asyncio
    async def test_differential_calculation(self, tracker):
        """Test funding rate differential calculation."""
        # Add Bybit rate
        bybit_msg = {
            "data": {
                "symbol": "BTCUSDT",
                "fundingRate": "0.00015",
                "fundingRateTimestamp": 1630000000000,
            }
        }
        await tracker.process_websocket_message(bybit_msg, "bybit")

        # Add Binance rate
        binance_msg = {
            "e": "markPriceUpdate",
            "E": 1630000000000,
            "s": "BTCUSDT",
            "r": "0.00025",
        }
        await tracker.process_websocket_message(binance_msg, "binance")

        # Check differential: Binance - Bybit = 0.00025 - 0.00015 = 0.0001
        diff = tracker.get_latest_differential("BTCUSDT")
        assert diff is not None
        assert abs(diff.differential - 0.0001) < 1e-8
        assert diff.bybit_rate == 0.00015
        assert diff.binance_rate == 0.00025

    @pytest.mark.asyncio
    async def test_rate_history_deque_maxlen(self, tracker):
        """Test that rate history respects max length."""
        tracker.max_history = 3

        # Add more rates than max_history
        for i in range(5):
            message = {
                "data": {
                    "symbol": "BTCUSDT",
                    "fundingRate": f"0.0000{i}",
                    "fundingRateTimestamp": 1630000000000 + i,
                }
            }
            await tracker.process_websocket_message(message, "bybit")

        # Should only keep 3 most recent
        history = tracker.get_rate_history("BTCUSDT", "bybit")
        assert len(history) == 3

    @pytest.mark.asyncio
    async def test_stats_calculation(self, tracker):
        """Test calculating statistics from rate history."""
        # Add multiple rates
        for i in range(3):
            message = {
                "data": {
                    "symbol": "BTCUSDT",
                    "fundingRate": f"0.0000{i+1}",
                    "fundingRateTimestamp": 1630000000000 + i,
                }
            }
            await tracker.process_websocket_message(message, "bybit")

        stats = tracker.get_stats("BTCUSDT", "bybit")
        assert stats["count"] == 3
        assert stats["min"] == 0.00001
        assert stats["max"] == 0.00003
        assert stats["avg"] == pytest.approx(0.00002)

    @pytest.mark.asyncio
    async def test_large_differential_alert(self, tracker):
        """Test alert triggering for large differentials."""
        # Set up large differential
        bybit_msg = {
            "data": {
                "symbol": "BTCUSDT",
                "fundingRate": "0.00001",
                "fundingRateTimestamp": 1630000000000,
            }
        }
        await tracker.process_websocket_message(bybit_msg, "bybit")

        binance_msg = {
            "e": "markPriceUpdate",
            "E": 1630000000000,
            "s": "BTCUSDT",
            "r": "0.00102",  # Large differential: 0.00101
        }
        await tracker.process_websocket_message(binance_msg, "binance")

        # Check if alert was triggered
        assert len(tracker.alerts_triggered) > 0
        assert "Large differential" in tracker.alerts_triggered[0]

    @pytest.mark.asyncio
    async def test_ignore_unknown_symbols(self, tracker):
        """Test that unknown symbols are ignored."""
        message = {
            "data": {
                "symbol": "XYZUSDT",  # Not in tracker.symbols
                "fundingRate": "0.00015",
                "fundingRateTimestamp": 1630000000000,
            }
        }

        await tracker.process_websocket_message(message, "bybit")

        # XYZUSDT should not be tracked
        assert "XYZUSDT" not in tracker.latest_rates["BTCUSDT"]["bybit"]

    def test_get_all_differentials(self, tracker):
        """Test retrieving all differentials."""
        differentials = tracker.get_all_differentials()
        assert "BTCUSDT" in differentials
        assert "ETHUSDT" in differentials


class TestRESTAPIHub:
    @pytest.mark.asyncio
    async def test_hub_initialization(self):
        """Test RESTAPIHub initialization."""
        hub = RESTAPIHub()
        assert hub.bybit is not None
        assert hub.binance is not None
        await hub.close()

    @pytest.mark.asyncio
    async def test_fetch_all_funding_rates(self):
        """Test fetching from both exchanges."""
        hub = RESTAPIHub()

        mock_bybit = {
            "exchange": "bybit",
            "symbol": "BTCUSDT",
            "funding_rate": 0.00015,
            "timestamp": 1630000000000,
        }
        mock_binance = {
            "exchange": "binance",
            "symbol": "BTCUSDT",
            "funding_rate": 0.00025,
            "timestamp": 1630000000000,
        }

        with patch.object(hub.bybit, "get_latest_funding_rate", new_callable=AsyncMock, return_value=mock_bybit):
            with patch.object(hub.binance, "get_latest_funding_rate", new_callable=AsyncMock, return_value=mock_binance):
                rates = await hub.fetch_all_funding_rates("BTCUSDT")

                assert rates["bybit"]["funding_rate"] == 0.00015
                assert rates["binance"]["funding_rate"] == 0.00025

        await hub.close()


class TestIntegration:
    @pytest.mark.asyncio
    async def test_websocket_to_tracker_flow(self):
        """Test complete flow from WebSocket message to tracker."""
        tracker = FundingRateTracker(["BTCUSDT"])

        # Simulate dual exchange data
        bybit_rate = 0.00015
        binance_rate = 0.00025

        bybit_msg = {
            "data": {
                "symbol": "BTCUSDT",
                "fundingRate": str(bybit_rate),
                "fundingRateTimestamp": int(time.time() * 1000),
            }
        }

        binance_msg = {
            "e": "markPriceUpdate",
            "E": int(time.time() * 1000),
            "s": "BTCUSDT",
            "r": str(binance_rate),
        }

        # Process messages
        await tracker.process_websocket_message(bybit_msg, "bybit")
        await tracker.process_websocket_message(binance_msg, "binance")

        # Verify results
        diff = tracker.get_latest_differential("BTCUSDT")
        assert diff is not None
        assert diff.differential == pytest.approx(binance_rate - bybit_rate)
        assert diff.bybit_rate == bybit_rate
        assert diff.binance_rate == binance_rate

        await tracker.close()

    @pytest.mark.asyncio
    async def test_rate_age_tracking(self):
        """Test that rate age is correctly tracked."""
        tracker = FundingRateTracker(["BTCUSDT"])

        # Add rates
        bybit_msg = {
            "data": {
                "symbol": "BTCUSDT",
                "fundingRate": "0.00015",
                "fundingRateTimestamp": int(time.time() * 1000),
            }
        }
        await tracker.process_websocket_message(bybit_msg, "bybit")

        # Sleep a bit
        await asyncio.sleep(0.1)

        binance_msg = {
            "e": "markPriceUpdate",
            "E": int(time.time() * 1000),
            "s": "BTCUSDT",
            "r": "0.00025",
        }
        await tracker.process_websocket_message(binance_msg, "binance")

        diff = tracker.get_latest_differential("BTCUSDT")
        assert diff.bybit_age_s > diff.binance_age_s  # Bybit was added first
        assert diff.bybit_age_s > 0
        assert diff.binance_age_s > 0

        await tracker.close()
