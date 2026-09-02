import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from collections import defaultdict

from src.utils.logger import setup_logger

logger = setup_logger("funding_accumulator")


@dataclass
class FundingAccrual:
    """Represents accumulated funding for a position."""
    symbol: str
    exchange: str
    position_size: float
    funding_rate: float
    accrual_time: float
    accumulated_amount: float

    def __repr__(self):
        return (f"FundingAccrual({self.exchange} {self.symbol} "
                f"${self.accumulated_amount:.2f} @ {self.funding_rate:.6f})")


@dataclass
class FundingRecord:
    """Represents a recorded funding payment."""
    symbol: str
    exchange: str
    timestamp: float
    funding_rate: float
    position_size: float
    amount: float

    def __repr__(self):
        return (f"FundingRecord({self.exchange} {self.symbol} "
                f"${self.amount:.2f} @ {self.funding_rate:.6f})")


class FundingAccumulator:
    """Tracks and accumulates funding earned on open positions."""

    def __init__(self):
        # Active accruals: {symbol -> {exchange -> FundingAccrual}}
        self.active_accruals: Dict[str, Dict[str, FundingAccrual]] = defaultdict(dict)

        # Completed funding records (historical)
        self.funding_records: List[FundingRecord] = []

        # Total accumulated funding per symbol
        self.total_funding: Dict[str, float] = defaultdict(float)

        # Last funding rate applied (8h intervals)
        self.last_funding_time: Dict[str, float] = defaultdict(int)

        logger.info("Initialized funding accumulator")

    def start_accrual(self, symbol: str, exchange: str, position_size: float,
                     funding_rate: float) -> FundingAccrual:
        """Start accumulating funding for a new position."""
        accrual = FundingAccrual(
            symbol=symbol,
            exchange=exchange,
            position_size=position_size,
            funding_rate=funding_rate,
            accrual_time=time.time(),
            accumulated_amount=0.0,
        )

        self.active_accruals[symbol][exchange] = accrual
        logger.info(f"[{symbol}] Started funding accrual on {exchange}: "
                   f"{position_size} @ {funding_rate:.6f}")

        return accrual

    def apply_funding(self, symbol: str, exchange: str, funding_rate: float,
                     funding_timestamp: float) -> Optional[FundingAccrual]:
        """
        Apply funding payment to an active position.
        Funding is paid every 8 hours.
        """
        if symbol not in self.active_accruals or exchange not in self.active_accruals[symbol]:
            logger.warning(f"[{symbol}] No active position on {exchange} for funding")
            return None

        accrual = self.active_accruals[symbol][exchange]

        # Check if enough time has passed (funding typically every 8h)
        time_since_last = funding_timestamp - self.last_funding_time.get(f"{symbol}_{exchange}", 0)
        if time_since_last < 3600:  # Less than 1h since last
            logger.debug(f"[{symbol}] Skipping funding (too soon)")
            return None

        # Calculate funding amount: position_size * funding_rate
        # Note: This is simplified; actual funding includes mark price adjustments
        funding_amount = accrual.position_size * funding_rate

        accrual.accumulated_amount += funding_amount
        accrual.funding_rate = funding_rate  # Update latest rate

        # Record funding payment
        record = FundingRecord(
            symbol=symbol,
            exchange=exchange,
            timestamp=funding_timestamp,
            funding_rate=funding_rate,
            position_size=accrual.position_size,
            amount=funding_amount,
        )
        self.funding_records.append(record)

        # Update totals
        self.total_funding[symbol] += funding_amount
        self.last_funding_time[f"{symbol}_{exchange}"] = funding_timestamp

        logger.info(f"[{symbol}] Funding applied on {exchange}: "
                   f"+${funding_amount:.2f} (total: ${accrual.accumulated_amount:.2f})")

        return accrual

    def close_accrual(self, symbol: str, exchange: str) -> Optional[FundingAccrual]:
        """Close funding accrual when position is closed."""
        if symbol not in self.active_accruals or exchange not in self.active_accruals[symbol]:
            logger.warning(f"[{symbol}] No active accrual on {exchange}")
            return None

        accrual = self.active_accruals[symbol].pop(exchange)
        logger.info(f"[{symbol}] Closed funding accrual on {exchange}: "
                   f"total accumulated ${accrual.accumulated_amount:.2f}")

        return accrual

    def get_accrual(self, symbol: str, exchange: str) -> Optional[FundingAccrual]:
        """Get current funding accrual for a position."""
        return self.active_accruals.get(symbol, {}).get(exchange)

    def get_total_funding(self, symbol: Optional[str] = None) -> float:
        """Get total funding earned."""
        if symbol:
            return self.total_funding.get(symbol, 0.0)
        return sum(self.total_funding.values())

    def get_funding_history(self, symbol: Optional[str] = None, limit: int = 100) -> List[FundingRecord]:
        """Get funding payment history."""
        records = self.funding_records
        if symbol:
            records = [r for r in records if r.symbol == symbol]
        return records[-limit:]

    def get_funding_stats(self, symbol: str) -> Dict:
        """Calculate funding statistics for a symbol."""
        records = [r for r in self.funding_records if r.symbol == symbol]

        if not records:
            return {
                "symbol": symbol,
                "total_payments": 0,
                "total_earned": 0.0,
                "avg_rate": 0.0,
                "max_rate": 0.0,
                "min_rate": 0.0,
            }

        amounts = [r.amount for r in records]
        rates = [r.funding_rate for r in records]

        return {
            "symbol": symbol,
            "total_payments": len(records),
            "total_earned": sum(amounts),
            "avg_rate": sum(rates) / len(rates),
            "max_rate": max(rates),
            "min_rate": min(rates),
            "exchanges": list(set(r.exchange for r in records)),
        }

    def print_summary(self):
        """Print funding accumulation summary."""
        if not self.active_accruals and not self.funding_records:
            print("No funding data yet")
            return

        print("\n" + "=" * 80)
        print("FUNDING ACCUMULATION SUMMARY")
        print("=" * 80)

        if self.active_accruals:
            print("\nActive Accruals:")
            for symbol, exchanges in self.active_accruals.items():
                for exchange, accrual in exchanges.items():
                    print(f"  {symbol} on {exchange}: "
                          f"${accrual.accumulated_amount:.2f} "
                          f"({accrual.position_size} units @ {accrual.funding_rate:.6f})")

        print(f"\nTotal Funding Earned: ${self.get_total_funding():.2f}")

        print("\nBy Symbol:")
        for symbol in sorted(set(r.symbol for r in self.funding_records)):
            stats = self.get_funding_stats(symbol)
            print(f"  {symbol}: ${stats['total_earned']:.2f} "
                  f"({stats['total_payments']} payments, "
                  f"avg rate {stats['avg_rate']:.6f})")

        print("=" * 80 + "\n")
