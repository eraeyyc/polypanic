# Polymarket BTC 5-Min Observer & Paper Trader

Watches every 5-minute Bitcoin up/down market on Polymarket, logs the full price lifecycle of both sides, correlates with actual BTC spot price, and runs a configurable paper trading strategy.

## The Strategy

The current edge is narrower than pure mean reversion. The database shows the losing trades are dominated by contrarian entries that decay into near-zero force exits. The play:

1. **Wait** for the first minute so the window has some directional information
2. **Buy** the cheap side only if it agrees with BTC's move from the window open
3. **Ignore** setups that appear too late in the 5-minute window
4. **Sell** when the market overreacts the other way (bid ≥ exit threshold)
5. **Never hold through resolution** — force-exit before close unless BTC has moved strongly in your favor

## Quick Start

```bash
# Install dependencies
pip install requests py-clob-client websockets flask

# Run paper trader (recommended settings)
./run.sh observer.py --entry 0.38 --exit 0.70 --min-entry 0.15

# Analyze collected data after running for a while
./run.sh observer.py --analyze

# See all options
./run.sh observer.py --help
```

`run.sh` activates the `.venv` automatically. You can also call `python observer.py` directly if your environment is already set up.

## Configuration Options

| Flag | Default | Description |
|------|---------|-------------|
| `--entry` | 0.38 | Buy when best ask ≤ this price |
| `--exit` | 0.70 | Sell when best bid ≥ this price |
| `--min-entry` | 0.15 | Reject entries below this price — avoids buying near-dead markets |
| `--stop-loss` | 0 | Sell if price drops to this (0 = disabled) |
| `--stop-loss-after` | 60 | Only trigger stop-loss in the final N seconds of the window (0 = fire anytime) |
| `--entry-delay` | 60 | Seconds to wait before first buy each window |
| `--max-entry-age` | 150 | Stop opening new positions after this many seconds from window open (0 = disabled) |
| `--btc-momentum` | 0 | Skip buying a side if BTC has moved $X against it from window open (0 = disabled) |
| `--allow-contrarian` | false | Allow buying against BTC's move from the window open |
| `--hold-threshold` | 15 | Skip force_exit near close if BTC has moved $X in your favor — let it resolve at $1.00 (0 = disabled) |
| `--only-side` | — | Restrict entries to `up` or `down` only |
| `--both-sides` | false | Allow positions in both UP and DOWN in the same market window |
| `--bankroll` | 1000 | Starting paper bankroll |
| `--poll` | 3.0 | Seconds between REST price polls (WebSocket takes over in live mode) |
| `--db` | polymarket_observer.db | SQLite database path |
| `--analyze` | — | Run post-hoc analysis on collected data |

## Architecture

Two files with a clean inheritance relationship:

**`observer.py`** — no auth required. Run this first to validate the strategy before touching real money.
- `StrategyConfig` — all tunable parameters
- `PolymarketClient` — read-only REST client. Market slugs are deterministic: `btc-updown-5m-{floor(unix_ts/300)*300}`
- `BTCPriceClient` — BTC spot price with fallback chain (Coinbase → Kraken → Binance → CoinGecko)
- `Database` — SQLite with 5 tables: `markets`, `price_ticks`, `paper_trades`, `live_trades`, `strategy_config`. WAL mode enabled.
- `PaperTrader` — simulated execution engine
- `Observer` — main loop, designed for subclassing

**`trader.py`** — imports from `observer.py`, adds live execution via the Polymarket CLOB API and WebSocket price feed.

## Terminal Output

Every poll tick shows:
```
19:25:44  ⏱ 256.7s | UP bid=0.63 ask=0.64 | DN bid=0.36 ask=0.37 | BTC $71,900 (-8.86) | ROI +30.00
```

ROI is green when up, red when down for the current window. When the window closes:
```
✅ Resolved: UP | BTC $71,900 → $71,960 | Window P&L: ▲ +30.00  (bankroll $1030.00)
```

## Live Trading Setup

```bash
# One-time key derivation (use env var — CLI args appear in ps aux)
export POLYMARKET_PRIVATE_KEY=0x...
./run.sh trader.py --setup-keys

# Run live trader
./run.sh trader.py
```

Live trading has not been tested against mainnet yet. Run paper mode for several days first and confirm the edge is holding via `--analyze`.

## Files

- `observer.py` — paper trading bot + data collection
- `trader.py` — live trading layer (imports observer.py)
- `run.sh` — activates `.venv` and runs any script
- `dashboard.py` — Flask dashboard (in repo but not actively maintained)
- `polymarket_observer.db` — SQLite database (created on first run, gitignored)
- `observer.log` — log file (gitignored)
