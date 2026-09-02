import asyncio
import time
from typing import Optional, Dict
from dataclasses import dataclass

from src.utils.logger import setup_logger
from src.execution.order_executor import OrderExecutor, OrderSide, Order
from src.execution.position_manager import PositionManager
from src.data.funding_rate_tracker import FundingRateDifferential

logger = setup_logger("paper_trader")


@dataclass
class PaperTradingConfig:
    """Configuration for paper trading."""
    initial_capital: float = 10000.0
    max_leverage: float = 2.0
    min_differential_threshold: float = 0.0005  # 0.05% minimum to trade
    max_position_usd: float = 5000.0
    hold_time_s: Optional[float] = None  # None = hold until better rate, or max 1 hour
    slippage_bps: float = 2.0  # 2 bps slippage on entry


class PaperTrader:
    """Paper trading engine (simulated trades without real capital)."""

    def __init__(self, config: Optional[PaperTradingConfig] = None):
        self.config = config or PaperTradingConfig()
        self.order_executor = OrderExecutor()
        self.position_manager = PositionManager()

        self.capital = self.config.initial_capital
        self.cash_available = self.capital
        self.active_trades: Dict[str, Dict] = {}

        self.trade_count = 0
        self.skip_count = 0

        logger.info(f"Initialized paper trader: capital=${self.capital:.2f}")

    async def evaluate_and_trade(self, symbol: str, differential: FundingRateDifferential) -> bool:
        """
        Evaluate funding rate differential and execute if conditions met.

        Args:
            symbol: Trading pair
            differential: Current funding rate differential

        Returns:
            True if trade executed, False otherwise
        """
        if differential.differential is None:
            logger.debug(f"[{symbol}] Skipping: differential not ready")
            self.skip_count += 1
            return False

        # Check if differential is large enough
        abs_diff = abs(differential.differential)
        if abs_diff < self.config.min_differential_threshold:
            logger.debug(f"[{symbol}] Differential too small: {abs_diff:.6f} "
                        f"(threshold: {self.config.min_differential_threshold:.6f})")
            self.skip_count += 1
            return False

        # Determine which exchange has higher funding
        if differential.differential > 0:
            # Binance funding is higher -> SHORT Binance, LONG Bybit
            logger.info(f"[{symbol}] Opportunity: Binance ${abs_diff:.6f} higher")
            bybit_side = OrderSide.LONG
            binance_side = OrderSide.SHORT
        else:
            # Bybit funding is higher -> SHORT Bybit, LONG Binance
            logger.info(f"[{symbol}] Opportunity: Bybit ${abs_diff:.6f} higher")
            bybit_side = OrderSide.SHORT
            binance_side = OrderSide.LONG

        # Calculate position size
        quantity = self._calculate_position_size(symbol)
        if quantity <= 0:
            logger.warning(f"[{symbol}] Insufficient capital for minimum position")
            self.skip_count += 1
            return False

        # Place dual order
        dual_order = await self.order_executor.place_dual_order(
            symbol=symbol,
            bybit_side=bybit_side,
            binance_side=binance_side,
            quantity=quantity,
            timeout_ms=5000
        )

        if not dual_order:
            logger.error(f"[{symbol}] Dual order failed")
            return False

        # Register position
        self.position_manager.register_dual_order(dual_order)
        self.trade_count += 1

        # Track for later
        self.active_trades[symbol] = {
            "arb_id": dual_order.arb_id,
            "opened_at": time.time(),
            "differential": differential.differential,
            "quantity": quantity,
        }

        logger.info(f"[{symbol}] TRADE #{self.trade_count} OPENED: "
                   f"Bybit {bybit_side.value}, Binance {binance_side.value} "
                   f"x{quantity} | Diff: {abs_diff:.6f}")

        return True

    async def check_exit_conditions(self, symbol: str, current_differential: Optional[float]) -> bool:
        """Check if we should close the position."""
        if symbol not in self.active_trades:
            return False

        if current_differential is None:
            return False

        trade_info = self.active_trades[symbol]
        entry_diff = trade_info["differential"]
        duration = time.time() - trade_info["opened_at"]

        # Exit conditions:
        # 1. Differential reversed (profitability gone)
        # 2. Timeout reached
        # 3. Breakeven or worse

        max_hold_time = self.config.hold_time_s or 3600  # Default 1 hour

        if duration > max_hold_time:
            logger.info(f"[{symbol}] Timeout: held for {duration:.0f}s")
            return True

        if (entry_diff > 0 and current_differential <= 0) or (entry_diff < 0 and current_differential >= 0):
            logger.info(f"[{symbol}] Differential reversed: {entry_diff:.6f} -> {current_differential:.6f}")
            return True

        if abs(current_differential) < self.config.min_differential_threshold / 2:
            logger.info(f"[{symbol}] Differential converged: {current_differential:.6f}")
            return True

        return False

    async def close_position(self, symbol: str, exit_prices: Dict[str, float],
                           accumulated_funding: float = 0.0) -> bool:
        """Close an open position."""
        if symbol not in self.active_trades:
            return False

        trade_info = self.active_trades.pop(symbol)
        arb_id = trade_info["arb_id"]

        bybit_trade, binance_trade = self.position_manager.close_dual_order(
            arb_id=arb_id,
            exit_prices=exit_prices,
            funding_earned=accumulated_funding
        )

        if bybit_trade and binance_trade:
            combined_pnl = (bybit_trade.realized_pnl or 0) + (binance_trade.realized_pnl or 0)
            logger.info(f"[{symbol}] Trade closed: Combined PnL=${combined_pnl:.2f} "
                       f"(Bybit: ${bybit_trade.realized_pnl:.2f}, "
                       f"Binance: ${binance_trade.realized_pnl:.2f})")
            return True
        else:
            logger.error(f"[{symbol}] Failed to close position")
            return False

    def _calculate_position_size(self, symbol: str) -> float:
        """Calculate position size based on capital and leverage limits."""
        # Max position size based on capital and leverage
        max_notional = self.capital * self.config.max_leverage

        # But cap at max_position_usd per pair
        max_position = min(max_notional, self.config.max_position_usd)

        # Estimate size (assuming $45k BTC, $2.5k ETH, etc.)
        # For now, simple: use full available position
        quantity = max_position / 1000  # Rough estimate

        return quantity

    def get_portfolio_stats(self) -> Dict:
        """Get current portfolio statistics."""
        all_positions = self.position_manager.get_all_positions()
        total_unrealized = sum(
            self.position_manager.get_unrealized_pnl(symbol)
            for symbol in all_positions.keys()
        )
        total_realized = self.position_manager.get_realized_pnl()

        return {
            "capital": self.capital,
            "active_positions": sum(len(pos) for pos in all_positions.values()),
            "active_trades": len(self.active_trades),
            "unrealized_pnl": total_unrealized,
            "realized_pnl": total_realized,
            "total_pnl": total_unrealized + total_realized,
            "return_pct": (total_unrealized + total_realized) / self.capital * 100 if self.capital else 0,
            "trades_executed": self.trade_count,
            "trades_skipped": self.skip_count,
        }

    def print_summary(self):
        """Print paper trading summary."""
        stats = self.get_portfolio_stats()

        print("\n" + "=" * 80)
        print("PAPER TRADING SUMMARY")
        print("=" * 80)
        print(f"Initial Capital:    ${self.capital:,.2f}")
        print(f"Active Positions:   {stats['active_positions']}")
        print(f"Active Trades:      {stats['active_trades']}")
        print(f"Unrealized P&L:     ${stats['unrealized_pnl']:,.2f}")
        print(f"Realized P&L:       ${stats['realized_pnl']:,.2f}")
        print(f"Total P&L:          ${stats['total_pnl']:,.2f} ({stats['return_pct']:.2f}%)")
        print(f"Trades Executed:    {stats['trades_executed']}")
        print(f"Trades Skipped:     {stats['trades_skipped']}")

        trade_stats = self.position_manager.get_trade_statistics()
        if trade_stats["total_trades"] > 0:
            print("\n--- Trade Statistics ---")
            print(f"Total Trades:       {trade_stats['total_trades']}")
            print(f"Winning Trades:     {trade_stats['winning_trades']}")
            print(f"Losing Trades:      {trade_stats['losing_trades']}")
            print(f"Win Rate:           {trade_stats['win_rate']*100:.1f}%")
            print(f"Avg Trade P&L:      ${trade_stats['avg_pnl']:.2f}")
            print(f"Avg Trade Duration: {trade_stats['avg_duration_s']:.0f}s")

        if self.active_trades:
            print("\n--- Open Positions ---")
            for symbol, trade_info in self.active_trades.items():
                duration = time.time() - trade_info["opened_at"]
                print(f"  {symbol}: {trade_info['quantity']:.2f} units, "
                      f"open for {duration:.0f}s, diff={trade_info['differential']:.6f}")

        print("=" * 80 + "\n")
