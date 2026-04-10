# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install requests py-clob-client websockets flask

# Run observer (paper trading + data collection, no auth needed)
python observer.py
python observer.py --entry 0.38 --exit 0.68 --min-entry 0.15 --bankroll 500

# Analyze collected data
python observer.py --analyze

# One-time live trading setup (generates ~/.polypanic/keys.json)
python trader.py --setup-keys --private-key 0x...

# Run live trader
python trader.py --private-key 0x...
python trader.py --max-position 5 --entry 0.38 --exit 0.68
# or via env var:
export POLYMARKET_PRIVATE_KEY=0x...
python trader.py
```

No build step, no test suite — these are single-file scripts. Syntax-check with:
```bash
python3 -c "import ast; ast.parse(open('observer.py').read())"
python3 -c "import ast; ast.parse(open('trader.py').read())"
```

## Architecture

Two files with a clean inheritance relationship:

**`observer.py`** — self-contained, no auth. Run this first to collect data and validate the strategy in paper mode before touching real money.
- `StrategyConfig` — all tunable parameters (entry/exit thresholds, position sizing, timing). Key fields:
  - `entry_threshold` (default 0.40) — buy when ask ≤ this
  - `min_entry_price` (default 0.15) — reject entries below this; avoids near-dead markets
  - `exit_threshold` (default 0.65) — sell when bid ≥ this
  - `stop_loss` (default 0.0 = disabled) — sell if bid drops to this
  - `stop_loss_after_secs` (default 60) — only fire stop_loss in the final N seconds of the window
  - `max_entry_spread` (default 0.06) — reject entry if bid/ask spread is too wide
- `PolymarketClient` — read-only REST client. Market slugs are deterministic: `btc-updown-5m-{floor(unix_ts/300)*300}`, so no scanning needed.
- `BTCPriceClient` — BTC spot price with fallback chain: Coinbase → Kraken → Binance → CoinGecko
- `Database` — SQLite with 5 tables: `markets`, `price_ticks`, `paper_trades`, `live_trades`, `strategy_config`. WAL mode + NORMAL sync enabled. Tick inserts are batched (commit every 10); trade/market inserts commit immediately. `flush_ticks()` called at window close and shutdown.
- `PaperTrader` — simulated execution. Tracks positions per slug, evaluates entry/exit conditions, handles resolution if still holding at window end.
- `Observer` — main loop. Designed for subclassing: override `_get_prices()` for non-REST sources, `_on_new_market()` for per-window setup hooks, `_mode_label()` for display. Accepts an optional `trader=` param so subclasses can inject a different trader.
- `analyze()` — post-hoc swing analysis and paper trading results from the DB

**`trader.py`** — imports from `observer.py`, adds live execution:
- `_TokenBook` / `PriceWebSocket` — background asyncio thread maintaining in-memory order books from the Polymarket WebSocket (`wss://ws-subscriptions-clob.polymarket.com/ws/market`). Handles `book` (full snapshot) and `price_change` (delta) events. `get_prices()` returns `None` if data is >5s stale so the caller falls back to REST.
- `LiveTrader(PaperTrader)` — overrides only `execute_buy`/`execute_sell` with real CLOB orders. `OrderArgs.size` is in **shares** (compute `shares = usdc / price`). Runs a heartbeat thread every 5s — Polymarket cancels all open orders if no heartbeat within 10s.
- `LiveObserver(Observer)` — wires the above together. Overrides `_get_prices()` (WebSocket-first, REST fallback — returns 3-tuple `(up_prices, down_prices, source)`), `_on_new_market()` (register tokens + subscribe WebSocket), `_finalize_market()` (cancel open orders before resolving).
- `setup_keys()` / `build_client()` — one-time credential derivation via `create_or_derive_api_creds()`. Saves `api_key`/`api_secret`/`api_passphrase` to `~/.polypanic/keys.json`.

## Database Schema

All data in `polymarket_observer.db` (SQLite), created on first run, shared by both scripts.

```sql
markets         slug, window_start/end_ts, up/down_token_id, btc_open/close_price, resolution
price_ticks     per-poll bid/ask/mid for UP+DOWN sides, btc_spot_price, btc_delta_from_open,
                price_source ('rest'|'ws'), up_spread, down_spread, up_change_10s, down_change_10s
paper_trades    simulated buy/sell: price, size, reason, pnl, bankroll_after,
                shares, seconds_remaining, spread_at_trade, signal_json
live_trades     real orders: order_id, requested_price, filled_price (NULL until confirmed), size_usdc, reason
strategy_config last-used StrategyConfig as JSON (id=1 always)
```

Indexes: `idx_ticks_slug`, `idx_ticks_time`, `idx_ticks_slug_ts` (composite — used by swing analysis), `idx_trades_slug`, `idx_live_trades_slug`.

## Key API Details

- **CLOB API:** `https://clob.polymarket.com` — order book, pricing, order placement
- **Gamma API:** `https://gamma-api.polymarket.com` — market/event discovery
- **WebSocket:** `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- **Auth:** `signature_type=0` for EOA wallets. L2 credentials derived from private key via `create_or_derive_api_creds()` (not `create_api_key()`). Keys saved as `api_key`/`api_secret`/`api_passphrase` (not `key`/`secret`/`passphrase`).
- **Orders:** `OrderArgs.size` is in **shares**, not USDC. Compute `shares = usdc_amount / price`. If price fails tick size validation, round: `round(round(price / tick) * tick, 10)`.
- **Cancel:** `clob.cancel(order_id)` — plain string, not a dict
- **Heartbeat (critical):** `clob.post_heartbeat(heartbeat_id)` — first call pass `None`, use returned ID for subsequent calls. Must fire within 10s or Polymarket cancels ALL open orders.
- **USDC approval** required once before first trade: `client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))`
- **Fees (from 2026-03-30):** crypto taker 0.072%, maker rebate 20%

**WebSocket subscription message:**
```json
{"type": "market", "assets_ids": ["token_id_1", "token_id_2"], "markets": [], "initial_dump": true}
```
Send `"PING"` string every ~45s to keep alive. Events received: `event_type: "book"` (full snapshot with `bids`/`asks` arrays of `{"price", "size"}`) and `event_type: "price_change"` (delta with `changes: [{"side": "BUY"|"SELL", "price", "size"}]` — size=0 means level removed).

## Architectural Decisions

- **Two files over one** — `observer.py` stays auth-free and self-contained. `trader.py` imports from it.
- **Optimistic fills** — local position state updated immediately on order placement; `filled_price` stays NULL until confirmed. Acceptable for v1.
- **GTC for all orders** — both entries and exits. `force_exit` uses `price * 0.95` to improve fill odds near window close.
- **REST fallback** — WebSocket is primary; REST polls only when WebSocket data is >5s stale. The `poll_interval_secs` config only kicks in during WebSocket unavailability.
- **context= param on execute_buy/execute_sell** — market signal snapshot (spread, BTC delta, seconds remaining, etc.) passed explicitly as a dict, not via instance variables.

## Strategy

Buy UP or DOWN when ask ≤ entry_threshold AND ask ≥ min_entry_price. Sell when bid ≥ exit_threshold. Never hold through resolution (unless hold_through_close_btc_threshold is set and BTC has moved strongly in your favor). Both sides can be held simultaneously. The edge is retail sentiment overshoot — the market reprices based on crowd psychology, often when BTC has barely moved.

Exit reasons logged in DB: `exit_target` (good), `stop_loss`, `force_exit` (approaching close), `resolution` (held to end — usually bad).

## What Still Needs Doing

See `HANDOFF.md` for full context. Key gaps:
1. Live trading not yet tested against mainnet
2. WebSocket message field names unverified against live feed
3. Fill confirmation (currently optimistic — `live_trades.filled_price` stays NULL)
4. USDC balance check on `LiveObserver` startup
