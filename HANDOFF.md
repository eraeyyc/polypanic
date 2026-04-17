# HANDOFF.md — Polymarket BTC 5-Min Research Pivot

Context for the next session.

## Repo / working directory

`/Users/MAC/projects/polypanic`

## Current state

This repo has pivoted away from the original one-sided exit-target strategy.

What is currently true:
- `observer.py` / `trader.py` remain runnable as legacy/reference infrastructure
- the live execution/accounting stack is much more robust than it was initially
- a public-wallet analysis strongly suggests the real opportunity may be a paired hold-to-resolution strategy
- the main path is now research-first, not more live tuning of the old strategy

Do not treat older live or paper DBs as clean strategy evidence. Many were contaminated by state bugs and by multiple strategy revisions.

## Current recommended workflow

### 1. Reconstruct the benchmark wallet

```bash
python3 wallet_analyzer.py 0xe0229e10a858860218b6132f4234602c47bd6603 --reconstruct 50 --summary-by winner
```

### 2. Collect a fresh paired research dataset

```bash
./run.sh paired_research.py --collect --db paired_research.db --duration-hours 24 --poll-interval 3
```

### 3. Simulate paired policies

```bash
./run.sh paired_research.py \
  --simulate-paired \
  --db paired_research.db \
  --policy all \
  --policy-config '{"fee_bps":7.2,"slippage_bps":10}'
```

Notes:
- Use separate research DBs from the legacy observer/live DBs.
- Keep collector settings stable for the full run.
- No new live trading should start until the paired-policy path shows durable net-positive results after costs.

## Current strategy picture

The old one-sided strategy still appears to have some paper edge in narrow slices, but it is no longer the main hypothesis.

The stronger current lead is:
- buy both sides in BTC 5-minute markets
- weight them unevenly
- hold to resolution
- redeem winners rather than relying on live exits

This came from reconstructing a successful public wallet and cross-checking API data with OCR’d activity/closed-position PDFs.

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

These fixes still matter because they preserved the exchange/accounting infrastructure that the repo may reuse later, even though the core strategy direction changed.

### Paper/live strategy alignment fixes

1. ROI display is now mark-to-market in `observer.py`, so both paper and live show unrealized P&L correctly instead of looking artificially flat after entry.
2. Paper trader now applies opposite-side post-sell cooldown the same way live trader does.
3. Paper trader now rejects sub-minimum effective trades when `min(max_position_size, bankroll) < min_position_usdc`, matching live behavior.

### Latency / order-path improvement

1. Live allowance/balance preflight now uses a short-lived cache, reducing redundant REST calls during repeated entry/exit attempts.

## New paired research surfaces

- `wallet_analyzer.py`
  - reconstructs public wallet BTC 5-minute windows
  - outputs spend, shares, payout profiles, gross P&L, ROI, timing, and skew metrics
  - supports grouped summaries by skew, timing, combined cost, and winner overweight
- `paired_research.py`
  - collects a dedicated research DB with tick-level paired-market inputs
  - simulates built-in paired policy families
  - supports fee/slippage assumptions
  - can rank policies when run with `--policy all`

## What still needs watching

### Research quality

- Need at least one uninterrupted 24–48h research dataset before taking simulator results too seriously.
- Need to determine whether the benchmark wallet’s edge comes mostly from:
  - combined cost,
  - correct directional overweighting,
  - or both.
- Need to confirm that any simulated edge survives realistic fee/slippage assumptions and is not driven by a few outlier windows.

### Legacy live execution / infra

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
- Do not let the new paired-research tools drift into the legacy one-sided strategy path; keep the split explicit.

## Useful files

- `observer.py` — base observer, paper trader, analysis path, shared strategy logic
- `trader.py` — live trader, exchange integration, reconciliation, heartbeats
- `wallet_analyzer.py` — public wallet reconstruction and behavioral benchmark analysis
- `paired_research.py` — fresh paired dataset collector and simulator
- `test_live_trader.py` — regression tests for live-state and shared strategy logic
- `test_research.py` — regression tests for wallet reconstruction and paired simulation
- `STATUS.md` — current operational summary
