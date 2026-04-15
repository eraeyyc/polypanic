# Polymarket BTC 5-Min Observer and Live Trader

This repo tracks Polymarket BTC 5-minute up/down markets, records the full price path, runs a paper strategy, and contains a live-trading layer that is now structurally safer but still needs runtime validation against the real exchange.

## Current paper baseline

The paper-tested baseline is the filtered strategy:

- entry `<= 0.38`
- exit `>= 0.70`
- `entry_delay=60`
- `max_entry_age=150`
- `min_entry=0.15`
- single-side by default
- BTC-aligned entries by default

Run it with a fresh DB:

```bash
./run.sh observer.py \
  --entry 0.38 \
  --exit 0.70 \
  --entry-delay 60 \
  --max-entry-age 150 \
  --min-entry 0.15 \
  --db polymarket_observer_v2.db
```

Analyze that DB separately:

```bash
./run.sh observer.py --analyze --db polymarket_observer_v2.db
```

## Repository structure

### `observer.py`

Paper trading and data collection.

- market discovery
- BTC spot tracking
- SQLite persistence
- paper execution
- post-hoc analysis

### `trader.py`

Live trading layer built on top of the observer foundation.

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

### `test_live_trader.py`

Local unit tests for the new live-trader support code.

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

Before testing anything live:

```bash
cd /Users/MAC/projects/polypanic
python3 -m py_compile observer.py trader.py test_live_trader.py
python3 -m unittest test_live_trader.py
```

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

## Live test commands

Do not start with normal size. Start with the smallest sensible notional.

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
