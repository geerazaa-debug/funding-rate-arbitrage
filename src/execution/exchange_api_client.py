import hmac
import hashlib
import time
import aiohttp
import json
from abc import ABC, abstractmethod
from typing import Optional, Dict, List
from urllib.parse import urlencode

from src.utils.logger import setup_logger

logger = setup_logger("exchange_api_client")


class AuthenticatedExchangeClient(ABC):
    """Base authenticated exchange API client with signature support."""

    def __init__(self, api_key: str, api_secret: str, exchange: str, base_url: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.exchange = exchange
        self.base_url = base_url
        self.session: Optional[aiohttp.ClientSession] = None
        self.orders: Dict[str, Dict] = {}  # order_id -> order_data

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create session."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    @abstractmethod
    async def _sign_request(self, method: str, endpoint: str, params: Dict) -> Dict:
        """Sign request according to exchange-specific requirements."""
        pass

    async def _request(self, method: str, endpoint: str, params: Optional[Dict] = None,
                      is_authenticated: bool = False) -> Optional[Dict]:
        """Make HTTP request with optional signature."""
        if params is None:
            params = {}

        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"

        try:
            headers = {"Content-Type": "application/json"}

            if is_authenticated:
                signed_headers = await self._sign_request(method, endpoint, params)
                headers.update(signed_headers)

            async with session.request(
                method, url, json=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                data = await response.json()

                if response.status in [200, 201]:
                    return data
                else:
                    logger.error(f"[{self.exchange}] HTTP {response.status}: {data}")
                    return None

        except Exception as e:
            logger.error(f"[{self.exchange}] Request error: {e}")
            return None

    async def close(self):
        """Close session."""
        if self.session:
            await self.session.close()


class BybitLiveClient(AuthenticatedExchangeClient):
    """Bybit authenticated REST client for live trading."""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        base_url = "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"
        super().__init__(api_key, api_secret, "Bybit", base_url)
        self.testnet = testnet

    async def _sign_request(self, method: str, endpoint: str, params: Dict) -> Dict:
        """Sign Bybit request using HMAC-SHA256."""
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"

        # Build parameter string
        param_str = urlencode(params) if params else ""
        sign_str = f"{timestamp}{self.api_key}{recv_window}{param_str}"

        signature = hmac.new(
            self.api_secret.encode(),
            sign_str.encode(),
            hashlib.sha256
        ).hexdigest()

        return {
            "X-BAPI-SIGN": signature,
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
        }

    async def place_order(self, symbol: str, side: str, qty: float, order_type: str = "Market",
                         price: Optional[float] = None, reduce_only: bool = False) -> Optional[Dict]:
        """Place order on Bybit."""
        params = {
            "category": "linear",
            "symbol": symbol,
            "side": side.upper(),
            "orderType": order_type,
            "qty": str(qty),
            "reduceOnly": reduce_only,
        }

        if price:
            params["price"] = str(price)

        logger.info(f"[Bybit] Placing {side} order: {qty} {symbol}")

        response = await self._request("POST", "/v5/order/create", params, is_authenticated=True)

        if response and response.get("retCode") == 0:
            order_data = response.get("result", {})
            order_id = order_data.get("orderId")
            logger.info(f"[Bybit] Order placed: {order_id}")
            self.orders[order_id] = {
                "exchange": "bybit",
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "order_id": order_id,
                "status": "pending",
                "timestamp": time.time(),
            }
            return order_data
        else:
            logger.error(f"[Bybit] Order placement failed: {response}")
            return None

    async def cancel_order(self, symbol: str, order_id: str) -> Optional[Dict]:
        """Cancel order on Bybit."""
        params = {
            "category": "linear",
            "symbol": symbol,
            "orderId": order_id,
        }

        response = await self._request("POST", "/v5/order/cancel", params, is_authenticated=True)

        if response and response.get("retCode") == 0:
            logger.info(f"[Bybit] Order cancelled: {order_id}")
            return response.get("result", {})
        else:
            logger.error(f"[Bybit] Cancel failed: {response}")
            return None

    async def get_order_status(self, symbol: str, order_id: str) -> Optional[Dict]:
        """Get order status from Bybit."""
        params = {
            "category": "linear",
            "symbol": symbol,
            "orderId": order_id,
        }

        response = await self._request("GET", "/v5/order/realtime", params, is_authenticated=True)

        if response and response.get("retCode") == 0:
            orders = response.get("result", {}).get("list", [])
            if orders:
                return orders[0]
        else:
            logger.error(f"[Bybit] Status fetch failed: {response}")

        return None

    async def get_positions(self, symbol: Optional[str] = None) -> Optional[List[Dict]]:
        """Get current positions."""
        params = {
            "category": "linear",
        }
        if symbol:
            params["symbol"] = symbol

        response = await self._request("GET", "/v5/position/list", params, is_authenticated=True)

        if response and response.get("retCode") == 0:
            return response.get("result", {}).get("list", [])
        else:
            logger.error(f"[Bybit] Position fetch failed: {response}")
            return None


class BinanceLiveClient(AuthenticatedExchangeClient):
    """Binance authenticated REST client for live trading."""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        base_url = "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"
        super().__init__(api_key, api_secret, "Binance", base_url)
        self.testnet = testnet

    async def _sign_request(self, method: str, endpoint: str, params: Dict) -> Dict:
        """Sign Binance request using HMAC-SHA256."""
        timestamp = str(int(time.time() * 1000))
        param_str = urlencode(params) if params else ""
        sign_str = f"{param_str}&timestamp={timestamp}"

        signature = hmac.new(
            self.api_secret.encode(),
            sign_str.encode(),
            hashlib.sha256
        ).hexdigest()

        return {
            "X-MBX-APIKEY": self.api_key,
        }

    async def place_order(self, symbol: str, side: str, qty: float, order_type: str = "MARKET",
                         price: Optional[float] = None, reduce_only: bool = False) -> Optional[Dict]:
        """Place order on Binance."""
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": order_type,
            "quantity": qty,
            "timestamp": int(time.time() * 1000),
        }

        if price:
            params["price"] = price

        if reduce_only:
            params["reduceOnly"] = "true"

        logger.info(f"[Binance] Placing {side} order: {qty} {symbol}")

        response = await self._request("POST", "/fapi/v1/order", params, is_authenticated=True)

        if response and "orderId" in response:
            order_id = str(response.get("orderId"))
            logger.info(f"[Binance] Order placed: {order_id}")
            self.orders[order_id] = {
                "exchange": "binance",
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "order_id": order_id,
                "status": "pending",
                "timestamp": time.time(),
            }
            return response
        else:
            logger.error(f"[Binance] Order placement failed: {response}")
            return None

    async def cancel_order(self, symbol: str, order_id: str) -> Optional[Dict]:
        """Cancel order on Binance."""
        params = {
            "symbol": symbol,
            "orderId": int(order_id),
            "timestamp": int(time.time() * 1000),
        }

        response = await self._request("DELETE", "/fapi/v1/order", params, is_authenticated=True)

        if response and "orderId" in response:
            logger.info(f"[Binance] Order cancelled: {order_id}")
            return response
        else:
            logger.error(f"[Binance] Cancel failed: {response}")
            return None

    async def get_order_status(self, symbol: str, order_id: str) -> Optional[Dict]:
        """Get order status from Binance."""
        params = {
            "symbol": symbol,
            "orderId": int(order_id),
            "timestamp": int(time.time() * 1000),
        }

        response = await self._request("GET", "/fapi/v1/openOrders", params, is_authenticated=True)

        if response and isinstance(response, list):
            for order in response:
                if str(order.get("orderId")) == order_id:
                    return order
        else:
            logger.error(f"[Binance] Status fetch failed: {response}")

        return None

    async def get_positions(self, symbol: Optional[str] = None) -> Optional[List[Dict]]:
        """Get current positions."""
        params = {
            "timestamp": int(time.time() * 1000),
        }

        response = await self._request("GET", "/fapi/v2/positionRisk", params, is_authenticated=True)

        if response and isinstance(response, list):
            if symbol:
                return [p for p in response if p.get("symbol") == symbol]
            return response
        else:
            logger.error(f"[Binance] Position fetch failed: {response}")
            return None


class LiveExchangeHub:
    """Manages authenticated clients for both exchanges."""

    def __init__(self, bybit_key: str, bybit_secret: str,
                 binance_key: str, binance_secret: str,
                 testnet: bool = True):
        self.bybit = BybitLiveClient(bybit_key, bybit_secret, testnet=testnet)
        self.binance = BinanceLiveClient(binance_key, binance_secret, testnet=testnet)
        self.testnet = testnet

        logger.info(f"Initialized live exchange hub (testnet={testnet})")

    async def place_dual_order(self, symbol: str, bybit_side: str, binance_side: str,
                              quantity: float, timeout_s: float = 5.0) -> Optional[Dict]:
        """Place simultaneous orders on both exchanges."""
        import asyncio

        logger.info(f"Placing dual live orders: {symbol} {bybit_side}/{binance_side} x{quantity}")

        try:
            bybit_task = asyncio.create_task(
                self.bybit.place_order(symbol, bybit_side, quantity)
            )
            binance_task = asyncio.create_task(
                self.binance.place_order(symbol, binance_side, quantity)
            )

            bybit_order, binance_order = await asyncio.wait_for(
                asyncio.gather(bybit_task, binance_task),
                timeout=timeout_s
            )

            if bybit_order and binance_order:
                return {
                    "bybit": bybit_order,
                    "binance": binance_order,
                    "timestamp": time.time(),
                }
            else:
                logger.error("One order failed, would need rollback")
                return None

        except asyncio.TimeoutError:
            logger.error("Dual order timeout")
            return None
        except Exception as e:
            logger.error(f"Dual order error: {e}")
            return None

    async def close(self):
        """Close all sessions."""
        await self.bybit.close()
        await self.binance.close()
        logger.info("Live exchange hub closed")
