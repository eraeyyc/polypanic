# Polymarket BTC 5-Min Research Repo

This repo now has two distinct paths:

- `wallet_analyzer.py` / `paired_research.py`: the current main research path
- `observer.py` / `trader.py`: the legacy paper/live execution stack, now maintained as reference infrastructure and a V2-capable live path

The main direction is no longer tuning the old one-sided exit-target strategy. The repo is now oriented around:

1. reconstructing successful public BTC 5-minute wallets,
2. collecting a clean paired-strategy research dataset,
3. simulating paired buy-both-side policies before any new live trading.

## Current primary workflow

### 1. Analyze a benchmark wallet

```bash
python3 wallet_analyzer.py 0xe0229e10a858860218b6132f4234602c47bd6603 --reconstruct 50 --summary-by winner
```

Useful options:

- `--reconstruct N`
- `--from-slugs file.txt`
- `--summary-by skew|timing|combined_cost|winner`
- `--export-csv out.csv`

### 2. Collect a fresh paired-strategy dataset

```bash
./run.sh paired_research.py \
  --collect \
  --db paired_research.db \
  --duration-hours 24 \
  --poll-interval 3
```

This creates a dedicated research DB with:

- BTC 5-minute market metadata
- timestamped best bid/ask for both sides
- BTC spot and BTC delta from window open
- final market resolution

### 3. Simulate paired policies

Run one policy:

```bash
./run.sh paired_research.py \
  --simulate-paired \
  --db paired_research.db \
  --policy payout_balanced \
  --policy-config '{"combined_threshold":1.01,"notional_step":20,"fee_bps":7.2,"slippage_bps":10}'
```

Compare all built-in policies:

```bash
./run.sh paired_research.py \
  --simulate-paired \
  --db paired_research.db \
  --policy all \
  --policy-config '{"fee_bps":7.2,"slippage_bps":10}'
```

## Repository structure

### `observer.py`

Legacy paper trading and market data collection.

- market discovery
- BTC spot tracking
- SQLite persistence
- paper execution
- post-hoc analysis

`observer.py` was also updated so the shared read-only market client can target a custom CLOB host, which the live V2 path uses for explicit pre-cutover testing.

### `trader.py`

Legacy live trading layer built on top of the observer foundation.

It has now been migrated to **Polymarket CLOB V2**.

Current live implementation includes:

- market WebSocket handling
- authenticated user WebSocket handling
- live order reconciliation
- live fill persistence
- live position tracking
- V2 market-constraint loading via `get_clob_market_info()`
- startup pUSD readiness checks
- settlement P&L at $1/$0 per share on market resolution
- restart state recovery and stale-order cleanup
- live safety gates
- historical token price backfill

Important live notes:

- the live stack is still considered legacy relative to the repo’s main research direction
- V2 migration is code-complete, but still needs real-host smoke testing and wallet validation
- local V1-style manual fee math is no longer the source of truth; the SDK and exchange payloads now own that path

### `wallet_analyzer.py`

Canonical wallet-research tool for public BTC 5-minute accounts.

It reconstructs per-window spend, shares, payout profiles, and rough hold-to-resolution P&L from Polymarket public APIs and accounting snapshots.

### `paired_research.py`

Dedicated paired hold-to-resolution research collector and simulator.

This is the new main path for strategy work.

### `test_live_trader.py` / `test_research.py`

Local regression tests for legacy live-trader logic and the research pipeline.

## Database

The SQLite DB now contains both paper and live execution state.

Core tables:

- `markets`
- `price_ticks`
- `paper_trades`
- `live_trades`
- `strategy_config`

Additional live-trader tables:

- `live_orders`
- `live_fills`
- `live_positions`
- `live_reconciliation_state`
- `live_market_state`

For live accounting, treat `live_orders`, `live_fills`, and `live_positions` as canonical. `live_trades` is mostly a compatibility/session summary table now.

## Installation

```bash
pip install requests py-clob-client-v2==1.0.0 websockets flask
```

If you use `./run.sh`, it will activate the repo `.venv` automatically.

## Safe local checks

Before trusting any code changes:

```bash
cd /Users/MAC/projects/polypanic
python3 -m py_compile observer.py trader.py wallet_analyzer.py paired_research.py test_live_trader.py test_research.py
python3 -m unittest test_live_trader.py test_research.py
```

## Legacy strategy status

The old one-sided paper/live strategy is retained for reference, debugging, and historical comparison only.

Do not treat it as the primary system to improve. The current evidence bar before any new live testing is:

- wallet reconstruction
- fresh paired research dataset
- paired-policy simulation results that remain positive after fee/slippage assumptions

## Live setup

One-time credential setup:

```bash
export POLYMARKET_PRIVATE_KEY=0x...
export POLYMARKET_SIGNATURE_TYPE=1
export POLYMARKET_FUNDER=0x...
python trader.py --setup-keys --clob-host https://clob-v2.polymarket.com
```

That derives and stores Polymarket API credentials in `~/.polypanic/keys.json`.

Wallet/auth notes:

- `POLYMARKET_PRIVATE_KEY` is the wallet private key used for signing
- `POLYMARKET_SIGNATURE_TYPE` is the wallet type:
  - `0` = EOA
  - `1` = POLY_PROXY
  - `2` = GNOSIS_SAFE
- `POLYMARKET_FUNDER` is required for signature types `1` and `2`

If your Polymarket account came from Magic Link and you exported the key, the expected setup is usually:

- `POLYMARKET_SIGNATURE_TYPE=1`
- `POLYMARKET_FUNDER=<your Polymarket wallet address shown on the site>`

### V2 collateral notes

For API-only trading on V2:

- collateral is `pUSD`
- API traders must wrap `USDC.e -> pUSD` via the Collateral Onramp before trading
- the live startup path now checks for usable collateral and fails fast if it is missing

Useful docs:

- V2 migration: [docs.polymarket.com/v2-migration](https://docs.polymarket.com/v2-migration)
- pUSD: [docs.polymarket.com/concepts/pusd](https://docs.polymarket.com/concepts/pusd)
- market makers getting started: [docs.polymarket.com/market-makers/getting-started](https://docs.polymarket.com/market-makers/getting-started)

## Legacy live test commands

These remain available for reference and infrastructure validation only.

Example tiny V2 smoke-style run:

```bash
export POLYMARKET_PRIVATE_KEY=0x...
export POLYMARKET_SIGNATURE_TYPE=1
export POLYMARKET_FUNDER=0x...
./run.sh trader.py \
  --clob-host https://clob-v2.polymarket.com \
  --max-position 1 \
  --min-position 1 \
  --max-total-exposure 2 \
  --max-open-orders 1 \
  --log-level INFO
```

Useful live options:

- `--clob-host`
- `--entry`
- `--exit`
- `--entry-delay`
- `--max-entry-age`
- `--allow-contrarian`
- `--both-sides`
- `--max-position`
- `--min-position`
- `--max-total-exposure`
- `--max-open-orders`
- `--max-live-errors`
- `--reconcile-interval`
- `--positions-poll-interval`
- `--max-entry-slippage`
- `--max-exit-slippage`

## What is still unverified

The V2 migration is implemented, and an authenticated smoke test was run against
`https://clob-v2.polymarket.com` on a fee-enabled test market
(`0xaf5e903876ad42de97e1cf02c2ef8484df69bcfc5541b96a400116557d1e504e`).
That confirmed:

- V2 market-info fetches work
- V2 orderbook fetches work
- V2 market-order signing works with the expected `timestamp` / `metadata` / `builder` fields
- posting to the live V2 `/order` endpoint works

What is still blocked / unverified:

- actual filled-order lifecycle on V2, because the tested wallet currently has `0` pUSD and `0` exchange allowance
- exact user WebSocket fee payload coverage on real fills
- real wallet pUSD readiness / allowance behavior after funding and approval
- heartbeat behavior on the V2 host
