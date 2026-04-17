# Polymarket BTC 5-Min Research Repo

This repo now has two distinct paths:

- `observer.py` / `trader.py`: the legacy one-sided strategy and live execution stack
- `wallet_analyzer.py` / `paired_research.py`: the current paired hold-to-resolution research path

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

### `trader.py`

Legacy live trading layer built on top of the observer foundation.

Current live implementation includes:

- market WebSocket handling
- authenticated user WebSocket handling
- live order reconciliation
- live fill persistence
- live position tracking
- fee-aware accounting
- market-constraint checks
- settlement P&L at $1/$0 per share on market resolution
- restart state recovery (mid-window and cross-boundary)
- live safety gates
- historical token price backfill

Notes:

- live trading is not based on optimistic paper fills
- settlement uses the actual `winning_asset_id` from the WebSocket, not the paper BTC proxy logic
- smoke test confirmed: fills, fee tracking, and realized P&L working correctly

### `wallet_analyzer.py`

Canonical wallet-research tool for public BTC 5-minute accounts.

It reconstructs per-window spend, shares, payout profiles, and rough hold-to-resolution P&L from Polymarket public APIs and accounting snapshots.

### `paired_research.py`

Dedicated paired hold-to-resolution research collector and simulator.

This is the new main path for strategy work.

### `test_live_trader.py` / `test_research.py`

Local regression tests for legacy live-trader logic and the new research pipeline.

## Database

The SQLite DB now contains both paper and live execution state.

Core tables:

- `markets`
- `price_ticks`
- `paper_trades`
- `live_trades`
- `strategy_config`

New live-trader tables:

- `live_orders`
- `live_fills`
- `live_positions`
- `live_reconciliation_state`
- `live_market_state`

For live accounting, treat `live_orders`, `live_fills`, and `live_positions` as canonical. `live_trades` is mostly a compatibility/session summary table now.

## Installation

```bash
pip install requests py-clob-client websockets flask
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
./run.sh trader.py --setup-keys
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

You also need approval on Polygon:

```python
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id="..."))
```

## Legacy live test commands

These remain available for reference and infrastructure validation only.

Example tiny smoke test:

```bash
export POLYMARKET_PRIVATE_KEY=0x...
export POLYMARKET_SIGNATURE_TYPE=1
export POLYMARKET_FUNDER=0x...
./run.sh trader.py \
  --max-position 1 \
  --min-position 1 \
  --max-total-exposure 2 \
  --max-open-orders 1 \
  --log-level INFO
```

Useful live options:

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
- `--db`

## Historical backfill

You can fetch token price history directly:

```bash
./run.sh trader.py --backfill-history TOKEN_ID --history-interval 1d --history-fidelity 60
```

Or with absolute timestamps:

```bash
./run.sh trader.py --backfill-history TOKEN_ID --history-start-ts 1775800000 --history-end-ts 1775880000
```

## What still needs runtime validation

The code is structurally complete. These pieces need real-exchange confirmation:

- **User WebSocket payloads at higher fill volume** — basic trade/fill events confirmed in smoke test. Partial fills, maker events, unusual order states not yet seen live. Run with `--log-level DEBUG` and watch for `Unknown user event type=` log lines.
- **Settlement P&L path** — `_settle_market()` is implemented but has never fired on a real resolved position. The first time a position holds to window close, verify `live_positions` is empty afterward and `realized_pnl` is correct.
- **Fee model accuracy** — compare `live_fills.fee_amount` against actual exchange-reported fees at meaningful notional.

Items from the previous list that are now resolved:

- ~~settlement/redeem reconciliation after resolution~~ — implemented in `_settle_market()`
- ~~`cancel_market_orders()` behavior~~ — guarded against empty-cancel errors and kill-switch bleed
- ~~state reload on restart~~ — mid-window and cross-boundary cases both handled
- ~~market WebSocket payload shape~~ — unknown event types logged at DEBUG
