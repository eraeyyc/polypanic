# Status

**Last updated:** 2026-04-15

## Current live run command

Use `./run.sh` and a fresh DB:

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

## What is currently fixed

### Live trading / reconciliation

- closed orders are terminal and no longer block future entries
- transient quote failures no longer permanently disable trading
- failed submissions persist as `failed`, not ghost `pending_submit`
- manual/external sells can be reconciled instead of leaving permanent ghost inventory
- active-window Data API lag no longer clears real positions
- active-window Data API lag no longer resurrects already-sold size
- sell fills that leave only dust now clear the position and mark the order effectively filled
- entry rejection reasons are logged
- opposite-side flips are blocked for the configured sell cooldown window

### Shared paper/live behavior

- ROI display is now mark-to-market instead of cost-basis-flat after entry
- paper trader now mirrors live opposite-side sell cooldown behavior
- paper trader now mirrors live minimum effective trade-size behavior
- `observer.py --analyze` now prints local-time session and hour-of-day trade timing summaries

### Order-path latency

- allowance/balance preflight is now briefly cached in live trading to reduce redundant REST calls during repeated attempts

## Current evidence

### Paper trading

`polymarket_observer_100.db` suggests regular business hours are materially better than overnight:
- overnight bucket: negative overall
- business-hours bucket: strongly positive overall
- force exits remain the main drag across all buckets

### Live trading

Live trading works, but the dataset is still small. Several older DBs were contaminated by bugs that have since been fixed, so do not tune the strategy based on those alone.

## What still needs validation

- more clean live sessions with no manual website intervention
- more daytime live sessions to compare against the earlier overnight tests
- more resolution / settlement cases in real live trading
- more unusual user WebSocket payloads and partial-fill edge cases

## Practical guidance

- Use a fresh DB per live session.
- Do not manually trade the same market while the bot is running.
- Keep config stable across a batch of test sessions so results are comparable.
- If the bot does something strange, inspect the session DB first before changing thresholds.
