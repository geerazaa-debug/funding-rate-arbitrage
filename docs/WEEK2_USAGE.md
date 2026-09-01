# Week 2: Order Execution, Paper Trading & Risk Management

## Overview

Week 2 adds the complete execution layer:
- **Order Executor**: Atomic dual-exchange order placement (Bybit + Binance)
- **Position Manager**: Track open positions and completed trades
- **Paper Trader**: Simulate trading without real capital
- **Risk Management**: Position sizing, circuit breakers, real-time risk monitoring

All components are production-ready with full error handling and logging.

## Architecture

```
FundingRateTracker (from Week 1)
  ↓ (funding rate differential)
  ↓
PaperTrader
  ├→ OrderExecutor (place orders on both exchanges)
  ├→ PositionManager (track positions & trades)
  └→ RiskMonitor
      ├→ CircuitBreaker (halt trading on breach)
      ├→ PositionSizer (calculate trade size)
      └→ RiskChecks (pre-trade validation)
```

## Key Components

### OrderExecutor (`src/execution/order_executor.py`)
Coordinates simultaneous order placement on both exchanges.

```python
executor = OrderExecutor()

# Place dual order (atomic: both succeed or both fail)
dual_order = await executor.place_dual_order(
    symbol="BTCUSDT",
    bybit_side=OrderSide.LONG,
    binance_side=OrderSide.SHORT,
    quantity=1.0,
    timeout_ms=5000
)

if dual_order:
    print(f"✓ Order filled: {dual_order.arb_id}")
    # Close position later
    await executor.close_dual_position(dual_order)
```

**Features:**
- Market orders with simulated execution latency
- Timeout + automatic rollback if one exchange fails
- Order history tracking
- Paper trading mode (no real capital)

### PositionManager (`src/execution/position_manager.py`)
Tracks all open positions and completed trades.

```python
manager = PositionManager()

# Register positions from dual order
manager.register_dual_order(dual_order)

# Track positions
positions = manager.get_all_positions()
delta = manager.get_net_delta("BTCUSDT")  # Should be ~0
exposure = manager.get_total_exposure("BTCUSDT")

# Close position
trade = manager.close_position("BTCUSDT", "Bybit", exit_price=46000.0)
print(f"Realized P&L: ${trade.realized_pnl:.2f}")

# Stats
stats = manager.get_trade_statistics()
# {total_trades, winning_trades, losing_trades, win_rate, avg_pnl, ...}
```

**Features:**
- Open position tracking
- Real-time P&L calculation
- Trade history & statistics
- Delta-neutral position validation
- Funding earned tracking

### PaperTrader (`src/execution/paper_trader.py`)
Simulates trading with configurable risk parameters.

```python
config = PaperTradingConfig(
    initial_capital=10000.0,
    max_leverage=2.0,
    min_differential_threshold=0.0005,  # 0.05% min to trade
    max_position_usd=5000.0,
)
trader = PaperTrader(config)

# Evaluate opportunity and trade
differential = tracker.get_latest_differential("BTCUSDT")
traded = await trader.evaluate_and_trade("BTCUSDT", differential)

# Check exit conditions
should_exit = await trader.check_exit_conditions("BTCUSDT", new_differential)
if should_exit:
    await trader.close_position("BTCUSDT", exit_prices={"bybit": 46000, "binance": 46100})

# Stats
stats = trader.get_portfolio_stats()
# {capital, active_positions, unrealized_pnl, realized_pnl, return_pct, ...}
trader.print_summary()
```

**Features:**
- Automatic trade entry/exit based on differential
- Capital management
- Position sizing based on available capital
- Trade performance tracking
- Paper P&L calculation

### Risk Management Suite

#### PositionSizer (`src/risk/position_sizer.py`)
Calculates optimal position sizes.

```python
config = SizingConfig(
    strategy=SizingStrategy.FIXED,
    base_size_usd=1000.0,
    max_size_usd=5000.0,
    max_total_exposure=10000.0,
)
sizer = PositionSizer(config)

# Strategies:
# - FIXED: Always trade same size
# - VOLATILITY_ADJUSTED: Scale inversely to volatility
# - KELLY: Edge-based sizing (sophisticated)

quantity = sizer.calculate_size(
    capital=10000.0,
    symbol="BTCUSDT",
    entry_price=45000.0,
    volatility=0.02,
    existing_exposure=3000.0
)
```

#### CircuitBreaker (`src/risk/circuit_breaker.py`)
Halts trading if risk limits exceeded.

```python
config = CircuitBreakerConfig(
    max_daily_loss_usd=1000.0,
    max_drawdown_pct=5.0,
    consecutive_losses_limit=3,
    recovery_timeout_minutes=60
)
breaker = CircuitBreaker(initial_capital=10000.0, config=config)

# After each trade
allowed = breaker.record_trade(trade_pnl, current_capital)
if not allowed:
    print("⚠️ Circuit breaker halted trading!")

# Check before trading
if not breaker.check_trade_allowed(current_capital):
    print("Trading not allowed")

# Get status
status = breaker.get_status_dict()
# {status, daily_loss, drawdown_pct, consecutive_losses, ...}
```

#### RiskMonitor (`src/risk/risk_monitor.py`)
Comprehensive risk monitoring system.

```python
position_manager = PositionManager()
monitor = RiskMonitor(initial_capital=10000.0, position_manager=position_manager)

# Pre-trade checks
checks = await monitor.pre_trade_checks("BTCUSDT", quantity=1.0, entry_price=45000.0)
# Returns list of RiskCheck results (all must pass to trade)

# Record trade results
monitor.record_trade_result("BTCUSDT", pnl=100.0, quantity=1.0, duration_s=3600)

# Get risk summary
summary = monitor.get_risk_summary()
# {capital, exposure, open_positions, unrealized_pnl, realized_pnl, circuit_breaker_status, ...}

monitor.print_risk_report()
```

## Configuration

Edit `config/config.json`:

```json
{
  "paper_trading": {
    "enabled": true,
    "initial_capital": 10000,
    "min_differential_threshold": 0.0005,
    "max_position_usd": 5000
  },
  "risk": {
    "max_leverage": 2.0,
    "max_daily_loss": 1000,
    "max_drawdown_pct": 5.0,
    "consecutive_losses_limit": 3,
    "circuit_breaker_recovery_minutes": 60
  },
  "position_sizing": {
    "strategy": "fixed",
    "base_size_usd": 1000,
    "max_size_usd": 5000
  }
}
```

## Testing

Run tests:
```bash
pytest tests/test_execution_and_risk.py -v
```

Run specific test:
```bash
pytest tests/test_execution_and_risk.py::TestPaperTrader::test_trade_execution -v
```

## Paper Trading Mode

In paper trading mode:
- Orders are simulated with configurable latency (150ms Bybit, 200ms Binance)
- P&L is tracked but no real capital moves
- Full position tracking and statistics
- Risk management fully active (circuit breakers, position limits)
- Ideal for validation before live trading

## Success Criteria (Week 2)

After running with Week 1 data layer:

✅ **Order Execution**: Dual orders placed and filled
✅ **Position Tracking**: Open/closed positions tracked accurately
✅ **P&L Calculation**: Realized and unrealized P&L correct
✅ **Risk Checks**: Pre-trade checks pass/fail as expected
✅ **Circuit Breaker**: Halts trading after defined losses
✅ **Paper Trading**: 100+ simulated trades without crashes
✅ **Statistics**: Win rate, avg P&L, duration tracked

## Integration with Week 1

Week 2 integrates seamlessly with Week 1:

```python
# From Week 1
tracker = FundingRateTracker(["BTCUSDT", "ETHUSDT"])

# Week 2 components
paper_trader = PaperTrader()
risk_monitor = RiskMonitor(initial_capital=10000.0, position_manager=paper_trader.position_manager)

# In main loop
differential = tracker.get_latest_differential("BTCUSDT")
if differential:
    # Risk checks
    checks = await risk_monitor.pre_trade_checks("BTCUSDT", quantity=1.0, entry_price=45000.0)
    if all(c.passed for c in checks):
        # Execute trade
        await paper_trader.evaluate_and_trade("BTCUSDT", differential)
    
    # Check exits
    should_exit = await paper_trader.check_exit_conditions("BTCUSDT", differential.differential)
    if should_exit:
        await paper_trader.close_position("BTCUSDT", exit_prices={...})
    
    # Monitor risk
    risk_monitor.print_risk_report()
```

## Next Steps (Week 3)

- Integrate live WebSocket position updates
- Add slippage estimation
- Implement stop-loss orders
- Refine funding rate earned tracking
- Add exchange account reconciliation

## Troubleshooting

**Orders not filling:**
- Check order latency in logs
- Verify exchange connectivity from Week 1

**Delta not neutral:**
- Orders may not have both executed atomically
- Check `DeltaNeutrality` risk check results

**Circuit breaker halting too early:**
- Adjust `max_daily_loss_usd` in config
- Check consecutive loss limit
- Review drawdown threshold

**Position manager showing stale data:**
- Ensure closing orders match opening sizes exactly
- Check for partial fills (not in paper trading)
