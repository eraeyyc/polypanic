# Status

**Last updated:** 2026-04-10

## Current setup

Both sides bot, with tightened entry controls. Previous "down only" config is retired — the UP side losses turned out to be a stop-loss timing problem, not an inherent UP side weakness.

```bash
caffeinate -i ./run.sh observer.py \
  --entry 0.40 --exit 0.65 \
  --min-entry 0.15 \
  --stop-loss-after 60 \
  --bankroll 1000
```

`--stop-loss` is left off (disabled) until there's enough live data to evaluate whether it adds value at the new polling speed.

## What changed in the 2026-04-10 session

### ROI fixes
- **Stop-loss default changed** from firing immediately (`stop_loss_after_secs=0`) to only firing in the final 60 seconds (`stop_loss_after_secs=60`). The old behaviour was executing at 0.07–0.10 due to 3s REST poll latency rather than the intended 0.20, costing ~$1,055 across 57 markets.
- **Min entry price floor added** (`min_entry_price=0.15`). Sub-0.15 entries are near-dead markets where recovery to the exit threshold is unlikely — all were losing trades. New `--min-entry` CLI flag (default 0.15).

### Database / performance
- WAL mode + NORMAL sync enabled on every connection
- Composite index added: `price_ticks(slug, timestamp)` — speeds up swing analysis correlated subqueries
- Tick commits batched every 10 rows (was every insert). Trade and market records still commit immediately.

### Code quality
- `_get_prices()` now returns a 3-tuple `(up, down, source)` — `price_source` column in ticks DB now accurately reflects `'ws'` vs `'rest'`
- `_entry_signal`/`_exit_signal` side-channel instance variable pattern replaced with explicit `context=` parameter on `execute_buy`/`execute_sell`

## Known limitation — BTC price source mismatch

Polymarket resolves BTC 5-min markets using **Chainlink Data Streams** (feed ID `0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8`). Data Streams is a paid subscription — we don't have access.

The bot uses Coinbase/Kraken/Binance/CoinGecko spot prices instead. Consequences:
- Resolution direction predictions in `_finalize_market()` can be wrong
- `btc_open_price` / `btc_close_price` in the DB may not match Polymarket's oracle exactly
- The BTC direction guard on stop-loss uses the same imprecise price

**For paper trading this is acceptable.** Before going live, evaluate Chainlink Data Streams: https://chain.link/contact?ref_id=datastreams

## What's broken / not yet tested

- Live trading not tested against mainnet
- WebSocket message field names unverified against live feed
- Fill confirmation is optimistic (`live_trades.filled_price` stays NULL)
- USDC balance check missing on `LiveObserver` startup
- `dashboard.py` has bugs and is not actively used

## What's next

- Run both-sides bot for 1–2 days with new settings; check that sub-0.15 entries are gone and stop-loss fires less
- Run `--analyze` after 100+ markets to confirm edge is holding
- If results are good, test live trader against mainnet with `--max-position 1`
- Evaluate Chainlink Data Streams cost before scaling up live trading
- Consider max-loss-per-session cutoff to protect bankroll
