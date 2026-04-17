# Status

**Last updated:** 2026-04-16

## Primary strategy status

The old one-sided entry/exit strategy is now legacy. The repo’s main path is:

1. public wallet reconstruction via `wallet_analyzer.py`
2. fresh paired BTC 5-minute dataset collection via `paired_research.py`
3. paired hold-to-resolution simulation before any new live deployment

## Current primary commands

Analyze the benchmark wallet:

```bash
python3 wallet_analyzer.py 0xe0229e10a858860218b6132f4234602c47bd6603 --reconstruct 50 --summary-by winner
```

Collect a fresh research dataset:

```bash
./run.sh paired_research.py --collect --db paired_research.db --duration-hours 24 --poll-interval 3
```

Simulate one paired policy:

```bash
./run.sh paired_research.py \
  --simulate-paired \
  --db paired_research.db \
  --policy payout_balanced \
  --policy-config '{"combined_threshold":1.01,"notional_step":20,"fee_bps":7.2,"slippage_bps":10}'
```

Rank all built-in paired policies:

```bash
./run.sh paired_research.py \
  --simulate-paired \
  --db paired_research.db \
  --policy all \
  --policy-config '{"fee_bps":7.2,"slippage_bps":10}'
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
- sell-side fills are now tentative until confirmed by on-chain ERC1155 balance decrease
- ghost/unconfirmed sell divergences are recorded and surfaced in summary output

### Shared paper/live behavior

- ROI display is now mark-to-market instead of cost-basis-flat after entry
- paper trader now mirrors live opposite-side sell cooldown behavior
- paper trader now mirrors live minimum effective trade-size behavior
- `observer.py --analyze` now prints local-time session and hour-of-day trade timing summaries

### New paired research path

- `wallet_analyzer.py` now reconstructs per-window BTC 5-minute public wallet behavior
- reconstruction output includes spend, shares, payout profile, gross P&L, ROI, timing, and skew metrics
- grouped summaries can bucket windows by skew, timing, combined cost, and winner overweight
- `paired_research.py` now collects a dedicated BTC 5-minute research DB separate from legacy paper DBs
- paired-policy simulation supports fee and slippage assumptions and can rank built-in policy families

### Order-path latency

- allowance/balance preflight is now briefly cached in live trading to reduce redundant REST calls during repeated attempts
- direct Polygon `eth_call` balance reads are now used for sell confirmation and divergence checks

## Current evidence

### Legacy paper trading

`polymarket_observer_100.db` suggests regular business hours are materially better than overnight:
- overnight bucket: negative overall
- business-hours bucket: strongly positive overall
- force exits remain the main drag across all buckets

### Public wallet reconstruction

The public benchmark wallet strongly suggests a different strategy shape:
- buys both `Up` and `Down` in most BTC 5-minute windows
- often scales in multiple times per window
- appears to hold through resolution and realize via redemption, not visible sells
- gross edge seems to come from paired cost structure plus uneven side weighting

### Legacy live trading

Live trading works at the infrastructure layer, but the old strategy is no longer the main direction. Do not start new live tests until the paired research path clears its evidence bar.

## What still needs validation

- at least one clean 24–48h paired research dataset
- simulation results that stay positive after fee/slippage assumptions
- explainable weighting rules, not just one-off wallet mimicry
- confirmation that the paired edge is not concentrated in a few outlier windows

## Practical guidance

- Treat `observer.py` / `trader.py` as legacy/reference.
- Use a fresh `paired_research.db` or clearly versioned research DBs.
- Keep collector settings stable for the full 24–48h dataset.
- Do not start new live tests until the paired simulation path looks durable after costs.
