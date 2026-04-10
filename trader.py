#!/usr/bin/env python3
"""
Polymarket BTC 5-Min Live Trader

Extends the observer with real CLOB order execution and a WebSocket price
feed for sub-second latency. Run observer.py first to collect data and
validate the strategy in paper mode before using this.

Setup (one-time):
    pip install py-clob-client websockets
    python trader.py --setup-keys --private-key 0x...

Run (live):
    python trader.py --private-key 0x...
    python trader.py --private-key 0x... --entry 0.38 --exit 0.68 --max-position 10

The private key can also be set via the POLYMARKET_PRIVATE_KEY environment
variable to avoid it appearing in shell history.

Fees (as of 2026-03-30): crypto taker 0.072%, maker rebate 20%.
Factor this into your exit threshold — e.g. on a $50 position at 0.40 entry
sold at 0.68, fee ≈ $0.034. Still very profitable if the edge is real.
"""

import os
import sys
import json
import time
import logging
import asyncio
import threading
import argparse
import signal
from pathlib import Path
from typing import Optional

# ── Shared foundation from observer ──────────────────────────────────────────
from observer import (
    StrategyConfig,
    Database,
    PaperTrader,
    Position,
    Observer,
    CLOB_API,
    analyze,
)

# ── py-clob-client (required for live trading) ────────────────────────────────
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    HAS_CLOB = True
except ImportError:
    HAS_CLOB = False

# ── websockets (required for real-time price feed) ────────────────────────────
try:
    import websockets
    HAS_WS = True
except ImportError:
    HAS_WS = False


WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


# ─── WebSocket Price Feed ─────────────────────────────────────────────────────

class _TokenBook:
    """
    In-memory order book for one token.
    Maintained from CLOB WebSocket snapshots and delta updates.
    """

    __slots__ = ("_bids", "_asks")

    def __init__(self):
        self._bids: dict[float, float] = {}  # price → size
        self._asks: dict[float, float] = {}

    def snapshot(self, bids: list, asks: list):
        """Replace entire book from a 'book' WebSocket event."""
        self._bids = {
            float(b["price"]): float(b["size"])
            for b in bids if float(b["size"]) > 0
        }
        self._asks = {
            float(a["price"]): float(a["size"])
            for a in asks if float(a["size"]) > 0
        }

    def apply_change(self, side: str, price: float, size: float):
        """Apply a single level change from a 'price_change' event."""
        book = self._bids if side == "BUY" else self._asks
        if size == 0:
            book.pop(price, None)
        else:
            book[price] = size

    @property
    def best_bid(self) -> float:
        return max(self._bids, default=0.0)

    @property
    def best_ask(self) -> float:
        return min(self._asks, default=0.0)


class PriceWebSocket:
    """
    Real-time price feed from the Polymarket CLOB WebSocket.

    Runs in a background thread. Thread-safe: call get_prices() from
    anywhere. Returns None if data is stale (>STALE_SEC old) so the
    caller can fall back to REST.

    Subscription flow:
        ws.subscribe(["token_id_1", "token_id_2"])
        ...
        prices = ws.get_prices("token_id_1")  # {"best_bid": 0.45, "best_ask": 0.46}
    """

    STALE_SEC      = 5.0
    PING_INTERVAL  = 45.0
    RECONNECT_WAIT = 2.0

    def __init__(self):
        self._books:      dict[str, _TokenBook] = {}
        self._timestamps: dict[str, float]      = {}   # token_id → last update
        self._lock        = threading.Lock()
        self._token_ids:  list[str]             = []
        self._thread:     Optional[threading.Thread] = None
        self._stop        = threading.Event()
        self._connected   = threading.Event()

    # ── Public API ────────────────────────────────────────────────────────────

    def subscribe(self, token_ids: list[str]):
        """Subscribe to these tokens. Stops any existing connection first."""
        self.stop()
        self._token_ids = list(token_ids)
        with self._lock:
            self._books.clear()
            self._timestamps.clear()
        self._start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=4.0)
        self._connected.clear()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def get_prices(self, token_id: str) -> Optional[dict]:
        """
        Return {"best_bid": float, "best_ask": float} if data is fresh.
        Returns None if stale or not yet received.
        """
        with self._lock:
            ts   = self._timestamps.get(token_id, 0.0)
            book = self._books.get(token_id)
            # Read best_bid / best_ask while holding the lock so the background
            # writer thread cannot mutate book._bids / book._asks concurrently.
            # Releasing the lock between fetching `book` and iterating its dicts
            # risks RuntimeError("dictionary changed size during iteration").
            if not book or time.time() - ts > self.STALE_SEC:
                return None
            bid = book.best_bid
            ask = book.best_ask
        if ask <= 0:
            return None
        return {"best_bid": bid, "best_ask": ask}

    # ── Background thread ─────────────────────────────────────────────────────

    def _start(self):
        if not HAS_WS:
            logging.warning(
                "websockets not installed — using REST fallback. "
                "Run: pip install websockets"
            )
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="ws-prices"
        )
        self._thread.start()

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._connect_loop())
        finally:
            loop.close()

    async def _connect_loop(self):
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    WS_MARKET_URL,
                    ping_interval=None,   # we send manual PINGs
                    open_timeout=10,
                ) as sock:
                    # Subscribe to both tokens for this market window
                    await sock.send(json.dumps({
                        "type":        "market",
                        "assets_ids":  self._token_ids,
                        "markets":     [],
                        "initial_dump": True,
                    }))
                    self._connected.set()
                    logging.info(
                        f"📡 WebSocket connected "
                        f"({len(self._token_ids)} tokens subscribed)"
                    )

                    last_ping = time.monotonic()
                    while not self._stop.is_set():
                        # Polymarket requires a PING every ~50s to stay alive
                        if time.monotonic() - last_ping > self.PING_INTERVAL:
                            await sock.send("PING")
                            last_ping = time.monotonic()
                        try:
                            raw = await asyncio.wait_for(sock.recv(), timeout=1.0)
                            self._handle(raw)
                        except asyncio.TimeoutError:
                            continue

            except Exception as exc:
                self._connected.clear()
                if not self._stop.is_set():
                    logging.warning(
                        f"WebSocket error: {exc}. "
                        f"Reconnecting in {self.RECONNECT_WAIT}s..."
                    )
                    await asyncio.sleep(self.RECONNECT_WAIT)

    def _handle(self, raw: str):
        """Parse a WebSocket message and update the in-memory order books."""
        try:
            data = json.loads(raw)
        except Exception:
            return   # "PONG" or other non-JSON keepalive

        events = data if isinstance(data, list) else [data]
        now    = time.time()

        with self._lock:
            for ev in events:
                asset_id = ev.get("asset_id", "")
                if not asset_id or asset_id not in self._token_ids:
                    continue

                etype = ev.get("event_type", "")

                if etype == "book":
                    # Full snapshot — replace the book entirely
                    if asset_id not in self._books:
                        self._books[asset_id] = _TokenBook()
                    self._books[asset_id].snapshot(
                        ev.get("bids", []), ev.get("asks", [])
                    )
                    self._timestamps[asset_id] = now

                elif etype == "price_change":
                    # Incremental update — apply each changed level
                    if asset_id not in self._books:
                        self._books[asset_id] = _TokenBook()
                    for change in ev.get("changes", []):
                        self._books[asset_id].apply_change(
                            change.get("side",  ""),
                            float(change.get("price", 0)),
                            float(change.get("size",  0)),
                        )
                    self._timestamps[asset_id] = now


# ─── Live Trading Engine ──────────────────────────────────────────────────────

class LiveTrader(PaperTrader):
    """
    Real order execution on the Polymarket CLOB.

    Overrides execute_buy() and execute_sell() to place GTC limit orders.
    All entry/exit decision logic is inherited from PaperTrader unchanged.

    Heartbeat: Polymarket auto-cancels ALL open orders if no heartbeat is
    received within 10 seconds. Call start_heartbeat() before trading and
    stop_heartbeat() on shutdown.

    Order sizes: OrderArgs.size is in SHARES (not USDC). We compute:
        shares = usdc_amount / price
    which matches how PaperTrader already tracks positions.
    """

    def __init__(self, config: StrategyConfig, db: Database, clob: ClobClient):
        super().__init__(config, db)
        self.clob = clob

        # "slug:side" → {"order_id": str, "type": "buy"|"sell"}
        self._pending: dict[str, dict] = {}

        # Keys where a SELL order was attempted but failed.  We track these to
        # prevent the main loop from re-triggering execute_sell every tick after
        # a transient API error, which would place duplicate real sell orders.
        self._sell_attempted: set[str] = set()

        # slug → {"up": token_id, "down": token_id}
        self._tokens: dict[str, dict] = {}

        # Heartbeat state
        self._hb_id:     Optional[str]             = None
        self._hb_stop    = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None

    # ── Market registration ───────────────────────────────────────────────────

    def register_market(self, slug: str, up_token: str, down_token: str):
        """Called by LiveObserver at the start of each new market window."""
        self._tokens[slug] = {"up": up_token, "down": down_token}

    def _token(self, slug: str, side: str) -> Optional[str]:
        return self._tokens.get(slug, {}).get(side)

    def _key(self, slug: str, side: str) -> str:
        return f"{slug}:{side}"

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    def start_heartbeat(self):
        """Start the background heartbeat thread. Must be called before trading."""
        self._hb_stop.clear()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="heartbeat"
        )
        self._hb_thread.start()
        logging.info("💓 Heartbeat started (5s interval)")

    def stop_heartbeat(self):
        self._hb_stop.set()

    def _heartbeat_loop(self):
        # Wait 5s between beats. _hb_stop.wait() returns True if stop is set,
        # so the loop exits immediately when stop_heartbeat() is called.
        while not self._hb_stop.wait(5.0):
            try:
                resp        = self.clob.post_heartbeat(self._hb_id)
                self._hb_id = resp.get("heartbeat_id")
            except Exception as exc:
                logging.warning(f"Heartbeat error: {exc}")

    # ── Order placement ───────────────────────────────────────────────────────

    def execute_buy(self, slug: str, side: str, price: float, now: float,
                    context=None):
        token_id = self._token(slug, side)
        if not token_id:
            logging.error(f"No token registered for {slug}:{side}")
            return

        size_usdc = min(self.config.max_position_size, self.bankroll)
        if size_usdc < self.config.min_position_usdc:
            logging.debug(f"Position too small (${size_usdc:.2f}), skipping")
            return

        shares = round(size_usdc / price, 4)

        try:
            signed = self.clob.create_order(
                OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
            )
            resp     = self.clob.post_order(signed, OrderType.GTC)
            order_id = resp.get("orderID", "unknown")
        except Exception as exc:
            logging.error(f"BUY order failed ({side} @ ${price:.2f}): {exc}")
            return

        self._pending[self._key(slug, side)] = {"order_id": order_id, "type": "buy"}
        self.db.insert_live_trade(
            slug, now, side, "buy", order_id, price, None, size_usdc, "entry"
        )
        logging.info(
            f"🟢 LIVE BUY  {side.upper()} @ ${price:.2f} | "
            f"{shares:.2f} shares (${size_usdc:.2f}) | id={order_id[:16]}..."
        )
        # Update local state optimistically — assumes fill at the ask price.
        # If the order doesn't fill (e.g., price moved away), it will be
        # cancelled by cancel_market() when the window closes.
        super().execute_buy(slug, side, price, now, context=context)

    def execute_sell(self, slug: str, side: str, price: float,
                     reason: str, now: float, context=None):
        key     = self._key(slug, side)
        pending = self._pending.get(key)

        # If the BUY order is still pending (not confirmed filled), cancel it.
        # There's no actual position to sell yet.
        if pending and pending["type"] == "buy":
            self._cancel_order(slug, side)
            return

        # A previous SELL attempt failed (API error).  Don't retry automatically
        # — this avoids placing duplicate real sell orders on every subsequent
        # tick.  The position will be resolved by cancel_market() at window close.
        if key in self._sell_attempted:
            logging.debug(
                f"Skipping repeat sell attempt for {slug}:{side} "
                f"(previous attempt failed)"
            )
            return

        token_id  = self._token(slug, side)
        positions = [p for p in self.get_positions(slug) if p.side == side]
        if not token_id or not positions:
            return

        pos = positions[0]

        # For force_exit we accept a slightly lower price to guarantee a fill
        # before the market closes; for normal exits we target the current bid.
        sell_price = (
            round(max(price * 0.95, 0.01), 4)
            if reason == "force_exit"
            else price
        )

        try:
            signed = self.clob.create_order(
                OrderArgs(token_id=token_id, price=sell_price,
                          size=round(pos.shares, 4), side=SELL)
            )
            resp     = self.clob.post_order(signed, OrderType.GTC)
            order_id = resp.get("orderID", "unknown")
        except Exception as exc:
            logging.error(f"SELL order failed ({side} @ ${sell_price:.2f}): {exc}")
            # Mark so the main loop doesn't re-trigger on the next tick.
            self._sell_attempted.add(key)
            return

        self._pending[key] = {"order_id": order_id, "type": "sell"}
        self.db.insert_live_trade(
            slug, now, side, "sell", order_id, sell_price,
            None, pos.shares * sell_price, reason
        )
        logging.info(
            f"🔴 LIVE SELL {side.upper()} @ ${sell_price:.2f} | "
            f"reason={reason} | id={order_id[:16]}..."
        )
        super().execute_sell(slug, side, sell_price, reason, now, context=context)
        self._pending.pop(key, None)
        self._sell_attempted.discard(key)  # clean up on success

    def _cancel_order(self, slug: str, side: str):
        key     = self._key(slug, side)
        pending = self._pending.pop(key, None)
        if not pending:
            return
        try:
            # py-clob-client: cancel(order_id) takes a plain string
            self.clob.cancel(pending["order_id"])
            logging.info(
                f"❌ Cancelled {pending['type'].upper()} {side.upper()} "
                f"id={pending['order_id'][:16]}..."
            )
        except Exception as exc:
            logging.warning(f"Cancel failed (may already be filled): {exc}")

    def cancel_market(self, slug: str):
        """Cancel all open orders for this market window (called on close)."""
        for side in ("up", "down"):
            # Clear any failed-sell flag so the next window starts clean.
            self._sell_attempted.discard(self._key(slug, side))
            self._cancel_order(slug, side)


# ─── Live Observer ────────────────────────────────────────────────────────────

class LiveObserver(Observer):
    """
    Observer subclass that wires in LiveTrader and PriceWebSocket.

    Overrides:
      _mode_label()       — displays "LIVE TRADING" warning
      _on_new_market()    — registers tokens with LiveTrader, re-subscribes WebSocket
      _get_prices()       — returns WebSocket data when fresh, REST otherwise
      _finalize_market()  — cancels open orders before resolving
      run()               — wraps super().run() with heartbeat start/stop
    """

    def __init__(self, config: StrategyConfig, db_path: str, clob: ClobClient):
        # Build the trader first so it owns the single DB connection for this run.
        trader = LiveTrader(config, Database(db_path), clob)

        # super().__init__ opens its own Database(db_path) and saves config to it.
        # We close that connection immediately after and use trader.db instead,
        # so only one SQLite connection is alive at a time.
        super().__init__(config, db_path, trader=trader)
        self.db.close()          # close the connection Observer just opened
        self.db     = trader.db  # use the one LiveTrader already holds
        self.trader = trader
        self.ws     = PriceWebSocket()

    def _mode_label(self) -> str:
        return "⚠️  LIVE TRADING — REAL MONEY ⚠️"

    def _on_new_market(self, slug: str, tokens: dict):
        self.trader.register_market(
            slug, tokens["up_token_id"], tokens["down_token_id"]
        )
        self.ws.subscribe([tokens["up_token_id"], tokens["down_token_id"]])

    def _get_prices(self, tokens: dict):
        up_ws = self.ws.get_prices(tokens["up_token_id"])
        dn_ws = self.ws.get_prices(tokens["down_token_id"])
        if up_ws and dn_ws:
            return up_ws, dn_ws, "ws"
        # WebSocket data stale or not yet connected — fall back to REST
        if up_ws or dn_ws:
            logging.debug("Partial WebSocket data, falling back to REST")
        return (
            self.poly.get_price(tokens["up_token_id"]),
            self.poly.get_price(tokens["down_token_id"]),
            "rest",
        )

    def _finalize_market(self, slug: str):
        # Cancel any open CLOB orders before resolving the window
        self.trader.cancel_market(slug)
        super()._finalize_market(slug)

    def run(self):
        self.trader.start_heartbeat()
        try:
            super().run()
        finally:
            self.trader.stop_heartbeat()
            self.ws.stop()
            logging.info("Heartbeat and WebSocket stopped.")


# ─── Auth Helpers ─────────────────────────────────────────────────────────────

def setup_keys(private_key: str, keys_file: str, chain_id: int = 137):
    """
    One-time setup: derive Polymarket API credentials from a wallet private key.
    Saves key/secret/passphrase to keys_file (chmod 600).

    The py-clob-client derives credentials deterministically from the private
    key via create_or_derive_api_creds() — running this again with the same
    key returns the same credentials.
    """
    if not HAS_CLOB:
        print("Error: py-clob-client not installed. Run: pip install py-clob-client")
        sys.exit(1)

    print("Deriving Polymarket API credentials from wallet...")

    # L1 client only — no creds needed yet, just the private key
    client = ClobClient(
        host=CLOB_API,
        key=private_key,
        chain_id=chain_id,
        signature_type=0,   # 0 = EOA (standard wallet)
    )
    creds = client.create_or_derive_api_creds()

    keys_path = Path(keys_file).expanduser()
    keys_path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "api_key":        creds.api_key,
        "api_secret":     creds.api_secret,
        "api_passphrase": creds.api_passphrase,
    }
    # Open with mode 0o600 at creation time so there is no window where the
    # file exists with world-readable permissions.  write_text() + chmod() has
    # a TOCTOU race: the file is readable by others between the two calls.
    fd = os.open(str(keys_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w") as f:
        json.dump(data, f, indent=2)

    print(f"✅ Credentials saved to {keys_path}")
    print(f"   api_key: {creds.api_key[:16]}...")
    print()
    print("Before trading you also need to approve USDC on Polygon once:")
    print("  from py_clob_client.clob_types import BalanceAllowanceParams, AssetType")
    print("  client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))")


def build_client(private_key: str, keys_file: str, chain_id: int = 137) -> "ClobClient":
    """
    Build a fully-authenticated ClobClient (L2) from a saved keys file.
    Raises FileNotFoundError if --setup-keys hasn't been run yet.
    """
    if not HAS_CLOB:
        raise RuntimeError(
            "py-clob-client not installed. Run: pip install py-clob-client"
        )

    keys_path = Path(keys_file).expanduser()
    if not keys_path.exists():
        raise FileNotFoundError(
            f"Keys file not found: {keys_path}\n"
            "Run: python trader.py --setup-keys --private-key 0x..."
        )

    saved = json.loads(keys_path.read_text())
    creds = ApiCreds(
        api_key        = saved["api_key"],
        api_secret     = saved["api_secret"],
        api_passphrase = saved["api_passphrase"],
    )
    return ClobClient(
        host           = CLOB_API,
        key            = private_key,
        chain_id       = chain_id,
        creds          = creds,
        signature_type = 0,   # EOA
    )


# ─── Entry Point ──────────────────────────────────────────────────────────────

def _warn_if_key_in_args(args) -> None:
    """
    Print a security warning if the private key was passed as a CLI argument
    rather than via the POLYMARKET_PRIVATE_KEY environment variable.

    CLI args are visible to all users on the machine via `ps aux` and are
    stored in most shell histories.  The env-var path avoids both.
    """
    if not os.environ.get("POLYMARKET_PRIVATE_KEY") and args.private_key:
        print(
            "\n⚠️  Security warning: --private-key was passed as a command-line "
            "argument.\n"
            "   This is visible in `ps aux` output and may be saved in shell "
            "history.\n"
            "   Use the environment variable instead:\n\n"
            "       export POLYMARKET_PRIVATE_KEY=0x...\n"
            "       python trader.py\n",
            file=sys.stderr,
        )


def main():
    parser = argparse.ArgumentParser(
        description="Polymarket BTC 5-Min Live Trader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # One-time key setup
  python trader.py --setup-keys --private-key 0x...

  # Start live trading with default thresholds
  python trader.py --private-key 0x...

  # Custom thresholds, small position sizes to test
  python trader.py --private-key 0x... --entry 0.38 --exit 0.68 --max-position 5

  # Use env var instead of passing key on command line
  export POLYMARKET_PRIVATE_KEY=0x...
  python trader.py
        """,
    )

    # Auth
    parser.add_argument(
        "--private-key",
        default=os.environ.get("POLYMARKET_PRIVATE_KEY"),
        help="Wallet private key (or set POLYMARKET_PRIVATE_KEY env var)",
    )
    parser.add_argument(
        "--keys-file",
        default="~/.polypanic/keys.json",
        help="Path to saved API credentials (default: ~/.polypanic/keys.json)",
    )
    parser.add_argument(
        "--setup-keys",
        action="store_true",
        help="Derive API credentials from private key and save them, then exit",
    )
    parser.add_argument(
        "--chain-id", type=int, default=137,
        help="Polygon chain ID (default: 137 = mainnet)",
    )

    # Strategy
    parser.add_argument("--entry",        type=float, default=0.40)
    parser.add_argument("--exit",         type=float, default=0.65)
    parser.add_argument("--stop-loss",    type=float, default=0.0)
    parser.add_argument("--stop-loss-after", type=int, default=60,
                        help="Only trigger stop_loss in final N seconds of window (default 60, 0=anytime)")
    parser.add_argument("--min-entry",    type=float, default=0.15,
                        help="Reject entries below this price (default 0.15, 0=disabled)")
    parser.add_argument("--max-position", type=float, default=50.0,
                        help="Max USDC per side per market (default: 50)")
    parser.add_argument("--min-position", type=float, default=5.0,
                        help="Min USDC per trade — avoid dust orders (default: 5)")
    parser.add_argument("--bankroll",     type=float, default=1000.0,
                        help="Starting bankroll for P&L tracking (default: 1000)")
    parser.add_argument("--single-side",  action="store_true",
                        help="Only one position per market (default: both sides allowed)")

    # Misc
    parser.add_argument("--db",        default="polymarket_observer.db",
                        help="SQLite database path (shared with observer.py)")
    parser.add_argument("--analyze",   action="store_true",
                        help="Run analysis on collected data, then exit")
    parser.add_argument("--log-level", default="INFO")

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("trader.log"),
        ],
    )

    # ── --analyze ─────────────────────────────────────────────────────────────
    if args.analyze:
        analyze(args.db)
        return

    # ── --setup-keys ──────────────────────────────────────────────────────────
    if args.setup_keys:
        if not args.private_key:
            print("Error: --private-key (or POLYMARKET_PRIVATE_KEY) required for --setup-keys")
            sys.exit(1)
        _warn_if_key_in_args(args)
        setup_keys(args.private_key, args.keys_file, args.chain_id)
        return

    # ── Live trading ──────────────────────────────────────────────────────────
    if not args.private_key:
        print(
            "Error: --private-key (or POLYMARKET_PRIVATE_KEY env var) is required.\n"
            "Run with --setup-keys first if you haven't set up credentials."
        )
        sys.exit(1)

    _warn_if_key_in_args(args)
    clob = build_client(args.private_key, args.keys_file, args.chain_id)

    config = StrategyConfig(
        entry_threshold          = args.entry,
        exit_threshold           = args.exit,
        stop_loss                = args.stop_loss,
        stop_loss_after_secs     = args.stop_loss_after,
        min_entry_price          = args.min_entry,
        max_position_size        = args.max_position,
        min_position_usdc        = args.min_position,
        starting_bankroll        = args.bankroll,
        allow_both_sides         = not args.single_side,
    )

    LiveObserver(config, db_path=args.db, clob=clob).run()


if __name__ == "__main__":
    main()
