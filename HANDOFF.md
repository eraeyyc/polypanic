# HANDOFF.md

Context for the next session.

## Repo / working directory

`C:\Users\erica\Projects\polypanic`

## Big picture

This repo started as a Polymarket BTC 5-minute one-sided observer/live-trader project.

The work then split into two tracks:

1. **Research track**
   - reverse-engineer a successful public wallet
   - collect a dedicated paired-market dataset
   - simulate paired hold-to-resolution policies

2. **Infrastructure track**
   - keep the legacy live execution stack operational and trustworthy enough to reuse
   - harden reconciliation, accounting, and state recovery
   - most recently, migrate the live path from CLOB V1 to **CLOB V2**

The research track is the current main strategy direction.
The live track is still important because it is the exchange/accounting infrastructure the repo may reuse later.

## Why the strategy focus changed

The original one-sided entry/exit strategy never fully inspired confidence. Over time, several things became clear:

- paper/live results were sensitive to bugs, state drift, and strategy revisions
- many apparent results were contaminated by legacy issues
- a successful public wallet (`0xe0229e10a858860218b6132f4234602c47bd6603`) did **not** look like a simple one-sided trader

Public wallet reconstruction suggested a very different shape:

- buys both `Up` and `Down` in most BTC 5-minute windows
- often scales in multiple times per window
- appears to hold through resolution and realize via redemption rather than visible sells
- seems to derive edge from a paired structure plus uneven side weighting, not from the old exit-target logic

That is why the repo pivoted toward:

- `wallet_analyzer.py`
- `paired_research.py`

rather than more tuning of the original one-sided strategy.

## What has been done so far

### 1. Legacy live stack hardening

Before the V2 migration, the legacy live stack was substantially cleaned up. Important shipped fixes included:

- `closed` orders are treated as terminal and no longer block entries
- transient market-data failures no longer permanently trip the live kill switch
- failed submissions persist as `failed`, not ghost `pending_submit`
- manual/external sells can be reconciled
- active-window Data API lag no longer clears real positions
- active-window Data API lag no longer resurrects already-sold size
- sell fills that leave only dust now flatten the position
- entry rejection reasons are logged
- opposite-side flips are blocked during the configured sell cooldown
- paper/live alignment was improved for cooldown and minimum effective trade size
- mark-to-market ROI display was fixed

These changes still matter. Even though the strategy thesis changed, the live execution layer is now much more trustworthy than it was at the start.

### 2. Public wallet reverse engineering

`wallet_analyzer.py` became the main behavioral benchmark tool.

What it now does:

- reconstructs BTC 5-minute public-wallet windows
- estimates spend, shares, payout geometry, ROI, timing, and skew
- groups windows by timing / skew / combined cost / winner overweight
- helps reason about what the benchmark wallet is doing

Research takeaways evolved over time, but the durable result was:

- the benchmark wallet does not look like the original one-sided strategy
- it looks much closer to a paired hold-to-resolution system

### 3. Paired research framework

`paired_research.py` was added as the dedicated collector/simulator for the new thesis.

What it now does:

- collects a separate paired-market research DB
- simulates built-in paired policy families
- supports fee/slippage assumptions
- supports multiple analysis helpers added during the reverse-engineering work

Important result:

- several simple “compressed” theories about the public wallet were tested and did **not** hold up cleanly in simulation
- the repo still does **not** have a clearly proven paired policy ready for live deployment

So the research conclusion is:

- the paired hold-to-resolution direction is still the most promising
- but the exact signal/weighting rule is still unresolved

### 4. CLOB V2 migration

Because Polymarket is migrating to CLOB V2, the legacy live path was updated so it does not remain stuck on dead infrastructure.

What changed:

- dependency switched to `py-clob-client-v2`
- live setup/build paths were migrated to the V2 Python SDK
- shared read-only host targeting in `observer.py` now supports a custom CLOB host
- `trader.py` now supports `--clob-host`
- live market constraints now come from `get_clob_market_info()` / V2 order book data
- local V1 fee math was removed as authoritative live logic
- market buys now pass `user_usdc_balance` into the V2 SDK
- startup now checks for usable **pUSD** collateral
- startup now neutralizes stale local open-order assumptions if the exchange no longer reports them
- V2 cancellation/open-order methods were wired into reconciliation
- tests were updated and passed locally

Important caveat:

- the installed `py-clob-client-v2==1.0.0` package does **not** exactly match the migration docs in every surface area
- implementation was done against the **actual installed SDK**, not the idealized docs

Also important:

- this migration is **code-complete**, not **runtime-proven**
- the real V2 smoke test still needs to happen

## Current state

### Main strategy path

Primary focus should still be:

1. reconstruct benchmark wallet behavior
2. collect clean paired datasets
3. simulate paired policies
4. avoid new live deployment until a durable edge survives fees/slippage

### Legacy live path

The live stack is now:

- still legacy relative to the repo’s strategy direction
- but operationally much more solid than before
- and now adapted for V2

So if someone needs to continue the live/infrastructure path, they should start from the **V2 code**, not from any old V1 assumption.

## What remains uncertain / unverified

### Research

- the exact paired entry / weighting / continuation rule is still not solved
- several intuitive theories about the public wallet turned out to be incomplete or wrong
- there is still no live-ready paired strategy that has clearly cleared the evidence bar

### V2 live runtime

These still need real validation:

- authenticated real-host smoke test was run on 2026-04-27 against `https://clob-v2.polymarket.com` using fee-enabled test market `0xaf5e903876ad42de97e1cf02c2ef8484df69bcfc5541b96a400116557d1e504e`; market info, orderbook, V2 order signing, and live `/order` posting all worked
- that same smoke test could not complete a fill because the tested wallet had `0` pUSD and `0` exchange allowance
- exact pUSD readiness / allowance behavior on a funded wallet
- exact user WebSocket fee payload coverage
- actual post/cancel/fill lifecycle on V2
- whether heartbeat behavior is unchanged in practice

## Recommended next steps

### If continuing the research path

1. ~~collect at least one clean uninterrupted 24–48h paired research dataset~~ — **in progress** (started 2026-05-17, output: `paired_research.db`)
2. once collection completes, run `--simulate-paired --policy all` and check net result after fees/slippage
3. use `wallet_analyzer.py` and `paired_research.py` as the main surfaces
4. treat the legacy observer/live DBs as contaminated historical evidence unless there is a specific reason to inspect them

### If continuing the live/V2 path

1. fund the actual wallet with pUSD and approve the exchange / CTF path
2. rerun a tiny fee-enabled V2 test order on `https://clob-v2.polymarket.com`
3. verify user WebSocket event fields for fees and fills
4. verify startup stale-order cleanup against real exchange state
5. only after that, consider adding richer Safe / approval helpers

## Things not to regress

- do not revert dust-fill handling
- do not revert active-window Data API protections
- do not revert failed-order persistence to `failed`
- do not revert mark-to-market ROI display
- do not remove entry-rejection logging
- do not let paper/live strategy logic drift again
- do not reintroduce V1 fee assumptions into the live path
- do not blur the distinction between the paired research path and the legacy live path

## Files worth reading first

- `CLAUDE.md` — Claude-specific working guidance
- `AGENTS.md` — Codex-specific working guidance
- `wallet_analyzer.py` — public wallet reconstruction
- `paired_research.py` — paired collector + simulator
- `trader.py` — V2-migrated legacy live stack
- `test_live_trader.py` — regression coverage for live logic
- `STATUS.md` — shorter operational summary
