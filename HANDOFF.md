# HANDOFF.md — Polymarket BTC 5-Min Bot

Context for the next session.

## Repo / working directory

`/Users/MAC/projects/polypanic`

## Current state

**Live trading is working.** End-to-end confirmed:
- Auth (POLY_PROXY, signature_type=1) ✅
- Order signing and submission ✅
- FAK fill execution ✅
- Fee tracking ✅
- Realized P&L accounting ✅
- Shutdown (clean thread teardown) ✅

Polymarket balance after smoke test: $32.17 from $17 deposited.

Working tree is clean. All changes committed.

## Live run command

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

Scale `--max-position` and `--bankroll` up after a few clean cycles.

## Strategy parameters (paper-validated baseline)

- Entry: `<= 0.38`
- Exit: `>= 0.70`
- `entry_delay=60`, `max_entry_age=150`
- `min_entry=0.15`
- Single-side, BTC-aligned by default

## Bugs fixed this session

1. **UNIQUE constraint on `order_id`** — failed FAK orders left `order_id=""` in DB; retries collided. Fixed by seeding `order_id = client_order_id` before first DB write in `execute_buy` / `execute_sell`.

2. **Reconcile thread using closed DB on shutdown** — `Observer.run()` closed the DB before `LiveObserver` stopped the reconcile thread. Fixed with `_on_before_close()` hook in `Observer`.

## What still needs watching

- **State reload on restart** — `LiveTrader` persists positions/orders to DB but startup reload hasn't been explicitly tested mid-window. Watch behavior if the bot is stopped and restarted while holding a position.
- **Phantom open orders** — session summary showed 5 open orders at shutdown (stale records from pre-fix failed attempts). These should clear up now that the UNIQUE constraint bug is fixed. Confirm open orders = 0 at clean shutdown.
- **Settlement reconciliation** — positions held through window close should get marked `settlement_pending`. Verify those reconcile correctly once the market resolves.
- **User WebSocket payload shapes** — only lightly exercised. If event parsing breaks, the reconcile loop will catch it via REST fallback, but log noise will increase.

## Things not to regress

- Do not revert to optimistic fills.
- Do not use `live_trades` as canonical accounting — use `live_orders`, `live_fills`, `live_positions`.
- Do not switch back to both-sides or the old `0.40 / 0.65` config.
- Do not remove the `_on_before_close()` shutdown ordering — it prevents the DB-closed crash.
- Do not remove `order_id = client_order_id` seeding — it prevents the UNIQUE constraint cascade.

## Files

- `observer.py` — base observer, DB schema, paper trader
- `trader.py` — live trader, WebSocket, auth
- `test_live_trader.py` — unit tests (tick rounding, DB round-trips, fee math)
- `STATUS.md` — current status and run commands
