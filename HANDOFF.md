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

3. **Stale orders blocking all entries after restart** — On restart, `_load_state_from_db` loaded orders with status `pending_submit` / `cancel_pending` from closed windows into `open_orders`. These counted against `max_open_live_orders=4`, so 5 stale orders from the first session silently blocked every `evaluate_entry` call in all 6 subsequent markets. Fixed: `_load_state_from_db` now detects non-terminal orders for windows that closed >60s ago and marks them `cancelled` (both in memory and DB) before they enter `open_orders`. Added `_slug_window_end_ts()` helper to extract the window timestamp from the slug.

4. **`reserved_notional()` always returned 0** — Checked `order.side == "buy"` but `order.side` stores `"up"` / `"down"`. Changed to `order.intent == "buy"`. This was a silent bug that could have allowed over-exposure if many buy orders were open simultaneously; in practice it was masked by the open-orders count gate.

## What still needs watching

- **State reload mid-window** — stale order expiry uses a 60s grace period after window close, so if the bot is stopped and restarted within the same 5-minute window, reload behaves correctly (orders aren't yet expired). Watch behavior across a restart mid-position.
- **Stale positions from closed windows** — `live_positions` with `settlement_status='open'` from resolved windows are loaded and eat into `bankroll` via `position_cost_basis()`. They don't block entries (slug-scoped checks), but they do reduce spendable capital until settlement reconciliation clears them. Priority: fix settlement reconciliation.
- **Settlement reconciliation** — positions held through window close should get marked `settlement_pending`. Verify those reconcile correctly once the market resolves.
- **User WebSocket payload shapes** — only lightly exercised. If event parsing breaks, the reconcile loop will catch it via REST fallback, but log noise will increase.

## Things not to regress

- Do not revert to optimistic fills.
- Do not use `live_trades` as canonical accounting — use `live_orders`, `live_fills`, `live_positions`.
- Do not switch back to both-sides or the old `0.40 / 0.65` config.
- Do not remove the `_on_before_close()` shutdown ordering — it prevents the DB-closed crash.
- Do not remove `order_id = client_order_id` seeding — it prevents the UNIQUE constraint cascade.
- Do not remove `_slug_window_end_ts` stale-order expiry — it prevents phantom orders from blocking future entries.

## Files

- `observer.py` — base observer, DB schema, paper trader
- `trader.py` — live trader, WebSocket, auth
- `test_live_trader.py` — unit tests (tick rounding, DB round-trips, fee math)
- `STATUS.md` — current status and run commands
