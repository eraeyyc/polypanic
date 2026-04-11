# Status

**Last updated:** 2026-04-11

## Current paper-trading setup

The current paper baseline is the filtered single-side strategy that performed better on the fresh DB:

```bash
caffeinate -i ./run.sh observer.py \
  --entry 0.38 \
  --exit 0.70 \
  --entry-delay 60 \
  --max-entry-age 150 \
  --min-entry 0.15 \
  --db polymarket_observer_v2.db
```

Expected startup banner for the patched observer:

```text
Entry:    ≤ $0.38
Exit:     ≥ $0.70
Sides:    one
Window:   +60s to +150s
BTC dir:  aligned only
```

## What changed in the 2026-04-11 session

### Live trader hardening

`trader.py` was substantially refactored away from optimistic paper-style execution.

- `LiveTrader` no longer inherits `PaperTrader` as the source of truth for live fills.
- Live state is now split into:
  - desired positions
  - open orders
  - actual confirmed positions
- Order/trade/position/settlement state is persisted in SQLite.
- Live defaults now match the paper-tested strategy:
  - `entry=0.38`
  - `exit=0.70`
  - `entry_delay=60`
  - `max_entry_age=150`
  - single-side by default
  - BTC alignment required by default unless `--allow-contrarian`
- Live auth now supports configurable wallet type and funder:
  - `--signature-type`
  - `--funder`
  - default signature type is now `1` (`POLY_PROXY`) to match the user's Magic Link exported-key setup
- Added paper-trader compatibility state back into `LiveTrader`:
  - `_stopped_out`
  - `_last_sell_time`
  - this fixed a runtime crash in `Observer.run()`

### Exchange-truth reconciliation

- Added live reconciliation loop for:
  - open orders
  - recent trades
  - current positions
- Added authenticated user WebSocket wiring and expanded market WebSocket event handling.
- Added market-state persistence for resolution/settlement tracking.
- End-of-window live handling now cancels orders and marks settlement pending instead of using the paper BTC proxy to “resolve” positions.

### Execution / safety changes

- Live orders now use immediate-execution semantics (`FAK`) instead of resting `GTC` for urgent entries/exits.
- Order placement now refreshes market constraints:
  - tick size
  - min order size
  - fee rate bps
- Added slippage gates before order submission.
- Added live safety controls:
  - max total exposure
  - max open orders
  - max consecutive live errors / kill switch
  - market-data health gating

### Fee-aware accounting

- Added explicit fee modeling for confirmed fills.
- Added `live_fills` and `live_positions` tables.
- Realized P&L and spendable bankroll now flow from confirmed fill/position state instead of optimistic order submission.
- Fixed a real accounting bug discovered during testing:
  - fills were being classified from market side (`up/down`) instead of execution action (`buy/sell`)
  - this would have broken fee handling and inventory updates
  - fixed in `trader.py`

### Historical backfill support

- Added `--backfill-history` support in `trader.py` using Polymarket price-history endpoints for token IDs.

## Database status

`observer.py` now manages additional live tables:

- `live_orders`
- `live_fills`
- `live_positions`
- `live_reconciliation_state`
- `live_market_state`

These are in addition to the existing:

- `markets`
- `price_ticks`
- `paper_trades`
- `live_trades`
- `strategy_config`

`live_trades` is now mostly a compatibility/session summary table. The canonical live execution records are `live_orders`, `live_fills`, and `live_positions`.

## Verification completed

These local checks passed on 2026-04-11:

```bash
python3 -m py_compile observer.py trader.py test_live_trader.py
python3 -m unittest test_live_trader.py
```

Current unit coverage includes:

- tick-size rounding helpers
- live order DB round-trip
- live position DB round-trip
- fee calculation
- fee-aware fill application / realized P&L update

## What is still not validated

The live code is structurally much safer than before, but it is still not proven against the real exchange.

Not yet validated end-to-end:

- POLY_PROXY auth flow against the real account/funder combination
- actual live order signing for the user's POLY_PROXY account
- authenticated user WebSocket payload shape
- market WebSocket payload shape beyond the documented fields
- `get_trades()` response shape in practice
- Data API positions response shape in practice
- `cancel_market_orders()` parameter expectations
- actual settlement / redemption reconciliation after market resolution
- exact Polymarket fee behavior as observed on real fills vs the current local model

This means the remaining risk is runtime integration mismatch, not obvious local logic bugs.

Most recent live finding:

- the bot now reaches authenticated order submission
- a real buy attempt was made
- Polymarket returned `400 invalid signature` on `POST /order`
- this strongly suggests the remaining blocker is wallet/signer/funder configuration for the proxy account, not market-data or L2 credential setup

## Recommended next step

Do not go straight to meaningful live size.

Run a dry/smoke validation against the real account with tiny notional and verify:

- order submission returns expected statuses
- user WS trade/order events arrive and parse correctly
- open orders in the exchange match `live_orders`
- actual wallet positions match `live_positions`
- fees and realized P&L in the DB match observed fills
- end-of-window cancellation and settlement-pending handling behave correctly

## Current working tree

Uncommitted local changes exist in:

- `observer.py`
- `trader.py`
- `test_live_trader.py`
