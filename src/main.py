import asyncio
import json
import time
import signal
from pathlib import Path

from datetime import datetime, timezone

from src.utils.logger import setup_logger
from src.data.websocket_manager import WebSocketHub
from src.data.funding_rate_tracker import FundingRateTracker
from src.storage.database import Database

logger = setup_logger("main")


class FundingRateBot:
    """Main bot orchestrator for Week 1 (data collection mode)."""

    def __init__(self, config_path: str = "config/config.json"):
        self.config = self._load_config(config_path)
        self.pairs = self.config.get("pairs", [])
        self.is_running = False

        self.tracker = FundingRateTracker(self.pairs)
        self.websocket_hub = WebSocketHub(self.pairs, self._on_websocket_message)

        self.last_metrics_report = 0
        self.metrics_interval = self.config.get("monitoring", {}).get("metrics_report_interval_s", 30)

        # Persistence. Without this a collector run leaves only log lines, and
        # the question it exists to answer - how often is the spread tradeable -
        # cannot be queried afterwards.
        collection = self.config.get("collection", {})
        self.sample_interval = collection.get("sample_interval_s", 10)
        self.db = Database(collection.get("db_path", "data/observations.db"))
        self.db.connect()
        self.last_sample = 0.0
        self.samples_written = 0

        logger.info(
            f"Initialized bot with pairs: {self.pairs} "
            f"(sampling every {self.sample_interval}s)"
        )

    @staticmethod
    def _load_config(config_path: str) -> dict:
        """Load configuration from JSON file."""
        try:
            with open(config_path, "r") as f:
                config = json.load(f)
            logger.info(f"Loaded config from {config_path}")
            return config
        except FileNotFoundError:
            logger.error(f"Config file not found: {config_path}")
            return {}
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in config: {e}")
            return {}

    async def _on_websocket_message(self, message: dict):
        """Callback for WebSocket messages."""
        try:
            # Determine exchange from message structure
            if "data" in message and "fundingRate" in message["data"]:
                exchange = "bybit"
            elif "e" in message and message["e"] == "markPriceUpdate":
                exchange = "binance"
            else:
                return

            await self.tracker.process_websocket_message(message, exchange)
        except Exception as e:
            logger.error(f"Error processing message: {e}")

    async def _backfill_history(self):
        """Backfill historical funding rates for all pairs."""
        backfill_config = self.config.get("backfill", {})
        if not backfill_config.get("enabled", True):
            logger.info("Backfill disabled")
            return

        limit = backfill_config.get("limit", 100)
        delay = backfill_config.get("delay_s", 5)

        logger.info(f"Starting backfill for {len(self.pairs)} pairs")
        for pair in self.pairs:
            await self.tracker.backfill_history(pair, limit=limit)
            await asyncio.sleep(delay)

        logger.info("Backfill complete")

    async def _report_metrics(self):
        """Periodically sample observations to disk and report metrics."""
        while self.is_running:
            try:
                now = time.time()

                if now - self.last_sample >= self.sample_interval:
                    self._persist_observations()
                    self.last_sample = now

                if now - self.last_metrics_report >= self.metrics_interval:
                    self._print_metrics()
                    self.last_metrics_report = now

                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Error in sampling loop: {e}")

    def _persist_observations(self):
        """Write the current differential for each pair to the database."""
        observed_at = datetime.now(timezone.utc).isoformat()

        for pair in self.pairs:
            diff = self.tracker.get_latest_differential(pair)
            # Nothing to learn from a sample where neither exchange has reported.
            if diff is None or diff.differential is None:
                continue
            try:
                self.db.insert_observation(
                    observed_at=observed_at,
                    symbol=pair,
                    bybit_rate=diff.bybit_rate,
                    binance_rate=diff.binance_rate,
                    differential=diff.differential,
                    bybit_age_s=diff.bybit_age_s,
                    binance_age_s=diff.binance_age_s,
                )
                self.samples_written += 1
            except Exception as e:
                # A storage failure must not take down a long collection run.
                logger.error(f"[{pair}] Failed to persist observation: {e}")

    def _print_metrics(self):
        """Print current metrics to console."""
        differentials = self.tracker.get_all_differentials()

        print("\n" + "=" * 100)
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] FUNDING RATE METRICS")
        print("=" * 100)

        ws_status = self.websocket_hub.get_status()
        print(f"WebSocket Status: Bybit={ws_status['bybit']}, Binance={ws_status['binance']}")
        print(f"Samples persisted: {self.samples_written}")

        for pair in self.pairs:
            diff = differentials.get(pair)
            if diff:
                bybit = f"{diff.bybit_rate:.6f}" if diff.bybit_rate else "---"
                binance = f"{diff.binance_rate:.6f}" if diff.binance_rate else "---"
                delta = f"{diff.differential:.6f}" if diff.differential else "---"
                bybit_age = f"{diff.bybit_age_s:.1f}s" if diff.bybit_age_s else "---"
                binance_age = f"{diff.binance_age_s:.1f}s" if diff.binance_age_s else "---"

                print(f"\n{pair}:")
                print(f"  Bybit:   {bybit:>12} (age: {bybit_age:>6})")
                print(f"  Binance: {binance:>12} (age: {binance_age:>6})")
                print(f"Delta (B-Y): {delta:>12}")

                # Print stats
                bybit_stats = self.tracker.get_stats(pair, "bybit")
                binance_stats = self.tracker.get_stats(pair, "binance")

                if bybit_stats["count"] > 0:
                    print(f"  Bybit   (8h): avg={bybit_stats['avg']:.6f}, "
                          f"min={bybit_stats['min']:.6f}, max={bybit_stats['max']:.6f}")
                if binance_stats["count"] > 0:
                    print(f"  Binance (8h): avg={binance_stats['avg']:.6f}, "
                          f"min={binance_stats['min']:.6f}, max={binance_stats['max']:.6f}")

        # Print recent alerts
        if self.tracker.alerts_triggered:
            print("\n--- Recent Alerts ---")
            for alert in self.tracker.alerts_triggered[-5:]:  # Last 5 alerts
                print(f"{alert}")

        print("=" * 100 + "\n")

    async def run(self):
        """Main bot loop."""
        self.is_running = True
        logger.info("Starting FundingRateBot (Week 1 - Data Collection)")

        try:
            # Backfill historical data
            await self._backfill_history()

            # Start WebSocket connections and metrics reporting
            tasks = [
                asyncio.create_task(self.websocket_hub.start()),
                asyncio.create_task(self._report_metrics()),
            ]

            logger.info("Bot is running. Press Ctrl+C to stop.")
            await asyncio.gather(*tasks)

        except KeyboardInterrupt:
            logger.info("Received interrupt signal")
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
        finally:
            await self.shutdown()

    async def shutdown(self):
        """Graceful shutdown."""
        logger.info("Shutting down bot...")
        self.is_running = False

        await self.websocket_hub.stop()
        await self.tracker.close()

        # Take a final sample so a run is not truncated mid-interval, then close
        # the database so the collected data is flushed and readable.
        try:
            self._persist_observations()
            self.db.disconnect()
            logger.info(f"Collection complete: {self.samples_written} samples persisted")
        except Exception as e:
            logger.error(f"Error closing database: {e}")

        logger.info("Shutdown complete")


async def main():
    """Entry point for the bot."""
    bot = FundingRateBot()

    # Handle signals for graceful shutdown
    def signal_handler(sig, frame):
        logger.info("Signal received, initiating shutdown")
        asyncio.create_task(bot.shutdown())

    signal.signal(signal.SIGINT, signal_handler)

    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
