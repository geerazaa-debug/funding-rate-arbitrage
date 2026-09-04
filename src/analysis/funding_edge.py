"""Fee-netted funding-edge math and dynamic symbol ranking.

Pure, side-effect-free calculation (no I/O) so it can be unit tested without
a database - orchestration that reads funding_observations lives in
scripts/analyze_observations.py.

Why "fee-netted" is not just "gross differential minus a fee percentage":
funding is captured every settlement period (normalised to an 8h basis) a
position stays open, while the round-trip cost - opening both legs, later
closing both legs, 4 taker fills total - is paid ONCE. A thin differential
held long enough can still be profitable; a wide one closed immediately can
still lose money net of fees. This module answers "how many periods to break
even at the observed average differential", not a single gross/net
threshold comparison that ignores holding time entirely.

UNITS: every fee/differential argument and return value here is a FRACTIONAL
rate in the same convention as funding_observations.differential_8h and the
raw exchange funding-rate fields it's built from - e.g. 0.0001 means a rate
of 0.01%, and a 0.05% taker fee is passed as 0.0005, NOT as 0.05. Mixing a
"0.05 means 0.05%" percentage-point convention into any one argument silently
breaks every comparison in this module against real differential data.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


def round_trip_fee_pct(taker_fee_pct_a: float, taker_fee_pct_b: float) -> float:
    """Fractional cost to open both legs of a delta-neutral position and
    later close both legs - 4 taker fills total (open+close on each of the
    two exchanges), each at that exchange's own taker rate.

    Args are fractional rates (0.0005 for a 0.05% taker fee), matching
    differential_8h's units - see module docstring.
    """
    if taker_fee_pct_a < 0 or taker_fee_pct_b < 0:
        raise ValueError("fee percentages must be non-negative")
    return 2 * (taker_fee_pct_a + taker_fee_pct_b)


def breakeven_periods(
    round_trip_pct: float, avg_abs_differential_8h: float
) -> Optional[float]:
    """How many 8h-normalised funding periods of holding are needed for
    captured funding to recover the round-trip fee cost, at the given average
    observed differential.

    Returns None when the differential is zero (or negative, which callers
    should never pass) - there is no holding period at which a zero edge
    breaks even, and reporting a number would misleadingly imply there is.
    """
    if round_trip_pct < 0:
        raise ValueError("round_trip_pct must be non-negative")
    if avg_abs_differential_8h <= 0:
        return None
    return round_trip_pct / avg_abs_differential_8h


def fee_netted_threshold(round_trip_pct: float, max_holding_periods: float) -> float:
    """The minimum |differential_8h| that recovers the round-trip fee cost
    within max_holding_periods - i.e. the threshold to pass into
    Database.get_observation_stats() in place of a fee-blind gross value, so
    "tradeable_pct" answers "tradeable AFTER fees", not just "gross spread
    wider than an arbitrary number".
    """
    if max_holding_periods <= 0:
        raise ValueError("max_holding_periods must be positive")
    return round_trip_pct / max_holding_periods


@dataclass
class SymbolEdge:
    """Fee-netted edge summary for one symbol, ready to sort/print."""

    symbol: str
    samples: int
    avg_abs_differential_8h: float
    max_abs_differential_8h: float
    round_trip_fee_pct: float
    breakeven_periods: Optional[float]
    fee_netted_tradeable_pct: float  # % of samples clearing fee_netted_threshold
    verdict: str


def _verdict(samples: int, min_samples: int, fee_netted_tradeable_pct: float,
             breakeven: Optional[float], max_holding_periods: float) -> str:
    if samples < min_samples:
        return f"insufficient data ({samples} samples, need {min_samples}+)"
    if breakeven is None:
        return "no measurable edge - average differential is zero"
    if fee_netted_tradeable_pct == 0:
        return "never clears fees within the acceptable holding period"
    if fee_netted_tradeable_pct < 5:
        return "rarely clears fees - thin, likely not worth the operational risk"
    if breakeven > max_holding_periods:
        return (f"average differential does not clear fees within "
                f"{max_holding_periods:.0f} periods, but does often enough "
                f"in the tails to be worth watching")
    return "clears fees within an acceptable holding period often enough to warrant a closer look"


def rank_symbols_by_fee_netted_edge(
    db,
    symbols: List[Dict],
    bybit_taker_fee_pct: float,
    binance_taker_fee_pct: float,
    max_holding_periods: float,
    min_samples: int = 30,
) -> List[SymbolEdge]:
    """Build a fee-netted ranking across every symbol with real collected
    data, replacing a static BTC/ETH-only analysis.

    `db` needs get_observation_stats(symbol, threshold) (see
    src/storage/database.py); `symbols` is the output of
    Database.get_distinct_symbols(). Sorted best-first: highest
    fee_netted_tradeable_pct, ties broken by shortest breakeven_periods.
    """
    fee_pct = round_trip_fee_pct(bybit_taker_fee_pct, binance_taker_fee_pct)
    threshold = fee_netted_threshold(fee_pct, max_holding_periods)

    rankings: List[SymbolEdge] = []
    for entry in symbols:
        symbol = entry["symbol"]
        stats = db.get_observation_stats(symbol, threshold)
        samples = stats.get("samples", 0)
        avg_abs = stats.get("avg_abs_diff", 0.0)
        breakeven = breakeven_periods(fee_pct, avg_abs)

        rankings.append(SymbolEdge(
            symbol=symbol,
            samples=samples,
            avg_abs_differential_8h=avg_abs,
            max_abs_differential_8h=stats.get("max_abs_diff", 0.0),
            round_trip_fee_pct=fee_pct,
            breakeven_periods=breakeven,
            fee_netted_tradeable_pct=stats.get("tradeable_pct", 0.0),
            verdict=_verdict(samples, min_samples, stats.get("tradeable_pct", 0.0),
                             breakeven, max_holding_periods),
        ))

    rankings.sort(
        key=lambda r: (
            -r.fee_netted_tradeable_pct,
            r.breakeven_periods if r.breakeven_periods is not None else float("inf"),
        )
    )
    return rankings
