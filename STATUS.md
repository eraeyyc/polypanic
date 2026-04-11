# Status

**Last updated:** 2026-04-11

## Current live setup

Live trading is confirmed working end-to-end. Run with real strategy parameters:

```bash
caffeinate -i python trader.py \
  --entry 0.38 \
  --exit 0.70 \
  --entry-delay 60 \
  --max-entry-age 150 \
  --min-entry 0.15 \
  --max-position 5 \
  --bankroll 20 \
  --db polymarket_live.db
```

Scale up `--max-position` and `--bankroll` once you've seen several clean cycles.

## What changed in the 2026-04-11 (evening) session

### Live trading confirmed

- Signature error (`400 invalid signature`) from earlier session is gone — auth flow is working.
- Ran a smoke test with loose settings (`--entry 0.90 --exit 0.95 --allow-contrarian`) to force a trade.
- Confirmed fills, fee tracking, and realized P&L all working correctly.
- Polymarket balance went from $17 deposited to $32.17 — smoke test positions resolved profitably.

### Two bugs fixed

**Order DB collision (`UNIQUE constraint failed: live_orders.order_id`)**
- Root cause: `LiveOrderState.order_id` defaulted to `""` and was written to DB before order submission. When a FAK order was killed by the exchange, `""` stayed in the DB. Every subsequent retry tried to insert another `""` and hit the UNIQUE constraint.
- Fix: seed `order_id = client_order_id` before the first DB write in both `execute_buy` and `execute_sell`. Failed orders now carry a unique local ID; successful orders get updated to the real exchange `orderID`.

**Reconcile thread crash on shutdown (`sqlite3: Cannot operate on a closed database`)**
- Root cause: `Observer.run()` called `db.close()`, then `LiveObserver`'s `finally` block called `stop_reconciliation()` — but the reconcile thread fired one more time on an already-closed connection.
- Fix: added `_on_before_close()` hook to `Observer.run()` (called just before `db.close()`). `LiveObserver` overrides it to stop reconcile, heartbeat, and WebSocket threads in the correct order.

## Database status

`polymarket_smoke_test.db` — smoke test data, keep for reference.
`polymarket_live.db` — use this for real strategy runs going forward.

Tables in both:
- `live_orders`, `live_fills`, `live_positions`, `live_reconciliation_state`, `live_market_state`
- `markets`, `price_ticks`, `paper_trades`, `live_trades`, `strategy_config`

## What is still not validated

- State reload on restart: `LiveTrader` persists `live_orders` / `live_positions` to DB on shutdown but reload behavior on startup hasn't been explicitly tested. If you stop mid-window, restart and watch whether the bot recognizes existing positions.
- Settlement/redemption reconciliation after market resolution.
- Authenticated user WebSocket payload shapes beyond what fired during the smoke test.
- `cancel_market_orders()` behavior at scale.
- Fee model accuracy vs actual exchange-reported fills at higher notional.

## Recommended next step

Run the real strategy command above for a full session (several windows). Watch for:
- Clean entry → exit cycles with expected reasons (`exit_target`)
- Correct position count after each window close
- Realized P&L matching what you see on Polymarket
- No phantom open orders in the session summary

Once that looks good, bump `--max-position` and `--bankroll` to meaningful size.
