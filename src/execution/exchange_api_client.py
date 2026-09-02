import asyncio
import hmac
import hashlib
import time
import aiohttp
import json
from abc import ABC, abstractmethod
from typing import Optional, Dict, List, Tuple
from urllib.parse import urlencode

from src.utils.logger import setup_logger

logger = setup_logger("exchange_api_client")


class AuthenticatedExchangeClient(ABC):
    """Base authenticated exchange API client with signature support."""

    def __init__(self, api_key: str, api_secret: str, exchange: str, base_url: str,
                 timeout: float = 10.0):
        self.api_key = api_key
        self.api_secret = api_secret
        self.exchange = exchange
        self.base_url = base_url
        self.timeout = timeout
        self.session: Optional[aiohttp.ClientSession] = None
        self.orders: Dict[str, Dict] = {}  # order_id -> order_data

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create session."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    @abstractmethod
    def _prepare_authenticated(self, method: str, params: Dict) -> Tuple[Dict, Optional[Dict], Optional[str]]:
        """
        Build the signed request.

        Returns (headers, query_params, body_string). Exactly the body string
        returned here is transmitted, because both exchanges sign the literal
        payload - signing one representation and sending another is why every
        authenticated call previously failed.
        """
        raise NotImplementedError

    async def _request(self, method: str, endpoint: str, params: Optional[Dict] = None,
                      is_authenticated: bool = False) -> Optional[Dict]:
        """Make an HTTP request, signing it when required."""
        params = dict(params or {})
        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"
        verb = method.upper()

        if is_authenticated:
            headers, query, body = self._prepare_authenticated(verb, params)
        else:
            headers, query, body = {}, params, None

        if body is not None:
            headers.setdefault("Content-Type", "application/json")

        try:
            async with session.request(
                verb, url,
                params=query,
                data=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    text = await response.text()
                    logger.error(f"[{self.exchange}] Non-JSON response "
                                 f"(HTTP {response.status}): {text[:300]}")
                    return None

                if response.status in (200, 201):
                    return data

                logger.error(f"[{self.exchange}] HTTP {response.status}: {data}")
                return None

        except asyncio.TimeoutError:
            logger.error(f"[{self.exchange}] Request timeout: {verb} {endpoint}")
            return None
        except Exception as e:
            logger.error(f"[{self.exchange}] Request error on {verb} {endpoint}: {e}")
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

    RECV_WINDOW = "5000"

    def _prepare_authenticated(self, method: str, params: Dict) -> Tuple[Dict, Optional[Dict], Optional[str]]:
        """
        Sign a Bybit v5 request.

        Bybit signs timestamp + api_key + recv_window + payload, where payload is
        the URL query string for GET/DELETE and the raw JSON body for POST. The
        signed payload and the transmitted payload must be byte-identical, so the
        serialised body produced here is the one handed to the transport.
        """
        timestamp = str(int(time.time() * 1000))

        if method in ("GET", "DELETE"):
            # Bybit verifies against the query string in the order it is sent.
            payload = urlencode(params) if params else ""
            query: Optional[Dict] = params or None
            body: Optional[str] = None
        else:
            # separators removes the whitespace json.dumps adds by default, which
            # would otherwise be included in the body but not in the digest.
            payload = json.dumps(params, separators=(",", ":")) if params else ""
            query = None
            body = payload

        signature = hmac.new(
            self.api_secret.encode(),
            f"{timestamp}{self.api_key}{self.RECV_WINDOW}{payload}".encode(),
            hashlib.sha256,
        ).hexdigest()

        headers = {
            "X-BAPI-SIGN": signature,
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": self.RECV_WINDOW,
        }
        return headers, query, body

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
        """
        Get order status from Bybit.

        /v5/order/realtime only returns open orders, so a market order that has
        already filled is absent from it. History is queried as a fallback,
        otherwise a filled exit could never be confirmed.
        """
        params = {"category": "linear", "symbol": symbol, "orderId": order_id}

        for endpoint in ("/v5/order/realtime", "/v5/order/history"):
            response = await self._request("GET", endpoint, params, is_authenticated=True)

            if not response:
                continue
            if response.get("retCode") != 0:
                logger.error(f"[Bybit] Status fetch failed on {endpoint}: {response}")
                continue

            orders = response.get("result", {}).get("list", [])
            if orders:
                return orders[0]

        logger.warning(f"[Bybit] Order {order_id} not found in realtime or history")
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

    RECV_WINDOW = 5000

    def _prepare_authenticated(self, method: str, params: Dict) -> Tuple[Dict, Optional[Dict], Optional[str]]:
        """
        Sign a Binance USD-M futures request.

        Binance signs the URL-encoded parameter string and expects the resulting
        digest back as a `signature` parameter. Every parameter, for every verb,
        travels in the query string. The previous implementation computed the
        digest and then discarded it, so every authenticated call returned
        -1022 "Signature for this request is not valid".
        """
        signed = dict(params)
        # A caller may already have set timestamp; keep one authoritative value so
        # the digest cannot disagree with the transmitted parameters.
        signed["timestamp"] = int(time.time() * 1000)
        signed.setdefault("recvWindow", self.RECV_WINDOW)

        query_string = urlencode(signed)
        signed["signature"] = hmac.new(
            self.api_secret.encode(),
            query_string.encode(),
            hashlib.sha256,
        ).hexdigest()

        return {"X-MBX-APIKEY": self.api_key}, signed, None

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
        """
        Get order status from Binance.

        Queries /fapi/v1/order, which returns an order in any state. The previous
        implementation used /fapi/v1/openOrders, which by definition excludes
        filled orders - so a completed exit always came back as None.
        """
        try:
            numeric_id = int(order_id)
        except (TypeError, ValueError):
            logger.error(f"[Binance] Invalid order id: {order_id!r}")
            return None

        params = {"symbol": symbol.upper(), "orderId": numeric_id}
        response = await self._request("GET", "/fapi/v1/order", params, is_authenticated=True)

        if isinstance(response, dict) and "orderId" in response:
            return response

        logger.warning(f"[Binance] Order {order_id} status unavailable: {response}")
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
