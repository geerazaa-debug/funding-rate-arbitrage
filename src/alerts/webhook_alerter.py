import aiohttp
import json
import time
from typing import Optional, Dict, List
from enum import Enum
from dataclasses import dataclass

from src.utils.logger import setup_logger

logger = setup_logger("webhook_alerter")


class AlertLevel(Enum):
    """Alert severity level."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertType(Enum):
    """Type of alert."""
    TRADE_OPENED = "trade_opened"
    TRADE_CLOSED = "trade_closed"
    POSITION_MISMATCH = "position_mismatch"
    CIRCUIT_BREAKER = "circuit_breaker"
    EMERGENCY_EXIT = "emergency_exit"
    CONNECTION_LOST = "connection_lost"
    HIGH_SLIPPAGE = "high_slippage"
    FUNDING_UPDATE = "funding_update"
    ERROR = "error"


@dataclass
class Alert:
    """Represents an alert to be sent."""
    type: AlertType
    level: AlertLevel
    title: str
    message: str
    data: Optional[Dict] = None
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()


class WebhookAlerter:
    """Sends alerts to multiple webhooks (Slack, Telegram, Discord, etc.)."""

    def __init__(self):
        self.webhooks: Dict[str, str] = {}
        self.sent_alerts: List[Alert] = []
        self.failed_alerts: List[Alert] = []
        self.session: Optional[aiohttp.ClientSession] = None

        logger.info("Initialized webhook alerter")

    def register_webhook(self, service: str, url: str):
        """Register a webhook URL."""
        self.webhooks[service] = url
        logger.info(f"Registered webhook for {service}")

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create session."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def send_alert(self, alert: Alert, services: Optional[List[str]] = None) -> bool:
        """
        Send alert to registered webhooks.

        Args:
            alert: Alert to send
            services: Specific services to send to (None = all)

        Returns:
            True if successfully sent to at least one service
        """
        if services is None:
            services = list(self.webhooks.keys())

        success = False

        for service in services:
            if service not in self.webhooks:
                logger.warning(f"Webhook not registered: {service}")
                continue

            try:
                await self._send_to_service(service, alert)
                success = True
            except Exception as e:
                logger.error(f"Failed to send to {service}: {e}")
                self.failed_alerts.append(alert)

        if success:
            self.sent_alerts.append(alert)
            logger.debug(f"Alert sent: {alert.title}")

        return success

    async def _send_to_service(self, service: str, alert: Alert):
        """Send alert to a specific service."""
        url = self.webhooks[service]

        if service.lower() == "slack":
            payload = self._format_slack(alert)
        elif service.lower() == "telegram":
            payload = self._format_telegram(alert)
        elif service.lower() == "discord":
            payload = self._format_discord(alert)
        else:
            payload = self._format_generic(alert)

        session = await self._get_session()

        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status not in [200, 201]:
                logger.error(f"[{service}] HTTP {response.status}")
                raise Exception(f"HTTP {response.status}")

    def _format_slack(self, alert: Alert) -> Dict:
        """Format alert for Slack."""
        color_map = {
            AlertLevel.INFO: "#36a64f",
            AlertLevel.WARNING: "#ff9900",
            AlertLevel.CRITICAL: "#ff0000",
        }

        return {
            "attachments": [
                {
                    "color": color_map.get(alert.level, "#808080"),
                    "title": alert.title,
                    "text": alert.message,
                    "fields": [
                        {"title": "Level", "value": alert.level.value, "short": True},
                        {"title": "Type", "value": alert.type.value, "short": True},
                        {"title": "Time", "value": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(alert.timestamp)), "short": True},
                    ],
                    "footer": "FundingRateBot",
                    "ts": int(alert.timestamp),
                }
            ]
        }

    def _format_telegram(self, alert: Alert) -> Dict:
        """Format alert for Telegram."""
        level_emoji = {
            AlertLevel.INFO: "ℹ️",
            AlertLevel.WARNING: "⚠️",
            AlertLevel.CRITICAL: "🚨",
        }

        text = f"{level_emoji.get(alert.level, '📌')} **{alert.title}**\n"
        text += f"{alert.message}\n\n"
        text += f"_Type: {alert.type.value}_\n"
        text += f"_Time: {time.strftime('%H:%M:%S', time.localtime(alert.timestamp))}_"

        return {
            "text": text,
            "parse_mode": "Markdown",
        }

    def _format_discord(self, alert: Alert) -> Dict:
        """Format alert for Discord."""
        color_map = {
            AlertLevel.INFO: 3447003,  # Blue
            AlertLevel.WARNING: 15105570,  # Orange
            AlertLevel.CRITICAL: 15158332,  # Red
        }

        return {
            "embeds": [
                {
                    "title": alert.title,
                    "description": alert.message,
                    "color": color_map.get(alert.level, 9807270),
                    "fields": [
                        {"name": "Level", "value": alert.level.value, "inline": True},
                        {"name": "Type", "value": alert.type.value, "inline": True},
                        {"name": "Time", "value": time.strftime("%H:%M:%S", time.localtime(alert.timestamp)), "inline": True},
                    ],
                    "footer": {"text": "FundingRateBot"},
                }
            ]
        }

    def _format_generic(self, alert: Alert) -> Dict:
        """Format alert as generic JSON."""
        return {
            "alert": {
                "type": alert.type.value,
                "level": alert.level.value,
                "title": alert.title,
                "message": alert.message,
                "data": alert.data or {},
                "timestamp": alert.timestamp,
            }
        }

    async def alert_trade_opened(self, symbol: str, exchanges: str, quantity: float,
                                  differential: float, services: Optional[List[str]] = None):
        """Alert when trade is opened."""
        alert = Alert(
            type=AlertType.TRADE_OPENED,
            level=AlertLevel.INFO,
            title=f"Trade Opened: {symbol}",
            message=f"{exchanges} x{quantity}\nDifferential: {differential:.6f}",
            data={"symbol": symbol, "quantity": quantity, "differential": differential},
        )
        await self.send_alert(alert, services)

    async def alert_trade_closed(self, symbol: str, pnl: float, duration_s: float,
                                services: Optional[List[str]] = None):
        """Alert when trade is closed."""
        alert = Alert(
            type=AlertType.TRADE_CLOSED,
            level=AlertLevel.INFO,
            title=f"Trade Closed: {symbol}",
            message=f"P&L: ${pnl:.2f}\nDuration: {duration_s:.0f}s",
            data={"symbol": symbol, "pnl": pnl, "duration_s": duration_s},
        )
        await self.send_alert(alert, services)

    async def alert_emergency_exit(self, reason: str, services: Optional[List[str]] = None):
        """Alert emergency exit."""
        alert = Alert(
            type=AlertType.EMERGENCY_EXIT,
            level=AlertLevel.CRITICAL,
            title="🚨 EMERGENCY EXIT TRIGGERED",
            message=f"Reason: {reason}",
            data={"reason": reason},
        )
        await self.send_alert(alert, services)

    async def alert_circuit_breaker(self, reason: str, services: Optional[List[str]] = None):
        """Alert circuit breaker activation."""
        alert = Alert(
            type=AlertType.CIRCUIT_BREAKER,
            level=AlertLevel.CRITICAL,
            title="⚠️ Circuit Breaker Halted",
            message=f"Reason: {reason}",
            data={"reason": reason},
        )
        await self.send_alert(alert, services)

    async def alert_position_mismatch(self, symbol: str, exchange: str, tracked_qty: float,
                                      actual_qty: float, services: Optional[List[str]] = None):
        """Alert position mismatch."""
        alert = Alert(
            type=AlertType.POSITION_MISMATCH,
            level=AlertLevel.WARNING,
            title=f"Position Mismatch: {symbol} on {exchange}",
            message=f"Tracked: {tracked_qty}\nActual: {actual_qty}",
            data={"symbol": symbol, "exchange": exchange, "tracked": tracked_qty, "actual": actual_qty},
        )
        await self.send_alert(alert, services)

    def get_alert_history(self, limit: int = 50) -> List[Alert]:
        """Get alert history."""
        return self.sent_alerts[-limit:]

    async def close(self):
        """Close session."""
        if self.session:
            await self.session.close()
        logger.info("Webhook alerter closed")
