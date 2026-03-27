# Polymarket BTC 5-Min Observer & Paper Trader

Watches every 5-minute Bitcoin up/down market on Polymarket, logs the full
price lifecycle of both sides, correlates with actual BTC spot price, and
runs a configurable paper trading strategy.

## The Strategy

Polymarket's 5-minute BTC markets are driven by retail sentiment that
overshoots in both directions — often independent of what BTC is actually
doing. The play:

1. **Buy** whichever side is cheap (e.g., ask ≤ $0.40)
2. **Sell** when the market overreacts the other way (e.g., bid ≥ $0.65)
3. **Never hold through resolution** — exit before the market closes
4. **Both sides** can be profitable in the same window if the market swings enough

## What It Does

### Observation Mode
- Discovers each new 5-minute market automatically via deterministic slug generation
- Polls prices on both UP and DOWN sides every 3 seconds
- Records BTC spot price alongside market prices to measure divergence
- Stores everything in SQLite for analysis

### Paper Trading
- Simulates the strategy with configurable thresholds
- Tracks bankroll, win/loss, P&L by exit reason
- Lets you validate whether the edge is real before risking real money

### Analysis Mode
- Swing analysis: "How often does a side go from X → Y within a window?"
- Divergence analysis: Market sentiment vs actual BTC movement
- Paper trading results: win rate, P&L, breakdown by exit reason

## Quick Start

```bash
# Install dependencies (just requests — keeping it simple)
pip install requests

# Run the observer + paper trader with defaults
python observer.py

# Run with custom thresholds
python observer.py --entry 0.35 --exit 0.70 --bankroll 500

# Analyze collected data after running for a while
python observer.py --analyze

# See all options
python observer.py --help
```

## Configuration Options

| Flag | Default | Description |
|------|---------|-------------|
| `--entry` | 0.40 | Buy when best ask ≤ this price |
| `--exit` | 0.65 | Sell when best bid ≥ this price |
| `--stop-loss` | 0.00 | Bail if price drops to this (0 = hold to resolution) |
| `--bankroll` | 1000 | Starting paper money |
| `--poll` | 3.0 | Seconds between price checks |
| `--both-sides` | true | Allow buying both UP and DOWN |
| `--single-side` | false | Only one position per market |
| `--db` | polymarket_observer.db | Database file path |
| `--analyze` | - | Run analysis on collected data |

## How It Works

### Market Discovery
Polymarket 5-min BTC markets use deterministic slugs:
```
btc-updown-5m-{unix_timestamp}
```
where the timestamp is `floor(now / 300) * 300`. No scanning needed —
we calculate which market is active from the clock.

### Data Pipeline
1. **CLOB server time** → sync clock with Polymarket
2. **Gamma API** → look up market by slug, get token IDs for UP/DOWN
3. **CLOB API** → poll best bid/ask for both tokens
4. **Coinbase/CoinGecko/Kraken** → BTC spot price (with fallbacks)
5. **SQLite** → log every tick for later analysis

### Paper Trading Logic
```
Every 3 seconds:
  1. Check exits first (exit_target, stop_loss, force_exit)
  2. Then check entries (is either side cheap enough?)
  3. At window end: resolve any remaining positions
```

## What To Look For In The Data

After running for a day or two, run `--analyze` and look at:

1. **Hit rate by threshold pair**: "Entry ≤$0.35 → Exit ≥$0.65" — what
   percentage of markets where this entry was possible also reached the
   exit target? If this is >55%, you likely have an edge.

2. **Exit reason breakdown**: Are most exits "exit_target" (good) or
   "force_exit"/"resolution" (bad)? If you're constantly getting forced
   out, your exit threshold might be too ambitious.

3. **Divergence patterns**: Markets where the market swung hard but BTC
   barely moved — that's where your edge lives. The analysis shows you
   how common this is.

## What This Doesn't Do (Yet)

- **Real trading**: This is observation + paper mode only. The CLOB API
  supports authenticated trading, but that's Phase 2 after you've
  validated the edge.
- **WebSocket streaming**: Currently polls REST every 3 seconds. For
  real trading you'd want WebSocket connections to both Polymarket
  and a BTC price feed for lower latency.
- **Sophisticated entry signals**: Right now it's just "is the price
  cheap enough?" You might find that combining cheap price + specific
  BTC volatility patterns improves the hit rate.

## Architecture (For When You Want To Add Real Trading)

```
observer.py
├── PolymarketClient     # Read-only market discovery + pricing
├── BTCPriceClient       # Multi-source BTC spot with fallbacks
├── Database             # SQLite logging (markets, ticks, trades)
├── PaperTrader          # Simulated execution engine
├── Observer             # Main loop tying it all together
└── analyze()            # Post-hoc data analysis
```

To add real trading, you'd:
1. Add `py-clob-client` as a dependency
2. Create an `AuthenticatedTrader` that extends `PaperTrader`
3. Replace `execute_buy`/`execute_sell` with real CLOB API calls
4. Add a `--live` flag to switch between paper and real mode

## Files

- `observer.py` — The entire bot (single file, ~600 lines)
- `polymarket_observer.db` — SQLite database (created on first run)
- `observer.log` — Log file
