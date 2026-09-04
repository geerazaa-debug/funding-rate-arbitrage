"""
Rank collected funding-rate observations by FEE-NETTED edge, across the
whole collected universe - not just the 1-2 symbols in config.json's
`pairs`.

The collector already discovers and tracks many symbols dynamically
(`collect.py --top N`, ranked by liquidity); this script previously only ever
analysed the static `pairs` list, silently ignoring everything else that was
actually collected, and only reported GROSS differential - a spread that
looks wide can still be unprofitable once the round-trip cost of opening and
closing both legs (4 taker fills total) is accounted for, and a thin one can
still be worth trading if held long enough to amortise that fixed cost.

Usage:
    python scripts/analyze_observations.py                  # rank everything collected
    python scripts/analyze_observations.py --top 10          # show only the best 10
    python scripts/analyze_observations.py --symbol BTCUSDT  # full detail on one symbol
    python scripts/analyze_observations.py --max-holding-periods 6
    python scripts/analyze_observations.py --min-samples 100
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.funding_edge import (  # noqa: E402
    breakeven_periods,
    rank_symbols_by_fee_netted_edge,
    round_trip_fee_pct,
)
from src.storage.database import Database  # noqa: E402


def load_config() -> dict:
    try:
        with open(PROJECT_ROOT / "config" / "config.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def print_leaderboard(rankings, fee_pct: float, max_holding_periods: float, top_n: int,
                       min_samples: int):
    print("=" * 92)
    print(f"FEE-NETTED FUNDING EDGE  (round-trip cost {fee_pct:.4%}, "
          f"max holding {max_holding_periods:.0f} periods = {max_holding_periods * 8:.0f}h)")
    print("=" * 92)
    print(f"{'symbol':<14}{'samples':>9}{'avg |diff8h|':>14}{'breakeven':>12}"
          f"{'fee-netted %':>14}  verdict")
    print("-" * 92)

    for r in rankings[:top_n]:
        breakeven_str = f"{r.breakeven_periods:.1f}p" if r.breakeven_periods is not None else "never"
        print(
            f"{r.symbol:<14}{r.samples:>9,}{r.avg_abs_differential_8h:>14.6f}"
            f"{breakeven_str:>12}{r.fee_netted_tradeable_pct:>13.2f}%  {r.verdict}"
        )

    print("-" * 92)
    # Excludes symbols below min_samples: a handful of observations can look
    # "tradeable" purely by chance, and that noise must not reach the
    # "worth a closer look" recommendation even though it's still shown above
    # (with its low-sample verdict) for transparency.
    tradeable_now = [
        r for r in rankings if r.fee_netted_tradeable_pct >= 5 and r.samples >= min_samples
    ]
    if tradeable_now:
        print(f"\n{len(tradeable_now)}/{len(rankings)} symbols clear fees >=5% of the time "
              f"within {max_holding_periods:.0f} periods - candidates worth a closer look:")
        print(f"  {', '.join(r.symbol for r in tradeable_now[:15])}"
              + (" ..." if len(tradeable_now) > 15 else ""))
    else:
        print(f"\nNo symbol in this collection clears fees >=5% of the time within "
              f"{max_holding_periods:.0f} periods. This measures real collected data, not a "
              f"hypothesis - if it holds up over a longer window, the strategy has no "
              f"tradeable edge at the current fee tier on the symbols actually collected.")
    print("=" * 92 + "\n")


def print_symbol_detail(db, symbol: str, fee_pct: float, max_holding_periods: float):
    from src.analysis.funding_edge import fee_netted_threshold

    threshold = fee_netted_threshold(fee_pct, max_holding_periods)
    stats = db.get_observation_stats(symbol, threshold)

    print("=" * 74)
    print(f"{symbol}  (interval-normalised to an 8h basis)")
    print("=" * 74)
    if not stats.get("samples"):
        print("no observations collected for this symbol")
        print("=" * 74 + "\n")
        return

    breakeven = breakeven_periods(fee_pct, stats["avg_abs_diff"])
    print(f"  samples              : {stats['samples']:,}")
    print(f"  window               : {stats['first_seen']}")
    print(f"                         {stats['last_seen']}")
    print(f"  |differential_8h|    : avg {stats['avg_abs_diff']:.6f}  "
          f"min {stats['min_abs_diff']:.6f}  max {stats['max_abs_diff']:.6f}")
    print(f"  round-trip fee cost  : {fee_pct:.6f} (4 taker fills: open+close x 2 exchanges)")
    print(f"  breakeven holding    : "
          + (f"{breakeven:.1f} periods ({breakeven * 8:.0f}h)" if breakeven else "never (zero avg edge)"))
    print(f"  fee-netted tradeable : {stats['tradeable_pct']:.2f}% of samples "
          f"(within {max_holding_periods:.0f} periods)")
    print("=" * 74 + "\n")


def main():
    config = load_config()
    collection = config.get("collection", {})
    fees = config.get("fees", {})
    analysis = config.get("analysis", {})

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--symbol", default=None,
                        help="show full detail for one symbol instead of the leaderboard")
    parser.add_argument("--top", type=int,
                        default=analysis.get("top_n_to_display", 20),
                        help="leaderboard rows to display")
    parser.add_argument("--min-samples", type=int,
                        default=analysis.get("min_samples_for_ranking", 30),
                        help="minimum observations before a symbol is ranked "
                             "(too few makes avg_abs_diff noise, not a real edge)")
    parser.add_argument("--max-holding-periods", type=float,
                        default=analysis.get("max_holding_periods", 3),
                        help="8h-normalised funding periods a position is "
                             "assumed held for when netting fees")
    parser.add_argument("--bybit-fee", type=float,
                        default=fees.get("bybit_taker_fee_pct", 0.00055),
                        help="Bybit taker fee, fractional (0.00055 = 0.055%%)")
    parser.add_argument("--binance-fee", type=float,
                        default=fees.get("binance_taker_fee_pct", 0.0005),
                        help="Binance taker fee, fractional (0.0005 = 0.05%%)")
    parser.add_argument("--db", default=collection.get("db_path", "data/observations.db"))
    args = parser.parse_args()

    db_path = PROJECT_ROOT / args.db
    if not db_path.exists():
        print(f"No database at {db_path} - has the collector run yet?")
        return 1

    fee_pct = round_trip_fee_pct(args.bybit_fee, args.binance_fee)

    db = Database(str(db_path))
    db.connect()
    try:
        if args.symbol:
            print_symbol_detail(db, args.symbol, fee_pct, args.max_holding_periods)
            return 0

        symbols = db.get_distinct_symbols(min_samples=1)
        if not symbols:
            print("No observations in the database yet - has the collector run?")
            return 1

        rankings = rank_symbols_by_fee_netted_edge(
            db, symbols,
            bybit_taker_fee_pct=args.bybit_fee,
            binance_taker_fee_pct=args.binance_fee,
            max_holding_periods=args.max_holding_periods,
            min_samples=args.min_samples,
        )
        print_leaderboard(rankings, fee_pct, args.max_holding_periods, args.top, args.min_samples)
        return 0
    finally:
        db.disconnect()


if __name__ == "__main__":
    sys.exit(main())
