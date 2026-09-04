"""
Order-book depth probe.

The funding analysis measures the SPREAD. It cannot say whether the spread is
capturable, because capturing it means crossing two order books, and on thin
alt perps the price impact of entering can exceed the entire edge.

This measures, for each candidate symbol:
  * how much notional can be filled on each venue within a slippage budget
  * the round-trip slippage cost at a given position size
  * the resulting NET edge after fees and slippage

Capacity is the min of the two venues: a delta-neutral pair is capped by the
thinner leg.

Usage:
    python scripts/depth_probe.py
    python scripts/depth_probe.py --symbols TUSDT ONGUSDT --size 5000
"""
import argparse
import asyncio
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.http import make_session  # noqa: E402
from src.utils.logger import setup_logger  # noqa: E402
from src.storage.database import Database  # noqa: E402

logger = setup_logger("depth_probe")

BYBIT_BOOK = "https://api.bybit.com/v5/market/orderbook"
BINANCE_BOOK = "https://fapi.binance.com/fapi/v1/depth"

# Taker fees, round trip across both legs (open and close on two venues).
# Bybit 0.055% + Binance 0.05% taker, doubled for entry and exit.
ROUND_TRIP_FEE_PCT = (0.00055 + 0.0005) * 2 * 100


def walk_book(levels: List[Tuple[float, float]], notional_usd: float) -> Optional[float]:
    """
    Average fill price when taking `notional_usd` from one side of a book.

    Returns None if the book is too thin to fill the order at all - which is
    itself the answer, and must not be silently reported as zero slippage.
    """
    remaining = notional_usd
    cost = 0.0
    qty = 0.0
    for price, size in levels:
        level_notional = price * size
        take = min(remaining, level_notional)
        if take <= 0:
            continue
        qty += take / price
        cost += take
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0 or qty <= 0:
        return None
    return cost / qty


async def fetch_bybit_book(session, symbol: str) -> Optional[Dict]:
    try:
        async with session.get(BYBIT_BOOK, params={
            "category": "linear", "symbol": symbol, "limit": 200
        }) as r:
            data = await r.json(content_type=None)
        if data.get("retCode") != 0:
            return None
        res = data.get("result", {})
        return {
            "bids": [(float(p), float(s)) for p, s in res.get("b", [])],
            "asks": [(float(p), float(s)) for p, s in res.get("a", [])],
        }
    except Exception as e:
        logger.error(f"[Bybit] {symbol}: {e}")
        return None


async def fetch_binance_book(session, symbol: str) -> Optional[Dict]:
    try:
        async with session.get(BINANCE_BOOK, params={
            "symbol": symbol, "limit": 500
        }) as r:
            data = await r.json(content_type=None)
        if "bids" not in data:
            return None
        return {
            "bids": [(float(p), float(s)) for p, s in data["bids"]],
            "asks": [(float(p), float(s)) for p, s in data["asks"]],
        }
    except Exception as e:
        logger.error(f"[Binance] {symbol}: {e}")
        return None


def slippage_pct(book: Dict, notional: float, side: str) -> Optional[float]:
    """Cost of crossing the book, as a percentage of mid."""
    bids, asks = book["bids"], book["asks"]
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2
    levels = asks if side == "buy" else bids
    fill = walk_book(levels, notional)
    if fill is None or mid <= 0:
        return None
    return abs(fill - mid) / mid * 100


async def probe(session, symbol: str, sizes: List[float]) -> Dict:
    by, bn = await asyncio.gather(
        fetch_bybit_book(session, symbol), fetch_binance_book(session, symbol)
    )
    out = {"symbol": symbol, "bybit_ok": by is not None,
           "binance_ok": bn is not None, "sizes": {}}
    if not by or not bn:
        return out

    for size in sizes:
        # One leg buys, the other sells; both pay slippage.
        by_slip = slippage_pct(by, size, "buy")
        bn_slip = slippage_pct(bn, size, "sell")
        if by_slip is None or bn_slip is None:
            out["sizes"][size] = None      # book cannot absorb this size
            continue
        # Round trip: in and out, on both legs.
        out["sizes"][size] = (by_slip + bn_slip) * 2
    return out


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--sizes", nargs="+", type=float,
                        default=[1000, 5000, 10000, 25000])
    parser.add_argument("--periods", type=int, default=3,
                        help="funding periods held (8h each)")
    args = parser.parse_args()

    db = Database(str(PROJECT_ROOT / "data" / "observations.db"))
    db.connect()
    try:
        symbols = args.symbols
        edges: Dict[str, float] = {}

        # Rank by SIGNED average: the absolute average assumes you are always
        # on the correct side, which overstates a harvestable edge by 2-5x.
        rows = db.connection.execute("""
            SELECT symbol, AVG(differential_8h) AS signed_avg, COUNT(*) AS n
            FROM funding_observations
            WHERE differential_8h IS NOT NULL
            GROUP BY symbol HAVING n > 100
            ORDER BY ABS(AVG(differential_8h)) DESC LIMIT 8
        """).fetchall()
        for r in rows:
            edges[r["symbol"]] = r["signed_avg"]
        if not symbols:
            symbols = list(edges)
    finally:
        db.disconnect()

    print("=" * 92)
    print(f"DEPTH PROBE  (round-trip fees {ROUND_TRIP_FEE_PCT:.3f}%, "
          f"{args.periods} periods = {args.periods * 8}h hold)")
    print("=" * 92)
    print("Net edge = signed funding edge over the hold, minus fees, minus "
          "round-trip slippage.\n")

    async with make_session() as session:
        for symbol in symbols:
            result = await probe(session, symbol, args.sizes)
            edge_8h = edges.get(symbol)
            gross = abs(edge_8h) * args.periods * 100 if edge_8h else None

            print(f"{symbol}")
            if not result["bybit_ok"] or not result["binance_ok"]:
                print("   book unavailable on "
                      f"{'Bybit' if not result['bybit_ok'] else 'Binance'}\n")
                continue
            if gross is None:
                print("   no measured funding edge\n")
                continue

            print(f"   gross funding edge over {args.periods * 8}h: {gross:.3f}%")
            print(f"   {'size':>9}{'slippage':>11}{'fees':>9}{'NET':>10}")
            for size in args.sizes:
                slip = result["sizes"].get(size)
                if slip is None:
                    print(f"   {size:>9,.0f}{'too thin':>11}{'':>9}{'--':>10}")
                    continue
                net = gross - ROUND_TRIP_FEE_PCT - slip
                flag = "" if net > 0 else "  <- negative"
                print(f"   {size:>9,.0f}{slip:>10.3f}%"
                      f"{ROUND_TRIP_FEE_PCT:>8.3f}%{net:>+9.3f}%{flag}")
            print()

    print("=" * 92)
    print("Slippage assumes taking liquidity at the top of the book RIGHT NOW.")
    print("Real entries compete with other takers and move the book further.")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
