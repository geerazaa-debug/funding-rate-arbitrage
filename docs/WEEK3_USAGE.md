# Week 3: Live Exchange Integration, Position Reconciliation & Funding Tracking

## Overview

Week 3 replaces Week 2's paper trading simulation with real exchange API integration and live tracking:

- **Exchange API Clients**: Authenticated REST clients for Bybit & Binance (testnet first, then live)
- **Live Position Reconciliation**: Verify tracked positions match actual exchange state
- **Funding Rate Accumulation**: Track and accumulate actual funding earned every 8 hours
- **Slippage Tracking**: Measure actual vs expected fills for performance analysis

All components support testnet mode for validation before live trading.

## Key Components

### Exchange API Clients (`src/execution/exchange_api_client.py`)

Authenticated REST clients for both exchanges with real order placement.

```python
from src.execution.exchange_api_client import LiveExchangeHub

hub = LiveExchangeHub(
    bybit_key="your_bybit_api_key",
    bybit_secret="your_bybit_secret",
    binance_key="your_binance_api_key",
    binance_secret="your_binance_secret",
    testnet=True  # Use testnet first
)

# Place real order on Bybit
order = await hub.bybit.place_order("BTCUSDT", "BUY", qty=1.0)
print(f"Order ID: {order['orderId']}")

# Get current positions
positions = await hub.bybit.get_positions("BTCUSDT")

# Place simultaneous orders on both exchanges (atomic)
dual_order = await hub.place_dual_order(
    symbol="BTCUSDT",
    bybit_side="BUY",
    binance_side="SELL",
    quantity=1.0,
    timeout_s=5.0
)
```

**Features:**
- HMAC-SHA256 signature support for both exchanges
- Testnet/live mode toggle
- Order status tracking
- Position queries
- Automatic error handling with logging

### Funding Accumulator (`src/execution/funding_accumulator.py`)

Tracks funding earned on open positions (funding paid every 8 hours).

```python
from src.execution.funding_accumulator import FundingAccumulator

accumulator = FundingAccumulator()

# Start tracking funding for a new position
accumulator.start_accrual(
    symbol="BTCUSDT",
    exchange="Bybit",
    position_size=1.0,
    funding_rate=0.00015
)

# Every 8 hours, apply new funding payment
funding_rate = 0.00020  # Updated rate
accumulator.apply_funding(
    symbol="BTCUSDT",
    exchange="Bybit",
    funding_rate=funding_rate,
    funding_timestamp=time.time()
)

# Close position and finalize funding
closed_accrual = accumulator.close_accrual("BTCUSDT", "Bybit")
print(f"Total funding earned: ${closed_accrual.accumulated_amount:.2f}")

# Get statistics
stats = accumulator.get_funding_stats("BTCUSDT")
# {symbol, total_payments, total_earned, avg_rate, max_rate, min_rate, exchanges}

accumulator.print_summary()
```

**Features:**
- Track funding per exchange
- Automatic funding payment calculation
- Historical funding records
- Statistics per symbol
- Integration with position manager

### Live Position Reconciler (`src/execution/live_position_reconciler.py`)

Reconciles tracked positions against live exchange state to catch discrepancies.

```python
from src.execution.live_position_reconciler import LivePositionReconciler

position_manager = PositionManager()
exchange_hub = LiveExchangeHub(...)

reconciler = LivePositionReconciler(position_manager, exchange_hub)

# Reconcile all positions
status = await reconciler.reconcile_all(symbols=["BTCUSDT", "ETHUSDT"])
# Returns: SYNCED, WARNING, or CRITICAL

# Get mismatches
mismatches = reconciler.get_mismatches(severity="critical")
for mismatch in mismatches:
    print(f"[CRITICAL] {mismatch.symbol} on {mismatch.exchange}:")
    print(f"  Tracked: {mismatch.tracked_qty}")
    print(f"  Actual:  {mismatch.actual_qty}")
    print(f"  Δ: {mismatch.difference_pct:.1f}%")

# Get status
status_dict = reconciler.get_sync_status()
# {status, critical_mismatches, warnings, sync_errors, last_sync}

reconciler.print_reconciliation_report()
```

**Features:**
- Real-time position matching against exchange API
- Severity levels: info, warning, critical
- Exchange-specific response format handling
- Partial fill detection
- Sync error tracking

### Slippage Tracker (`src/execution/slippage_tracker.py`)

Measures actual vs expected fills to identify market impact and execution quality.

```python
from src.execution.slippage_tracker import SlippageTracker

tracker = SlippageTracker()

# Record a trade with slippage
tracker.record_slippage(
    symbol="BTCUSDT",
    exchange="Bybit",
    expected_price=45000.0,
    actual_price=45010.0,  # 10 USD higher for BUY
    quantity=1.0,
    side="BUY"
)

# Get statistics
stats = tracker.get_slippage_stats()
# {count, avg_slippage_bps, max_slippage_bps, min_slippage_bps, total_slippage_usd, avg_slippage_usd}

# Compare across exchanges
by_exchange = tracker.get_exchange_comparison()
# {Bybit: {...stats...}, Binance: {...stats...}}

# Find worst trades
worst = tracker.get_worst_slippage(limit=5)
for record in worst:
    print(f"{record.symbol}: {record.slippage_bps:.1f} bps (${record.slippage_usd:.2f})")

tracker.print_slippage_report()
```

**Features:**
- Per-trade slippage measurement (basis points and USD)
- Aggregated statistics by exchange/symbol
- Historical slippage records
- Worst-case analysis
- Exchange quality comparison

## Integration with Week 1 & 2

Week 3 replaces paper trading simulation with real execution:

```python
# Week 1: Data layer
tracker = FundingRateTracker(["BTCUSDT", "ETHUSDT"])

# Week 2: Paper trading (now replaced with Week 3)
paper_trader = PaperTrader()  # Still available for testing

# Week 3: Live trading
exchange_hub = LiveExchangeHub(api_keys...)
funding_accumulator = FundingAccumulator()
reconciler = LivePositionReconciler(position_manager, exchange_hub)
slippage_tracker = SlippageTracker()

# Main loop
differential = tracker.get_latest_differential("BTCUSDT")
if differential and abs(differential.differential) > threshold:
    # Real order placement
    dual_order = await exchange_hub.place_dual_order(...)
    
    # Track funding
    funding_accumulator.start_accrual(...)
    
    # Monitor positions
    status = await reconciler.reconcile_all(["BTCUSDT"])
    
    # Measure slippage
    slippage_tracker.record_slippage(...)
```

## Configuration

Add to `config/config.json`:

```json
{
  "live_trading": {
    "enabled": true,
    "testnet": true,
    "api_keys": {
      "bybit": {
        "key": "${BYBIT_API_KEY}",
        "secret": "${BYBIT_API_SECRET}"
      },
      "binance": {
        "key": "${BINANCE_API_KEY}",
        "secret": "${BINANCE_API_SECRET}"
      }
    }
  },
  "reconciliation": {
    "enabled": true,
    "interval_seconds": 60,
    "alert_on_critical": true
  },
  "slippage": {
    "enabled": true,
    "track_all_trades": true
  }
}
```

Store API keys in `.env`:
```
BYBIT_API_KEY=your_testnet_key
BYBIT_API_SECRET=your_testnet_secret
BINANCE_API_KEY=your_testnet_key
BINANCE_API_SECRET=your_testnet_secret
```

## Testing

Run Week 3 tests:
```bash
pytest tests/test_week3_live.py -v
```

Run against testnet (requires API keys):
```bash
# Set env vars with testnet credentials
export BYBIT_API_KEY=...
export BYBIT_API_SECRET=...
export BINANCE_API_KEY=...
export BINANCE_API_SECRET=...

python -m src.main --live --testnet
```

## Safety Checklist Before Live Trading

- [ ] All tests passing (unit + integration)
- [ ] Reconciliation synced on testnet for 24h
- [ ] Slippage metrics stable (no outliers)
- [ ] Funding accumulation verified against exchanges
- [ ] Circuit breaker tested (can it halt?)
- [ ] Position sizing verified (max exposure enforced)
- [ ] Risk limits in place (daily loss, drawdown)
- [ ] Manual kill-switch working (can stop bot immediately)
- [ ] Logging active (all trades recorded)
- [ ] Capital allocated (small amount first)

## Troubleshooting

### Authentication Errors
- Check API keys are correct (use testnet keys first)
- Verify IP whitelist on exchange
- Ensure API permissions include order placement

### Position Mismatch
- Check for partial fills on the exchange
- Verify position size matches exactly
- Look for off-by-one errors in quantity tracking
- Check decimal precision (BTC vs satoshi)

### Funding Not Accumulating
- Verify funding is being paid (check exchange)
- Ensure 8h interval threshold is met
- Check position is actually open on exchange

### High Slippage
- Market conditions (check order book depth)
- Order size too large for market
- Consider splitting orders
- Check if market moving fast (high volatility)

## Week 4 Preview

Week 4 will add:
- Automated emergency exits (if exchange connection lost)
- Live dashboard with real-time metrics
- Webhook notifications (Telegram/Slack)
- Historical P&L database
- Tax reporting exports

## Key Differences from Paper Trading

| Aspect | Week 2 (Paper) | Week 3 (Live) |
|--------|---|---|
| Order Placement | Simulated | Real API calls |
| Fill Time | Fixed latency | Actual latency |
| Slippage | None | Measured |
| Funding Earned | Calculated | From exchange |
| Position Tracking | Ideal | Real & reconciled |
| Risk | None | Real capital |
| Reversibility | Can restart | Orders executed |

## API Rate Limits

**Bybit Testnet:**
- 20 requests/second (order placement)
- 10 requests/second (queries)

**Binance Testnet:**
- 1200 weight per minute (default)
- Orders typically 1 weight each

Monitor rate limit headers in logs if hitting limits.
