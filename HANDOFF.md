# HANDOFF.md — Polymarket BTC 5-Min Trading Bot

Context for continuing this project in a new conversation.

## What Was Built

Two-file Python trading bot for Polymarket's BTC 5-minute up/down markets.

### The Strategy

Buy DOWN when ask ≤ 40¢. Wait for the market to reprice to 65¢+ from retail sentiment overshoot, then sell. Never hold through resolution. The edge is crowd psychology — BTC can move $10-20 and the market swings 30 cents.

**Why DOWN only:** Data from 8+ hours of paper trading showed DOWN outperforming consistently. Theory: whales can push BTC price up (buying pressure), but can't easily push it down — so DOWN overshoots are more organic and less manipulated. Running both sides causes UP losses to offset DOWN gains.

**Current tuned parameters (`config_down.json`):**
- Entry ≤ 0.40, Exit ≥ 0.65
- Stop-loss at 0.15, only fires in final 20s AND only when BTC is moving *against* the position
- Entry delay 5s (avoid first-tick noise)
- BTC momentum filter $50 (skip DOWN entry if BTC has already moved up $50+ from window open)

### Files

| File | Purpose |
|------|---------|
| `observer.py` | Paper trading + data collection. No auth. Run this first. |
| `trader.py` | Live trading. Imports from observer.py. Requires wallet + USDC. |
| `dashboard.py` | Flask web UI — built but has bugs, not actively used. Run from terminal instead. |
| `config_down.json` | Active config for Down Only bot |
| `config.json` | Both Sides config (bot retired, kept for reference) |
| `requirements.txt` | `requests`, `py-clob-client`, `websockets`, `flask` |
| `CLAUDE.md` | Architecture reference for Claude Code |
| `polymarket_down.db` | Active SQLite DB for Down Only bot |
| `polymarket_observer.db` | SQLite DB for Both Sides bot (retired) |

---

## Architecture

### observer.py

Self-contained. No live trading code.

```
StrategyConfig          all tunable params (entry/exit thresholds, position sizing, timing)
PolymarketClient        read-only REST — market discovery + price polling
BTCPriceClient          BTC spot price, fallback chain: Coinbase → Kraken → Binance → CoinGecko
Database                SQLite: markets, price_ticks, paper_trades, live_trades, strategy_config
PaperTrader             simulated execution — buy/sell/resolve, position tracking
Observer                main loop — designed for subclassing (see extension hooks below)
analyze()               post-hoc swing analysis + paper trading results
```

**Observer extension hooks** (override in subclasses, no-ops by default):
- `_get_prices(tokens) -> (up_prices, down_prices)` — default: REST poll
- `_on_new_market(slug, tokens)` — called once per window after token resolution
- `_mode_label() -> str` — display label in startup banner
- `trader=` param in `__init__` — inject a different trader (else creates PaperTrader)

**Market slug formula:** `btc-updown-5m-{floor(unix_timestamp / 300) * 300}` — deterministic, no scanning needed.

### trader.py

Imports `StrategyConfig, Database, PaperTrader, Position, Observer, CLOB_API, analyze` from observer.py.

```
_TokenBook              in-memory order book (bids/asks dicts), applies snapshots + deltas
PriceWebSocket          background asyncio thread, full book maintenance, thread-safe get_prices()
LiveTrader(PaperTrader) real CLOB orders, heartbeat thread, cancel on window close
LiveObserver(Observer)  wires WebSocket + LiveTrader, overrides _get_prices/_on_new_market
setup_keys()            one-time credential derivation → ~/.polypanic/keys.json
build_client()          loads saved keys → authenticated ClobClient
```

---

## Key API Details (from docs research done in this session)

**Endpoints:**
- CLOB API: `https://clob.polymarket.com`
- Gamma API: `https://gamma-api.polymarket.com`
- WebSocket: `wss://ws-subscriptions-clob.polymarket.com/ws/market`

**Authentication:**
- `signature_type=0` for EOA (standard) wallets. No `funder` param needed.
- Derive credentials: `client.create_or_derive_api_creds()` — returns `ApiCreds(api_key, api_secret, api_passphrase)`. Do NOT use `create_api_key()` — wrong method name.
- Saved keys file uses fields: `api_key`, `api_secret`, `api_passphrase` (not `key`/`secret`/`passphrase`)

**Orders:**
- `OrderArgs.size` is in **SHARES**, not USDC. Compute: `shares = usdc_amount / price`
- Order types: GTC (used for both entries and exits), FOK, FAK, GTD
- Cancel: `clob.cancel(order_id)` — plain string, not `cancel_order({"orderID": ...})`
- `create_order()` auto-fetches and validates tick size. If price doesn't conform, it raises.

**Heartbeat — CRITICAL:**
- `clob.post_heartbeat(heartbeat_id)` — first call: `None`, subsequent: use returned `heartbeat_id`
- Must call within 10 seconds or Polymarket cancels ALL open orders
- LiveTrader runs this every 5s in a background thread

**WebSocket subscription message:**
```json
{
    "type": "market",
    "assets_ids": ["token_id_1", "token_id_2"],
    "markets": [],
    "initial_dump": true
}
```
Send `"PING"` string every ~45s to keep alive.

**WebSocket events received:**
- `event_type: "book"` — full snapshot, fields: `asset_id`, `bids`, `asks` (each `[{"price": "0.45", "size": "100"}]`)
- `event_type: "price_change"` — delta, fields: `asset_id`, `changes: [{"side": "BUY"|"SELL", "price": "0.45", "size": "0"}]` (size=0 means level removed)

**Fees (as of 2026-03-30):** crypto taker 0.072%, maker rebate 20%.

**USDC approval (one-time before first trade):**
```python
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
```

---

## Setup & Usage

```bash
pip install requests py-clob-client websockets

# Step 1: Collect data (run for 1-2 days)
python observer.py

# Step 2: Analyze to find optimal thresholds
python observer.py --analyze
# Look for entry/exit pairs with >55% hit rate and "exit_target" as dominant exit reason

# Step 3: One-time live trading setup
python trader.py --setup-keys --private-key 0x...
# Then approve USDC (see above)

# Step 4: Test with minimum position
python trader.py --private-key 0x... --max-position 1 --min-position 1

# Step 5: Full live trading
export POLYMARKET_PRIVATE_KEY=0x...
python trader.py --entry 0.38 --exit 0.68 --max-position 50
```

---

## Changes Made (2026-04-10)

### Strategy / ROI fixes

Analysis of 57 observed markets revealed two major ROI drains:

**1. Stop-loss was firing too late due to REST polling latency.**
The stop_loss was configured at 0.20 but the 3-second poll interval meant actual fill prices were 0.07–0.10 (near-zero). Combined with the strategy thesis ("markets overshoot, then reprice"), an immediate stop_loss directly contradicts the edge. Changed:
- `StrategyConfig.stop_loss_after_secs` default: `0` → `60` (only fires in final 60s of window)
- CLI default for `--stop-loss-after` updated to match in both `observer.py` and `trader.py`

**2. No minimum entry price floor.**
Entries at sub-0.15 prices (markets already nearly resolved) were 100% losing trades. Added:
- `StrategyConfig.min_entry_price = 0.15` — new field, `evaluate_entry` rejects any ask below this
- `--min-entry` CLI flag added to both scripts (default 0.15, pass 0 to disable)

### Database / performance fixes

- **WAL mode** — `PRAGMA journal_mode=WAL` + `synchronous=NORMAL` on every new connection. Faster writes, concurrent reads don't block.
- **Composite index** — `idx_ticks_slug_ts ON price_ticks(slug, timestamp)` added. The swing analysis correlated subqueries were doing full scans; this covers them.
- **Batched tick commits** — `insert_tick` no longer commits after every row. Commits every 10 ticks via `_pending_ticks` counter. `flush_ticks()` added and called at window close and shutdown. Trade/market inserts still commit immediately.

### Code quality fixes

- **`price_source` now accurate** — `_get_prices()` return type changed from 2-tuple to 3-tuple `(up, down, source)`. Base observer returns `"rest"`; `LiveObserver` returns `"ws"` when WebSocket data is used. The `price_source` column was previously always `'rest'` even in live mode.
- **`_entry_signal`/`_exit_signal` removed** — These were instance variables set immediately before calling `execute_buy`/`execute_sell` as a side-channel for passing context. Replaced with an explicit `context: Optional[dict]` parameter on both methods. Thread-safe and readable.

---

## What Still Needs to Be Done

### Must-do before trusting live trading

1. **Test against mainnet** — trader.py has never been run. Do a full end-to-end test with `--max-position 1`. Verify:
   - `setup_keys()` creates valid credentials
   - `build_client()` authenticates successfully
   - `execute_buy()` places a real order and returns an `orderID`
   - Heartbeat keeps orders alive
   - `execute_sell()` / `cancel_market()` work correctly

2. **Verify WebSocket message format** — The field names in `PriceWebSocket._handle()` (`event_type`, `asset_id`, `bids`, `asks`, `changes`, `side`, `price`, `size`) are based on documented examples but haven't been validated against the live feed. Connect and `print()` raw messages to confirm before relying on them.

3. **Tick size handling** — `create_order()` auto-validates price against market tick size. If orders fail with a price error, round entry/exit prices to the tick size:
   ```python
   tick = float(self.clob.get_tick_size(token_id))
   price = round(round(price / tick) * tick, 10)
   ```

### Nice-to-have improvements

4. **Fill confirmation** — Currently optimistic (local state updated immediately on order placement). The `live_trades.filled_price` column is NULL until confirmed. Add polling of `clob.get_order(order_id)` and call `db.update_live_trade_fill(order_id, filled_price)` when status is `MATCHED`.

5. **USDC balance check on startup** — `LiveObserver.run()` should check balance before starting:
   ```python
   from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
   bal = self.clob.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
   logging.info(f"USDC balance: {bal}")
   ```

6. **Paper mode in trader.py** — Add `--paper` flag to `trader.py` that uses WebSocket prices but `PaperTrader` for execution. Faster paper testing than `observer.py`'s 3s REST polling since WebSocket gives sub-second updates.

7. **README.md update** — Currently describes the old single-file architecture. Needs updating for the two-file split and `trader.py` setup instructions.

---

## Database Schema

All data in `polymarket_observer.db` (SQLite), shared by both scripts.

```sql
markets         slug, window_start/end_ts, up/down_token_id, btc_open/close_price, resolution
price_ticks     per-poll bid/ask/mid for UP+DOWN sides, btc_spot, btc_delta, price_source
paper_trades    simulated buy/sell: price, size, reason, pnl, bankroll_after
live_trades     real orders: order_id, requested_price, filled_price (NULL), size_usdc, reason
strategy_config last-used StrategyConfig as JSON (id=1 always)
```

---

## Key Logic: BTC Direction Guard on Stop-Loss

Added in `evaluate_exit()` in `observer.py`. When the bid drops below `stop_loss` threshold, before firing the stop we check whether BTC is moving in our favor:

```python
btc_confirms = (side == "down" and btc_delta < 0) or \
               (side == "up"   and btc_delta > 0)
if not btc_confirms:
    return "stop_loss"
```

If BTC is falling and we're holding DOWN, the price dip is likely mid-window noise — the market is still likely to resolve DOWN. Suppressing the stop here avoids getting shaken out of winning positions.

---

## Decisions Made in This Session

- **Python over Rust** — bottleneck is network I/O and blockchain confirmation, not CPU. Python asyncio is sufficient and py-clob-client is better documented.
- **Two files over one** — observer.py stays clean/auth-free. trader.py imports from it.
- **REST fallback** — WebSocket is primary but the 3s REST poll is still the fallback. This means the poll_interval_secs config value only kicks in when WebSocket is unavailable.
- **Optimistic fills** — local position state updated immediately on order placement. Acceptable for v1; fill confirmation is a future improvement.
- **GTC for all orders** — both entries and exits use GTC. force_exit uses `price * 0.95` to improve fill odds near window close.
