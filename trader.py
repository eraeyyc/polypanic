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

Fees are modeled from the documented fee-rate fields returned by Polymarket.
Do not trust static comments or rough hand calculations here; live fills are
recorded with fee-aware accounting and should be validated against exchange
truth before risking capital.
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
import uuid
import math
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

import requests

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
    from py_clob_client.clob_types import (
        ApiCreds,
        BalanceAllowanceParams,
        AssetType,
        MarketOrderArgs,
        OpenOrderParams,
        OrderType,
        TradeParams,
    )
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
WS_USER_URL   = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
DATA_API      = "https://data-api.polymarket.com"


def _now_ts() -> float:
    return time.time()


def _round_down_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return round(price, 4)
    steps = math.floor((price / tick_size) + 1e-9)
    return round(steps * tick_size, 4)


def _round_up_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return round(price, 4)
    steps = math.ceil((price / tick_size) - 1e-9)
    return round(steps * tick_size, 4)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _parse_ts(value) -> float:
    try:
        return float(value)
    except Exception:
        return _now_ts()


@dataclass
class MarketConstraints:
    token_id: str
    tick_size: float
    min_order_size: float
    fee_rate_bps: int


@dataclass
class LiveOrderState:
    client_order_id: str
    slug: str
    market_id: str
    token_id: str
    side: str
    intent: str
    order_type: str
    tif: str
    requested_price: float
    requested_shares: float
    requested_notional: float
    fee_rate_bps: int
    created_at: float
    order_id: str = ""
    filled_shares: float = 0.0
    avg_fill_price: float = 0.0
    status: str = "pending_submit"
    error_text: str = ""
    updated_at: float = 0.0
    raw_json: str = ""

    def to_record(self) -> dict:
        record = asdict(self)
        if not record["updated_at"]:
            record["updated_at"] = self.created_at
        return record


@dataclass
class LivePositionState:
    slug: str
    token_id: str
    side: str
    shares: float
    avg_cost: float
    realized_pnl: float = 0.0
    total_fees: float = 0.0
    settlement_status: str = "open"
    updated_at: float = 0.0

    def to_record(self) -> dict:
        record = asdict(self)
        if not record["updated_at"]:
            record["updated_at"] = _now_ts()
        return record


class DataAPIClient:
    """Minimal Polymarket Data API client for current positions and price history."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "PolymarketLiveTrader/1.0"})

    def get_positions(self, user: str) -> list[dict]:
        try:
            resp = self.session.get(
                f"{DATA_API}/positions",
                params={"user": user, "sizeThreshold": 0},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as exc:
            logging.warning(f"Data API positions fetch failed: {exc}")
            return []

    def get_prices_history(
        self,
        token_id: str,
        interval: Optional[str] = None,
        fidelity: Optional[int] = None,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> list[dict]:
        params = {"market": token_id}
        if interval:
            params["interval"] = interval
        if fidelity is not None:
            params["fidelity"] = fidelity
        if start_ts is not None:
            params["startTs"] = start_ts
        if end_ts is not None:
            params["endTs"] = end_ts
        try:
            resp = self.session.get(
                f"{CLOB_API}/prices-history",
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "history" in data:
                return data["history"]
            return data if isinstance(data, list) else []
        except Exception as exc:
            logging.warning(f"Price history fetch failed for {token_id[:18]}...: {exc}")
            return []


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

    def __init__(self, event_callback=None):
        self._books:      dict[str, _TokenBook] = {}
        self._timestamps: dict[str, float]      = {}   # token_id → last update
        self._lock        = threading.Lock()
        self._token_ids:  list[str]             = []
        self._thread:     Optional[threading.Thread] = None
        self._stop        = threading.Event()
        self._connected   = threading.Event()
        self._event_callback = event_callback

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
                        "custom_feature_enabled": True,
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
                    changes = ev.get("price_changes", []) or ev.get("changes", [])
                    for change in changes:
                        change_asset = change.get("asset_id", asset_id)
                        if change_asset not in self._books:
                            self._books[change_asset] = _TokenBook()
                        self._books[change_asset].apply_change(
                            change.get("side",  ""),
                            float(change.get("price", 0)),
                            float(change.get("size",  0)),
                        )
                        self._timestamps[change_asset] = now

                elif etype == "best_bid_ask":
                    if asset_id not in self._books:
                        self._books[asset_id] = _TokenBook()
                    book = self._books[asset_id]
                    best_bid = _safe_float(ev.get("best_bid"))
                    best_ask = _safe_float(ev.get("best_ask"))
                    if best_bid > 0:
                        book._bids = {best_bid: 1.0}
                    if best_ask > 0:
                        book._asks = {best_ask: 1.0}
                    self._timestamps[asset_id] = now

                if etype in {
                    "best_bid_ask",
                    "last_trade_price",
                    "tick_size_change",
                    "market_resolved",
                } and self._event_callback:
                    try:
                        self._event_callback(ev)
                    except Exception as exc:
                        logging.warning(f"Market event callback failed: {exc}")


class UserWebSocket:
    """Authenticated user channel for order/trade lifecycle updates."""

    RECONNECT_WAIT = 2.0

    def __init__(self, creds: ApiCreds, event_callback):
        self._creds = creds
        self._event_callback = event_callback
        self._markets: list[str] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = threading.Event()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def subscribe(self, markets: list[str]):
        self.stop()
        self._markets = [m for m in markets if m]
        self._start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=4.0)
        self._connected.clear()

    def _start(self):
        if not HAS_WS or not self._markets:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="ws-user"
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
                async with websockets.connect(WS_USER_URL, ping_interval=20, open_timeout=10) as sock:
                    await sock.send(json.dumps({
                        "auth": {
                            "apiKey": self._creds.api_key,
                            "secret": self._creds.api_secret,
                            "passphrase": self._creds.api_passphrase,
                        },
                        "markets": self._markets,
                        "type": "user",
                    }))
                    self._connected.set()
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(sock.recv(), timeout=1.0)
                        try:
                            data = json.loads(raw)
                        except Exception:
                            continue
                        events = data if isinstance(data, list) else [data]
                        for ev in events:
                            if self._event_callback:
                                try:
                                    self._event_callback(ev)
                                except Exception as exc:
                                    logging.warning(f"User event callback failed: {exc}")
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                self._connected.clear()
                if not self._stop.is_set():
                    logging.warning(f"User WebSocket error: {exc}. Reconnecting in {self.RECONNECT_WAIT}s...")
                    await asyncio.sleep(self.RECONNECT_WAIT)


# ─── Live Trading Engine ──────────────────────────────────────────────────────

class LiveTrader:
    """Live execution engine with explicit desired/open/actual state."""

    def __init__(self, config: StrategyConfig, db: Database, clob: ClobClient):
        self.config = config
        self.db = db
        self.clob = clob
        self.data_api = DataAPIClient()

        self.desired_positions: dict[str, str] = {}  # slug:side -> reason
        self.open_orders: dict[str, LiveOrderState] = {}  # order_id/client_id -> state
        self.actual_positions: dict[str, LivePositionState] = {}  # slug:side -> state
        self.constraints: dict[str, MarketConstraints] = {}  # token_id -> constraints
        self._stopped_out: dict[str, set[str]] = {}  # slug -> sides stopped this window
        self._last_sell_time: dict[str, dict[str, float]] = {}  # slug -> side -> ts
        self._tokens: dict[str, dict] = {}
        self._asset_map: dict[str, tuple[str, str]] = {}
        self._market_ids: set[str] = set()
        self._market_data_ok = True
        self._market_data_failures = 0
        self._consecutive_errors = 0
        self._kill_switch = False

        self._hb_id: Optional[str] = None
        self._hb_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        self._reconcile_stop = threading.Event()
        self._reconcile_thread: Optional[threading.Thread] = None
        self._last_positions_sync = 0.0

        self._load_state_from_db()

    @property
    def bankroll(self) -> float:
        equity = self.config.starting_bankroll + self.realized_pnl()
        return max(0.0, equity - self.position_cost_basis() - self.reserved_notional())

    def realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self.actual_positions.values())

    def position_cost_basis(self) -> float:
        return sum(p.avg_cost * p.shares for p in self.actual_positions.values())

    def reserved_notional(self) -> float:
        total = 0.0
        for order in self.open_orders.values():
            if order.intent == "buy" and order.status not in {"cancelled", "filled", "failed", "confirmed"}:
                total += max(0.0, order.requested_notional)
        return total

    def total_live_exposure(self) -> float:
        return self.position_cost_basis() + self.reserved_notional()

    @staticmethod
    def _slug_window_end_ts(slug: str) -> float:
        """Extract the 5-minute window end timestamp from a slug like btc-updown-5m-1775904600."""
        try:
            return float(slug.rsplit("-", 1)[-1]) + 300.0
        except (ValueError, IndexError):
            return 0.0

    def _load_state_from_db(self):
        _terminal = {"cancelled", "failed", "filled", "confirmed"}
        _non_terminal = lambda s: s not in _terminal
        now = _now_ts()

        for row in self.db.get_live_orders():
            state = LiveOrderState(
                client_order_id=row["client_order_id"],
                order_id=row["order_id"] or "",
                slug=row["slug"],
                market_id=row["market_id"] or "",
                token_id=row["token_id"],
                side=row["side"],
                intent=row["intent"] or "",
                order_type=row["order_type"] or "",
                tif=row["tif"] or "",
                requested_price=row["requested_price"] or 0.0,
                requested_shares=row["requested_shares"] or 0.0,
                requested_notional=row["requested_notional"] or 0.0,
                filled_shares=row["filled_shares"] or 0.0,
                avg_fill_price=row["avg_fill_price"] or 0.0,
                fee_rate_bps=row["fee_rate_bps"] or 0,
                status=row["status"] or "unknown",
                error_text=row["error_text"] or "",
                created_at=row["created_at"] or now,
                updated_at=row["updated_at"] or now,
                raw_json=row["raw_json"] or "",
            )
            # Expire stale non-terminal orders from windows that have already closed.
            # These orders can never be filled and must not block future entries.
            if _non_terminal(state.status):
                window_end = self._slug_window_end_ts(state.slug)
                if window_end > 0 and now > window_end + 60:
                    logging.info(
                        f"Startup: expiring stale {state.status} order {state.client_order_id} "
                        f"for closed window {state.slug}"
                    )
                    state.status = "cancelled"
                    state.error_text = "expired_on_restart"
                    state.updated_at = now
                    self.db.upsert_live_order(state.to_record())
            key = state.order_id or state.client_order_id
            self.open_orders[key] = state

        for row in self.db.get_live_positions():
            state = LivePositionState(
                slug=row["slug"],
                token_id=row["token_id"],
                side=row["side"],
                shares=row["shares"] or 0.0,
                avg_cost=row["avg_cost"] or 0.0,
                realized_pnl=row["realized_pnl"] or 0.0,
                total_fees=row["total_fees"] or 0.0,
                settlement_status=row["settlement_status"] or "open",
                updated_at=row["updated_at"] or _now_ts(),
            )
            self.actual_positions[self._key(row["slug"], row["side"])] = state

    def register_market(self, slug: str, up_token: str, down_token: str, market_id: str = "", condition_id: str = ""):
        self._tokens[slug] = {
            "up": up_token,
            "down": down_token,
            "market_id": market_id,
            "condition_id": condition_id or market_id,
        }
        self._asset_map[up_token] = (slug, "up")
        self._asset_map[down_token] = (slug, "down")
        if condition_id or market_id:
            self._market_ids.add(condition_id or market_id)
        self._refresh_constraints(up_token)
        self._refresh_constraints(down_token)
        self.db.upsert_live_market_state({
            "slug": slug,
            "market_id": condition_id or market_id,
            "resolved": 0,
            "resolution_outcome": None,
            "winning_token_id": None,
            "settlement_status": "open",
            "updated_at": _now_ts(),
            "raw_json": "",
        })

    def market_ids(self) -> list[str]:
        return sorted(self._market_ids)

    def _token(self, slug: str, side: str) -> Optional[str]:
        return self._tokens.get(slug, {}).get(side)

    def _market(self, slug: str) -> str:
        return self._tokens.get(slug, {}).get("condition_id") or self._tokens.get(slug, {}).get("market_id", "")

    def _key(self, slug: str, side: str) -> str:
        return f"{slug}:{side}"

    def get_positions(self, slug: str) -> list[Position]:
        positions = []
        for key, pos in self.actual_positions.items():
            if pos.slug != slug or pos.shares <= 0:
                continue
            positions.append(
                Position(
                    side=pos.side,
                    entry_price=pos.avg_cost,
                    size=pos.avg_cost * pos.shares,
                    shares=pos.shares,
                    entry_time=pos.updated_at,
                )
            )
        return positions

    def has_position(self, slug: str, side: str) -> bool:
        pos = self.actual_positions.get(self._key(slug, side))
        return bool(pos and pos.shares > 0)

    def _open_order_for(self, slug: str, side: str) -> Optional[LiveOrderState]:
        for order in self.open_orders.values():
            if order.slug == slug and order.side == side and order.status not in {
                "cancelled", "failed", "filled", "confirmed",
            }:
                return order
        return None

    def set_market_data_health(self, ok: bool):
        if ok:
            self._market_data_ok = True
            self._market_data_failures = 0
            return
        self._market_data_failures += 1
        self._market_data_ok = False
        if self._market_data_failures >= self.config.max_consecutive_live_errors:
            self._kill_switch = True
            logging.error("Live trader kill switch tripped: market data unhealthy")

    def _record_error(self, message: str):
        self._consecutive_errors += 1
        logging.error(message)
        if self._consecutive_errors >= self.config.max_consecutive_live_errors:
            self._kill_switch = True
            logging.error("Live trader kill switch tripped after repeated errors")

    def _clear_error(self):
        self._consecutive_errors = 0

    def _refresh_constraints(self, token_id: str) -> Optional[MarketConstraints]:
        try:
            book = self.clob.get_order_book(token_id)
            tick_size = _safe_float(book.tick_size, 0.01)
            min_order_size = _safe_float(book.min_order_size, 0.0)
            fee_rate_bps = int(self.clob.get_fee_rate_bps(token_id) or 0)
            self.constraints[token_id] = MarketConstraints(
                token_id=token_id,
                tick_size=tick_size,
                min_order_size=min_order_size,
                fee_rate_bps=fee_rate_bps,
            )
            return self.constraints[token_id]
        except Exception as exc:
            self._record_error(f"Constraint refresh failed for {token_id[:18]}...: {exc}")
            return None

    def _constraint(self, token_id: str) -> Optional[MarketConstraints]:
        return self.constraints.get(token_id) or self._refresh_constraints(token_id)

    def _ensure_buy_capacity(self, notional: float) -> bool:
        if self._kill_switch or not self._market_data_ok:
            return False
        if len([o for o in self.open_orders.values() if o.status not in {"cancelled", "failed", "filled", "confirmed"}]) >= self.config.max_open_live_orders:
            return False
        return self.total_live_exposure() + notional <= self.config.max_total_live_exposure

    def _check_allowance(self, token_id: str, side: str, expected_shares: float, expected_notional: float) -> bool:
        try:
            if side == "buy":
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                resp = self.clob.get_balance_allowance(params)
            else:
                params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
                resp = self.clob.get_balance_allowance(params)
            raw_balance = resp.get("balance")
            if raw_balance is None:
                raw_balance = resp.get("available")
            raw_allowance = resp.get("allowance")
            if raw_allowance is None:
                raw_allowance = resp.get("approved")
            balance = _safe_float(raw_balance)
            allowance = _safe_float(raw_allowance)
            required = expected_notional if side == "buy" else expected_shares
            if raw_balance is not None and balance + 1e-9 < required:
                logging.warning(
                    f"Skipping {side.upper()} — balance {balance:.4f} below required {required:.4f}"
                )
                return False
            if raw_allowance is not None and allowance + 1e-9 < required:
                logging.warning(
                    f"Skipping {side.upper()} — allowance {allowance:.4f} below required {required:.4f}"
                )
                return False
        except Exception as exc:
            logging.warning(f"Allowance preflight failed ({side}): {exc}")
        return True

    def evaluate_entry(self, slug: str, side: str, best_ask: float,
                       best_bid: float, seconds_remaining: float,
                       btc_delta: float = 0.0, elapsed_secs: float = 0.0) -> bool:
        if self._kill_switch or not self._market_data_ok:
            return False
        if side in self._stopped_out.get(slug, set()):
            return False
        if self.config.post_sell_cooldown_secs > 0:
            last_sell = self._last_sell_time.get(slug, {}).get(side, 0.0)
            if time.time() - last_sell < self.config.post_sell_cooldown_secs:
                return False
        if self.config.only_side and side != self.config.only_side:
            return False
        if best_ask <= 0 or best_ask > self.config.entry_threshold:
            return False
        if best_ask < self.config.min_entry_price:
            return False
        if best_ask - best_bid > self.config.max_entry_spread:
            return False
        if seconds_remaining < self.config.min_time_remaining_secs:
            return False
        if self.config.entry_delay_secs > 0 and elapsed_secs < self.config.entry_delay_secs:
            return False
        if self.config.max_entry_age_secs > 0 and elapsed_secs > self.config.max_entry_age_secs:
            return False
        if self.config.require_btc_alignment:
            if side == "up" and btc_delta < 0:
                return False
            if side == "down" and btc_delta > 0:
                return False
        if self.config.btc_momentum_threshold > 0:
            if side == "down" and btc_delta > self.config.btc_momentum_threshold:
                return False
            if side == "up" and btc_delta < -self.config.btc_momentum_threshold:
                return False
        if not self.config.allow_both_sides:
            other = "down" if side == "up" else "up"
            if self.has_position(slug, other) or self._open_order_for(slug, other):
                return False
        if self.has_position(slug, side) or self._open_order_for(slug, side):
            return False
        return self._ensure_buy_capacity(min(self.config.max_position_size, self.bankroll))

    def evaluate_exit(self, slug: str, side: str, best_bid: float,
                      seconds_remaining: float, btc_delta: float = 0.0) -> Optional[str]:
        if not self.has_position(slug, side):
            return None
        if self._open_order_for(slug, side):
            return None
        if best_bid >= self.config.exit_threshold:
            return "exit_target"
        if self.config.stop_loss > 0 and best_bid <= self.config.stop_loss:
            if self.config.stop_loss_after_secs == 0 or seconds_remaining <= self.config.stop_loss_after_secs:
                btc_confirms = (side == "down" and btc_delta < 0) or (side == "up" and btc_delta > 0)
                if not btc_confirms:
                    return "stop_loss"
        if seconds_remaining <= self.config.force_exit_before_close_secs:
            t = self.config.hold_through_close_btc_threshold
            if t > 0:
                if side == "up" and btc_delta >= t:
                    return None
                if side == "down" and btc_delta <= -t:
                    return None
            return "force_exit"
        return None

    def start_heartbeat(self):
        self._hb_stop.clear()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="heartbeat"
        )
        self._hb_thread.start()
        logging.info("💓 Heartbeat started (5s interval)")

    def stop_heartbeat(self):
        self._hb_stop.set()
        if self._hb_thread and self._hb_thread.is_alive():
            self._hb_thread.join(timeout=4.0)

    def _heartbeat_loop(self):
        while not self._hb_stop.wait(5.0):
            try:
                resp = self.clob.post_heartbeat(self._hb_id)
                self._hb_id = resp.get("heartbeat_id")
            except Exception as exc:
                self._record_error(f"Heartbeat error: {exc}")

    def start_reconciliation(self):
        self._reconcile_stop.clear()
        self._reconcile_thread = threading.Thread(
            target=self._reconcile_loop, daemon=True, name="reconcile"
        )
        self._reconcile_thread.start()

    def stop_reconciliation(self):
        self._reconcile_stop.set()
        if self._reconcile_thread and self._reconcile_thread.is_alive():
            self._reconcile_thread.join(timeout=4.0)

    def _reconcile_loop(self):
        while not self._reconcile_stop.wait(self.config.reconcile_interval_secs):
            self.reconcile_exchange_state()

    def reconcile_exchange_state(self):
        self._sync_open_orders()
        checkpoint = self.db.get_reconciliation_value("last_trade_after")
        after = int(float(checkpoint)) if checkpoint else None
        self._sync_recent_trades(after=after)
        now = _now_ts()
        if now - self._last_positions_sync >= self.config.positions_poll_interval_secs:
            self._sync_positions_from_data_api()
            self._last_positions_sync = now

    def _sync_open_orders(self):
        try:
            exchange_orders = self.clob.get_orders(OpenOrderParams())
            exchange_ids = set()
            for raw in exchange_orders:
                order_id = raw.get("id") or raw.get("order_id") or ""
                if not order_id:
                    continue
                exchange_ids.add(order_id)
                state = self.db.get_live_order(order_id)
                if state:
                    live = self.open_orders.get(order_id) or self.open_orders.get(state["client_order_id"])
                    if live:
                        live.order_id = order_id
                        live.status = raw.get("status") or raw.get("type") or "open"
                        live.filled_shares = _safe_float(raw.get("size_matched"), live.filled_shares)
                        live.updated_at = _now_ts()
                        live.raw_json = json.dumps(raw)
                        self.db.upsert_live_order(live.to_record())
                        self.open_orders.pop(live.client_order_id, None)
                        self.open_orders[order_id] = live
            for key, order in list(self.open_orders.items()):
                if order.order_id and order.order_id not in exchange_ids and order.status in {"open", "live", "delayed", "matched", "submitted"}:
                    order.status = "closed"
                    order.updated_at = _now_ts()
                    self.db.upsert_live_order(order.to_record())
        except Exception as exc:
            self._record_error(f"Open-order reconciliation failed: {exc}")

    def _sync_recent_trades(self, after: Optional[int] = None):
        try:
            params = TradeParams(after=after) if after else TradeParams()
            trades = self.clob.get_trades(params)
            max_ts = after or 0
            for trade in trades:
                self._handle_trade_like_event(trade)
                max_ts = max(max_ts, int(_safe_float(trade.get("timestamp"))))
            if max_ts:
                self.db.set_reconciliation_value("last_trade_after", str(max_ts))
            self._clear_error()
        except Exception as exc:
            self._record_error(f"Trade reconciliation failed: {exc}")

    def _sync_positions_from_data_api(self):
        try:
            rows = self.data_api.get_positions(self.clob.get_address())
            seen = set()
            for row in rows:
                asset_id = row.get("asset") or row.get("asset_id") or row.get("token_id")
                mapping = self._asset_map.get(asset_id)
                if not mapping:
                    continue
                slug, side = mapping
                key = self._key(slug, side)
                seen.add(key)
                prev = self.actual_positions.get(key)
                state = LivePositionState(
                    slug=slug,
                    token_id=asset_id,
                    side=side,
                    shares=_safe_float(row.get("size") or row.get("shares")),
                    avg_cost=_safe_float(row.get("avgPrice") or row.get("avg_cost")),
                    realized_pnl=(prev.realized_pnl if prev else 0.0),
                    total_fees=(prev.total_fees if prev else 0.0),
                    settlement_status=(prev.settlement_status if prev else "open"),
                    updated_at=_now_ts(),
                )
                self.actual_positions[key] = state
                self.db.upsert_live_position(state.to_record())
            for key, pos in list(self.actual_positions.items()):
                if pos.shares <= 0:
                    self.db.delete_live_position(pos.slug, pos.token_id, pos.side)
                    self.actual_positions.pop(key, None)
                    continue
                if key not in seen:
                    # Keep local reconciled state if Data API is temporarily behind.
                    continue
            self._clear_error()
        except Exception as exc:
            self._record_error(f"Position reconciliation failed: {exc}")

    def _compute_fee(self, shares: float, price: float, action: str, fee_rate_bps: int) -> tuple[float, str, float]:
        fee_rate = max(0.0, fee_rate_bps) / 10000.0
        if fee_rate <= 0 or shares <= 0:
            return 0.0, "USDC", 0.0
        if action == "buy":
            fee_shares = shares * fee_rate * (1.0 - price)
            return fee_shares * price, "SHARES", fee_shares
        fee_usdc = shares * fee_rate * price * (1.0 - price)
        return fee_usdc, "USDC", 0.0

    def _apply_fill_to_positions(self, order: LiveOrderState, fill_shares: float, fill_price: float, fee_amount: float, fee_asset: str):
        key = self._key(order.slug, order.side)
        pos = self.actual_positions.get(key)
        if order.intent == "buy":
            net_shares = fill_shares
            if fee_asset == "SHARES" and fill_shares > 0 and fill_price > 0:
                net_shares = max(0.0, fill_shares - (fee_amount / fill_price))
            if pos:
                total_cost = pos.avg_cost * pos.shares + fill_shares * fill_price
                total_shares = pos.shares + net_shares
                pos.avg_cost = (total_cost / total_shares) if total_shares > 0 else 0.0
                pos.shares = total_shares
                pos.total_fees += fee_amount
                pos.updated_at = _now_ts()
            else:
                pos = LivePositionState(
                    slug=order.slug,
                    token_id=order.token_id,
                    side=order.side,
                    shares=net_shares,
                    avg_cost=(fill_shares * fill_price / net_shares) if net_shares > 0 else fill_price,
                    total_fees=fee_amount,
                    updated_at=_now_ts(),
                )
            self.actual_positions[key] = pos
            self.db.upsert_live_position(pos.to_record())
            return

        if not pos:
            return
        proceeds = fill_shares * fill_price - fee_amount
        realized = proceeds - (pos.avg_cost * fill_shares)
        remaining = max(0.0, pos.shares - fill_shares)
        pos.realized_pnl += realized
        pos.total_fees += fee_amount
        pos.shares = remaining
        pos.updated_at = _now_ts()
        if remaining <= 0:
            self.db.delete_live_position(pos.slug, pos.token_id, pos.side)
            self.actual_positions.pop(key, None)
        else:
            self.db.upsert_live_position(pos.to_record())

    def _handle_trade_like_event(self, event: dict):
        if not isinstance(event, dict):
            return
        is_trade = (
            event.get("event_type") == "trade"
            or event.get("type") == "TRADE"
            or bool(event.get("taker_order_id"))
            or bool(event.get("maker_orders"))
            or bool(event.get("order_id"))
        )
        if is_trade:
            candidate_ids = []
            taker_order_id = event.get("taker_order_id")
            if taker_order_id:
                candidate_ids.append((taker_order_id, _safe_float(event.get("size")), "taker"))
            plain_order_id = event.get("order_id")
            if plain_order_id:
                candidate_ids.append((plain_order_id, _safe_float(event.get("size") or event.get("matched_amount")), event.get("role", "unknown")))
            for maker in event.get("maker_orders", []):
                oid = maker.get("order_id")
                if oid:
                    candidate_ids.append((oid, _safe_float(maker.get("matched_amount") or event.get("size")), "maker"))
            for order_id, matched, role in candidate_ids:
                order = self.open_orders.get(order_id)
                if not order:
                    row = self.db.get_live_order(order_id)
                    if row:
                        order = LiveOrderState(
                            client_order_id=row["client_order_id"],
                            order_id=row["order_id"] or "",
                            slug=row["slug"],
                            market_id=row["market_id"] or "",
                            token_id=row["token_id"],
                            side=row["side"],
                            intent=row["intent"] or "",
                            order_type=row["order_type"] or "",
                            tif=row["tif"] or "",
                            requested_price=row["requested_price"] or 0.0,
                            requested_shares=row["requested_shares"] or 0.0,
                            requested_notional=row["requested_notional"] or 0.0,
                            filled_shares=row["filled_shares"] or 0.0,
                            avg_fill_price=row["avg_fill_price"] or 0.0,
                            fee_rate_bps=row["fee_rate_bps"] or 0,
                            status=row["status"] or "unknown",
                            error_text=row["error_text"] or "",
                            created_at=row["created_at"] or _now_ts(),
                            updated_at=row["updated_at"] or _now_ts(),
                            raw_json=row["raw_json"] or "",
                        )
                        self.open_orders[order_id] = order
                if not order or matched <= 0:
                    continue
                trade_id = event.get("id")
                price = _safe_float(event.get("price"))
                fill_action = "buy" if order.intent == "buy" else "sell"
                fee_amount, fee_asset, _ = self._compute_fee(
                    matched, price, fill_action, order.fee_rate_bps
                )
                fill = {
                    "trade_id": f"{trade_id}:{order_id}" if trade_id else f"{order_id}:{event.get('timestamp')}",
                    "order_id": order_id,
                    "slug": order.slug,
                    "market_id": order.market_id,
                    "token_id": order.token_id,
                    "side": order.side,
                    "fill_price": price,
                    "fill_shares": matched,
                    "gross_notional": matched * price,
                    "fee_amount": fee_amount,
                    "fee_asset": fee_asset,
                    "role": role,
                    "status": event.get("status") or "MATCHED",
                    "trade_ts": _parse_ts(event.get("timestamp")),
                    "raw_json": json.dumps(event),
                }
                if self.db.insert_live_fill(fill):
                    prev_shares = order.filled_shares
                    total_shares = prev_shares + matched
                    order.avg_fill_price = (
                        ((order.avg_fill_price * prev_shares) + (matched * price)) / total_shares
                        if total_shares > 0 else price
                    )
                    order.filled_shares = total_shares
                    order.status = (event.get("status") or "MATCHED").lower()
                    order.updated_at = _now_ts()
                    order.raw_json = json.dumps(event)
                    self.db.upsert_live_order(order.to_record())
                    self._apply_fill_to_positions(order, matched, price, fee_amount, fee_asset)
                    self.db.insert_live_trade(
                        order.slug, _parse_ts(event.get("timestamp")), order.side, order.intent,
                        order_id, order.requested_price, price, matched * price, order.intent
                    )
                    if order.filled_shares + 1e-9 >= order.requested_shares:
                        order.status = "filled"
                        self.db.upsert_live_order(order.to_record())
        elif event.get("event_type") == "order" or event.get("size_matched") is not None:
            order_id = event.get("id") or event.get("order_id")
            order = self.open_orders.get(order_id)
            if not order:
                return
            order.status = (event.get("type") or "update").lower()
            order.filled_shares = max(order.filled_shares, _safe_float(event.get("size_matched")))
            order.updated_at = _now_ts()
            order.raw_json = json.dumps(event)
            self.db.upsert_live_order(order.to_record())

    def handle_user_event(self, event: dict):
        self._handle_trade_like_event(event)

    def handle_market_event(self, event: dict):
        etype = event.get("event_type", "")
        if etype == "tick_size_change":
            asset_id = event.get("asset_id")
            if asset_id:
                self.clob.clear_tick_size_cache(asset_id)
                self._refresh_constraints(asset_id)
        elif etype == "market_resolved":
            market_id = event.get("market") or event.get("condition_id") or ""
            winning_asset = event.get("winning_asset_id", "")
            outcome = (event.get("winning_outcome") or "").lower()
            for slug, meta in self._tokens.items():
                if market_id and market_id not in {meta.get("condition_id"), meta.get("market_id")}:
                    continue
                self.db.upsert_live_market_state({
                    "slug": slug,
                    "market_id": meta.get("condition_id") or meta.get("market_id", ""),
                    "resolved": 1,
                    "resolution_outcome": outcome,
                    "winning_token_id": winning_asset,
                    "settlement_status": "pending_reconciliation",
                    "updated_at": _now_ts(),
                    "raw_json": json.dumps(event),
                })

    def execute_buy(self, slug: str, side: str, price: float, now: float, context=None):
        token_id = self._token(slug, side)
        constraint = self._constraint(token_id) if token_id else None
        if not token_id or not constraint:
            logging.error(f"No live market metadata for {slug}:{side}")
            return

        size_usdc = min(self.config.max_position_size, self.bankroll)
        if size_usdc < self.config.min_position_usdc or not self._ensure_buy_capacity(size_usdc):
            return

        try:
            est_price = self.clob.calculate_market_price(
                token_id, BUY, size_usdc, OrderType.FAK
            )
            max_acceptable = min(
                self.config.entry_threshold,
                price + self.config.max_live_entry_slippage,
            )
            if est_price <= 0 or est_price > max_acceptable:
                logging.info(
                    f"Skipping BUY {side.upper()} — estimated price ${est_price:.4f} exceeds cap ${max_acceptable:.4f}"
                )
                return
            est_price = _round_up_to_tick(est_price, constraint.tick_size)
            est_shares = round(size_usdc / est_price, 4)
            if est_shares < constraint.min_order_size:
                logging.info(
                    f"Skipping BUY {side.upper()} — estimated shares {est_shares:.4f} below minimum {constraint.min_order_size:.4f}"
                )
                return
            if not self._check_allowance(token_id, "buy", est_shares, size_usdc):
                return

            client_order_id = uuid.uuid4().hex
            order_state = LiveOrderState(
                client_order_id=client_order_id,
                order_id=client_order_id,
                slug=slug,
                market_id=self._market(slug),
                token_id=token_id,
                side=side,
                intent="buy",
                order_type="market",
                tif=OrderType.FAK,
                requested_price=est_price,
                requested_shares=est_shares,
                requested_notional=size_usdc,
                fee_rate_bps=constraint.fee_rate_bps,
                created_at=now,
                updated_at=now,
                status="pending_submit",
                raw_json=json.dumps(context or {}),
            )
            self.db.upsert_live_order(order_state.to_record())

            signed = self.clob.create_market_order(
                MarketOrderArgs(
                    token_id=token_id,
                    amount=size_usdc,
                    side=BUY,
                    price=est_price,
                    order_type=OrderType.FAK,
                )
            )
            resp = self.clob.post_order(signed, OrderType.FAK)
            order_state.order_id = resp.get("orderID", "")
            order_state.status = (resp.get("status") or "submitted").lower()
            order_state.updated_at = _now_ts()
            order_state.raw_json = json.dumps(resp)
            self.db.upsert_live_order(order_state.to_record())
            self.open_orders.pop(client_order_id, None)
            self.open_orders[order_state.order_id or client_order_id] = order_state
            self.desired_positions[self._key(slug, side)] = "entry"
            logging.info(
                f"🟢 LIVE BUY  {side.upper()} req=${est_price:.4f} | "
                f"{est_shares:.4f} shares (${size_usdc:.2f}) | id={(order_state.order_id or client_order_id)[:16]}..."
            )
            self._handle_trade_like_event(resp if isinstance(resp, dict) else {})
            self._clear_error()
        except Exception as exc:
            self._record_error(f"BUY order failed ({side} @ ${price:.4f}): {exc}")

    def execute_sell(self, slug: str, side: str, price: float, reason: str, now: float, context=None):
        token_id = self._token(slug, side)
        constraint = self._constraint(token_id) if token_id else None
        if not token_id or not constraint:
            return
        pos = self.actual_positions.get(self._key(slug, side))
        if not pos or pos.shares <= 0 or self._open_order_for(slug, side):
            return

        try:
            est_price = self.clob.calculate_market_price(
                token_id, SELL, pos.shares, OrderType.FAK
            )
            min_acceptable = max(
                0.01,
                price - self.config.max_live_exit_slippage,
            )
            if reason != "force_exit" and est_price < min_acceptable:
                logging.info(
                    f"Skipping SELL {side.upper()} — estimated price ${est_price:.4f} below floor ${min_acceptable:.4f}"
                )
                return
            est_price = _round_down_to_tick(max(est_price, min_acceptable), constraint.tick_size)
            if not self._check_allowance(token_id, "sell", pos.shares, pos.shares * est_price):
                return
            client_order_id = uuid.uuid4().hex
            order_state = LiveOrderState(
                client_order_id=client_order_id,
                order_id=client_order_id,
                slug=slug,
                market_id=self._market(slug),
                token_id=token_id,
                side=side,
                intent=reason,
                order_type="market",
                tif=OrderType.FAK,
                requested_price=est_price,
                requested_shares=round(pos.shares, 4),
                requested_notional=round(pos.shares * est_price, 4),
                fee_rate_bps=constraint.fee_rate_bps,
                created_at=now,
                updated_at=now,
                status="pending_submit",
                raw_json=json.dumps(context or {}),
            )
            self.db.upsert_live_order(order_state.to_record())

            signed = self.clob.create_market_order(
                MarketOrderArgs(
                    token_id=token_id,
                    amount=round(pos.shares, 4),
                    side=SELL,
                    price=est_price,
                    order_type=OrderType.FAK,
                )
            )
            resp = self.clob.post_order(signed, OrderType.FAK)
            order_state.order_id = resp.get("orderID", "")
            order_state.status = (resp.get("status") or "submitted").lower()
            order_state.updated_at = _now_ts()
            order_state.raw_json = json.dumps(resp)
            self.db.upsert_live_order(order_state.to_record())
            self.open_orders.pop(client_order_id, None)
            self.open_orders[order_state.order_id or client_order_id] = order_state
            if reason == "stop_loss":
                self._stopped_out.setdefault(slug, set()).add(side)
            self._last_sell_time.setdefault(slug, {})[side] = now
            logging.info(
                f"🔴 LIVE SELL {side.upper()} req=${est_price:.4f} | "
                f"{pos.shares:.4f} shares | reason={reason} | id={(order_state.order_id or client_order_id)[:16]}..."
            )
            self._handle_trade_like_event(resp if isinstance(resp, dict) else {})
            self._clear_error()
        except Exception as exc:
            self._record_error(f"SELL order failed ({side} @ ${price:.4f}): {exc}")

    def cancel_market(self, slug: str):
        market_id = self._market(slug)
        try:
            if market_id:
                self.clob.cancel_market_orders(market=market_id)
            else:
                for side in ("up", "down"):
                    order = self._open_order_for(slug, side)
                    if order and order.order_id:
                        self.clob.cancel(order.order_id)
            for order in self.open_orders.values():
                if order.slug == slug and order.status not in {"filled", "confirmed", "failed"}:
                    order.status = "cancel_pending"
                    order.updated_at = _now_ts()
                    self.db.upsert_live_order(order.to_record())
        except Exception as exc:
            self._record_error(f"Cancel market failed for {slug}: {exc}")

    def mark_settlement_pending(self, slug: str):
        state = self.db.get_live_market_state(slug)
        if not state:
            return
        self.db.upsert_live_market_state({
            "slug": slug,
            "market_id": state["market_id"],
            "resolved": state["resolved"] or 0,
            "resolution_outcome": state["resolution_outcome"],
            "winning_token_id": state["winning_token_id"],
            "settlement_status": "pending_reconciliation",
            "updated_at": _now_ts(),
            "raw_json": state["raw_json"] or "",
        })


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
        self.ws     = PriceWebSocket(event_callback=self.trader.handle_market_event)
        self.user_ws = UserWebSocket(clob.creds, self.trader.handle_user_event)

    def _mode_label(self) -> str:
        return "⚠️  LIVE TRADING — REAL MONEY ⚠️"

    def _on_new_market(self, slug: str, tokens: dict):
        self.trader.register_market(
            slug,
            tokens["up_token_id"],
            tokens["down_token_id"],
            market_id=tokens.get("market_id", ""),
            condition_id=tokens.get("condition_id", ""),
        )
        self.ws.subscribe([tokens["up_token_id"], tokens["down_token_id"]])
        self.user_ws.subscribe(self.trader.market_ids())

    def _get_prices(self, tokens: dict):
        up_ws = self.ws.get_prices(tokens["up_token_id"])
        dn_ws = self.ws.get_prices(tokens["down_token_id"])
        if up_ws and dn_ws:
            self.trader.set_market_data_health(True)
            return up_ws, dn_ws, "ws"
        # WebSocket data stale or not yet connected — fall back to REST
        if up_ws or dn_ws:
            logging.debug("Partial WebSocket data, falling back to REST")
        prices = (
            self.poly.get_price(tokens["up_token_id"]),
            self.poly.get_price(tokens["down_token_id"]),
            "rest",
        )
        self.trader.set_market_data_health(bool(prices[0] and prices[1]))
        return prices

    def _finalize_market(self, slug: str):
        self.trader.cancel_market(slug)
        time.sleep(1.0)
        self.trader.reconcile_exchange_state()
        if self.trader.get_positions(slug):
            self.trader.mark_settlement_pending(slug)
            logging.info(
                f"   ⌛ Settlement pending for {slug} | "
                f"{len(self.trader.get_positions(slug))} live position(s) still open"
            )
        else:
            logging.info(f"   ✅ Flat on {slug} after order cancellation/reconciliation")

    def _on_before_close(self):
        self.trader.stop_reconciliation()
        self.trader.stop_heartbeat()
        self.ws.stop()
        self.user_ws.stop()
        logging.info("Heartbeat and WebSocket stopped.")

    def run(self):
        self.trader.start_heartbeat()
        self.trader.start_reconciliation()
        self.trader.reconcile_exchange_state()
        super().run()

    def _print_summary(self):
        logging.info("\n" + "=" * 70)
        logging.info("📊 LIVE SESSION SUMMARY")
        logging.info("=" * 70)
        fills = self.db.get_live_fills()
        if fills:
            gross = sum((f["gross_notional"] or 0.0) for f in fills)
            fees = sum((f["fee_amount"] or 0.0) for f in fills)
            logging.info(f"   Confirmed fills: {len(fills)}")
            logging.info(f"   Gross notional:  ${gross:.2f}")
            logging.info(f"   Fees:            ${fees:.2f}")
        open_orders = [
            o for o in self.db.get_live_orders()
            if (o["status"] or "") not in {"filled", "confirmed", "cancelled", "failed", "closed"}
        ]
        logging.info(f"   Open orders:     {len(open_orders)}")
        live_positions = [p for p in self.trader.actual_positions.values() if p.shares > 0]
        logging.info(f"   Live positions:  {len(live_positions)}")
        logging.info(f"   Realized P&L:    ${self.trader.realized_pnl():+.2f}")
        logging.info(f"   Spendable cap:   ${self.trader.bankroll:.2f}")
        logging.info("=" * 70)


# ─── Auth Helpers ─────────────────────────────────────────────────────────────

def setup_keys(
    private_key: str,
    keys_file: str,
    chain_id: int = 137,
    signature_type: int = 1,
    funder: Optional[str] = None,
):
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

    if signature_type != 0 and not funder:
        raise ValueError(
            "Proxy and Safe wallets require a funder address. "
            "Pass --funder or set POLYMARKET_FUNDER."
        )

    print(
        f"Deriving Polymarket API credentials from wallet "
        f"(signature_type={signature_type}, funder={funder or 'wallet'})..."
    )

    # L1 client only — no creds needed yet, just the private key
    client = ClobClient(
        host=CLOB_API,
        key=private_key,
        chain_id=chain_id,
        signature_type=signature_type,
        funder=funder,
    )
    creds = client.create_or_derive_api_creds()

    keys_path = Path(keys_file).expanduser()
    keys_path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "api_key":        creds.api_key,
        "api_secret":     creds.api_secret,
        "api_passphrase": creds.api_passphrase,
        "signature_type": signature_type,
        "funder":         funder,
    }
    # Open with mode 0o600 at creation time so there is no window where the
    # file exists with world-readable permissions.  write_text() + chmod() has
    # a TOCTOU race: the file is readable by others between the two calls.
    fd = os.open(str(keys_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w") as f:
        json.dump(data, f, indent=2)

    print(f"✅ Credentials saved to {keys_path}")
    print(f"   api_key: {creds.api_key[:16]}...")
    print(f"   signature_type: {signature_type}")
    if funder:
        print(f"   funder: {funder}")
    print()
    print("Before trading you also need to approve USDC and conditional tokens on Polygon:")
    print("  from py_clob_client.clob_types import BalanceAllowanceParams, AssetType")
    print("  client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))")
    print("  client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id='...'))")


def build_client(
    private_key: str,
    keys_file: str,
    chain_id: int = 137,
    signature_type: Optional[int] = None,
    funder: Optional[str] = None,
) -> "ClobClient":
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
    resolved_signature_type = (
        signature_type if signature_type is not None
        else int(saved.get("signature_type", 1))
    )
    resolved_funder = funder if funder is not None else saved.get("funder")
    if resolved_signature_type != 0 and not resolved_funder:
        raise ValueError(
            "Missing funder address for non-EOA wallet. "
            "Pass --funder or re-run --setup-keys with the correct funder."
        )
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
        signature_type = resolved_signature_type,
        funder         = resolved_funder,
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
    parser.add_argument(
        "--signature-type",
        type=int,
        default=int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1")),
        help="Wallet type: 0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE (default: 1)",
    )
    parser.add_argument(
        "--funder",
        default=os.environ.get("POLYMARKET_FUNDER"),
        help="Polymarket funder/proxy wallet address for signature_type 1 or 2",
    )

    # Strategy
    parser.add_argument("--entry",        type=float, default=0.38)
    parser.add_argument("--exit",         type=float, default=0.70)
    parser.add_argument("--stop-loss",    type=float, default=0.0)
    parser.add_argument("--stop-loss-after", type=int, default=60,
                        help="Only trigger stop_loss in final N seconds of window (default 60, 0=anytime)")
    parser.add_argument("--min-entry",    type=float, default=0.15,
                        help="Reject entries below this price (default 0.15, 0=disabled)")
    parser.add_argument("--entry-delay",  type=int,   default=60,
                        help="Seconds to wait before first buy (default 60)")
    parser.add_argument("--max-entry-age", type=int,  default=150,
                        help="Stop opening new positions after N seconds from window open (default 150, 0=off)")
    parser.add_argument("--allow-contrarian", action="store_true",
                        help="Allow entries against BTC move from window open")
    parser.add_argument("--hold-threshold", type=float, default=15.0,
                        help="Hold through close if BTC moved $X in your favor (default 15.0, 0=off)")
    parser.add_argument("--cooldown",     type=int,   default=10,
                        help="Seconds to block re-entry after a sell (default 10, 0=off)")
    parser.add_argument("--max-position", type=float, default=50.0,
                        help="Max USDC per side per market (default: 50)")
    parser.add_argument("--max-total-exposure", type=float, default=100.0,
                        help="Max aggregate live exposure across open orders and inventory (default: 100)")
    parser.add_argument("--max-open-orders", type=int, default=4,
                        help="Max simultaneous live open orders (default: 4)")
    parser.add_argument("--max-live-errors", type=int, default=5,
                        help="Kill switch after N consecutive live errors (default: 5)")
    parser.add_argument("--min-position", type=float, default=5.0,
                        help="Min USDC per trade — avoid dust orders (default: 5)")
    parser.add_argument("--bankroll",     type=float, default=1000.0,
                        help="Starting bankroll for P&L tracking (default: 1000)")
    parser.add_argument("--single-side",  action="store_true",
                        help="Deprecated: single-side is now the default")
    parser.add_argument("--both-sides",   action="store_true",
                        help="Allow both UP and DOWN positions in the same market")
    parser.add_argument("--reconcile-interval", type=float, default=2.0,
                        help="Seconds between order/trade reconciliation passes (default: 2.0)")
    parser.add_argument("--positions-poll-interval", type=float, default=10.0,
                        help="Seconds between Data API position polls (default: 10.0)")
    parser.add_argument("--max-entry-slippage", type=float, default=0.03,
                        help="Max acceptable entry slippage over the observed ask (default: 0.03)")
    parser.add_argument("--max-exit-slippage", type=float, default=0.05,
                        help="Max acceptable exit slippage below the observed bid (default: 0.05)")
    parser.add_argument("--backfill-history", nargs="*", default=[],
                        help="Fetch and print historical token price data, then exit")
    parser.add_argument("--history-interval", default="1d",
                        help="History interval for --backfill-history (default: 1d)")
    parser.add_argument("--history-fidelity", type=int, default=60,
                        help="History fidelity for --backfill-history (default: 60)")
    parser.add_argument("--history-start-ts", type=int, default=None,
                        help="Absolute start timestamp for --backfill-history")
    parser.add_argument("--history-end-ts", type=int, default=None,
                        help="Absolute end timestamp for --backfill-history")

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

    if args.backfill_history:
        client = DataAPIClient()
        for token_id in args.backfill_history:
            history = client.get_prices_history(
                token_id=token_id,
                interval=args.history_interval,
                fidelity=args.history_fidelity,
                start_ts=args.history_start_ts,
                end_ts=args.history_end_ts,
            )
            print(json.dumps({"token_id": token_id, "history": history}, indent=2))
        return

    # ── --setup-keys ──────────────────────────────────────────────────────────
    if args.setup_keys:
        if not args.private_key:
            print("Error: --private-key (or POLYMARKET_PRIVATE_KEY) required for --setup-keys")
            sys.exit(1)
        _warn_if_key_in_args(args)
        setup_keys(
            args.private_key,
            args.keys_file,
            args.chain_id,
            signature_type=args.signature_type,
            funder=args.funder,
        )
        return

    # ── Live trading ──────────────────────────────────────────────────────────
    if not args.private_key:
        print(
            "Error: --private-key (or POLYMARKET_PRIVATE_KEY env var) is required.\n"
            "Run with --setup-keys first if you haven't set up credentials."
        )
        sys.exit(1)

    _warn_if_key_in_args(args)
    clob = build_client(
        args.private_key,
        args.keys_file,
        args.chain_id,
        signature_type=args.signature_type,
        funder=args.funder,
    )

    config = StrategyConfig(
        entry_threshold          = args.entry,
        exit_threshold           = args.exit,
        stop_loss                = args.stop_loss,
        stop_loss_after_secs     = args.stop_loss_after,
        min_entry_price          = args.min_entry,
        entry_delay_secs         = args.entry_delay,
        max_entry_age_secs       = args.max_entry_age,
        require_btc_alignment    = not args.allow_contrarian,
        hold_through_close_btc_threshold  = args.hold_threshold,
        post_sell_cooldown_secs  = args.cooldown,
        max_position_size        = args.max_position,
        max_total_live_exposure  = args.max_total_exposure,
        max_open_live_orders     = args.max_open_orders,
        max_consecutive_live_errors = args.max_live_errors,
        reconcile_interval_secs  = args.reconcile_interval,
        positions_poll_interval_secs = args.positions_poll_interval,
        max_live_entry_slippage  = args.max_entry_slippage,
        max_live_exit_slippage   = args.max_exit_slippage,
        min_position_usdc        = args.min_position,
        starting_bankroll        = args.bankroll,
        allow_both_sides         = args.both_sides,
    )

    LiveObserver(config, db_path=args.db, clob=clob).run()


if __name__ == "__main__":
    main()
