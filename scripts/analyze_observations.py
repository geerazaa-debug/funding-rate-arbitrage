"""
Summarise collected funding-rate observations.

Answers the question the collector exists to answer: how much of the time is the
Bybit/Binance spread actually wide enough to trade? Run it at any point during or
after a collection run - it reads the database without disturbing the collector.

Usage:
    python scripts/analyze_observations.py
    python scripts/analyze_observations.py --threshold 0.0003
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.database import Database  # noqa: E402


def load_config() -> dict:
    try:
        with open(PROJECT_ROOT / "config" / "config.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def main():
    config = load_config()
    collection = config.get("collection", {})

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--threshold", type=float,
        default=collection.get("tradeable_threshold", 0.0005),
        help="absolute differential at which a trade is worth opening",
    )
    parser.add_argument(
        "--db", default=collection.get("db_path", "data/observations.db"),
    )
    args = parser.parse_args()

    db_path = PROJECT_ROOT / args.db
    if not db_path.exists():
        print(f"No database at {db_path} - has the collector run yet?")
        return 1

    db = Database(str(db_path))
    db.connect()
    try:
        symbols = config.get("pairs", ["BTCUSDT", "ETHUSDT"])

        print("=" * 74)
        print(f"FUNDING DIFFERENTIAL ANALYSIS  (threshold {args.threshold:.6f})")
        print("=" * 74)

        any_data = False
        for symbol in symbols:
            stats = db.get_observation_stats(symbol, args.threshold)
            if not stats.get("samples"):
                print(f"\n{symbol}: no observations yet")
                continue

            any_data = True
            print(f"\n{symbol}")
            print(f"  samples          : {stats['samples']:,}")
            print(f"  window           : {stats['first_seen']}")
            print(f"                     {stats['last_seen']}")
            print(f"  |differential|   : avg {stats['avg_abs_diff']:.6f}  "
                  f"min {stats['min_abs_diff']:.6f}  max {stats['max_abs_diff']:.6f}")
            print(f"  tradeable        : {stats['tradeable_samples']:,} samples "
                  f"({stats['tradeable_pct']:.2f}% of the time)")

            # The headline judgement. A spread that is almost never wide enough
            # means the strategy has no edge to harvest, regardless of how well
            # the machinery works.
            pct = stats["tradeable_pct"]
            if pct == 0:
                verdict = "NO tradeable spread observed in this window"
            elif pct < 1:
                verdict = "spread is tradeable <1% of the time - very thin"
            elif pct < 10:
                verdict = "occasionally tradeable - check fees cover the edge"
            else:
                verdict = "frequently tradeable - worth modelling costs properly"
            print(f"  verdict          : {verdict}")

        if any_data:
            print("\n" + "-" * 74)
            print("Reminder: this measures gross spread only. Subtract taker fees on")
            print("both legs plus slippage before treating any of it as edge.")
        print("=" * 74)
        return 0
    finally:
        db.disconnect()


if __name__ == "__main__":
    sys.exit(main())
