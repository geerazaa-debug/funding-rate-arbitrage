import time
from typing import Optional, Dict
from datetime import datetime

from src.utils.logger import setup_logger
from src.execution.position_manager import PositionManager
from src.execution.funding_accumulator import FundingAccumulator
from src.execution.slippage_tracker import SlippageTracker
from src.risk.circuit_breaker import CircuitBreaker

logger = setup_logger("live_dashboard")


class LiveDashboard:
    """Real-time performance dashboard for the bot."""

    def __init__(self, position_manager: PositionManager, funding_accumulator: FundingAccumulator,
                 slippage_tracker: SlippageTracker, circuit_breaker: CircuitBreaker):
        self.position_manager = position_manager
        self.funding_accumulator = funding_accumulator
        self.slippage_tracker = slippage_tracker
        self.circuit_breaker = circuit_breaker

        self.start_time = time.time()
        self.last_update = 0

        logger.info("Initialized live dashboard")

    def get_dashboard_data(self) -> Dict:
        """Get current dashboard data."""
        uptime = time.time() - self.start_time
        trade_stats = self.position_manager.get_trade_statistics()
        all_positions = self.position_manager.get_all_positions()
        total_unrealized = sum(
            self.position_manager.get_unrealized_pnl(symbol)
            for symbol in all_positions.keys()
        )
        total_realized = self.position_manager.get_realized_pnl()

        slippage_stats = self.slippage_tracker.get_slippage_stats()
        cb_status = self.circuit_breaker.get_status_dict()

        return {
            "timestamp": datetime.now().isoformat(),
            "uptime_seconds": int(uptime),
            "positions": {
                "open": sum(len(pos) for pos in all_positions.values()),
                "total_exposure": sum(
                    self.position_manager.get_total_exposure(symbol)
                    for symbol in all_positions.keys()
                ),
            },
            "pnl": {
                "unrealized": total_unrealized,
                "realized": total_realized,
                "total": total_unrealized + total_realized,
            },
            "trades": {
                "total": trade_stats.get("total_trades", 0),
                "winning": trade_stats.get("winning_trades", 0),
                "losing": trade_stats.get("losing_trades", 0),
                "win_rate": trade_stats.get("win_rate", 0.0),
                "avg_pnl": trade_stats.get("avg_pnl", 0.0),
            },
            "funding": {
                "total_earned": self.funding_accumulator.get_total_funding(),
            },
            "slippage": {
                "avg_bps": slippage_stats.get("avg_slippage_bps", 0.0),
                "total_usd": slippage_stats.get("total_slippage_usd", 0.0),
            },
            "risk": {
                "circuit_breaker_status": cb_status.get("status", "unknown"),
                "daily_loss": cb_status.get("daily_loss", 0.0),
                "drawdown_pct": cb_status.get("drawdown_pct", 0.0),
            },
        }

    def print_dashboard(self):
        """Print formatted dashboard to console."""
        data = self.get_dashboard_data()

        print("\n" + "=" * 100)
        print(f"{'LIVE DASHBOARD':<40} {data['timestamp']:<40} Uptime: {data['uptime_seconds']//3600}h {(data['uptime_seconds']%3600)//60}m")
        print("=" * 100)

        # Positions section
        print(f"\nPOSITIONS ({data['positions']['open']} open)")
        print(f"   Total Exposure: ${data['positions']['total_exposure']:,.2f}")

        # P&L section
        print(f"\nP&L")
        unrealized = data['pnl']['unrealized']
        realized = data['pnl']['realized']
        total = data['pnl']['total']

        unrealized_color = "+" if unrealized > 0 else "-"
        realized_color = "+" if realized > 0 else "-"
        total_color = "+" if total > 0 else "-"

        print(f"   Unrealized: {unrealized_color} ${unrealized:,.2f}")
        print(f"   Realized:   {realized_color} ${realized:,.2f}")
        print(f"   Total:      {total_color} ${total:,.2f}")

        # Trades section
        trades = data['trades']
        print(f"\nTRADES ({trades['total']} total)")
        print(f"   W/L: {trades['winning']}/{trades['losing']}")
        print(f"   Win Rate: {trades['win_rate']*100:.1f}%")
        print(f"   Avg P&L: ${trades['avg_pnl']:.2f}")

        # Funding section
        print(f"\nFUNDING EARNED")
        print(f"   Total: ${data['funding']['total_earned']:.2f}")

        # Slippage section
        slippage = data['slippage']
        print(f"\nSLIPPAGE")
        print(f"   Avg: {slippage['avg_bps']:.2f} bps")
        print(f"   Total Cost: ${slippage['total_usd']:.2f}")

        # Risk section
        risk = data['risk']
        print(f"\n RISK MANAGEMENT")
        print(f"   Circuit Breaker: {risk['circuit_breaker_status'].upper()}")
        print(f"   Daily Loss: ${risk['daily_loss']:.2f}")
        print(f"   Drawdown: {risk['drawdown_pct']:.2f}%")

        print("=" * 100 + "\n")

    def print_detailed_report(self):
        """Print detailed performance report."""
        print("\n" + "=" * 100)
        print("DETAILED PERFORMANCE REPORT")
        print("=" * 100)

        # Position details
        all_positions = self.position_manager.get_all_positions()
        if all_positions:
            print("\nOPEN POSITIONS")
            for symbol, exchanges in all_positions.items():
                print(f"\n  {symbol}:")
                for exchange, position in exchanges.items():
                    pnl_str = f"${position.unrealized_pnl:.2f}" if position.unrealized_pnl else "N/A"
                    print(f"    {exchange}: {position.side} {position.quantity} @ ${position.entry_price:.2f}")
                    print(f"      P&L: {pnl_str}")

        # Trade details
        recent_trades = self.position_manager.trades[-10:]
        if recent_trades:
            print("\nRECENT TRADES (last 10)")
            for trade in recent_trades:
                pnl = f"${trade.realized_pnl:.2f}" if trade.realized_pnl else "N/A"
                duration = f"{trade.duration_seconds}s" if trade.duration_seconds else "N/A"
                print(f"  {trade.symbol}: {pnl} ({duration})")

        # Funding details
        recent_funding = self.funding_accumulator.funding_records[-10:]
        if recent_funding:
            print("\nRECENT FUNDING PAYMENTS (last 10)")
            for record in recent_funding:
                print(f"  {record.symbol} on {record.exchange}: ${record.amount:.2f} @ {record.funding_rate:.6f}")

        # Slippage details
        worst_slippage = self.slippage_tracker.get_worst_slippage(5)
        if worst_slippage:
            print("\nWORST SLIPPAGE EVENTS (top 5)")
            for record in worst_slippage:
                print(f"  {record.symbol} on {record.exchange}: {record.slippage_bps:.1f} bps (${record.slippage_usd:.2f})")

        print("=" * 100 + "\n")

    def export_metrics_json(self) -> str:
        """Export current metrics as JSON string."""
        import json
        data = self.get_dashboard_data()
        return json.dumps(data, indent=2)

    def export_metrics_csv(self, filename: str = "metrics.csv"):
        """Export metrics history to CSV."""
        import csv

        trades = self.position_manager.trades
        with open(filename, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                "Symbol", "Entry Time", "Exit Time", "Entry Price", "Exit Price",
                "Quantity", "Realized P&L", "Funding Earned", "Total P&L", "Duration (s)"
            ])
            for trade in trades:
                realized = trade.realized_pnl or 0.0
                funding = trade.funding_earned or 0.0
                writer.writerow([
                    trade.symbol,
                    trade.entry_time,
                    trade.exit_time,
                    trade.entry_price,
                    trade.exit_price,
                    trade.quantity,
                    realized,
                    funding,
                    # Previously wrote realized_pnl a second time, so the exported
                    # total silently omitted funding and disagreed with the dashboard.
                    realized + funding,
                    trade.duration_seconds,
                ])

        logger.info(f"Metrics exported to {filename}")
