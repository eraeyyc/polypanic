# Status

**Last updated:** 2026-04-09

## Current setup

Running **Down Only** bot only. Both Sides bot is retired — UP side losses were negating DOWN gains.

```bash
caffeinate -i ./run.sh observer.py --db polymarket_down.db --only-side down \
  --entry 0.40 --exit 0.65 --stop-loss 0.15 --stop-loss-after 20 \
  --entry-delay 5 --btc-momentum 50 --bankroll 1000
```

## What we did this session

### Strategy changes (based on data analysis)
- Switched to **Down Only** — DOWN +$124 vs Both Sides +$106 over the same 8 markets; UP legs were drag
- Entry back to **0.40** (was 0.45) — lower entry = more shares per dollar = bigger wins
- Stop-loss price lowered to **0.15** (was 0.20) — more room for mid-window price dips before stopping
- BTC momentum filter raised to **50** (was 30) — avoid DOWN entries when BTC is already running up
- **BTC direction guard on stop-loss** (new logic in `evaluate_exit`): if BTC is falling while holding DOWN (or rising while holding UP), suppress the stop-loss — the dip is likely temporary and the market is confirming our direction. Only fires when BTC is moving *against* the position.

### Dashboard (built and abandoned)
- Built a Flask dashboard (`dashboard.py`) with live tick feed, JSON polling endpoints, collapsible controls
- Had bugs (wrong DB column name `btc_spot` vs `btc_spot_price`, stats grid not updating)
- User reverted to terminal — dashboard is still in the repo but not in active use

### Earlier this session
- Added `--stop-loss-after N` flag: only fire stop-loss in final N seconds of window
- Fixed critical stop-loss re-entry loop: after stop-loss fires, `_stopped_out` blocks re-entry for that side/window
- Added `run.sh` — activates `.venv` so `python` resolves correctly
- Added `--entry-delay`, `--btc-momentum`, `--hold-threshold` flags
- Added live colored ROI display per tick and window P&L summary on close
- Fixed ANSI codes leaking into `observer.log`
- Fixed thread-safety race in `PriceWebSocket.get_prices`
- Added `_sell_attempted` guard in `LiveTrader` to prevent duplicate sell orders

## Known limitation — BTC price source mismatch

Polymarket resolves BTC 5-min markets using **Chainlink Data Streams** (feed ID `0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8`). Data Streams is a paid subscription product — we don't have access.

The bot uses Coinbase/Kraken/Binance/CoinGecko spot prices instead. Consequences:
- Resolution direction predictions in `_finalize_market()` can be wrong
- `btc_open_price` / `btc_close_price` in the DB may not match Polymarket's oracle exactly
- The BTC direction guard on stop-loss uses the same imprecise price — good enough for paper trading

**For paper trading this is acceptable.** Before going live, get a Data Streams quote: https://chain.link/contact?ref_id=datastreams

## What's broken / not yet tested

- Live trading not tested against mainnet
- WebSocket message field names unverified against live feed
- Fill confirmation still optimistic (`live_trades.filled_price` stays NULL)
- USDC balance check missing on `LiveObserver` startup
- Dashboard has bugs (see above) — not actively used

## What's next

- Let Down Only bot run for 1-2 days with new settings and check results
- Run `python observer.py --analyze` after 100+ markets to confirm edge is holding
- If stop-loss guard works well, consider raising exit threshold slightly (0.68?) to capture bigger swings
- Consider max-loss-per-session cutoff to protect bankroll
- Evaluate Chainlink Data Streams cost before going live
