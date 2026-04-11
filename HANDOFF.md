# HANDOFF.md — Polymarket BTC 5-Min Bot

Context for the next session. This file is intended to let another model pick up the work quickly without reconstructing the recent decisions from chat history.

## Repo / working directory

Use this repo only:

- `/Users/MAC/projects/polypanic`

Do not work in the desktop thread cwd if it points somewhere else.

## Current state

There are uncommitted local changes in:

- `/Users/MAC/projects/polypanic/observer.py`
- `/Users/MAC/projects/polypanic/trader.py`
- `/Users/MAC/projects/polypanic/test_live_trader.py`
- `/Users/MAC/projects/polypanic/STATUS.md`
- `/Users/MAC/projects/polypanic/HANDOFF.md`

As of 2026-04-11, the live-trader hardening refactor is implemented locally but not runtime-validated against real Polymarket credentials.

Additional auth context:

- the user believes their account is `POLY_PROXY`
- they have an exported private key from Magic Link
- `trader.py` was patched to support:
  - `--signature-type`
  - `--funder`
- default `signature_type` is now `1`
- next live setup should use the exported private key plus the Polymarket wallet/proxy address as funder
- auth setup via `--setup-keys` succeeded
- authenticated reads work
- actual order submission currently fails with `400 invalid signature`
- this means the remaining blocker is likely proxy wallet signing context, not L2 credential generation

## Strategy context

Paper testing moved away from the older both-sides `0.40 / 0.65` setup.

Current paper-tested baseline:

- entry `<= 0.38`
- exit `>= 0.70`
- `entry_delay=60`
- `max_entry_age=150`
- `min_entry=0.15`
- single-side by default
- BTC-aligned entries by default

Command used for fresh paper DB testing:

```bash
./run.sh observer.py --entry 0.38 --exit 0.70 --entry-delay 60 --max-entry-age 150 --db polymarket_observer_v2.db
```

The fresh filtered DB looked materially better than the old cumulative DB. The earlier “it got worse” confusion was caused by analyzing an old cumulative DB while still running the pre-patch observer config.

## What changed in code

### observer.py

Live-trading support and persistence were extended.

Key additions:

- `StrategyConfig` now includes live execution/safety fields:
  - `max_total_live_exposure`
  - `max_open_live_orders`
  - `max_consecutive_live_errors`
  - `reconcile_interval_secs`
  - `positions_poll_interval_secs`
  - `max_live_entry_slippage`
  - `max_live_exit_slippage`
- `Database` schema now includes:
  - `live_orders`
  - `live_fills`
  - `live_positions`
  - `live_reconciliation_state`
  - `live_market_state`
- New DB helpers were added for upserting/fetching those entities.

Important note:

- `live_trades` is no longer the canonical live record. Treat `live_orders`, `live_fills`, and `live_positions` as the real live execution tables.

### trader.py

This file changed the most.

Major refactor:

- `LiveTrader` no longer uses `PaperTrader` as the live source of truth.
- It now tracks:
  - desired positions
  - open orders
  - actual confirmed positions
- It persists and reloads state from the DB on startup.
- It runs a reconciliation loop for:
  - open orders
  - recent trades
  - current positions

Added support code:

- `DataAPIClient` for positions and historical price backfill
- market WebSocket event callback support
- authenticated user WebSocket
- market constraints:
  - tick size
  - min order size
  - fee rate bps
- allowance preflight for both:
  - collateral on buys
  - conditional tokens on sells

Execution changes:

- entries/exits use immediate-execution flow (`FAK`) rather than old resting-order assumptions
- slippage is checked with estimated market price before submission
- end-of-window live logic cancels orders and marks settlement pending instead of using the paper BTC resolution proxy
- live CLI defaults were aligned to the paper-tested strategy
- auth setup/build paths now support `signature_type` and `funder`
- `--backfill-history` was added for token price history fetches
- `LiveTrader` now also carries `_stopped_out` and `_last_sell_time` for compatibility with `Observer.run()` and paper-style cooldown logic

### test_live_trader.py

Added local tests covering:

- tick-size rounding
- live order DB round-trip
- live position DB round-trip
- fee calculation
- fee-aware fill application / realized P&L updates

## Important bug fixed during the refactor

A real accounting bug was found while adding tests:

- fill accounting had been classifying fills from market side (`up/down`) instead of execution action (`buy/sell`)
- that would have broken fee handling and position updates
- this was fixed in `trader.py`

If another model reviews the live accounting path, start there and preserve that fix.

## Polymarket docs context already researched

Docs reviewed in prior sessions included:

- fees
- builders overview
- orderbook / prices / price history
- order lifecycle
- order creation
- single-order lookup
- trades
- market WebSocket
- user WebSocket
- resolution
- matching engine restarts
- rate limits
- error codes

High-signal conclusions already established:

- the old live trader was not safe because it treated posted orders as filled trades
- Polymarket tick size / min order size / fee-rate fields matter
- urgent exits should use immediate execution, not naive resting `GTC`
- market and user WebSocket payloads need real runtime validation
- settlement must not use the paper BTC spot proxy in live mode
- historical price data is available and can be fetched by token ID

## What has been verified locally

These commands passed:

```bash
python3 -m py_compile observer.py trader.py test_live_trader.py
python3 -m unittest test_live_trader.py
```

That means:

- syntax is clean
- the new helper logic is at least locally testable
- fee-aware accounting has some unit coverage

It does **not** mean the live integration is proven.

## What still needs real-world validation

This is the most important boundary for the next session.

Still unvalidated against the real exchange:

- POLY_PROXY auth against the user's real exported-key + funder combination
- actual order-signing correctness for the proxy wallet
- authenticated user WebSocket auth/subscription shape
- actual user trade/order event payload fields
- actual market WebSocket payload fields beyond the documented examples
- `get_trades()` response fields
- Data API positions response fields
- `cancel_market_orders()` behavior
- exact order-status transitions in practice
- actual settlement/redeem flow after resolution
- fee model vs real exchange-reported fills

In other words:

- code structure is much better now
- runtime integration truth is still the next hard step

## Recommended next session

The next useful session should not start with more theory. It should do a controlled integration validation.

Recommended sequence:

1. Review the current diff in `/Users/MAC/projects/polypanic`.
2. Run the local checks again.
3. If credentials are available, perform a tiny-notional live smoke test.
4. Log and inspect raw user WS + market WS payloads.
5. Compare:
   - exchange open orders
   - local `live_orders`
   - wallet positions / Data API positions
   - local `live_positions`
   - local fee/P&L records vs actual fills
6. Investigate the current `invalid signature` order-submission failure before assuming the wallet config is correct.
7. Only then patch any remaining runtime mismatches discovered.

## Things not to regress

- Do not revert to optimistic fills.
- Do not reintroduce paper `Observer._finalize_market()` style live settlement.
- Do not switch live defaults back to the old `0.40 / 0.65` both-sides config.
- Do not treat `live_trades` as canonical live accounting.
- Do not assume docs payload examples exactly match runtime without checking.

## Useful files

- `/Users/MAC/projects/polypanic/observer.py`
- `/Users/MAC/projects/polypanic/trader.py`
- `/Users/MAC/projects/polypanic/test_live_trader.py`
- `/Users/MAC/projects/polypanic/STATUS.md`
- `/Users/MAC/projects/polypanic/README.md`

## Last known verification commands

```bash
cd /Users/MAC/projects/polypanic
python3 -m py_compile observer.py trader.py test_live_trader.py
python3 -m unittest test_live_trader.py
git status --short
```
