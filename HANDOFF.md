# HANDOFF.md — Polymarket BTC 5-Min Bot

Context for the next session.

## Repo / working directory

`/Users/MAC/projects/polypanic`

## Current state

Live trading is functional, but this repo is still in active debugging and tuning, not “set and forget” mode.

What is currently true:
- live auth / signing / order submission works
- fill tracking and realized P&L accounting work
- paper and live strategy logic are mostly aligned again
- several major live-state bugs have been fixed
- the best next step is more clean live testing, especially during regular business hours

Do not treat older logs / DBs as clean strategy evidence. A number of historical runs were contaminated by state bugs that have since been fixed.

## Current recommended live run

Use `./run.sh` so the repo virtualenv is activated, and use a fresh DB per session:

```bash
cd /Users/MAC/projects/polypanic

export POLYMARKET_PRIVATE_KEY=0x...
export POLYMARKET_SIGNATURE_TYPE=1
export POLYMARKET_FUNDER=0x...

./run.sh trader.py \
  --entry 0.38 \
  --exit 0.54 \
  --entry-delay 20 \
  --max-entry-age 240 \
  --min-entry 0.18 \
  --cooldown 10 \
  --max-position 3 \
  --min-position 1 \
  --bankroll 10 \
  --max-total-exposure 10 \
  --max-open-orders 2 \
  --allow-contrarian \
  --both-sides \
  --hold-threshold 15 \
  --hold-neutral-range 5 \
  --db polymarket_live10.db \
  --log-level INFO
```

Notes:
- Use a new `--db` file for each clean test session.
- Do not manually trade the same market on the Polymarket website while the bot is running.
- If using an EOA instead of proxy/funder mode, use the correct `signature_type` and env vars.

## Current strategy picture

Paper analysis from `polymarket_observer_100.db` suggests:
- business-hour paper performance is materially better than overnight
- `exit_target` trades are strong
- `force_exit` is still the biggest drag

Do not overfit yet. The live sample is still too small and too noisy from prior debugging sessions.

## Major fixes already shipped

### Live-state / execution fixes

1. Terminal order handling now treats `closed` as terminal, so disappeared FAK orders no longer block new entries.
2. Transient market-data failures no longer permanently trip the live kill switch.
3. Failed submissions are marked `failed` instead of persisting forever as `pending_submit`.
4. Manual/external sells no longer leave permanent ghost inventory.
5. Active-window positions are no longer cleared just because the Data API temporarily misses them.
6. Entry rejection reasons are now logged, which makes “why didn’t it buy?” diagnosable.
7. Near-close neutral BTC behavior was changed so the bot can hold through resolution when BTC is still within a small neutral range instead of forcing out at a terrible last-second price.
8. Successful sell fills that leave only sub-minimum dust are now treated as flat positions, and the corresponding orders are marked effectively filled.
9. Data API position sync no longer resurrects stale position size during an active window after a real sell.
10. Sell-side ghost-fill hardening is now in place: sell fills are tentative until confirmed by on-chain ERC1155 balance decrease.
11. Buy-side accounting is still phase-2 work; buys remain off-chain-accounted for now, but divergence checks now compare local/Data API/on-chain balances.

### Paper/live strategy alignment fixes

1. ROI display is now mark-to-market in `observer.py`, so both paper and live show unrealized P&L correctly instead of looking artificially flat after entry.
2. Paper trader now applies opposite-side post-sell cooldown the same way live trader does.
3. Paper trader now rejects sub-minimum effective trades when `min(max_position_size, bankroll) < min_position_usdc`, matching live behavior.

### Latency / order-path improvement

1. Live allowance/balance preflight now uses a short-lived cache, reducing redundant REST calls during repeated entry/exit attempts.

## What still needs watching

### Strategy / market behavior

- Force-exit remains the biggest P&L drag in paper results.
- Regular business-hour liquidity likely matters a lot; most early live tests were at bad overnight hours.
- The strategy is still being tuned empirically. Do not assume the current `0.54` exit is final.
- Local realized P&L is now more trustworthy on sells than buys, because only sells are balance-confirmed in phase 1.

### Live execution / infra

- User WebSocket payload coverage is still limited; reconciliation is the safety net.
- Partial-fill and unusual order-state paths need more live exposure.
- Settlement-through-resolution needs more real-world validation.
- Exchange/API latency still dominates local logic time; if the bot feels slow, look at network-bound preflight and reconciliation work first, not `if` statements.
- Buy-side ghost-fill hardening is still not implemented; current protection is sell-side confirmation plus divergence logging.

## Things not to regress

- Do not revert the dust-fill handling. It prevents fake residual positions from blocking future trades.
- Do not revert active-window protection in Data API position sync. It prevents stale API snapshots from resurrecting sold positions.
- Do not revert failed-order persistence to `failed`.
- Do not remove entry-rejection logging; it is now the fastest way to debug non-entries.
- Do not revert the mark-to-market ROI display.
- Do not revert paper/live alignment on cooldown and min-position behavior.

## Useful files

- `observer.py` — base observer, paper trader, analysis path, shared strategy logic
- `trader.py` — live trader, exchange integration, reconciliation, heartbeats
- `test_live_trader.py` — regression tests for live-state and shared strategy logic
- `STATUS.md` — current operational summary
