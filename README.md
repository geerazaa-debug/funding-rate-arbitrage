# Cross-exchange funding-rate arbitrage (Bybit × Binance): built, audited, measured, NO-GO

A delta-neutral bot that goes long perpetual futures on one venue and short on the other
to collect the difference in funding rates. The execution, risk and reconciliation
stack is built and tested: 177 tests, and exchange signing was verified against live
endpoints. Then the opportunity was measured, and **the edge does not survive
execution costs.** No capital was deployed.

The git history is kept as it happened. Weeks 1–4 are a fast scaffold. A hard audit
then found 22 defects, several of which would have destroyed capital. Each fix landed
with a regression test (see "Audit" below).

## Result

**Data:** a cross-sectional collector polled the full funding universe of both venues
(635 overlapping symbols; tracked the top 100) every 30 s. At the time of the verdict it
had about **437k observations over about 37 h**. The rates were normalised to an 8 h basis
before comparing (see trap 3).

**Step 1: the spread looked tradable.** BTC/ETH differentials sit around 0.000014 per 8 h,
about 35× below the fee threshold, so the majors are efficiently arbitraged. In the
illiquid tail, 10–12 symbols were above the threshold at any instant. The spread
analysis said 16 of 100 symbols "clear fees".

**Step 2: depth said otherwise.** [`scripts/depth_probe.py`](scripts/depth_probe.py) walks
both live order books for the top dislocated symbols. It measures round-trip slippage at a
given notional and nets out taker fees on both legs. Snapshot of 2026-09-04:

| Symbol | Gross funding / 24h | Net @ $1k | Net @ $5k | Net @ $10k |
|---|---:|---:|---:|---:|
| TUSDT | 0.571% | +0.235% | +0.054% | −0.049% |
| 6 other top candidates | | negative | negative | negative |

The best case is about **$2.70/day on $5k**. That figure comes before other takers
compete for the spread, before the rate moves mid-hold, and before the operational risk of
running live on two venues. **Verdict: NO-GO.**

## Measurement traps found along the way

1. **`|spread|` is not harvestable spread.** Averaging the absolute differential assumes
   you are always on the right side. The signed average was 2–5× smaller. TUSDT had an
   absolute mean of 0.005838 but a signed mean of −0.001902, with 103 sign flips, so only
   about 33% was capturable. Rank on the signed average
   ([`src/analysis/funding_edge.py`](src/analysis/funding_edge.py)).
2. **Gross spread is not edge.** "16/100 clear fees" became "1/7 clears fees and
   slippage, at trivial size". Slippage on thin alt perps dominates everything.
3. **Funding intervals are not uniformly 8 h.** Most pairs settle every 4 h: Bybit
   410 × 4 h / 403 × 8 h, Binance 443 / 331. The interval can differ between venues for
   the same symbol. Comparing raw rates invents arbitrage: 0.005%/4 h equals 0.01%/8 h.
   [`scripts/collect.py`](scripts/collect.py) normalises everything to per-8 h.
4. **Connection status is not data flow.** An early run reported both websockets
   connected for hours while receiving zero funding updates. Bybit has no `funding.*`
   topic (funding arrives on `tickers.*` as snapshot + deltas), and Binance's
   `markPrice` stream delivered nothing from this network. The collector polls REST
   instead. Both venues return their whole universe in one request, so tracking 100
   symbols costs the same as tracking 2.

## Engineering

- **Execution** ([`src/execution/`](src/execution/)): dual-leg order executor, position
  manager, paper trader, funding accumulator, slippage tracker. HMAC signing for both
  venues. Wire-format tests run a local HTTP server that captures the real request
  (signer, verb, query, body, headers), and `preflight.py` confirms the exchanges accept
  the signature.
- **Reconciliation:** bidirectional reconciliation between local state and both
  exchanges, plus a single `get_unhedged_exposure()` view. Binance signs position
  direction (`positionAmt`), while Bybit uses unsigned `size` + `side`. Comparing them
  naively made every hedged short look like a 200% mismatch.
- **Risk** ([`src/risk/`](src/risk/)): circuit breaker, position sizer, risk monitor,
  and emergency exit that flattens both legs.
- **Ops:** SQLite persistence, webhook alerts (Telegram/Discord/Slack), live dashboard,
  [`scripts/preflight.py`](scripts/preflight.py) to check API keys and signing against
  live endpoints before anything else runs. A regression guard covers a Windows DNS
  failure: `aiodns` silently broke every aiohttp request on the dev machine.

## Audit (2026-09-02)

A pre-testnet review of the scaffold found 22 defects. The worst ones:

| Defect | Consequence | Fix |
|---|---|---|
| Emergency exit compared an `OrderSide` enum to a string | **Every exit was sent as BUY**, doubling long legs instead of closing | `test_emergency_exit.py` |
| `all([])` treated as success | Reported success when every leg had failed | `test_emergency_exit.py` |
| Partial dual-order failure called `cancel_order` on an already-filled market order | **Untracked naked exposure** | flatten-on-partial-fill, `test_dual_order_flatten.py` |
| Binance HMAC computed, then discarded | Every signed request would fail | `test_exchange_signing.py` |
| Reconciliation one-directional | Exchange-side positions unknown to the bot were never seen | `test_reconciler_bidirectional.py` |

There was one process lesson too. During the scaffold weeks, "120+ tests passing" was
reported without the suite ever being run. The real state was 32 passed / 41 failed /
4 errors: `pytest-asyncio` was listed in requirements but never installed, so every
async test failed silently. Requirements are now pinned to the versions actually
tested.

## Layout

```
src/data/        websocket + REST clients, funding-rate tracker
src/analysis/    fee-netted, signed funding-edge ranking
src/execution/   orders, positions, paper trading, reconciliation, emergency exit
src/risk/        circuit breaker, sizing, risk monitor
src/storage/     SQLite persistence
src/alerts/      webhook alerter
scripts/         preflight, collector, observation analysis, depth probe
docs/            original week-by-week usage notes (scaffold era, kept for history)
```

## Running

```bash
pip install -r requirements.txt
pytest                                   # 177 tests, no network or keys needed
python scripts/collect.py --top 100 --interval 30 --hours 1   # public endpoints only
python scripts/analyze_observations.py
python scripts/depth_probe.py --symbols TUSDT --size 5000
```

API keys (only for `preflight.py` and the order paths) are read from `.env`, which is
gitignored. No script here places live orders. `src/main.py` only monitors funding rates.
