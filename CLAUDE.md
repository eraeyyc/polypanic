# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Current strategy priority

The old one-sided observer/live-trader strategy is now legacy. The main path is paired hold-to-resolution research:

- `wallet_analyzer.py` for public-wallet reconstruction
- `paired_research.py` for fresh dataset collection and paired-policy simulation

Do not default to tuning the old `observer.py` / `trader.py` strategy unless the user explicitly asks to work on the legacy path.

## Commands

```bash
# Install dependencies
pip install requests py-clob-client websockets flask

# Run legacy observer / paper trader
python observer.py
python observer.py --entry 0.38 --exit 0.54 --min-entry 0.18 --cooldown 10 --bankroll 500

# Analyze collected data
python observer.py --analyze
python observer.py --analyze --db polymarket_observer_100.db

# Set up live credentials
python trader.py --setup-keys --private-key 0x...

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

The repo has two main Python entrypoints with a shared strategy layer:

**`observer.py`**
- shared `StrategyConfig`
- paper trading engine (`PaperTrader`)
- main observer loop (`Observer`)
- database schema and helpers
- analytics path via `analyze()`
- legacy one-sided strategy path

**`trader.py`**
- imports and extends `observer.py`
- live execution engine (`LiveTrader`)
- market and user WebSockets
- reconciliation against Polymarket orders, trades, and positions
- auth and client bootstrap
- legacy one-sided live path

**`wallet_analyzer.py`**
- canonical public-wallet reconstruction tool
- summarizes both-sides behavior, scaling, timing, cost, payout, and ROI per window
- supports grouped summaries and CSV export for inferred strategy analysis

**`paired_research.py`**
- dedicated paired-strategy collector and simulator
- stores a fresh BTC 5-minute research DB separate from legacy paper/live DBs
- compares policy families such as equal-time, combined-cost threshold, payout-balanced, and conviction-weighted

## Important implementation reality

Paper and live do not just share config. They also share substantial logic:
- entry and exit strategy rules
- cooldown behavior
- timing gates
- hold-through-close logic
- ROI display in the observer loop

If strategy behavior is changed for live and should also apply to paper, check `observer.py` too.

## Current state

The repo is no longer primarily a one-sided live-trading bot. The current thesis is that a public benchmark wallet may be exploiting a paired hold-to-resolution edge by buying both sides and weighting them unevenly.

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

Important new research surfaces:
- public-wallet reconstruction
- fresh paired research dataset collection
- paired-policy simulation with fee/slippage assumptions

## Database reality

Do not rely only on the older simplified schema description. Relevant tables now include:

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
- which timing / BTC-delta regimes look best for paired hold-to-resolution?

## Things not to regress

- Do not revert dust-fill handling.
- Do not revert active-window Data API protections.
- Do not revert failed-order persistence to `failed`.
- Do not revert mark-to-market ROI display.
- Do not let paper/live strategy logic drift again.
- Do not remove entry-rejection logging.

## Still worth validating

- more clean daytime live sessions
- more real settlement-through-resolution cases
- broader user WebSocket payload coverage
- further latency trimming on the network-bound order path if needed

See `HANDOFF.md` and `STATUS.md` for current operational details.
