"""
Funding-rate differential collector.

Polls the current funding rate for each configured pair on both exchanges and
records the differential, building the dataset needed to answer whether a
tradeable spread appears often enough to be worth trading.

Why REST rather than websockets: funding changes every 8 hours, so a 10s poll
loses nothing, and REST is reliable here while Binance's markPrice stream
delivers nothing from this network (its bookTicker stream does, so the silence
is stream-specific, not connectivity). A collector meant to run unattended for
days should depend on the path that provably works.

Usage:
    python scripts/collect.py                 # run until Ctrl+C
    python scripts/collect.py --hours 48      # stop automatically after 48h
"""
import argparse
import asyncio
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.http import make_session  # noqa: E402
from src.utils.logger import setup_logger  # noqa: E402
from src.storage.database import Database  # noqa: E402

logger = setup_logger("collector")

BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers"
BINANCE_PREMIUM = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_FUNDING_INFO = "https://fapi.binance.com/fapi/v1/fundingInfo"

# Binance omits symbols that use the standard interval from fundingInfo.
BINANCE_DEFAULT_INTERVAL_H = 8.0

# Everything is normalised to a per-8h basis before comparison. Roughly half of
# all pairs settle every 4h, and the interval can differ between venues for the
# same symbol, so raw rates are not comparable across venues.
NORMALISATION_BASIS_H = 8.0


def normalise(rate: float, interval_h: float) -> float:
    """Express a funding rate as its equivalent over NORMALISATION_BASIS_H."""
    if not interval_h or interval_h <= 0:
        return rate
    return rate * (NORMALISATION_BASIS_H / interval_h)


class Collector:
    def __init__(self, config: dict, hours: float = None, top: int = None):
        collection = config.get("collection", {})
        # Explicit pairs are a fallback; `top` discovers the universe instead.
        self.pairs = config.get("pairs", ["BTCUSDT", "ETHUSDT"])
        self.top = top
        self.interval = collection.get("sample_interval_s", 10)
        self.threshold = collection.get("tradeable_threshold", 0.0005)
        # symbol -> (bybit_interval_h, binance_interval_h)
        self.intervals: dict = {}

        self.db = Database(str(PROJECT_ROOT / collection.get(
            "db_path", "data/observations.db")))
        self.db.connect()

        self.deadline = time.time() + hours * 3600 if hours else None
        self.running = True
        self.samples = 0
        self.errors = 0
        self.started = time.time()

        logger.info(
            f"Collector starting: pairs={self.pairs} interval={self.interval}s "
            f"threshold={self.threshold} "
            f"duration={'%.1fh' % hours if hours else 'until stopped'}"
        )

    async def _bybit_all(self, session) -> dict:
        """
        Every Bybit linear ticker in ONE request.

        Returns symbol -> {rate, interval_h, volume}. Requesting per-symbol would
        cost one request per pair; this costs one regardless of universe size.
        """
        out = {}
        try:
            async with session.get(
                BYBIT_TICKERS, params={"category": "linear"}
            ) as r:
                data = await r.json()
            if data.get("retCode") != 0:
                logger.error(f"[Bybit] retCode={data.get('retCode')} "
                             f"{data.get('retMsg')}")
                return out
            for e in data.get("result", {}).get("list") or []:
                symbol, rate = e.get("symbol"), e.get("fundingRate")
                if not symbol or rate in (None, ""):
                    continue
                try:
                    out[symbol] = {
                        "rate": float(rate),
                        "interval_h": float(e.get("fundingIntervalHour")
                                            or NORMALISATION_BASIS_H),
                        "volume": float(e.get("turnover24h") or 0),
                    }
                except (TypeError, ValueError):
                    continue
        except Exception as e:
            logger.error(f"[Bybit] {type(e).__name__}: {e}")
        return out

    async def _binance_all(self, session) -> dict:
        """Every Binance perp funding rate in ONE request."""
        out = {}
        try:
            async with session.get(BINANCE_PREMIUM) as r:
                data = await r.json()
            if not isinstance(data, list):
                logger.error(f"[Binance] unexpected response: {str(data)[:200]}")
                return out
            for e in data:
                symbol, rate = e.get("symbol"), e.get("lastFundingRate")
                if not symbol or rate in (None, ""):
                    continue
                try:
                    out[symbol] = {"rate": float(rate)}
                except (TypeError, ValueError):
                    continue
        except Exception as e:
            logger.error(f"[Binance] {type(e).__name__}: {e}")
        return out

    async def _binance_intervals(self, session) -> dict:
        """
        Funding interval per Binance symbol.

        fundingInfo lists only symbols with a non-default configuration, so any
        symbol absent from it settles on the 8h default.
        """
        out = {}
        try:
            async with session.get(BINANCE_FUNDING_INFO) as r:
                data = await r.json()
            if isinstance(data, list):
                for e in data:
                    symbol = e.get("symbol")
                    hours = e.get("fundingIntervalHours")
                    if symbol and hours:
                        out[symbol] = float(hours)
        except Exception as e:
            logger.error(f"[Binance] fundingInfo: {type(e).__name__}: {e}")
        return out

    async def discover_universe(self, session):
        """
        Select the pairs to track: listed on both venues, ranked by liquidity.

        Ranked on min(bybit_volume, binance_volume) is not possible from these
        endpoints alone, so Bybit turnover24h is used - it is the thinner venue
        of the two by listing count, and an arb is capped by the thinner side.
        """
        bybit, binance = await asyncio.gather(
            self._bybit_all(session), self._binance_all(session)
        )
        bn_intervals = await self._binance_intervals(session)

        both = set(bybit) & set(binance)
        if not both:
            logger.error("No overlapping symbols found; keeping configured pairs")
            return

        ranked = sorted(both, key=lambda s: bybit[s]["volume"], reverse=True)
        self.pairs = ranked[: self.top] if self.top else ranked

        self.intervals = {
            s: (bybit[s]["interval_h"],
                bn_intervals.get(s, BINANCE_DEFAULT_INTERVAL_H))
            for s in self.pairs
        }

        mismatched = [s for s, (b, n) in self.intervals.items() if b != n]
        logger.info(
            f"Universe: {len(both)} pairs on both venues, tracking top "
            f"{len(self.pairs)} by Bybit 24h turnover"
        )
        logger.info(
            f"{len(mismatched)} tracked pairs have MISMATCHED funding intervals "
            f"between venues; all rates normalised to a {NORMALISATION_BASIS_H:.0f}h basis"
        )

    async def _sample(self, session):
        """One paired reading across the whole universe: two requests total."""
        bybit, binance = await asyncio.gather(
            self._bybit_all(session), self._binance_all(session)
        )
        observed_at = datetime.now(timezone.utc).isoformat()

        rows = []
        tradeable = []

        for symbol in self.pairs:
            b_entry, n_entry = bybit.get(symbol), binance.get(symbol)
            # A one-sided reading says nothing about the spread.
            if not b_entry or not n_entry:
                self.errors += 1
                continue

            b, n = b_entry["rate"], n_entry["rate"]
            b_int, n_int = self.intervals.get(
                symbol, (NORMALISATION_BASIS_H, NORMALISATION_BASIS_H))

            # Raw differential is retained for continuity with earlier rows, but
            # differential_8h is the one that is economically meaningful when the
            # two venues settle on different schedules.
            differential = n - b
            differential_8h = normalise(n, n_int) - normalise(b, b_int)

            rows.append((observed_at, symbol, b, n, differential,
                         b_int, n_int, differential_8h))

            if abs(differential_8h) >= self.threshold:
                tradeable.append((symbol, differential_8h, b, n))

        try:
            self.samples += self.db.insert_observations_bulk(rows)
        except Exception as e:
            logger.error(f"bulk persist failed ({len(rows)} rows): {e}")
            self.errors += len(rows)
            return

        # Only the widest few, so a genuine dislocation is not buried in noise.
        for symbol, diff, b, n in sorted(
            tradeable, key=lambda t: abs(t[1]), reverse=True
        )[:5]:
            logger.info(
                f"[{symbol}] TRADEABLE {diff:+.6f} per 8h "
                f"(bybit {b:.6f} / binance {n:.6f})"
            )
        if len(tradeable) > 5:
            logger.info(f"  ... and {len(tradeable) - 5} more above threshold")

    def _log_progress(self):
        elapsed = time.time() - self.started
        logger.info(
            f"progress: {self.samples} samples, {self.errors} errors, "
            f"{elapsed / 3600:.2f}h elapsed"
        )

    async def run(self):
        last_progress = time.time()
        async with make_session() as session:
            if self.top:
                await self.discover_universe(session)

            while self.running:
                if self.deadline and time.time() >= self.deadline:
                    logger.info("Requested duration reached")
                    break
                try:
                    await self._sample(session)
                except Exception as e:
                    # A collector that dies overnight collects nothing.
                    logger.error(f"sample loop error: {type(e).__name__}: {e}")
                    self.errors += 1

                if time.time() - last_progress >= 300:
                    self._log_progress()
                    last_progress = time.time()

                await asyncio.sleep(self.interval)

        self.shutdown()

    def shutdown(self):
        self.running = False
        self._log_progress()
        try:
            self.db.disconnect()
        except Exception as e:
            logger.error(f"error closing database: {e}")
        logger.info("Collector stopped")


def load_config() -> dict:
    try:
        with open(PROJECT_ROOT / "config" / "config.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"could not load config: {e}")
        return {}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=None,
                        help="stop automatically after this many hours")
    parser.add_argument("--top", type=int, default=None,
                        help="track the top N pairs by liquidity that are listed "
                             "on both venues, instead of the configured pairs")
    parser.add_argument("--interval", type=float, default=None,
                        help="seconds between samples (overrides config)")
    args = parser.parse_args()

    collector = Collector(load_config(), hours=args.hours, top=args.top)
    if args.interval:
        collector.interval = args.interval

    def stop(_sig, _frame):
        logger.info("Stop signal received")
        collector.running = False

    signal.signal(signal.SIGINT, stop)
    try:
        signal.signal(signal.SIGTERM, stop)
    except (AttributeError, ValueError):
        pass  # SIGTERM is not available in all Windows contexts

    await collector.run()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
