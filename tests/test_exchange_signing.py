"""Signature correctness tests for the authenticated exchange clients.

These pin the exact digest each exchange expects. The previous implementation
computed a Binance signature and discarded it, and signed a urlencoded Bybit
payload while transmitting JSON - both produced auth failures on every call.
"""
import hmac
import hashlib
import json
from urllib.parse import urlencode

import pytest

from src.execution.exchange_api_client import BybitLiveClient, BinanceLiveClient

KEY = "testkey"
SECRET = "testsecret"


class TestBinanceSigning:
    def setup_method(self):
        self.client = BinanceLiveClient(KEY, SECRET, testnet=True)

    def test_signature_is_present_in_query(self):
        """The regression: the digest used to be computed and thrown away."""
        headers, query, body = self.client._prepare_authenticated(
            "POST", {"symbol": "BTCUSDT", "side": "BUY", "quantity": 1.0}
        )
        assert "signature" in query, "signature must be sent as a parameter"
        assert len(query["signature"]) == 64, "HMAC-SHA256 hex digest is 64 chars"
        assert headers["X-MBX-APIKEY"] == KEY
        assert body is None, "Binance sends everything in the query string"

    def test_signature_matches_independent_computation(self):
        headers, query, _ = self.client._prepare_authenticated(
            "GET", {"symbol": "BTCUSDT"}
        )
        signature = query.pop("signature")
        expected = hmac.new(
            SECRET.encode(), urlencode(query).encode(), hashlib.sha256
        ).hexdigest()
        assert signature == expected

    def test_timestamp_and_recv_window_are_signed(self):
        _, query, _ = self.client._prepare_authenticated("GET", {"symbol": "BTCUSDT"})
        assert "timestamp" in query
        assert query["recvWindow"] == BinanceLiveClient.RECV_WINDOW

    def test_caller_timestamp_does_not_duplicate(self):
        """A caller-supplied timestamp must not desync the digest."""
        _, query, _ = self.client._prepare_authenticated(
            "GET", {"symbol": "BTCUSDT", "timestamp": 1}
        )
        assert query["timestamp"] != 1, "client must own the timestamp"
        signature = query.pop("signature")
        expected = hmac.new(
            SECRET.encode(), urlencode(query).encode(), hashlib.sha256
        ).hexdigest()
        assert signature == expected


class TestBybitSigning:
    def setup_method(self):
        self.client = BybitLiveClient(KEY, SECRET, testnet=True)

    def test_post_signs_the_exact_body_sent(self):
        """Signed payload and transmitted payload must be byte-identical."""
        params = {"category": "linear", "symbol": "BTCUSDT", "side": "Buy"}
        headers, query, body = self.client._prepare_authenticated("POST", params)

        assert query is None, "POST params belong in the body, not the query"
        assert body == json.dumps(params, separators=(",", ":"))

        expected = hmac.new(
            SECRET.encode(),
            f"{headers['X-BAPI-TIMESTAMP']}{KEY}"
            f"{BybitLiveClient.RECV_WINDOW}{body}".encode(),
            hashlib.sha256,
        ).hexdigest()
        assert headers["X-BAPI-SIGN"] == expected

    def test_body_has_no_incidental_whitespace(self):
        """json.dumps' default spacing would be in the body but not the digest."""
        _, _, body = self.client._prepare_authenticated("POST", {"a": 1, "b": 2})
        assert " " not in body

    def test_get_signs_the_query_string(self):
        params = {"category": "linear", "symbol": "BTCUSDT"}
        headers, query, body = self.client._prepare_authenticated("GET", params)

        assert body is None
        assert query == params

        expected = hmac.new(
            SECRET.encode(),
            f"{headers['X-BAPI-TIMESTAMP']}{KEY}"
            f"{BybitLiveClient.RECV_WINDOW}{urlencode(params)}".encode(),
            hashlib.sha256,
        ).hexdigest()
        assert headers["X-BAPI-SIGN"] == expected

    def test_required_headers_present(self):
        headers, _, _ = self.client._prepare_authenticated("GET", {})
        for required in (
            "X-BAPI-SIGN", "X-BAPI-API-KEY",
            "X-BAPI-TIMESTAMP", "X-BAPI-RECV-WINDOW",
        ):
            assert required in headers
