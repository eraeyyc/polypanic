# Polymarket BTC 5-Min Observer & Paper Trader

Watches every 5-minute Bitcoin up/down market on Polymarket, logs the full price lifecycle of both sides, correlates with actual BTC spot price, and runs a configurable paper trading strategy.

## The Strategy

Polymarket's 5-minute BTC markets are driven by retail sentiment that overshoots in both directions — often independent of what BTC is actually doing. The play:

1. **Buy** whichever side is cheap (ask ≤ entry threshold)
2. **Sell** when the market overreacts the other way (bid ≥ exit threshold)
3. **Cut losses** via stop-loss if the position goes the wrong way
4. **Hold through close** if BTC has moved strongly in your favor — let resolution pay $1.00/share instead of force-selling cheap

## Quick Start

```bash
# Install dependencies
pip install requests py-clob-client websockets

# Run paper trader with recommended settings
./run.sh observer.py --stop-loss 0.10 --entry-delay 5 --btc-momentum 30 --hold-threshold 15

# Analyze collected data after running for a while
./run.sh observer.py --analyze

# See all options
./run.sh observer.py --help
```

## Configuration Options

| Flag | Default | Description |
|------|---------|-------------|
| `--entry` | 0.40 | Buy when best ask ≤ this price |
| `--exit` | 0.65 | Sell when best bid ≥ this price |
| `--stop-loss` | 0 | Sell if price drops to this (0 = disabled) |
| `--entry-delay` | 0 | Seconds to wait before first buy each window |
| `--btc-momentum` | 0 | Skip buying a side if BTC has moved $X against it from window open (0 = disabled) |
| `--hold-threshold` | 0 | Skip force_exit near close if BTC has moved $X in your favor — let it resolve at $1.00 (0 = disabled) |
| `--bankroll` | 1000 | Starting paper bankroll |
| `--poll` | 3.0 | Seconds between REST price polls |
| `--single-side` | false | Only allow one position per market window |
| `--db` | polymarket_observer.db | SQLite database path |
| `--analyze` | — | Run post-hoc analysis on collected data |

## Architecture

Two files with a clean inheritance relationship:

**`observer.py`** — no auth required. Run this first to validate the strategy before touching real money.
- `StrategyConfig` — all tunable parameters
- `PolymarketClient` — read-only REST client
- `BTCPriceClient` — BTC spot price with fallback chain (Coinbase → Kraken → Binance → CoinGecko)
- `Database` — SQLite with 5 tables: `markets`, `price_ticks`, `paper_trades`, `live_trades`, `strategy_config`
- `PaperTrader` — simulated execution engine
- `Observer` — main loop, designed for subclassing

**`trader.py`** — imports from `observer.py`, adds live execution via the Polymarket CLOB API.

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
# One-time key derivation
./run.sh trader.py --setup-keys --private-key 0x...
# (use env var instead of CLI flag — CLI args appear in ps aux)
export POLYMARKET_PRIVATE_KEY=0x...

# Run live trader
./run.sh trader.py
```

Live trading has not been tested against mainnet yet. Run paper mode for several days first.

## Files

- `observer.py` — paper trading bot
- `trader.py` — live trading layer (imports observer.py)
- `run.sh` — activates `.venv` and runs any script
- `polymarket_observer.db` — SQLite database (created on first run, gitignored)
- `observer.log` — log file (gitignored)
