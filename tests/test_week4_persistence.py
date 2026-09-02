import pytest
import asyncio
from datetime import datetime
from unittest.mock import Mock, AsyncMock, patch
from pathlib import Path
import tempfile

from src.execution.emergency_exit import EmergencyExitHandler, EmergencyReason, EmergencyExitReport
from src.alerts.webhook_alerter import WebhookAlerter, Alert, AlertType, AlertLevel
from src.storage.database import Database
from src.storage.models import TradeRecord, PositionRecord, FundingRecord
from src.monitoring.live_dashboard import LiveDashboard
from src.execution.position_manager import PositionManager
from src.execution.funding_accumulator import FundingAccumulator
from src.execution.slippage_tracker import SlippageTracker
from src.risk.circuit_breaker import CircuitBreaker


class TestEmergencyExitHandler:
    @pytest.mark.asyncio
    async def test_initialization(self):
        """Test emergency exit handler initialization."""
        position_manager = PositionManager()
        exchange_hub = Mock()

        handler = EmergencyExitHandler(position_manager, exchange_hub)

        assert handler.position_manager is not None
        assert handler.is_exiting is False

    @pytest.mark.asyncio
    async def test_trigger_exit_no_positions(self):
        """Test exit when no positions open."""
        position_manager = PositionManager()
        exchange_hub = Mock()

        handler = EmergencyExitHandler(position_manager, exchange_hub)
        report = await handler.trigger_exit(EmergencyReason.MANUAL_TRIGGER, "Testing")

        assert report is not None
        assert report.success is True
        assert len(report.orders) == 0

    @pytest.mark.asyncio
    async def test_exit_report(self):
        """Test exit report generation."""
        position_manager = PositionManager()
        exchange_hub = Mock()

        handler = EmergencyExitHandler(position_manager, exchange_hub)
        report = await handler.trigger_exit(
            EmergencyReason.CIRCUIT_BREAKER,
            "Daily loss limit exceeded"
        )

        assert report.reason == EmergencyReason.CIRCUIT_BREAKER
        assert report.details == "Daily loss limit exceeded"


class TestWebhookAlerter:
    @pytest.fixture
    def alerter(self):
        return WebhookAlerter()

    @pytest.mark.asyncio
    async def test_register_webhook(self, alerter):
        """Test webhook registration."""
        alerter.register_webhook("slack", "https://hooks.slack.com/...")

        assert "slack" in alerter.webhooks

    @pytest.mark.asyncio
    async def test_alert_trade_opened(self, alerter):
        """Test trade opened alert."""
        with patch.object(alerter, "_send_to_service", new_callable=AsyncMock):
            await alerter.alert_trade_opened("BTCUSDT", "Bybit/Binance", 1.0, 0.0001)

            assert len(alerter.sent_alerts) > 0
            assert alerter.sent_alerts[0].type == AlertType.TRADE_OPENED

    @pytest.mark.asyncio
    async def test_alert_emergency_exit(self, alerter):
        """Test emergency exit alert."""
        with patch.object(alerter, "_send_to_service", new_callable=AsyncMock):
            await alerter.alert_emergency_exit("Circuit breaker halted")

            assert len(alerter.sent_alerts) > 0
            latest = alerter.sent_alerts[-1]
            assert latest.type == AlertType.EMERGENCY_EXIT
            assert latest.level == AlertLevel.CRITICAL

    def test_format_slack(self, alerter):
        """Test Slack message formatting."""
        alert = Alert(
            type=AlertType.TRADE_OPENED,
            level=AlertLevel.INFO,
            title="Test Alert",
            message="Test message",
        )

        payload = alerter._format_slack(alert)

        assert "attachments" in payload
        assert payload["attachments"][0]["title"] == "Test Alert"

    def test_format_telegram(self, alerter):
        """Test Telegram message formatting."""
        alert = Alert(
            type=AlertType.TRADE_OPENED,
            level=AlertLevel.INFO,
            title="Test Alert",
            message="Test message",
        )

        payload = alerter._format_telegram(alert)

        assert "text" in payload
        assert "Test Alert" in payload["text"]

    def test_format_discord(self, alerter):
        """Test Discord message formatting."""
        alert = Alert(
            type=AlertType.TRADE_OPENED,
            level=AlertLevel.INFO,
            title="Test Alert",
            message="Test message",
        )

        payload = alerter._format_discord(alert)

        assert "embeds" in payload
        assert payload["embeds"][0]["title"] == "Test Alert"


class TestDatabase:
    @pytest.fixture
    def db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            database = Database(db_path)
            database.connect()
            yield database
            database.disconnect()

    def test_initialization(self, db):
        """Test database initialization."""
        assert db.connection is not None

    def test_insert_trade(self, db):
        """Test inserting a trade record."""
        trade = TradeRecord(
            symbol="BTCUSDT",
            entry_time=datetime.now(),
            exit_time=datetime.now(),
            entry_price=45000.0,
            exit_price=46000.0,
            quantity=1.0,
            realized_pnl=1000.0,
        )

        trade_id = db.insert_trade(trade)

        assert trade_id > 0
        retrieved = db.get_trades("BTCUSDT")
        assert len(retrieved) > 0

    def test_insert_position(self, db):
        """Test inserting a position record."""
        position = PositionRecord(
            symbol="BTCUSDT",
            exchange="Bybit",
            side="LONG",
            quantity=1.0,
            entry_price=45000.0,
            entry_time=datetime.now(),
        )

        position_id = db.insert_position(position)

        assert position_id > 0
        positions = db.get_open_positions("BTCUSDT")
        assert len(positions) > 0

    def test_insert_funding(self, db):
        """Test inserting funding records."""
        funding = FundingRecord(
            symbol="BTCUSDT",
            exchange="Bybit",
            timestamp=datetime.now(),
            funding_rate=0.00015,
            position_size=1.0,
            amount=0.00015,
        )

        funding_id = db.insert_funding(funding)

        assert funding_id > 0
        history = db.get_funding_history("BTCUSDT")
        assert len(history) > 0

    def test_get_trade_statistics(self, db):
        """Test retrieving trade statistics."""
        # Insert some trades
        for i in range(3):
            trade = TradeRecord(
                symbol="BTCUSDT",
                entry_time=datetime.now(),
                exit_time=datetime.now(),
                entry_price=45000.0,
                exit_price=45000.0 + (i * 100),
                quantity=1.0,
                realized_pnl=i * 100,
                total_pnl=i * 100,
            )
            db.insert_trade(trade)

        stats = db.get_trade_statistics()

        assert stats.get("total_trades", 0) == 3
        assert stats.get("winning_trades", 0) >= 0


class TestLiveDashboard:
    @pytest.fixture
    def dashboard(self):
        position_manager = PositionManager()
        funding_accumulator = FundingAccumulator()
        slippage_tracker = SlippageTracker()
        circuit_breaker = CircuitBreaker(10000.0)

        return LiveDashboard(
            position_manager,
            funding_accumulator,
            slippage_tracker,
            circuit_breaker
        )

    def test_initialization(self, dashboard):
        """Test dashboard initialization."""
        assert dashboard.position_manager is not None

    def test_get_dashboard_data(self, dashboard):
        """Test getting dashboard data."""
        data = dashboard.get_dashboard_data()

        assert "timestamp" in data
        assert "uptime_seconds" in data
        assert "positions" in data
        assert "pnl" in data
        assert "trades" in data

    def test_export_metrics_json(self, dashboard):
        """Test JSON export."""
        json_str = dashboard.export_metrics_json()

        assert isinstance(json_str, str)
        assert '"timestamp"' in json_str

    def test_export_metrics_csv(self, dashboard):
        """Test CSV export."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_file = f"{tmpdir}/metrics.csv"
            dashboard.export_metrics_csv(csv_file)

            # File should be created even if empty
            assert Path(csv_file).exists()
