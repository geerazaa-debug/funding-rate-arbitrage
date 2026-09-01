# FundingRateBot: Delta-Neutral Cross-Exchange Funding Rate Arbitrage

## Project Goal
Build a production-ready funding rate arbitrage bot operating across Bybit and Binance perpetual futures. Target: 1-month delivery, capable of sustainable alpha extraction via funding rate differentials.

## Core Strategy
- **Long on Binance, Short on Bybit** (or vice versa, depending on funding rate sign)
- **Delta-neutral**: No directional bias; profit from basis/funding differential only
- **Real-time monitoring**: WebSocket-driven funding rate detection + execution
- **Risk-first**: Circuit breakers, position limits, emergency exit before manual intervention

## Project Scope (Month 1)
### Week 1: Data Ingestion & Real-Time Monitoring
- Set up WebSocket streams (both exchanges, funding rate updates)
- REST API fallback for missing data
- Funding rate differential calculation
- Logging & alerting infrastructure

### Week 2: Execution Engine & Paper Trading
- Order placement logic (both exchanges simultaneously)
- Position tracking & reconciliation
- Paper trading mode (no real capital)
- Execution latency monitoring

### Week 3: Risk Management & Live Prep
- Circuit breakers (max loss, net exposure)
- Position sizing rules
- Emergency exit procedures
- Metrics dashboard

### Week 4: Live Trading & Refinement
- Go live with capital allocation
- Monitoring & tuning
- Rollout documentation

## Key Constraints
- **Latency**: WebSocket streams must be <100ms apart (funding rate changes are slow; no ultra-HFT required)
- **Reliability**: 99.5% uptime target (reconnection logic, fallbacks)
- **Capital efficiency**: Max leverage 2x, no naked short-squeezes
- **Safety first**: All orders go through paper mode first; manual approval before live capital

## Team Context
- Solo dev (you)
- Q available for strategy validation & backtest review
- Heimdall required gate before any live trading

## External Docs
- Bybit WebSocket API: https://bybit-exchange.github.io/docs/v5/websocket/public/funding
- Binance WebSocket API: https://binance-docs.github.io/apidocs/#websocket-market-streams
