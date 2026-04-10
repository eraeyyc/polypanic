# Status

**Last updated:** 2026-04-09

## What we did this session

- Added `run.sh` — activates `.venv` so `python` resolves correctly
- Added `--entry-delay` flag: wait N seconds into a window before buying anything
- Added `--btc-momentum` flag: skip buying a side if BTC has moved $X against it from window open
- Added `--hold-threshold` flag: skip force_exit near close if BTC has moved $X in your favor — let the market resolve at $1.00 instead
- Added live colored ROI display on every tick (green/red, bold) and window P&L summary on market close
- Fixed ANSI codes leaking into `observer.log` (file handler now strips them)
- Fixed thread-safety race in `PriceWebSocket.get_prices` (read bid/ask inside lock)
- Added `_sell_attempted` set in `LiveTrader` to prevent duplicate sell orders after API errors
- Fixed TOCTOU race in `setup_keys` (atomic 0o600 file creation)
- Added security warning when `--private-key` is passed as CLI arg
- Fixed `LiveObserver` DB connection leak

## Current recommended run command

```bash
./run.sh observer.py --stop-loss 0.10 --entry-delay 5 --btc-momentum 30 --hold-threshold 15
```

## What's broken / not yet tested

- Live trading not tested against mainnet
- WebSocket message field names unverified against live feed
- Fill confirmation still optimistic (`live_trades.filled_price` stays NULL until manually confirmed)
- USDC balance check missing on `LiveObserver` startup

## What's next

- Run paper trader for several days with the new flags and compare P&L vs baseline
- Tune `--btc-momentum` and `--hold-threshold` based on observed data
- Check `--analyze` output after 100+ markets to see if hit rate improves
- Consider adding a max-loss-per-session cutoff to protect bankroll
