"""
Read-only live API check.

Verifies the one thing the unit tests cannot: that Bybit and Binance actually
ACCEPT our HMAC signatures. The test suite proves each digest is well-formed and
that the transmitted bytes match what was signed, but only a real call proves the
exchange agrees.

Read-only by construction - it queries positions and never places an order.

Run before any session that will touch live capital, and after any change to the
signing or transport layer.

Usage (from anywhere):
    python scripts/preflight.py

Credentials are read from .env at the project root, which is gitignored. Never
hardcode them here: the argument to os.getenv is the variable NAME, not its
value. This file is tracked, so a literal key would go straight into history.
"""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Python puts THIS file's directory on sys.path, not the working directory, so
# `src` is not importable without help when run as `python scripts/preflight.py`.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.execution.exchange_api_client import BinanceLiveClient, BybitLiveClient  # noqa: E402

# Anchored to the project root so the script works from any working directory.
load_dotenv(PROJECT_ROOT / ".env")


def require(name: str) -> str:
    """Fetch an env var by name, failing loudly rather than signing with None."""
    value = os.getenv(name)
    if not value:
        print(f"[FAIL] {name} is not set in .env")
        sys.exit(1)
    return value


async def check(label: str, client, endpoint: str, params: dict, mainnet: bool):
    """
    Issue one authenticated read-only call and report precisely what came back.

    Calls _request directly so the exchange's own error body is visible. The
    public wrappers log and return None, which hides the error code - and the
    error code is the whole point here: -1022 or 10004 means the signature was
    rejected, anything else means signing worked and something else is wrong.
    """
    print(f"\n=== {label} ===")
    print(f"  target : {client.base_url}")
    if mainnet:
        print("  WARNING: this is MAINNET, not testnet (read-only call)")

    try:
        response = await client._request(
            "GET", endpoint, params, is_authenticated=True
        )
    except Exception as e:
        print(f"  [FAIL] request raised: {type(e).__name__}: {e}")
        return False
    finally:
        await client.close()

    if response is None:
        print("  [FAIL] no parseable response - see the log above for HTTP status")
        return False

    # Binance positionRisk returns a bare list on success. Checked first: a list
    # has no .get(), and reaching it means the request was accepted.
    if isinstance(response, list):
        live = [p for p in response if float(p.get("positionAmt") or 0) != 0]
        print(f"  [OK] signature accepted - {len(live)} open position(s) "
              f"across {len(response)} symbol(s)")
        return True

    # Bybit signals success with retCode 0; Binance uses an HTTP error plus a
    # negative `code` field, so a dict carrying `code` is a rejection.
    ret_code = response.get("retCode")
    err_code = response.get("code")

    if ret_code == 0:
        positions = response.get("result", {}).get("list", [])
        print(f"  [OK] signature accepted - {len(positions)} open position(s)")
        return True

    if ret_code is not None:
        print(f"  [FAIL] retCode={ret_code}: {response.get('retMsg')}")
        if ret_code == 10004:
            print("         -> signature rejected")
        return False

    if err_code is not None:
        print(f"  [FAIL] code={err_code}: {response.get('msg')}")
        if err_code == -1022:
            print("         -> signature rejected")
        elif err_code == -2015:
            print("         -> bad key, IP restriction, or missing permission")
        return False

    print(f"  [OK] signature accepted - response: {str(response)[:200]}")
    return True


async def main():
    print("Read-only signature verification. No orders are placed.")

    binance_ok = await check(
        "Binance",
        BinanceLiveClient(
            require("BINANCE_API_KEY"),
            require("BINANCE_API_SECRET"),
            testnet=True,
        ),
        "/fapi/v2/positionRisk",
        {},
        mainnet=False,
    )

    bybit_ok = await check(
        "Bybit",
        BybitLiveClient(
            require("BYBIT_API_KEY"),
            require("BYBIT_API_SECRET"),
            testnet=False,  # confirmed mainnet credential
        ),
        "/v5/position/list",
        {"category": "linear", "settleCoin": "USDT"},
        mainnet=True,
    )

    print("\n" + "=" * 60)
    print(f"Binance signing: {'VERIFIED' if binance_ok else 'NOT VERIFIED'}")
    print(f"Bybit signing:   {'VERIFIED' if bybit_ok else 'NOT VERIFIED'}")
    print("=" * 60)

    return 0 if (binance_ok and bybit_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
