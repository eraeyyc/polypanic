# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Current priority

The repo has two distinct tracks:

- `wallet_analyzer.py` / `paired_research.py`: the current main research path
- `observer.py` / `trader.py`: the legacy live/paper execution stack, now retained for infrastructure and reference

Do not default to tuning the old one-sided observer/live-trader strategy unless the user explicitly asks to work on the legacy path.

## Current repo state

Two important things are true at once:

1. The **strategy thesis** has pivoted toward paired hold-to-resolution research.
2. The **legacy live stack** was recently migrated from Polymarket CLOB V1 to **CLOB V2** so it remains operationally usable.

That means:
- research work should usually happen in `wallet_analyzer.py` and `paired_research.py`
- live-infrastructure work should assume **V2**, not V1

## Commands

```bash
# Install dependencies
pip install requests py-clob-client-v2==1.0.0 websockets flask

# Run legacy observer / paper trader
python observer.py
python observer.py --entry 0.38 --exit 0.54 --min-entry 0.18 --cooldown 10 --bankroll 500

# Analyze collected observer data
python observer.py --analyze
python observer.py --analyze --db polymarket_observer_100.db

# Set up live credentials
python trader.py --setup-keys --private-key 0x...

# Explicit host override (post-migration production host)
python trader.py --private-key 0x... --clob-host https://clob.polymarket.com

# Analyze benchmark wallet
python3 wallet_analyzer.py 0xe0229e10a858860218b6132f4234602c47bd6603 --reconstruct 50 --summary-by winner

# Collect paired research dataset
./run.sh paired_research.py --collect --db paired_research.db --duration-hours 24 --poll-interval 3

# Compare paired policies
./run.sh paired_research.py --simulate-paired --db paired_research.db --policy all --policy-config '{"fee_bps":7.2,"slippage_bps":10}'

# Legacy live run path remains available for infrastructure/reference only
./run.sh trader.py --db polymarket_live_legacy.db --log-level INFO
```

Tests:

```bash
python3 -m unittest test_live_trader.py test_research.py
```

Syntax checks:

```bash
python3 -c "import ast; ast.parse(open('observer.py').read())"
python3 -c "import ast; ast.parse(open('trader.py').read())"
python3 -c "import ast; ast.parse(open('wallet_analyzer.py').read())"
python3 -c "import ast; ast.parse(open('paired_research.py').read())"
python3 -c "import ast; ast.parse(open('test_live_trader.py').read())"
python3 -c "import ast; ast.parse(open('test_research.py').read())"
```

## Architecture

### `observer.py`
- shared `StrategyConfig`
- paper trading engine (`PaperTrader`)
- main observer loop (`Observer`)
- database schema and helpers
- analytics path via `analyze()`
- legacy one-sided strategy path

### `trader.py`
- imports and extends `observer.py`
- live execution engine (`LiveTrader`)
- market and user WebSockets
- reconciliation against Polymarket orders, trades, and positions
- auth and client bootstrap
- legacy one-sided live path

### `wallet_analyzer.py`
- canonical public-wallet reconstruction tool
- summarizes both-sides behavior, scaling, timing, cost, payout, and ROI per window
- supports grouped summaries and CSV export for inferred strategy analysis

### `paired_research.py`
- dedicated paired-strategy collector and simulator
- stores a fresh BTC 5-minute research DB separate from legacy paper/live DBs
- compares policy families and analysis modes for paired hold-to-resolution research

## Important implementation reality

Paper and live do not just share config. They also share substantial logic:
- entry and exit strategy rules
- cooldown behavior
- timing gates
- hold-through-close logic
- ROI display in the observer loop

If strategy behavior is changed for live and should also apply to paper, check `observer.py` too.

## V2 live-trading notes

The live path now assumes **Polymarket CLOB V2**.

Important details:
- Python dependency is `py-clob-client-v2`
- `trader.py` supports `--clob-host` for explicit V2 testing
- market constraints come from `get_clob_market_info()` / V2 book data
- market buys pass `user_usdc_balance` into the SDK for fee-adjusted sizing
- local V1-style fee math is no longer authoritative
- startup readiness now checks for usable **pUSD** collateral
- stale local open-order assumptions are closed on startup if the exchange no longer reports them
- `POLY_BUILDER_CODE` is passed into the V2 Python client if present

Important caveat:
- the installed `py-clob-client-v2==1.0.0` Python package does **not** exactly match the migration docs in every surface area
- the code was updated against the **actual installed SDK**, not the idealized doc examples

## Current state

The repo is no longer primarily a one-sided live-trading bot. The main current thesis is that a public benchmark wallet may be exploiting a paired hold-to-resolution edge by buying both sides and weighting them unevenly.

Important preserved infrastructure from the legacy path:
- closed-order terminal handling
- market-data kill-switch recovery
- failed submission persistence
- external/manual sell reconciliation
- active-window Data API lag protection
- dust-fill flattening
- opposite-side post-sell cooldown handling
- mark-to-market ROI display
- paper/live alignment on minimum trade-size gating
- short-lived allowance preflight caching for live latency reduction

Important new live-path work:
- CLOB V2 migration
- pUSD collateral readiness checks
- V2 cancel/open-order reconciliation updates
- startup handling for wiped/stale orders

Important new research surfaces:
- public-wallet reconstruction
- fresh paired research dataset collection
- paired-policy simulation with fee/slippage assumptions

## Database reality

Relevant tables include:

```sql
markets
price_ticks
paper_trades
live_trades
strategy_config
live_orders
live_fills
live_positions
live_market_state
live_reconciliation_state
```

For live accounting and debugging, prefer:
- `live_orders`
- `live_fills`
- `live_positions`

over `live_trades`.

## Strategy notes

Current main research questions:
- does buying both sides with equal sizing ever survive costs?
- does uneven sizing produce a durable edge?
- is the edge driven more by combined pair cost or by correct directional overweighting?
- which timing / BTC-delta / quote-state regimes look best for paired hold-to-resolution?

## Things not to regress

- Do not revert dust-fill handling.
- Do not revert active-window Data API protections.
- Do not revert failed-order persistence to `failed`.
- Do not revert mark-to-market ROI display.
- Do not let paper/live strategy logic drift again.
- Do not remove entry-rejection logging.
- Do not reintroduce V1 fee or collateral assumptions into the live path.

## Still worth validating

- a funded-wallet V2 fill test against `https://clob.polymarket.com` (post-cutover production host); an authenticated pre-cutover smoke test against `https://clob-v2.polymarket.com` already reached live `/order` and failed only because the wallet had `0` pUSD / allowance
- exact user WebSocket payload coverage for fee fields and unusual fills
- pUSD readiness behavior against a real funded wallet
- whether heartbeat behavior is unchanged in practice on V2
- more clean daytime live sessions
- broader settlement-through-resolution coverage

See `HANDOFF.md` and `STATUS.md` for the current operational picture.
