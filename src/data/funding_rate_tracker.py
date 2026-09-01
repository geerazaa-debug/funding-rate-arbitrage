import asyncio
import time
from collections import deque
from typing import Dict, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime

from src.utils.logger import setup_logger
from src.data.rest_api_client import RESTAPIHub

logger = setup_logger("funding_rate_tracker")


@dataclass
class FundingRate:
    """Represents a single funding rate data point."""
    exchange: str
    symbol: str
    rate: float
    timestamp: int
    received_at: float

    def __repr__(self):
        return f"FundingRate({self.exchange}, {self.symbol}, {self.rate:.6f}, {self.timestamp})"


@dataclass
class FundingRateDifferential:
    """Represents the differential between two exchanges."""
    symbol: str
    bybit_rate: Optional[float]
    binance_rate: Optional[float]
    differential: Optional[float]  # Binance - Bybit
    timestamp: float
    bybit_age_s: Optional[float] = None
    binance_age_s: Optional[float] = None

    def __repr__(self):
        bybit = f"{self.bybit_rate:.6f}" if self.bybit_rate else "None"
        binance = f"{self.binance_rate:.6f}" if self.binance_rate else "None"
        diff = f"{self.differential:.6f}" if self.differential else "None"
        return f"Diff({self.symbol}: Bybit={bybit}, Binance={binance}, Delta={diff})"


class FundingRateTracker:
    """Central hub for tracking funding rates and calculating differentials."""

    def __init__(self, symbols: list[str], max_history: int = 480):
        """
        Initialize tracker.

        Args:
            symbols: List of trading pairs (e.g., ["BTCUSDT", "ETHUSDT"])
            max_history: Max funding rates to keep in memory per symbol/exchange (8h * 60min)
        """
        self.symbols = symbols
        self.max_history = max_history
        self.rest_hub = RESTAPIHub()

        # In-memory caches: symbol -> deque of FundingRate
        self.bybit_rates: Dict[str, deque] = {s: deque(maxlen=max_history) for s in symbols}
        self.binance_rates: Dict[str, deque] = {s: deque(maxlen=max_history) for s in symbols}

        # Latest rates cache (fast access)
        self.latest_rates: Dict[str, Dict[str, Optional[FundingRate]]] = {
            s: {"bybit": None, "binance": None} for s in symbols
        }

        # Differential tracking
        self.latest_differentials: Dict[str, Optional[FundingRateDifferential]] = {
            s: None for s in symbols
        }

        self.alerts_triggered = []
        logger.info(f"Initialized tracker for {len(symbols)} symbols")

    async def process_websocket_message(self, message: Dict, exchange: str):
        """Process incoming WebSocket message."""
        try:
            if exchange.lower() == "bybit":
                await self._process_bybit_message(message)
            elif exchange.lower() == "binance":
                await self._process_binance_message(message)
        except Exception as e:
            logger.error(f"Error processing {exchange} message: {e}")

    async def _process_bybit_message(self, message: Dict):
        """Parse and store Bybit funding rate message."""
        try:
            # Bybit public funding rate message structure
            if "data" in message and "fundingRate" in message["data"]:
                data = message["data"]
                symbol = data.get("symbol")
                rate = float(data.get("fundingRate"))
                timestamp = int(data.get("fundingRateTimestamp", time.time() * 1000))

                if symbol in self.symbols:
                    funding_rate = FundingRate(
                        exchange="bybit",
                        symbol=symbol,
                        rate=rate,
                        timestamp=timestamp,
                        received_at=time.time(),
                    )
                    self.bybit_rates[symbol].append(funding_rate)
                    self.latest_rates[symbol]["bybit"] = funding_rate

                    # Update differential
                    await self._update_differential(symbol)
                    logger.debug(f"[Bybit] Updated {symbol}: {rate:.6f}")
        except Exception as e:
            logger.error(f"Error processing Bybit message: {e}")

    async def _process_binance_message(self, message: Dict):
        """Parse and store Binance funding rate message."""
        try:
            # Binance markPrice message structure
            if "e" in message and message["e"] == "markPriceUpdate":
                symbol = message.get("s")
                rate = float(message.get("r", 0))
                timestamp = int(message.get("E", int(time.time() * 1000)))

                if symbol in self.symbols:
                    funding_rate = FundingRate(
                        exchange="binance",
                        symbol=symbol,
                        rate=rate,
                        timestamp=timestamp,
                        received_at=time.time(),
                    )
                    self.binance_rates[symbol].append(funding_rate)
                    self.latest_rates[symbol]["binance"] = funding_rate

                    # Update differential
                    await self._update_differential(symbol)
                    logger.debug(f"[Binance] Updated {symbol}: {rate:.6f}")
        except Exception as e:
            logger.error(f"Error processing Binance message: {e}")

    async def _update_differential(self, symbol: str):
        """Calculate and store funding rate differential."""
        bybit = self.latest_rates[symbol]["bybit"]
        binance = self.latest_rates[symbol]["binance"]

        bybit_rate = bybit.rate if bybit else None
        binance_rate = binance.rate if binance else None
        differential = None

        if bybit_rate is not None and binance_rate is not None:
            differential = binance_rate - bybit_rate

        # Calculate age of rates
        now = time.time()
        bybit_age = (now - bybit.received_at) if bybit else None
        binance_age = (now - binance.received_at) if binance else None

        diff = FundingRateDifferential(
            symbol=symbol,
            bybit_rate=bybit_rate,
            binance_rate=binance_rate,
            differential=differential,
            timestamp=now,
            bybit_age_s=bybit_age,
            binance_age_s=binance_age,
        )
        self.latest_differentials[symbol] = diff

        # Check for alerts
        await self._check_alerts(symbol, diff)

    async def _check_alerts(self, symbol: str, diff: FundingRateDifferential):
        """Check for trading alerts."""
        # Alert if differential is large (potential arb)
        if diff.differential is not None and abs(diff.differential) > 0.10:
            alert = f"Large differential detected for {symbol}: {diff.differential:.6f}"
            logger.warning(alert)
            self.alerts_triggered.append(alert)

        # Alert if data is stale
        if diff.bybit_age_s and diff.bybit_age_s > 30:
            alert = f"Bybit data stale for {symbol}: {diff.bybit_age_s:.1f}s old"
            logger.warning(alert)

        if diff.binance_age_s and diff.binance_age_s > 30:
            alert = f"Binance data stale for {symbol}: {diff.binance_age_s:.1f}s old"
            logger.warning(alert)

    async def backfill_history(self, symbol: str, limit: int = 100):
        """Backfill historical funding rates via REST API."""
        logger.info(f"Backfilling history for {symbol}")

        bybit_rates = await self.rest_hub.bybit.get_funding_rates(symbol, limit)
        binance_rates = await self.rest_hub.binance.get_funding_rates(symbol, limit)

        if bybit_rates:
            for rate_data in bybit_rates:
                self.bybit_rates[symbol].append(
                    FundingRate(
                        exchange="bybit",
                        symbol=symbol,
                        rate=rate_data["funding_rate"],
                        timestamp=rate_data["timestamp"],
                        received_at=time.time(),
                    )
                )

        if binance_rates:
            for rate_data in binance_rates:
                self.binance_rates[symbol].append(
                    FundingRate(
                        exchange="binance",
                        symbol=symbol,
                        rate=rate_data["funding_rate"],
                        timestamp=rate_data["timestamp"],
                        received_at=time.time(),
                    )
                )

        logger.info(
            f"Backfilled {symbol}: Bybit={len(bybit_rates) if bybit_rates else 0}, "
            f"Binance={len(binance_rates) if binance_rates else 0}"
        )

    def get_latest_differential(self, symbol: str) -> Optional[FundingRateDifferential]:
        """Get latest funding rate differential for a symbol."""
        return self.latest_differentials.get(symbol)

    def get_all_differentials(self) -> Dict[str, Optional[FundingRateDifferential]]:
        """Get latest differentials for all symbols."""
        return self.latest_differentials.copy()

    def get_rate_history(self, symbol: str, exchange: str) -> list[FundingRate]:
        """Get funding rate history for a symbol/exchange."""
        if exchange.lower() == "bybit":
            return list(self.bybit_rates.get(symbol, []))
        elif exchange.lower() == "binance":
            return list(self.binance_rates.get(symbol, []))
        return []

    def get_stats(self, symbol: str, exchange: str) -> Dict:
        """Calculate stats for a symbol's funding rates."""
        rates = self.get_rate_history(symbol, exchange)
        if not rates:
            return {"count": 0, "avg": None, "min": None, "max": None}

        values = [r.rate for r in rates]
        return {
            "count": len(values),
            "avg": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
        }

    async def close(self):
        """Clean up resources."""
        await self.rest_hub.close()
        logger.info("Tracker closed")
