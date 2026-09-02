# Week 4: Emergency Exits, Live Dashboard, Webhooks & Database

## Overview

Week 4 adds operational monitoring, alerting, emergency procedures, and persistent data storage:

- **Emergency Exit Handler**: Automatic position liquidation on critical conditions
- **Webhook Alerts**: Send alerts to Slack, Telegram, Discord, or custom services
- **Live Dashboard**: Real-time performance metrics (terminal + JSON export)
- **Database Storage**: SQLite persistence for all trades, positions, funding, metrics

## Key Components

### Emergency Exit Handler (`src/execution/emergency_exit.py`)

Handles controlled liquidation of all positions when critical conditions detected.

```python
from src.execution.emergency_exit import EmergencyExitHandler, EmergencyReason

handler = EmergencyExitHandler(position_manager, exchange_hub)

# Trigger emergency exit
report = await handler.trigger_exit(
    reason=EmergencyReason.CIRCUIT_BREAKER,
    details="Daily loss limit exceeded: $1200 > $1000"
)

# Check result
if report.success:
    print(f"All {len(report.orders)} positions exited successfully")
else:
    print(f"Partial exit: {sum(1 for o in report.orders if o.status == 'filled')} filled")

# Wait for orders to complete
filled = await handler.wait_for_exits(timeout_s=30)

# View history
history = handler.get_exit_history(limit=10)
for report in history:
    handler.print_exit_summary(report)
```

**Exit Reasons:**
- `MANUAL_TRIGGER` — User initiated shutdown
- `CIRCUIT_BREAKER` — Risk limits breached
- `CONNECTION_LOST` — Exchange connection failed
- `EXCHANGE_ERROR` — API errors detected
- `POSITION_MISMATCH` — Tracking vs actual mismatch
- `MARKET_HALT` — Exchange halted trading
- `CRITICAL_ERROR` — Bot error detected

**Features:**
- Atomic multi-position exit (all or nothing)
- Order status tracking
- Timeout protection (default 30s)
- Exit order history
- Detailed reporting

### Webhook Alerts (`src/alerts/webhook_alerter.py`)

Send alerts to multiple services (Slack, Telegram, Discord).

```python
from src.alerts.webhook_alerter import WebhookAlerter

alerter = WebhookAlerter()

# Register webhooks
alerter.register_webhook("slack", "https://hooks.slack.com/services/YOUR/WEBHOOK/URL")
alerter.register_webhook("telegram", "https://api.telegram.org/botXXX/sendMessage?chat_id=YYY")

# Send alert
await alerter.alert_trade_opened(
    symbol="BTCUSDT",
    exchanges="Bybit/Binance",
    quantity=1.0,
    differential=0.0001,
    services=["slack", "telegram"]  # Send to both
)

# Other alerts
await alerter.alert_trade_closed("BTCUSDT", pnl=250.50, duration_s=3600)
await alerter.alert_circuit_breaker("Daily loss exceeded")
await alerter.alert_emergency_exit("Connection lost to exchange")
await alerter.alert_position_mismatch(
    symbol="BTCUSDT",
    exchange="Bybit",
    tracked_qty=1.0,
    actual_qty=0.5
)

# Get alert history
alerts = alerter.get_alert_history(limit=50)
```

**Alert Types:**
- `TRADE_OPENED` — Position opened
- `TRADE_CLOSED` — Position closed with P&L
- `POSITION_MISMATCH` — Tracking discrepancy detected
- `CIRCUIT_BREAKER` — Trading halted
- `EMERGENCY_EXIT` — All positions liquidated
- `CONNECTION_LOST` — Exchange unavailable
- `HIGH_SLIPPAGE` — Unusual fill cost
- `FUNDING_UPDATE` — Funding payment received
- `ERROR` — Bot error occurred

**Supported Services:**
- **Slack**: Formatted with colors and fields
- **Telegram**: Markdown formatted messages
- **Discord**: Rich embeds
- **Custom**: Generic JSON format

### Database Storage (`src/storage/database.py`)

Persistent SQLite storage for all trading data.

```python
from src.storage.database import Database
from src.storage.models import TradeRecord, PositionRecord, FundingRecord

db = Database("data/bot.db")
db.connect()

# Insert records
trade = TradeRecord(
    symbol="BTCUSDT",
    entry_price=45000.0,
    exit_price=46000.0,
    quantity=1.0,
    realized_pnl=1000.0,
    funding_earned=50.0,
)
db.insert_trade(trade)

# Query data
trades = db.get_trades("BTCUSDT", limit=100)
positions = db.get_open_positions()
funding = db.get_funding_history("BTCUSDT")

# Statistics
stats = db.get_trade_statistics()
print(f"Win rate: {stats['win_rate']*100:.1f}%")
print(f"Total P&L: ${stats['total_pnl']:.2f}")

# Cleanup
db.cleanup_old_data(days=90)  # Delete records older than 90 days

db.disconnect()
```

**Tables:**
- `trades` — Completed trades with entry/exit prices, P&L, duration
- `positions` — Open positions with current price and unrealized P&L
- `funding` — Funding payment history
- `slippage` — Individual trade slippage records
- `metrics` — Performance snapshots (capital, drawdown, win rate, etc.)
- `alerts` — All alerts sent (with timestamps and services)

### Live Dashboard (`src/monitoring/live_dashboard.py`)

Real-time performance monitoring in terminal or JSON.

```python
from src.monitoring.live_dashboard import LiveDashboard

dashboard = LiveDashboard(
    position_manager,
    funding_accumulator,
    slippage_tracker,
    circuit_breaker
)

# Print to console
dashboard.print_dashboard()  # Quick metrics
dashboard.print_detailed_report()  # Full details

# Export formats
json_data = dashboard.export_metrics_json()  # Returns JSON string
dashboard.export_metrics_csv("metrics.csv")  # Write to file

# Get programmatic access
data = dashboard.get_dashboard_data()
print(f"P&L: ${data['pnl']['total']:.2f}")
print(f"Open Positions: {data['positions']['open']}")
print(f"Win Rate: {data['trades']['win_rate']*100:.1f}%")
```

**Metrics Displayed:**
- **Positions**: Count and total exposure
- **P&L**: Unrealized, realized, and total
- **Trades**: Win/loss count, win rate, average P&L
- **Funding**: Total earned
- **Slippage**: Average and total cost
- **Risk**: Circuit breaker status, daily loss, drawdown

## Integration: Complete Stack

```python
# Week 1 + 2 + 3
tracker = FundingRateTracker()
paper_trader = PaperTrader()  # or use live clients
exchange_hub = LiveExchangeHub(...)

# Week 4 additions
from src.execution.emergency_exit import EmergencyExitHandler
from src.alerts.webhook_alerter import WebhookAlerter
from src.storage.database import Database
from src.monitoring.live_dashboard import LiveDashboard

# Initialize all components
emergency_handler = EmergencyExitHandler(position_manager, exchange_hub)
alerter = WebhookAlerter()
alerter.register_webhook("slack", os.getenv("SLACK_WEBHOOK_URL"))

db = Database("data/bot.db")
db.connect()

dashboard = LiveDashboard(position_manager, funding_acc, slippage, circuit_breaker)

# Main loop
async def main_loop():
    while True:
        try:
            # ... trading logic ...
            
            # On trade closed
            db.insert_trade(trade_record)
            await alerter.alert_trade_closed(symbol, pnl, duration)
            
            # Periodic reporting
            if time.time() % 300 < 1:  # Every 5 minutes
                dashboard.print_dashboard()
                db.insert_metrics(metrics_snapshot)
                
        except Exception as e:
            await alerter.alert_error(f"Bot error: {e}")
            await emergency_handler.trigger_exit(EmergencyReason.CRITICAL_ERROR, str(e))
            break

asyncio.run(main_loop())
```

## Configuration

Add to `config/config.json`:

```json
{
  "alerts": {
    "enabled": true,
    "webhooks": {
      "slack": "${SLACK_WEBHOOK_URL}",
      "telegram": "${TELEGRAM_WEBHOOK_URL}",
      "discord": "${DISCORD_WEBHOOK_URL}"
    },
    "alert_on": {
      "trade_opened": true,
      "trade_closed": true,
      "circuit_breaker": true,
      "emergency_exit": true,
      "position_mismatch": true
    }
  },
  "database": {
    "enabled": true,
    "path": "data/bot.db",
    "cleanup_days": 90
  },
  "dashboard": {
    "enabled": true,
    "print_interval_seconds": 300,
    "export_csv": true
  },
  "emergency_exit": {
    "enabled": true,
    "auto_trigger_on_circuit_breaker": true,
    "auto_trigger_on_connection_loss": true
  }
}
```

Store secrets in `.env`:
```
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
TELEGRAM_WEBHOOK_URL=https://api.telegram.org/bot...
DISCORD_WEBHOOK_URL=https://discordapp.com/api/webhooks/...
```

## Examples

### Auto-Alert Trade Lifecycle

```python
# On trade entry
await alerter.alert_trade_opened(symbol, exchanges, qty, diff)

# Periodically check
for symbol in active_symbols:
    # Every 8h when funding is paid
    await alerter.alert_funding_update(symbol, amount)

# On exit
await alerter.alert_trade_closed(symbol, pnl, duration)

# If circuit breaker halts
await alerter.alert_circuit_breaker("Daily loss exceeded")
```

### Emergency Exit Sequence

```python
# Detect critical condition
if circuit_breaker.status == CircuitBreakerStatus.HALTED:
    # Initiate exit
    report = await emergency_handler.trigger_exit(
        EmergencyReason.CIRCUIT_BREAKER,
        "Daily loss limit breached"
    )
    
    # Alert users
    await alerter.alert_emergency_exit("Circuit breaker activated")
    
    # Record in database
    db.insert_alert(alert_record)
    
    # Wait for completion
    success = await emergency_handler.wait_for_exits(timeout_s=30)
```

### Data Export for Analysis

```python
# Export everything for analysis
trades = db.get_trades(limit=1000)
dashboard.export_metrics_csv("backtest_results.csv")

# Calculate stats
stats = db.get_trade_statistics()
slippage = db.get_slippage_stats()
```

## Monitoring Checklist

Daily:
- [ ] Check dashboard output
- [ ] Review alert history
- [ ] Verify database size (cleanup if >100MB)

Weekly:
- [ ] Export metrics for analysis
- [ ] Review slippage by exchange
- [ ] Analyze win/loss rates
- [ ] Check alert delivery (test with dummy alert)

Monthly:
- [ ] Archive historical database
- [ ] Review circuit breaker triggers
- [ ] Audit emergency exit procedures
- [ ] Optimize alert thresholds

## Troubleshooting

**Database locked error:**
- Only one process can access the database at a time
- Ensure bot isn't running twice

**Webhooks not sending:**
- Verify URL is correct (test with curl)
- Check service authentication (API tokens, channel IDs)
- Review network connectivity

**Dashboard showing zeros:**
- No trades executed yet (wait or check order execution)
- Position manager not updated (verify position tracking)

**Emergency exit hangs:**
- Exchange API slow (increase timeout)
- Orders partially filled (check exchange manually)
- Position already closed (check reconciliation)

## Week 5 Preview

Future enhancements:
- Web UI dashboard (instead of terminal)
- Telegram bot for manual commands
- Email reports (daily/weekly summaries)
- Tax reporting automation
- Advanced charting (matplotlib/plotly)
