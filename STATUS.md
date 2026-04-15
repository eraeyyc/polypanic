# Status

**Last updated:** 2026-04-15

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

## What changed in the 2026-04-15 session

### Four structural gaps closed

**Settlement reconciliation (was the biggest gap)**
- When a `market_resolved` WebSocket event arrives with a `winning_asset_id`, `_settle_market()` now computes final P&L: winning shares settle at $1.00/share, losing shares at $0.00. Positions are removed from active tracking and `realized_pnl` is persisted before deletion.
- On startup, `_load_state_from_db` checks `live_market_state` for any loaded positions from already-resolved markets and settles them immediately.
- If `winning_asset_id` is absent from the event (shouldn't happen), falls back to `pending_reconciliation` as before.

**Cross-boundary restart**
- If the process stops near a window close and restarts after the next window has opened, positions from the expired window no longer silently accumulate. `_settle_stale_positions()` runs once at startup and marks them `pending_reconciliation` so the reconcile loop catches up via trade history.
- Mid-window restart was already correct (main loop fires `_on_new_market` on first iteration).

**`cancel_market_orders()` kill-switch bleed**
- `cancel_market()` no longer calls the exchange if there are no non-terminal open orders. Some APIs return errors on empty cancels, which previously would have incremented `_consecutive_errors` toward the kill switch.
- Cancel failures are now routed to `logging.warning` instead of `_record_error` — a flaky cancel at window close can't trip the kill switch on the next window.

**WebSocket unknown event types**
- Both `handle_market_event` and `handle_user_event` log any unrecognized `event_type` at DEBUG level. Silent at INFO (default), visible with `--log-level DEBUG`. Lets you catch unexpected payload shapes in production without adding noise.

## Database status

`polymarket_smoke_test.db` — smoke test data, keep for reference.
`polymarket_live.db` — use this for real strategy runs going forward.

Tables:
- `live_orders`, `live_fills`, `live_positions`, `live_reconciliation_state`, `live_market_state`
- `markets`, `price_ticks`, `paper_trades`, `live_trades`, `strategy_config`

## What is still not validated at runtime

- **User WebSocket payload shapes at higher volume** — the smoke test confirmed basic trade/fill events. Partial fills, maker events, and unusual order states haven't been seen live. Run with `--log-level DEBUG` and watch for `Unknown user event type=` lines.
- **Fee model accuracy** — local fee computation vs actual exchange-reported fills at meaningful notional. Compare `live_fills.fee_amount` against what Polymarket shows.
- **Settlement path end-to-end** — `_settle_market()` is implemented but has never fired on a real resolved position. The first time a position holds to window close, verify `realized_pnl` in `live_positions` matches the payout.

## Recommended next step

Run the real strategy command above for a full session (several windows). Watch for:
- Clean entry → exit cycles with expected reasons (`exit_target`)
- Correct position count after each window close
- Realized P&L matching what you see on Polymarket
- No phantom open orders in the session summary
- If anything holds to resolution: check `live_positions` is empty afterward and `realized_pnl` is correct

Once that looks good, bump `--max-position` and `--bankroll` to meaningful size.
