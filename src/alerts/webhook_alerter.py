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
        # Per-service extras, e.g. Telegram's chat_id, which is mandatory and was
        # previously absent - making every Telegram alert fail with HTTP 400.
        self.webhook_options: Dict[str, Dict] = {}
        self.sent_alerts: List[Alert] = []
        self.failed_alerts: List[Alert] = []
        self.session: Optional[aiohttp.ClientSession] = None

        logger.info("Initialized webhook alerter")

    def register_webhook(self, service: str, url: str, **options):
        """
        Register a webhook URL.

        Telegram requires chat_id: register_webhook("telegram", url, chat_id="...").
        """
        if service.lower() == "telegram" and not options.get("chat_id"):
            raise ValueError(
                "Telegram webhooks require chat_id: "
                'register_webhook("telegram", url, chat_id="...")'
            )
        self.webhooks[service] = url
        self.webhook_options[service] = options
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

        # An alert with nowhere to go previously vanished: it landed in neither
        # sent_alerts nor failed_alerts and logged nothing, so a misconfigured
        # bot looked like it had working alerting while emitting none.
        if not services:
            logger.error(
                f"ALERT NOT DELIVERED (no webhooks registered): "
                f"[{alert.level.value}] {alert.title} - {alert.message}"
            )
            self.failed_alerts.append(alert)
            return False

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
        else:
            # Every configured channel rejected it; make sure the content still
            # reaches the log rather than being lost with the HTTP error.
            logger.error(
                f"ALERT NOT DELIVERED (all channels failed): "
                f"[{alert.level.value}] {alert.title} - {alert.message}"
            )

        return success

    async def _send_to_service(self, service: str, alert: Alert):
        """Send alert to a specific service."""
        url = self.webhooks[service]

        options = self.webhook_options.get(service, {})

        if service.lower() == "slack":
            payload = self._format_slack(alert)
        elif service.lower() == "telegram":
            payload = self._format_telegram(alert, options.get("chat_id"))
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

    def _format_telegram(self, alert: Alert, chat_id: Optional[str] = None) -> Dict:
        """
        Format alert for Telegram's sendMessage.

        chat_id is mandatory - omitting it returns HTTP 400. Legacy "Markdown"
        parse mode uses *single asterisks* for bold; **double** renders literally.
        """
        level_emoji = {
            AlertLevel.INFO: "ℹ️",
            AlertLevel.WARNING: "⚠️",
            AlertLevel.CRITICAL: "\U0001f6a8",
        }

        text = f"{level_emoji.get(alert.level, '\U0001f4cc')} *{alert.title}*\n"
        text += f"{alert.message}\n\n"
        text += f"_Type: {alert.type.value}_\n"
        text += f"_Time: {time.strftime('%H:%M:%S', time.localtime(alert.timestamp))}_"

        payload = {
            "text": text,
            "parse_mode": "Markdown",
        }
        if chat_id:
            payload["chat_id"] = chat_id
        return payload

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
            title="EMERGENCY EXIT TRIGGERED",
            message=f"Reason: {reason}",
            data={"reason": reason},
        )
        await self.send_alert(alert, services)

    async def alert_circuit_breaker(self, reason: str, services: Optional[List[str]] = None):
        """Alert circuit breaker activation."""
        alert = Alert(
            type=AlertType.CIRCUIT_BREAKER,
            level=AlertLevel.CRITICAL,
            title="Circuit Breaker Halted",
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

    async def alert_error(self, message: str, context: Optional[str] = None,
                         services: Optional[List[str]] = None):
        """
        Alert on an unexpected bot error.

        Called from exception handlers, so it must never raise: an alerting
        failure inside an `except` block would mask the original error and skip
        whatever recovery follows it.
        """
        try:
            alert = Alert(
                type=AlertType.ERROR,
                level=AlertLevel.CRITICAL,
                title="Bot Error",
                message=f"{message}\nContext: {context}" if context else message,
                data={"error": message, "context": context},
            )
            await self.send_alert(alert, services)
        except Exception as e:
            logger.error(f"alert_error failed while reporting '{message}': {e}")

    async def alert_high_slippage(self, symbol: str, exchange: str, slippage_bps: float,
                                 services: Optional[List[str]] = None):
        """Alert on an unusually costly fill."""
        alert = Alert(
            type=AlertType.HIGH_SLIPPAGE,
            level=AlertLevel.WARNING,
            title=f"High Slippage: {symbol}",
            message=f"{exchange} filled {slippage_bps:.1f} bps away from expected",
            data={"symbol": symbol, "exchange": exchange, "slippage_bps": slippage_bps},
        )
        await self.send_alert(alert, services)

    async def alert_funding_update(self, symbol: str, amount: float,
                                  services: Optional[List[str]] = None):
        """Alert when a funding payment is received."""
        alert = Alert(
            type=AlertType.FUNDING_UPDATE,
            level=AlertLevel.INFO,
            title=f"Funding Received: {symbol}",
            message=f"Amount: ${amount:.2f}",
            data={"symbol": symbol, "amount": amount},
        )
        await self.send_alert(alert, services)

    async def alert_connection_lost(self, exchange: str,
                                   services: Optional[List[str]] = None):
        """Alert when an exchange connection drops."""
        alert = Alert(
            type=AlertType.CONNECTION_LOST,
            level=AlertLevel.CRITICAL,
            title=f"Connection Lost: {exchange}",
            message=f"Lost connection to {exchange}",
            data={"exchange": exchange},
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
