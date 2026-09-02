from typing import Dict, Optional, List
from dataclasses import dataclass

from src.utils.logger import setup_logger
from src.risk.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from src.risk.position_sizer import PositionSizer, SizingConfig
from src.execution.position_manager import PositionManager

logger = setup_logger("risk_monitor")


@dataclass
class RiskCheck:
    """Result of a risk check."""
    name: str
    passed: bool
    details: str
    severity: str = "warning"  # "warning" or "error"


class RiskMonitor:
    """Central risk monitoring and management system."""

    def __init__(self, initial_capital: float, position_manager: PositionManager,
                 circuit_config: Optional[CircuitBreakerConfig] = None,
                 sizing_config: Optional[SizingConfig] = None):
        self.position_manager = position_manager
        self.circuit_breaker = CircuitBreaker(initial_capital, circuit_config)
        self.position_sizer = PositionSizer(sizing_config or SizingConfig())

        self.initial_capital = initial_capital
        self.current_capital = initial_capital

        self.checks_passed = 0
        self.checks_failed = 0

        logger.info("Initialized risk monitor")

    async def pre_trade_checks(self, symbol: str, quantity: float, entry_price: float) -> List[RiskCheck]:
        """Run pre-trade risk checks."""
        checks = []

        # 1. Circuit breaker
        if not self.circuit_breaker.check_trade_allowed(self.current_capital):
            checks.append(RiskCheck(
                name="CircuitBreaker",
                passed=False,
                details=f"Trading halted: {self.circuit_breaker.status.value}",
                severity="error"
            ))
        else:
            checks.append(RiskCheck(
                name="CircuitBreaker",
                passed=True,
                details="Trading allowed",
            ))

        # 2. Position exposure
        current_exposure = self.position_manager.get_total_exposure(symbol)
        new_notional = quantity * entry_price
        if current_exposure + new_notional > 10000:
            checks.append(RiskCheck(
                name="PositionExposure",
                passed=False,
                details=f"Exposure limit: ${current_exposure + new_notional:.2f} > $10000",
                severity="error"
            ))
        else:
            checks.append(RiskCheck(
                name="PositionExposure",
                passed=True,
                details=f"Exposure OK: ${current_exposure + new_notional:.2f} / $10000",
            ))

        # 3. Net delta check (should be ~0 for delta-neutral)
        current_delta = self.position_manager.get_net_delta(symbol)
        if abs(current_delta) > quantity * 1.5:
            checks.append(RiskCheck(
                name="DeltaNeutrality",
                passed=False,
                details=f"Current delta {current_delta:.4f} would become {current_delta + quantity:.4f}",
                severity="warning"
            ))
        else:
            checks.append(RiskCheck(
                name="DeltaNeutrality",
                passed=True,
                details=f"Delta OK: {current_delta:.4f} -> {current_delta + quantity:.4f}",
            ))

        # 4. Capital sufficiency
        capital_used = new_notional / 2.0  # Assume 2x leverage
        if capital_used > self.current_capital * 0.5:
            checks.append(RiskCheck(
                name="CapitalSufficiency",
                passed=False,
                details=f"Would use {capital_used/self.current_capital*100:.1f}% of capital",
                severity="warning"
            ))
        else:
            checks.append(RiskCheck(
                name="CapitalSufficiency",
                passed=True,
                details=f"Capital usage: {capital_used/self.current_capital*100:.1f}%",
            ))

        # Summary
        passed = sum(1 for c in checks if c.passed)
        failed = sum(1 for c in checks if not c.passed)

        if failed > 0:
            self.checks_failed += failed
            logger.warning(f"[{symbol}] Pre-trade checks: {passed} passed, {failed} failed")
        else:
            self.checks_passed += passed
            logger.info(f"[{symbol}] All pre-trade checks passed")

        return checks

    def record_trade_result(self, symbol: str, pnl: float, quantity: float, duration_s: float):
        """Record trade results for risk tracking."""
        self.current_capital += pnl

        # Check circuit breaker
        allowed = self.circuit_breaker.record_trade(pnl, self.current_capital)

        logger.info(f"[{symbol}] Trade closed: PnL=${pnl:.2f}, "
                   f"Capital=${self.current_capital:.2f}, Duration={duration_s:.0f}s")

        if not allowed:
            logger.error(f"[{symbol}] Circuit breaker triggered!")

        return allowed

    def get_current_capital(self) -> float:
        """Get current capital."""
        return self.current_capital

    def get_risk_summary(self) -> Dict:
        """Get comprehensive risk summary."""
        all_positions = self.position_manager.get_all_positions()

        total_exposure = 0
        for symbol in all_positions:
            total_exposure += self.position_manager.get_total_exposure(symbol)

        unrealized_pnl = 0
        for symbol in all_positions:
            unrealized_pnl += self.position_manager.get_unrealized_pnl(symbol)

        realized_pnl = self.position_manager.get_realized_pnl()

        return {
            "current_capital": self.current_capital,
            "return_pct": ((self.current_capital - self.initial_capital) / self.initial_capital) * 100,
            "total_exposure": total_exposure,
            "open_positions": sum(len(pos) for pos in all_positions.values()),
            "unrealized_pnl": unrealized_pnl,
            "realized_pnl": realized_pnl,
            "circuit_breaker": self.circuit_breaker.get_status_dict(),
            "checks_passed": self.checks_passed,
            "checks_failed": self.checks_failed,
        }

    def print_risk_report(self):
        """Print risk monitoring report."""
        summary = self.get_risk_summary()
        cb_status = summary["circuit_breaker"]

        print("\n" + "=" * 80)
        print("RISK MANAGEMENT REPORT")
        print("=" * 80)
        print(f"Current Capital:    ${summary['current_capital']:,.2f}")
        print(f"Return:             {summary['return_pct']:.2f}%")
        print(f"Total Exposure:     ${summary['total_exposure']:,.2f}")
        print(f"Open Positions:     {summary['open_positions']}")
        print(f"Unrealized P&L:     ${summary['unrealized_pnl']:,.2f}")
        print(f"Realized P&L:       ${summary['realized_pnl']:,.2f}")

        print("\n--- Circuit Breaker ---")
        print(f"Status:             {cb_status['status'].upper()}")
        print(f"Daily Loss:         ${cb_status['daily_loss']:,.2f} / ${cb_status['daily_loss_limit']:,.2f} "
              f"({cb_status['daily_loss_pct_of_limit']:.1f}%)")
        print(f"Drawdown:           {cb_status['drawdown_pct']:.2f}% / {cb_status['drawdown_limit']:.2f}%")
        print(f"Consecutive Losses: {cb_status['consecutive_losses']} / {cb_status['consecutive_losses_limit']}")

        print("\n--- Checks ---")
        print(f"Passed: {summary['checks_passed']}")
        print(f"Failed: {summary['checks_failed']}")

        print("=" * 80 + "\n")
