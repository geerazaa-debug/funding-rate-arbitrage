# Week 1: Data Ingestion & Real-Time Monitoring

## Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the Bot
```bash
python -m src.main
```

Expected output (every 30 seconds):
```
============================================================================================
[2026-09-01 12:30:45] FUNDING RATE METRICS
============================================================================================
WebSocket Status: Bybit=True, Binance=True

BTCUSDT:
  Bybit:   0.000150 (age:   2.3s)
  Binance: 0.000250 (age:   1.8s)
  Δ (B-Y): 0.000100

  Bybit   (8h): avg=0.000145, min=0.000120, max=0.000180
  Binance (8h): avg=0.000235, min=0.000200, max=0.000270

ETHUSDT:
  Bybit:   0.000080 (age:   2.5s)
  Binance: 0.000120 (age:   2.0s)
  Δ (B-Y): 0.000040

  Bybit   (8h): avg=0.000075, min=0.000050, max=0.000100
  Binance (8h): avg=0.000115, min=0.000090, max=0.000150

============================================================================================
```

### 3. Stop the Bot
Press `Ctrl+C` to gracefully shutdown.

---

## What Week 1 Does

### Data Collection
- **Real-time WebSocket streams** from Bybit and Binance
- **Funding rate updates** every 8 hours (official)
- **Mark price updates** more frequently for testing
- **Automatic reconnection** if connection drops

### Storage & Tracking
- **In-memory cache** of last 480 funding rates per pair (~8 hours)
- **Differential calculation**: `Binance_rate - Bybit_rate`
- **Rate age tracking**: How long since last update
- **Historical statistics**: min/max/avg over collected period

### Monitoring
- **Metrics report**: Printed every 30 seconds (configurable)
- **Alerts**:
  - Large differential (> 0.1%)
  - Stale data (> 30s without update)
  - Connection status changes
- **Logging**: Saved to `logs/` folder with timestamps

---

## Configuration

Edit `config/config.json` to customize:

```json
{
  "pairs": ["BTCUSDT", "ETHUSDT"],  // Trading pairs to monitor
  "monitoring": {
    "metrics_report_interval_s": 30,  // How often to print metrics
    "alert_stale_data_threshold_s": 30,  // Alert if data > 30s old
    "alert_large_differential_threshold": 0.001  // Alert if |diff| > 0.1%
  },
  "backfill": {
    "enabled": true,  // Fetch historical rates on startup
    "limit": 100,  // How many historical rates to fetch
    "delay_s": 5  // Delay between exchanges (rate limit avoidance)
  }
}
```

---

## File Structure

```
src/
├── data/
│   ├── websocket_manager.py       # WebSocket connections
│   ├── rest_api_client.py         # REST API for backfill
│   └── funding_rate_tracker.py    # Central rate tracking hub
├── utils/
│   └── logger.py                   # Logging setup
└── main.py                         # Entry point

tests/
└── test_data_ingestion.py         # Unit tests

config/
└── config.json                     # Configuration

logs/
└── *.log                          # Runtime logs
```

---

## Testing

Run unit tests:
```bash
pytest tests/test_data_ingestion.py -v
```

Run specific test:
```bash
pytest tests/test_data_ingestion.py::TestFundingRateTracker::test_differential_calculation -v
```

---

## Troubleshooting

### WebSocket Connection Failed
- **Issue**: `[Bybit] Connection failed: ...`
- **Solution**: Check internet connection, Bybit API endpoint, or firewall
- **Bot will**: Auto-reconnect with exponential backoff (up to 10 attempts)

### High Latency Between Exchanges
- **Issue**: `Δ (B-Y): ---` or large age difference
- **Solution**: This is normal. Network latency for public data is typically <1s
- **Check**: Monitor logs for `received_at` differences

### No Funding Rate Updates
- **Issue**: Metrics show `---` for funding rates
- **Solution**: Bybit/Binance may be between funding rate updates (8h cycle)
  - Or: Your symbols don't exist (check `config.json`)
  - Or: WebSocket messages not matching expected format (check logs)

### Memory Usage Growing
- **Issue**: Memory keeps increasing
- **Solution**: This is expected as we collect historical data
  - Each pair keeps 480 rates (~8 hours)
  - ~100 bytes per rate = ~100KB per pair
  - Should stabilize after 8 hours of runtime

---

## Success Criteria (Week 1)

After running for 4+ hours, you should see:

✅ **WebSocket Status**: Both `True`
✅ **Rate Updates**: New rates every ~1min (more frequent during test periods)
✅ **Differentials**: Non-zero values (arbitrage opportunities detectable)
✅ **No Crashes**: Bot runs for hours without errors
✅ **Logs**: Clean logs with zero parsing errors
✅ **Stats**: Stats update as more data collected (min/max/avg change)

---

## Next Steps (Week 2 Preview)

- Order execution engine (place orders on both exchanges)
- Paper trading simulation
- Position tracking & reconciliation
- Emergency exit procedures

---

## Questions?

Check logs: `logs/main_*.log`
Check code: `src/data/funding_rate_tracker.py` for data structures
