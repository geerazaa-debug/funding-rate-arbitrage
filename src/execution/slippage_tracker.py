import time
from dataclasses import dataclass
from typing import Dict, List, Optional
from collections import defaultdict

from src.utils.logger import setup_logger

logger = setup_logger("slippage_tracker")


@dataclass
class SlippageRecord:
    """Records slippage for a single trade."""
    symbol: str
    exchange: str
    timestamp: float
    expected_price: float
    actual_price: float
    quantity: float
    side: str  # BUY or SELL

    @property
    def slippage_bps(self) -> float:
        """Slippage in basis points."""
        if self.side.upper() == "BUY":
            return ((self.actual_price - self.expected_price) / self.expected_price) * 10000
        else:  # SELL
            return ((self.expected_price - self.actual_price) / self.expected_price) * 10000

    @property
    def slippage_usd(self) -> float:
        """Slippage in USD."""
        diff = abs(self.actual_price - self.expected_price)
        return diff * self.quantity

    def __repr__(self):
        return (f"Slippage({self.exchange} {self.symbol} {self.side} "
                f"{self.slippage_bps:.1f} bps, ${self.slippage_usd:.2f})")


class SlippageTracker:
    """Tracks slippage between expected and actual fills."""

    def __init__(self):
        # All slippage records
        self.records: List[SlippageRecord] = []

        # Aggregates by exchange
        self.by_exchange: Dict[str, List[SlippageRecord]] = defaultdict(list)

        # Aggregates by symbol
        self.by_symbol: Dict[str, List[SlippageRecord]] = defaultdict(list)

        logger.info("Initialized slippage tracker")

    def record_slippage(self, symbol: str, exchange: str, expected_price: float,
                       actual_price: float, quantity: float, side: str) -> SlippageRecord:
        """Record a slippage event."""
        record = SlippageRecord(
            symbol=symbol,
            exchange=exchange,
            timestamp=time.time(),
            expected_price=expected_price,
            actual_price=actual_price,
            quantity=quantity,
            side=side,
        )

        self.records.append(record)
        self.by_exchange[exchange].append(record)
        self.by_symbol[symbol].append(record)

        logger.debug(f"{record}")

        return record

    def get_slippage_stats(self, exchange: Optional[str] = None,
                          symbol: Optional[str] = None) -> Dict:
        """Calculate slippage statistics."""
        if exchange and symbol:
            records = [r for r in self.records
                      if r.exchange == exchange and r.symbol == symbol]
        elif exchange:
            records = self.by_exchange.get(exchange, [])
        elif symbol:
            records = self.by_symbol.get(symbol, [])
        else:
            records = self.records

        if not records:
            return {
                "count": 0,
                "avg_slippage_bps": 0.0,
                "max_slippage_bps": 0.0,
                "min_slippage_bps": 0.0,
                "total_slippage_usd": 0.0,
                "avg_slippage_usd": 0.0,
            }

        bps_values = [r.slippage_bps for r in records]
        usd_values = [r.slippage_usd for r in records]

        return {
            "count": len(records),
            "avg_slippage_bps": sum(bps_values) / len(bps_values),
            "max_slippage_bps": max(bps_values),
            "min_slippage_bps": min(bps_values),
            "total_slippage_usd": sum(usd_values),
            "avg_slippage_usd": sum(usd_values) / len(usd_values),
        }

    def get_exchange_comparison(self) -> Dict[str, Dict]:
        """Compare slippage across exchanges."""
        comparison = {}
        for exchange in self.by_exchange:
            comparison[exchange] = self.get_slippage_stats(exchange=exchange)
        return comparison

    def get_symbol_comparison(self) -> Dict[str, Dict]:
        """Compare slippage across symbols."""
        comparison = {}
        for symbol in self.by_symbol:
            comparison[symbol] = self.get_slippage_stats(symbol=symbol)
        return comparison

    def get_worst_slippage(self, limit: int = 10) -> List[SlippageRecord]:
        """Get trades with worst slippage."""
        sorted_records = sorted(self.records, key=lambda r: r.slippage_bps, reverse=True)
        return sorted_records[:limit]

    def get_recent_slippage(self, limit: int = 10) -> List[SlippageRecord]:
        """Get most recent slippage records."""
        return self.records[-limit:]

    def print_slippage_report(self):
        """Print comprehensive slippage report."""
        print("\n" + "=" * 80)
        print("SLIPPAGE REPORT")
        print("=" * 80)

        # Overall stats
        overall_stats = self.get_slippage_stats()
        print(f"\nOverall (all {overall_stats['count']} trades):")
        print(f"  Avg Slippage: {overall_stats['avg_slippage_bps']:.2f} bps (${overall_stats['avg_slippage_usd']:.2f})")
        print(f"  Range:        {overall_stats['min_slippage_bps']:.2f} to {overall_stats['max_slippage_bps']:.2f} bps")
        print(f"  Total Loss:   ${overall_stats['total_slippage_usd']:.2f}")

        # By exchange
        print("\nBy Exchange:")
        for exchange, stats in self.get_exchange_comparison().items():
            if stats['count'] > 0:
                print(f"  {exchange}: {stats['count']} trades, "
                      f"avg {stats['avg_slippage_bps']:.2f} bps, "
                      f"total ${stats['total_slippage_usd']:.2f}")

        # By symbol
        print("\nBy Symbol:")
        for symbol, stats in self.get_symbol_comparison().items():
            if stats['count'] > 0:
                print(f"  {symbol}: {stats['count']} trades, "
                      f"avg {stats['avg_slippage_bps']:.2f} bps, "
                      f"total ${stats['total_slippage_usd']:.2f}")

        # Worst slippage
        worst = self.get_worst_slippage(5)
        if worst:
            print("\nWorst Slippage Events:")
            for record in worst:
                print(f"  {record.symbol} on {record.exchange}: "
                      f"{record.slippage_bps:.1f} bps (${record.slippage_usd:.2f})")

        print("=" * 80 + "\n")
