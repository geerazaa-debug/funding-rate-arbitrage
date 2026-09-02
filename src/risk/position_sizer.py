from dataclasses import dataclass
from enum import Enum

from src.utils.logger import setup_logger

logger = setup_logger("position_sizer")


class SizingStrategy(Enum):
    """Position sizing strategies."""
    FIXED = "fixed"
    VOLATILITY_ADJUSTED = "volatility_adjusted"
    KELLY = "kelly"


@dataclass
class SizingConfig:
    """Configuration for position sizing."""
    strategy: SizingStrategy = SizingStrategy.FIXED
    base_size_usd: float = 1000.0  # Base position size in USD
    max_size_usd: float = 5000.0  # Max position per pair
    min_size_usd: float = 100.0  # Min position per pair
    max_total_exposure: float = 10000.0  # Total across all positions
    max_leverage: float = 2.0


class PositionSizer:
    """Calculates optimal position sizes based on risk parameters."""

    def __init__(self, config: SizingConfig):
        self.config = config
        logger.info(f"Initialized position sizer: strategy={config.strategy.value}")

    def calculate_size(
        self,
        capital: float,
        symbol: str,
        entry_price: float,
        volatility: float = 0.02,
        existing_exposure: float = 0.0,
        differential: float = 0.0
    ) -> float:
        """
        Calculate position size in base units.

        Args:
            capital: Available capital
            symbol: Trading pair
            entry_price: Entry price for position
            volatility: Annualized volatility (for vol adjustment)
            existing_exposure: Current notional exposure
            differential: Current funding rate differential

        Returns:
            Position size in base units (e.g., BTC, ETH)
        """
        if self.config.strategy == SizingStrategy.FIXED:
            return self._fixed_size(entry_price, existing_exposure)
        elif self.config.strategy == SizingStrategy.VOLATILITY_ADJUSTED:
            return self._volatility_adjusted_size(entry_price, volatility, existing_exposure)
        elif self.config.strategy == SizingStrategy.KELLY:
            return self._kelly_size(entry_price, differential, existing_exposure)
        else:
            return self._fixed_size(entry_price, existing_exposure)

    def _fixed_size(self, entry_price: float, existing_exposure: float) -> float:
        """Fixed position sizing."""
        # Check we won't exceed max exposure
        new_notional = self.config.base_size_usd
        if existing_exposure + new_notional > self.config.max_total_exposure:
            logger.warning(f"Reducing size: would exceed max exposure ${self.config.max_total_exposure}")
            new_notional = max(
                self.config.min_size_usd,
                self.config.max_total_exposure - existing_exposure
            )

        quantity = new_notional / entry_price
        logger.info(f"Fixed size: ${new_notional:.2f} -> {quantity:.4f} units")
        return quantity

    def _volatility_adjusted_size(self, entry_price: float, volatility: float,
                                  existing_exposure: float) -> float:
        """Size based on volatility (lower vol = larger position)."""
        if volatility <= 0:
            volatility = 0.02

        # Inverse vol scaling: higher vol = smaller position
        vol_adjustment = 1.0 / (1.0 + volatility)
        adjusted_size = self.config.base_size_usd * vol_adjustment

        # Clamp to min/max
        adjusted_size = max(self.config.min_size_usd, adjusted_size)
        adjusted_size = min(self.config.max_size_usd, adjusted_size)

        # Check total exposure
        if existing_exposure + adjusted_size > self.config.max_total_exposure:
            adjusted_size = max(
                self.config.min_size_usd,
                self.config.max_total_exposure - existing_exposure
            )

        quantity = adjusted_size / entry_price
        logger.info(f"Vol-adjusted size: vol={volatility:.2%}, ${adjusted_size:.2f} -> {quantity:.4f} units")
        return quantity

    def _kelly_size(self, entry_price: float, differential: float,
                   existing_exposure: float) -> float:
        """
        Kelly sizing based on expected edge (funding rate differential).
        Kelly = (edge * win_rate - loss_rate) / odds

        Simplified: scale position based on differential magnitude
        """
        if differential <= 0:
            # No edge, min size
            quantity = self.config.min_size_usd / entry_price
            logger.info(f"Kelly size: no positive edge, min position {quantity:.4f} units")
            return quantity

        # Assume 60% win rate from arb (very conservative)
        win_rate = 0.60
        loss_rate = 0.40

        # Odds = 1:1 (for delta-neutral arb)
        odds = 1.0

        # Kelly fraction (edge units): differential-based
        kelly_frac = (differential * win_rate - loss_rate) / odds if odds > 0 else 0.01

        # Scale to position size (clamp to 0-25% Kelly)
        kelly_frac = max(0, min(0.25, kelly_frac))

        base_size = self.config.base_size_usd * kelly_frac
        base_size = max(self.config.min_size_usd, base_size)
        base_size = min(self.config.max_size_usd, base_size)

        if existing_exposure + base_size > self.config.max_total_exposure:
            base_size = max(
                self.config.min_size_usd,
                self.config.max_total_exposure - existing_exposure
            )

        quantity = base_size / entry_price
        logger.info(f"Kelly size: kelly_frac={kelly_frac:.2%}, ${base_size:.2f} -> {quantity:.4f} units")
        return quantity

    def can_trade(self, existing_exposure: float) -> bool:
        """Check if we can open a new position."""
        return existing_exposure + self.config.min_size_usd <= self.config.max_total_exposure
