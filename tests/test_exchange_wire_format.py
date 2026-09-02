"""End-to-end wire-format tests for the authenticated clients.

These stand in for a live testnet call (blocked by DNS in CI/sandbox). A local
HTTP server captures the real request the client emits, so the assertions cover
the whole path - signer, transport, verb, query, body and headers - rather than
the signer in isolation. What they cannot prove is that the exchange accepts the
digest; only a live call does that.
"""
import hmac
import hashlib
import json
from urllib.parse import urlencode

import pytest
from aiohttp import web

from src.execution.exchange_api_client import BybitLiveClient, BinanceLiveClient

KEY = "testkey"
SECRET = "testsecret"


class CaptureServer:
    """Minimal HTTP server that records the last request it received."""

    def __init__(self, payload):
        self.payload = payload
        self.request = None
        self.body = None
        self.runner = None

    async def start(self):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = list(self.runner.addresses)[0][1]
        return f"http://127.0.0.1:{port}"

    async def _handle(self, request):
        self.request = request
        self.body = await request.text()
        return web.json_response(self.payload)

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()


@pytest.fixture
async def bybit_server():
    server = CaptureServer({"retCode": 0, "result": {"orderId": "b1"}})
    url = await server.start()
    yield server, url
    await server.stop()


@pytest.fixture
async def binance_server():
    server = CaptureServer({"orderId": 991, "symbol": "BTCUSDT", "status": "FILLED"})
    url = await server.start()
    yield server, url
    await server.stop()


class TestBinanceWireFormat:
    async def test_signed_get_is_verifiable_by_the_server(self, binance_server):
        """Recompute the digest server-side exactly as Binance would."""
        server, url = binance_server
        client = BinanceLiveClient(KEY, SECRET, testnet=True)
        client.base_url = url

        result = await client.get_order_status("BTCUSDT", "991")
        await client.close()

        assert result is not None
        assert server.request.method == "GET"
        assert server.request.headers["X-MBX-APIKEY"] == KEY

        received = dict(server.request.query)
        signature = received.pop("signature")
        expected = hmac.new(
            SECRET.encode(), urlencode(received).encode(), hashlib.sha256
        ).hexdigest()
        assert signature == expected, "server-side verification of the digest"

    async def test_order_params_travel_in_query_not_body(self, binance_server):
        server, url = binance_server
        client = BinanceLiveClient(KEY, SECRET, testnet=True)
        client.base_url = url

        await client.place_order("BTCUSDT", "BUY", 1.0, reduce_only=True)
        await client.close()

        assert server.request.query["symbol"] == "BTCUSDT"
        assert server.request.query["side"] == "BUY"
        assert server.request.query["reduceOnly"] == "true"
        assert server.body == "", "Binance futures takes no request body"


class TestBybitWireFormat:
    async def test_post_body_matches_signed_payload(self, bybit_server):
        """The digest must verify against the literal bytes transmitted."""
        server, url = bybit_server
        client = BybitLiveClient(KEY, SECRET, testnet=True)
        client.base_url = url

        await client.place_order("BTCUSDT", "SELL", 1.0, reduce_only=True)
        await client.close()

        assert server.request.method == "POST"
        headers = server.request.headers
        expected = hmac.new(
            SECRET.encode(),
            f"{headers['X-BAPI-TIMESTAMP']}{KEY}"
            f"{BybitLiveClient.RECV_WINDOW}{server.body}".encode(),
            hashlib.sha256,
        ).hexdigest()
        assert headers["X-BAPI-SIGN"] == expected, \
            "signature must verify against the received body"

        sent = json.loads(server.body)
        assert sent["symbol"] == "BTCUSDT"
        assert sent["side"] == "SELL"
        assert sent["reduceOnly"] is True

    async def test_get_sends_query_and_no_body(self, bybit_server):
        server, url = bybit_server
        client = BybitLiveClient(KEY, SECRET, testnet=True)
        client.base_url = url

        await client.get_positions("BTCUSDT")
        await client.close()

        assert server.request.method == "GET"
        assert server.request.query["symbol"] == "BTCUSDT"
        assert server.request.query["category"] == "linear"
        assert server.body == ""

    async def test_reduce_only_reaches_the_wire(self, bybit_server):
        """reduce_only is what stops an emergency exit opening fresh exposure."""
        server, url = bybit_server
        client = BybitLiveClient(KEY, SECRET, testnet=True)
        client.base_url = url

        await client.place_order("BTCUSDT", "SELL", 1.0, reduce_only=True)
        await client.close()

        assert json.loads(server.body)["reduceOnly"] is True
