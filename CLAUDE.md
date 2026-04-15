# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install requests py-clob-client websockets flask

# Run observer / paper trader
python observer.py
python observer.py --entry 0.38 --exit 0.54 --min-entry 0.18 --cooldown 10 --bankroll 500

# Analyze collected data
python observer.py --analyze
python observer.py --analyze --db polymarket_observer_100.db

# Set up live credentials
python trader.py --setup-keys --private-key 0x...

# Preferred live run path
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

Tests:

```bash
python3 -m unittest test_live_trader.py
```

Syntax checks:

```bash
python3 -c "import ast; ast.parse(open('observer.py').read())"
python3 -c "import ast; ast.parse(open('trader.py').read())"
python3 -c "import ast; ast.parse(open('test_live_trader.py').read())"
```

## Architecture

The repo has two main Python entrypoints with a shared strategy layer:

**`observer.py`**
- shared `StrategyConfig`
- paper trading engine (`PaperTrader`)
- main observer loop (`Observer`)
- database schema and helpers
- analytics path via `analyze()`

**`trader.py`**
- imports and extends `observer.py`
- live execution engine (`LiveTrader`)
- market and user WebSockets
- reconciliation against Polymarket orders, trades, and positions
- auth and client bootstrap

## Important implementation reality

Paper and live do not just share config. They also share substantial logic:
- entry and exit strategy rules
- cooldown behavior
- timing gates
- hold-through-close logic
- ROI display in the observer loop

If strategy behavior is changed for live and should also apply to paper, check `observer.py` too.

## Current state

The repo is no longer in the very early “untested mainnet” state. Live trading has been exercised and multiple real bugs were fixed.

Important fixed areas:
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

Current practical live baseline:
- entry `0.38`
- exit `0.54`
- min entry `0.18`
- entry delay `20`
- max entry age `240`
- cooldown `10`
- `allow_contrarian`
- `both_sides`
- hold threshold `15`
- hold neutral range `5`

Paper analysis so far suggests time of day matters. Business-hour paper performance has looked materially better than overnight, while force exits remain the biggest drag.

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
