import asyncio
import json
import time
from abc import ABC, abstractmethod
from typing import Callable, Dict, Optional
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from src.utils.logger import setup_logger

logger = setup_logger("websocket_manager")


class WebSocketManager(ABC):
    """Base WebSocket manager with reconnection logic and message parsing."""

    def __init__(self, url: str, exchange: str, pairs: list[str], on_message: Callable):
        self.url = url
        self.exchange = exchange
        self.pairs = pairs
        self.on_message = on_message
        self.websocket = None
        self.is_connected = False
        self.last_heartbeat = 0
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 10
        self.reconnect_delay = 5

    async def connect(self):
        """Establish WebSocket connection."""
        try:
            logger.info(f"[{self.exchange}] Connecting to {self.url}")
            self.websocket = await websockets.connect(self.url)
            self.is_connected = True
            self.reconnect_attempts = 0
            logger.info(f"[{self.exchange}] Connected successfully")
            await self._subscribe()
            await self._listen()
        except (ConnectionClosed, WebSocketException) as e:
            logger.error(f"[{self.exchange}] Connection failed: {e}")
            await self._handle_reconnect()
        except Exception as e:
            logger.error(f"[{self.exchange}] Unexpected error: {e}")
            await self._handle_reconnect()

    async def _handle_reconnect(self):
        """Reconnect with exponential backoff."""
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            logger.error(f"[{self.exchange}] Max reconnection attempts reached")
            self.is_connected = False
            return

        self.reconnect_attempts += 1
        delay = self.reconnect_delay * (2 ** (self.reconnect_attempts - 1))
        delay = min(delay, 60)  # Cap at 60 seconds
        logger.warning(f"[{self.exchange}] Reconnecting in {delay}s (attempt {self.reconnect_attempts})")
        await asyncio.sleep(delay)
        await self.connect()

    async def _listen(self):
        """Listen for WebSocket messages."""
        try:
            async for message in self.websocket:
                try:
                    data = json.loads(message)
                    self.last_heartbeat = time.time()
                    await self.on_message(data)
                except json.JSONDecodeError as e:
                    logger.error(f"[{self.exchange}] Failed to parse message: {e}")
                except Exception as e:
                    logger.error(f"[{self.exchange}] Error processing message: {e}")
        except ConnectionClosed as e:
            logger.warning(f"[{self.exchange}] Connection closed: {e}")
            self.is_connected = False
            await self._handle_reconnect()
        except Exception as e:
            logger.error(f"[{self.exchange}] Listen error: {e}")
            self.is_connected = False
            await self._handle_reconnect()

    @abstractmethod
    async def _subscribe(self):
        """Override to implement exchange-specific subscription."""
        pass

    async def close(self):
        """Close WebSocket connection gracefully."""
        if self.websocket:
            await self.websocket.close()
            self.is_connected = False
            logger.info(f"[{self.exchange}] Connection closed")


class BybitWebSocketManager(WebSocketManager):
    """Bybit-specific WebSocket manager."""

    def __init__(self, pairs: list[str], on_message: Callable):
        url = "wss://stream.bybit.com/v5/public/linear"
        super().__init__(url, "Bybit", pairs, on_message)

    async def _subscribe(self):
        """Subscribe to Bybit funding rate channels."""
        for pair in self.pairs:
            channel = f"funding.{pair}"
            message = {
                "op": "subscribe",
                "args": [channel]
            }
            try:
                await self.websocket.send(json.dumps(message))
                logger.info(f"[Bybit] Subscribed to {channel}")
            except Exception as e:
                logger.error(f"[Bybit] Failed to subscribe to {channel}: {e}")
                raise


class BinanceWebSocketManager(WebSocketManager):
    """Binance-specific WebSocket manager."""

    def __init__(self, pairs: list[str], on_message: Callable):
        # Binance uses individual streams
        self.base_url = "wss://fstream.binance.com/ws"
        self.pairs = pairs
        super().__init__(self.base_url, "Binance", pairs, on_message)

    async def _subscribe(self):
        """Binance uses URL-based subscriptions for public streams."""
        # For Binance, we construct the URL with all streams
        # This is already handled in connect(), so we just log
        logger.info(f"[Binance] Subscribed to {len(self.pairs)} pairs via URL")

    async def connect(self):
        """Override: Binance uses streams in URL."""
        try:
            streams = [f"{pair.lower()}@markPrice" for pair in self.pairs]
            stream_url = f"{self.base_url}/{'/'.join(streams)}"

            logger.info(f"[Binance] Connecting to {len(self.pairs)} streams")
            self.websocket = await websockets.connect(stream_url)
            self.is_connected = True
            self.reconnect_attempts = 0
            logger.info(f"[Binance] Connected successfully")
            await self._listen()
        except (ConnectionClosed, WebSocketException) as e:
            logger.error(f"[Binance] Connection failed: {e}")
            await self._handle_reconnect()
        except Exception as e:
            logger.error(f"[Binance] Unexpected error: {e}")
            await self._handle_reconnect()


class WebSocketHub:
    """Manages multiple WebSocket connections."""

    def __init__(self, pairs: list[str], on_message: Callable):
        self.pairs = pairs
        self.on_message = on_message
        self.bybit_manager = BybitWebSocketManager(pairs, on_message)
        self.binance_manager = BinanceWebSocketManager(pairs, on_message)
        self.tasks = []

    async def start(self):
        """Start both WebSocket managers concurrently."""
        logger.info("Starting WebSocket hub")
        self.tasks = [
            asyncio.create_task(self.bybit_manager.connect()),
            asyncio.create_task(self.binance_manager.connect()),
        ]
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def stop(self):
        """Stop both WebSocket managers."""
        logger.info("Stopping WebSocket hub")
        await self.bybit_manager.close()
        await self.binance_manager.close()
        for task in self.tasks:
            if not task.done():
                task.cancel()

    def get_status(self) -> Dict[str, bool]:
        """Get connection status for both exchanges."""
        return {
            "bybit": self.bybit_manager.is_connected,
            "binance": self.binance_manager.is_connected,
        }
