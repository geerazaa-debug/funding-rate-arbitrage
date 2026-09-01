from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
import time

from src.utils.logger import setup_logger

logger = setup_logger("circuit_breaker")


class CircuitBreakerStatus(Enum):
    """Circuit breaker status."""
    ACTIVE = "active"
    WARNING = "warning"
    HALTED = "halted"


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""
    max_daily_loss_usd: float = 1000.0  # Max loss per day
    max_drawdown_pct: float = 5.0  # Max drawdown from peak
    consecutive_losses_limit: int = 3  # Stop after N consecutive losses
    recovery_timeout_minutes: int = 60  # Time to reset after halt
    enable_alerts: bool = True


class CircuitBreaker:
    """Monitors risk and halts trading if thresholds exceeded."""

    def __init__(self, initial_capital: float, config: Optional[CircuitBreakerConfig] = None):
        self.config = config or CircuitBreakerConfig()
        self.initial_capital = initial_capital
        self.peak_capital = initial_capital

        self.status = CircuitBreakerStatus.ACTIVE
        self.daily_loss = 0.0
        self.consecutive_losses = 0
        self.last_halt_time: Optional[float] = None

        self.daily_loss_reset_time = time.time() + 86400  # Reset daily loss after 24h

        logger.info(f"Initialized circuit breaker: max_daily_loss=${config.max_daily_loss_usd}")

    def check_trade_allowed(self, current_capital: float) -> bool:
        """Check if trading is allowed given current state."""
        # Auto-recover after timeout
        if self.status == CircuitBreakerStatus.HALTED:
            if self.last_halt_time and (time.time() - self.last_halt_time) > (self.config.recovery_timeout_minutes * 60):
                logger.warning("Circuit breaker recovery timeout reached, resuming trading")
                self._reset()
            else:
                logger.warning("Circuit breaker HALTED, trading not allowed")
                return False

        return self.status == CircuitBreakerStatus.ACTIVE

    def record_trade(self, entry_pnl: float, current_capital: float) -> bool:
        """Record trade result and check breach conditions."""
        # Reset daily loss if 24h passed
        if time.time() > self.daily_loss_reset_time:
            self.daily_loss = 0.0
            self.daily_loss_reset_time = time.time() + 86400
            logger.info("Daily loss reset")

        # Update peak capital
        if current_capital > self.peak_capital:
            self.peak_capital = current_capital

        # Track daily loss
        if entry_pnl < 0:
            self.daily_loss += abs(entry_pnl)

        # Track consecutive losses
        if entry_pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        # Check breaches
        return self._check_breaches(current_capital, entry_pnl)

    def _check_breaches(self, current_capital: float, trade_pnl: float) -> bool:
        """Check for circuit breaker triggers."""
        breaches = []

        # Daily loss limit
        if self.daily_loss > self.config.max_daily_loss_usd:
            breaches.append(f"Daily loss limit exceeded: ${self.daily_loss:.2f} > ${self.config.max_daily_loss_usd:.2f}")

        # Max drawdown
        drawdown = ((self.peak_capital - current_capital) / self.peak_capital) * 100
        if drawdown > self.config.max_drawdown_pct:
            breaches.append(f"Max drawdown exceeded: {drawdown:.2f}% > {self.config.max_drawdown_pct}%")

        # Consecutive losses
        if self.consecutive_losses >= self.config.consecutive_losses_limit:
            breaches.append(f"Consecutive losses: {self.consecutive_losses} >= {self.config.consecutive_losses_limit}")

        if breaches:
            logger.error("⚠️ CIRCUIT BREAKER TRIGGERED:")
            for breach in breaches:
                logger.error(f"  - {breach}")

            self.status = CircuitBreakerStatus.HALTED
            self.last_halt_time = time.time()
            return False

        # Warnings
        if self.daily_loss > self.config.max_daily_loss_usd * 0.75:
            self.status = CircuitBreakerStatus.WARNING
            logger.warning(f"Circuit breaker WARNING: Daily loss approaching limit "
                          f"(${self.daily_loss:.2f} / ${self.config.max_daily_loss_usd:.2f})")
            return True

        # All good
        self.status = CircuitBreakerStatus.ACTIVE
        return True

    def _reset(self):
        """Reset circuit breaker to active state."""
        self.status = CircuitBreakerStatus.ACTIVE
        self.peak_capital = self.peak_capital  # Keep peak
        self.daily_loss = 0.0  # Reset daily counter
        self.consecutive_losses = 0
        self.last_halt_time = None
        logger.info("Circuit breaker reset to ACTIVE")

    def get_status_dict(self) -> dict:
        """Get current status as dict."""
        drawdown = ((self.peak_capital - self.initial_capital) / self.peak_capital) * 100

        return {
            "status": self.status.value,
            "daily_loss": self.daily_loss,
            "daily_loss_limit": self.config.max_daily_loss_usd,
            "daily_loss_pct_of_limit": (self.daily_loss / self.config.max_daily_loss_usd) * 100,
            "drawdown_pct": drawdown,
            "drawdown_limit": self.config.max_drawdown_pct,
            "consecutive_losses": self.consecutive_losses,
            "consecutive_losses_limit": self.config.consecutive_losses_limit,
        }

    def __repr__(self):
        return f"CircuitBreaker(status={self.status.value}, daily_loss=${self.daily_loss:.2f})"
