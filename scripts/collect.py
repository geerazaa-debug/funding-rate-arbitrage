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


class Collector:
    def __init__(self, config: dict, hours: float = None):
        collection = config.get("collection", {})
        self.pairs = config.get("pairs", ["BTCUSDT", "ETHUSDT"])
        self.interval = collection.get("sample_interval_s", 10)
        self.threshold = collection.get("tradeable_threshold", 0.0005)

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

    async def _bybit_rates(self, session) -> dict:
        """Current funding rate per symbol from Bybit. One request per symbol."""
        rates = {}
        for symbol in self.pairs:
            try:
                async with session.get(
                    BYBIT_TICKERS,
                    params={"category": "linear", "symbol": symbol},
                ) as r:
                    data = await r.json()
                if data.get("retCode") != 0:
                    logger.error(f"[Bybit] {symbol}: retCode={data.get('retCode')} "
                                 f"{data.get('retMsg')}")
                    continue
                entries = data.get("result", {}).get("list") or []
                if entries and entries[0].get("fundingRate") is not None:
                    rates[symbol] = float(entries[0]["fundingRate"])
            except Exception as e:
                logger.error(f"[Bybit] {symbol}: {type(e).__name__}: {e}")
        return rates

    async def _binance_rates(self, session) -> dict:
        """
        Current funding rate per symbol from Binance.

        One unparameterised call returns every symbol, so this costs a single
        request regardless of how many pairs are configured.
        """
        rates = {}
        try:
            async with session.get(BINANCE_PREMIUM) as r:
                data = await r.json()
            if not isinstance(data, list):
                logger.error(f"[Binance] unexpected response: {str(data)[:200]}")
                return rates
            wanted = set(self.pairs)
            for entry in data:
                symbol = entry.get("symbol")
                if symbol in wanted and entry.get("lastFundingRate") is not None:
                    rates[symbol] = float(entry["lastFundingRate"])
        except Exception as e:
            logger.error(f"[Binance] {type(e).__name__}: {e}")
        return rates

    async def _sample(self, session):
        """Take one paired reading and persist any pair we got both sides for."""
        bybit, binance = await asyncio.gather(
            self._bybit_rates(session), self._binance_rates(session)
        )
        observed_at = datetime.now(timezone.utc).isoformat()

        for symbol in self.pairs:
            b, n = bybit.get(symbol), binance.get(symbol)
            # A one-sided reading says nothing about the spread.
            if b is None or n is None:
                self.errors += 1
                continue

            differential = n - b
            try:
                self.db.insert_observation(
                    observed_at=observed_at, symbol=symbol,
                    bybit_rate=b, binance_rate=n, differential=differential,
                )
                self.samples += 1
            except Exception as e:
                logger.error(f"[{symbol}] persist failed: {e}")
                self.errors += 1
                continue

            if abs(differential) >= self.threshold:
                logger.info(
                    f"[{symbol}] TRADEABLE spread: {differential:+.6f} "
                    f"(bybit {b:.6f} / binance {n:.6f})"
                )

    def _log_progress(self):
        elapsed = time.time() - self.started
        logger.info(
            f"progress: {self.samples} samples, {self.errors} errors, "
            f"{elapsed / 3600:.2f}h elapsed"
        )

    async def run(self):
        last_progress = time.time()
        async with make_session() as session:
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
    args = parser.parse_args()

    collector = Collector(load_config(), hours=args.hours)

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
