import aiohttp
import asyncio
from typing import Optional, Dict, List
from datetime import datetime
import json

from src.utils.logger import setup_logger

logger = setup_logger("rest_api_client")


class RESTAPIClient:
    """Base REST API client with retry logic and error handling."""

    def __init__(self, base_url: str, exchange: str, timeout: int = 10, max_retries: int = 3):
        self.base_url = base_url
        self.exchange = exchange
        self.timeout = timeout
        self.max_retries = max_retries
        self.session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def _request(self, method: str, endpoint: str, **kwargs) -> Optional[Dict]:
        """Make HTTP request with retry logic."""
        url = f"{self.base_url}{endpoint}"
        session = await self._get_session()

        for attempt in range(self.max_retries):
            try:
                async with session.request(
                    method, url, timeout=aiohttp.ClientTimeout(total=self.timeout), **kwargs
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 429:
                        logger.warning(f"[{self.exchange}] Rate limited, retrying in {2 ** attempt}s")
                        await asyncio.sleep(2 ** attempt)
                        continue
                    else:
                        logger.error(f"[{self.exchange}] HTTP {response.status}: {await response.text()}")
                        return None
            except asyncio.TimeoutError:
                logger.error(f"[{self.exchange}] Request timeout (attempt {attempt + 1})")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"[{self.exchange}] Request error: {e} (attempt {attempt + 1})")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2 ** attempt)

        logger.error(f"[{self.exchange}] Failed after {self.max_retries} attempts")
        return None

    async def close(self):
        """Close session."""
        if self.session:
            await self.session.close()


class BybitRESTClient(RESTAPIClient):
    """Bybit REST API client."""

    def __init__(self):
        super().__init__("https://api.bybit.com", "Bybit")

    async def get_funding_rates(self, symbol: str, limit: int = 100) -> Optional[List[Dict]]:
        """Fetch funding rate history from Bybit."""
        try:
            params = {"symbol": symbol, "limit": limit}
            data = await self._request("GET", "/v5/market/funding/history", params=params)

            if data and data.get("retCode") == 0:
                rates = []
                for item in data.get("result", {}).get("list", []):
                    rates.append({
                        "exchange": "bybit",
                        "symbol": symbol,
                        "funding_rate": float(item["fundingRate"]),
                        "timestamp": int(item["fundingRateTimestamp"]),
                    })
                logger.info(f"[Bybit] Fetched {len(rates)} funding rates for {symbol}")
                return rates
            else:
                logger.error(f"[Bybit] API error: {data}")
                return None
        except Exception as e:
            logger.error(f"[Bybit] Error fetching funding rates: {e}")
            return None

    async def get_latest_funding_rate(self, symbol: str) -> Optional[Dict]:
        """Fetch latest funding rate for a symbol."""
        rates = await self.get_funding_rates(symbol, limit=1)
        return rates[0] if rates else None


class BinanceRESTClient(RESTAPIClient):
    """Binance REST API client."""

    def __init__(self):
        super().__init__("https://fapi.binance.com", "Binance")

    async def get_funding_rates(self, symbol: str, limit: int = 100) -> Optional[List[Dict]]:
        """Fetch funding rate history from Binance."""
        try:
            params = {"symbol": symbol.upper(), "limit": limit}
            data = await self._request("GET", "/fapi/v1/fundingRate", params=params)

            if data and isinstance(data, list):
                rates = []
                for item in data:
                    rates.append({
                        "exchange": "binance",
                        "symbol": symbol,
                        "funding_rate": float(item["fundingRate"]),
                        "timestamp": int(item["fundingTime"]),
                    })
                logger.info(f"[Binance] Fetched {len(rates)} funding rates for {symbol}")
                return rates
            else:
                logger.error(f"[Binance] Invalid response: {data}")
                return None
        except Exception as e:
            logger.error(f"[Binance] Error fetching funding rates: {e}")
            return None

    async def get_latest_funding_rate(self, symbol: str) -> Optional[Dict]:
        """Fetch latest funding rate for a symbol."""
        try:
            params = {"symbol": symbol.upper()}
            data = await self._request("GET", "/fapi/v1/fundingRate", params=params)

            if data and isinstance(data, list) and len(data) > 0:
                item = data[-1]  # Latest is last
                return {
                    "exchange": "binance",
                    "symbol": symbol,
                    "funding_rate": float(item["fundingRate"]),
                    "timestamp": int(item["fundingTime"]),
                }
            return None
        except Exception as e:
            logger.error(f"[Binance] Error fetching latest funding rate: {e}")
            return None

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        """Fetch current mark price."""
        try:
            params = {"symbol": symbol.upper()}
            data = await self._request("GET", "/fapi/v1/premiumIndex", params=params)
            if data:
                return float(data.get("markPrice", 0))
            return None
        except Exception as e:
            logger.error(f"[Binance] Error fetching mark price: {e}")
            return None


class RESTAPIHub:
    """Manages REST API clients for both exchanges."""

    def __init__(self):
        self.bybit = BybitRESTClient()
        self.binance = BinanceRESTClient()

    async def fetch_all_funding_rates(self, symbol: str) -> Dict[str, Optional[Dict]]:
        """Fetch latest funding rates from both exchanges."""
        bybit_rate = await self.bybit.get_latest_funding_rate(symbol)
        binance_rate = await self.binance.get_latest_funding_rate(symbol)

        return {
            "bybit": bybit_rate,
            "binance": binance_rate,
        }

    async def close(self):
        """Close all sessions."""
        await self.bybit.close()
        await self.binance.close()
