#!/usr/bin/env python3
"""
Polymarket BTC 5-Minute Market Observer & Paper Trader

Watches every 5-minute BTC up/down market on Polymarket, logs the full
price lifecycle of both sides, correlates with actual BTC spot price,
and runs a configurable paper trading strategy.

The core thesis: Polymarket's 5-min BTC markets are driven by retail
sentiment that overshoots in both directions. Buy a side when it's cheap
(<entry_threshold), sell when the market overreacts (>exit_threshold),
and never hold through resolution.

Usage:
    python observer.py                  # Run live observation + paper trading
    python observer.py --analyze        # Analyze collected data
    python observer.py --help           # Show all options
"""

import os
import sys
import json
import time
import sqlite3
import logging
import argparse
import signal
from datetime import datetime, timezone
from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import Optional
import re
import requests


# ─── Terminal colors ──────────────────────────────────────────────────────────

_GREEN  = "\033[92m"
_RED    = "\033[91m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"
_ANSI   = re.compile(r'\033\[[0-9;]*m')


def _colored(text: str, color: str) -> str:
    return f"{color}{_BOLD}{text}{_RESET}"


# ─── Constants ────────────────────────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"


# ─── Configuration ────────────────────────────────────────────────────────────

@dataclass
class StrategyConfig:
    """Strategy parameters — tune these after running --analyze."""

    # Entry: buy a side when its best ask is at or below this price
    entry_threshold: float = 0.38

    # Exit: sell when best bid hits this price
    exit_threshold: float = 0.70

    # Stop-loss: sell if price drops to this (0 = disabled, hold to resolution)
    stop_loss: float = 0.0

    # Don't enter a new position if fewer than this many seconds remain
    min_time_remaining_secs: int = 30

    # Force-exit all positions when fewer than this many seconds remain
    force_exit_before_close_secs: int = 15

    # Max USDC per side per market
    max_position_size: float = 50.0

    # Minimum USDC per trade (guards against dust; also used by LiveTrader)
    min_position_usdc: float = 5.0

    # Starting paper bankroll
    starting_bankroll: float = 1000.0

    # Seconds between REST price polls (only used when WebSocket is unavailable)
    poll_interval_secs: float = 3.0

    # Allow buying both UP and DOWN in the same window
    allow_both_sides: bool = False

    # Reject entry if bid/ask spread exceeds this (wide spreads hide fake edge)
    max_entry_spread: float = 0.06

    # Don't enter until this many seconds have elapsed since window open (0 = off)
    entry_delay_secs: int = 60

    # Stop entering once the setup is too late in the 5-minute window.
    # The recorded dataset performs materially worse once entries happen
    # deep into the contract lifecycle, where mean-reversion time is limited.
    max_entry_age_secs: int = 150

    # Only buy in the same direction as BTC's move from window open.
    # This removes the worst-performing entries from the current dataset:
    # buying UP while BTC is down, and buying DOWN while BTC is up.
    require_btc_alignment: bool = True

    # Don't buy a side if BTC has moved more than this many dollars against it
    # e.g. 30.0 means: skip DOWN if BTC is up $30+ from open, skip UP if BTC is down $30+
    # 0 = disabled
    btc_momentum_threshold: float = 0.0

    # Skip force_exit and let the market resolve if BTC has moved this many dollars
    # in favor of our position (e.g. 15.0 = hold UP if BTC is up $15+ from open).
    # 0 = disabled (always force_exit)
    hold_through_close_btc_threshold: float = 15.0

    # Skip force_exit and let the market resolve if BTC is still within this
    # many dollars of the window-open price near expiry. Small last-second BTC
    # noise can flip the winner, so forcing out in a near-flat market often
    # locks in a bad exit just before a favorable resolution.
    hold_through_close_neutral_btc_range: float = 5.0

    # Only activate stop_loss when fewer than this many seconds remain in the window.
    # Prevents cutting a position that still has time to recover.
    # Default 60: stop_loss only fires in the last 60 seconds so positions have
    # time to reprice before being cut.  0 = fires immediately regardless of time.
    stop_loss_after_secs: int = 60

    # Reject entry if best ask is below this price.
    # Prices below ~0.15 mean the crowd has already priced this side as near-dead —
    # recovery to the exit threshold is unlikely and buying here contradicts the
    # sentiment-overshoot thesis.
    min_entry_price: float = 0.15

    # Seconds to block re-entry on the same side after selling it.
    # Prevents the bot from immediately re-buying the same side
    # right after an exit — a pattern that has historically lost money.
    # 0 = no cooldown (re-entry allowed immediately)
    post_sell_cooldown_secs: int = 10

    # Restrict entries to one side only: "up", "down", or "" for both
    only_side: str = ""

    # Live trading: cap total notional tied up in confirmed inventory + open buy orders.
    max_total_live_exposure: float = 100.0

    # Live trading: maximum number of simultaneously open exchange orders.
    max_open_live_orders: int = 4

    # Live trading: kill-switch threshold for repeated exchange/reconciliation failures.
    max_consecutive_live_errors: int = 5

    # Live trading: how often to poll exchange truth for orders/trades.
    reconcile_interval_secs: float = 2.0

    # Live trading: how often to poll Data API positions for inventory sanity checks.
    positions_poll_interval_secs: float = 10.0

    # Live trading: maximum acceptable market-price slippage beyond the observed quote.
    max_live_entry_slippage: float = 0.03
    max_live_exit_slippage: float = 0.05

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ─── API Clients ──────────────────────────────────────────────────────────────

class PolymarketClient:
    """Read-only Polymarket API client — market discovery and price polling."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "PolymarketObserver/1.0"})

    def get_server_time(self) -> float:
        try:
            resp = self.session.get(f"{CLOB_API}/time", timeout=5)
            resp.raise_for_status()
            return float(resp.text)
        except Exception:
            return time.time()

    def compute_window_times(self, server_time: Optional[float] = None):
        """Return (window_start_ts, window_end_ts, slug) for the current 5-min window."""
        t = server_time or self.get_server_time()
        start = int(t) - (int(t) % 300)
        return start, start + 300, f"btc-updown-5m-{start}"

    def get_market_by_slug(self, slug: str) -> Optional[dict]:
        try:
            resp = self.session.get(
                f"{GAMMA_API}/events",
                params={"slug": slug},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict) and data.get("id"):
                return data
            return None
        except Exception as exc:
            logging.warning(f"Failed to fetch market {slug}: {exc}")
            return None

    def extract_token_ids(self, event_data: dict) -> Optional[dict]:
        markets = event_data.get("markets", [])
        if not markets:
            return None
        market = markets[0]
        raw_ids     = market.get("clobTokenIds")
        raw_outcomes = market.get("outcomes")
        if not raw_ids or not raw_outcomes:
            return None
        token_ids = json.loads(raw_ids)     if isinstance(raw_ids, str)      else raw_ids
        outcomes  = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes

        result = {"market_id": market.get("id"), "condition_id": market.get("conditionId")}
        for outcome, tid in zip(outcomes, token_ids):
            key = outcome.lower().strip()
            if key in ("up", "yes"):
                result["up_token_id"] = tid
            elif key in ("down", "no"):
                result["down_token_id"] = tid

        return result if "up_token_id" in result and "down_token_id" in result else None

    @staticmethod
    def _best_book_price(levels: list, side: str) -> float:
        prices = []
        for level in levels or []:
            try:
                size = float(level.get("size", 0))
                price = float(level.get("price", 0))
            except Exception:
                continue
            if size > 0 and price > 0:
                prices.append(price)
        if not prices:
            return 0.0
        return max(prices) if side == "bid" else min(prices)

    def get_price(self, token_id: str) -> Optional[dict]:
        """Return {"best_bid": float, "best_ask": float} for a token."""
        try:
            book = self.get_order_book(token_id)
            if not book:
                return None
            best_bid = self._best_book_price(book.get("bids", []), "bid")
            best_ask = self._best_book_price(book.get("asks", []), "ask")
            if best_bid > 0 and best_ask > 0 and best_ask < best_bid:
                logging.warning(
                    f"Crossed order book for {token_id[:20]}...: bid={best_bid:.4f} ask={best_ask:.4f}"
                )
                return None
            return {"best_bid": best_bid, "best_ask": best_ask}
        except Exception as exc:
            logging.warning(f"Price fetch failed for {token_id[:20]}...: {exc}")
            return None

    def get_order_book(self, token_id: str) -> Optional[dict]:
        try:
            resp = self.session.get(f"{CLOB_API}/book",
                                    params={"token_id": token_id}, timeout=5)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logging.warning(f"Order book fetch failed: {exc}")
            return None


class BTCPriceClient:
    """Multi-source BTC/USD spot price with automatic fallback.

    NOTE — resolution price mismatch:
    Polymarket resolves BTC 5-min markets using Chainlink Data Streams
    (feed ID 0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8).
    Data Streams is a paid subscription product — we do not have access to it.

    This client uses Coinbase/Kraken/Binance/CoinGecko spot prices instead.
    Spot can diverge from the Chainlink oracle price, which means:
      - btc_open_price and btc_close_price in the DB may not match Polymarket's
        resolution source exactly
      - _finalize_market() resolution predictions (up/down) can be wrong
      - --hold-threshold logic is unreliable because the price we're reading
        is not the price the market resolves against

    This is acceptable for paper trading and strategy validation. Before going
    live, evaluate Chainlink Data Streams pricing at:
    https://chain.link/contact?ref_id=datastreams
    """

    def __init__(self):
        self.session = requests.Session()

    def get_btc_price(self) -> Optional[float]:
        for source in (self._coinbase, self._kraken, self._binance, self._coingecko):
            price = source()
            if price and price > 0:
                return price
        logging.warning("All BTC price sources failed")
        return None

    def _coinbase(self) -> Optional[float]:
        try:
            r = self.session.get(
                "https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5)
            r.raise_for_status()
            return float(r.json()["data"]["amount"])
        except Exception:
            return None

    def _kraken(self) -> Optional[float]:
        try:
            r = self.session.get(
                "https://api.kraken.com/0/public/Ticker",
                params={"pair": "XBTUSD"}, timeout=5)
            r.raise_for_status()
            return float(r.json()["result"]["XXBTZUSD"]["c"][0])
        except Exception:
            return None

    def _binance(self) -> Optional[float]:
        try:
            r = self.session.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "BTCUSDT"}, timeout=5)
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception:
            return None

    def _coingecko(self) -> Optional[float]:
        try:
            r = self.session.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "usd"}, timeout=5)
            r.raise_for_status()
            return float(r.json()["bitcoin"]["usd"])
        except Exception:
            return None


# ─── Database ─────────────────────────────────────────────────────────────────

class Database:
    """SQLite store for market observations, price ticks, and trades."""

    def __init__(self, db_path: str = "polymarket_observer.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL mode: concurrent reads don't block writes; much faster for
        # high-frequency tick inserts alongside occasional analysis queries.
        self.conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL sync is safe with WAL (no data loss on OS crash, only power loss)
        # and avoids the full fsync on every commit.
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._pending_ticks = 0
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS markets (
                slug             TEXT PRIMARY KEY,
                window_start_ts  INTEGER,
                window_end_ts    INTEGER,
                market_id        TEXT,
                up_token_id      TEXT,
                down_token_id    TEXT,
                btc_open_price   REAL,
                btc_close_price  REAL,
                resolution       TEXT,
                created_at       TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS price_ticks (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                slug               TEXT,
                timestamp          REAL,
                seconds_remaining  REAL,
                up_best_bid        REAL,
                up_best_ask        REAL,
                up_midpoint        REAL,
                down_best_bid      REAL,
                down_best_ask      REAL,
                down_midpoint      REAL,
                btc_spot_price     REAL,
                btc_delta_from_open REAL,
                price_source       TEXT DEFAULT 'rest',
                FOREIGN KEY (slug) REFERENCES markets(slug)
            );

            CREATE TABLE IF NOT EXISTS paper_trades (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                slug           TEXT,
                timestamp      REAL,
                side           TEXT,
                action         TEXT,
                price          REAL,
                size           REAL,
                reason         TEXT,
                pnl            REAL,
                bankroll_after REAL,
                FOREIGN KEY (slug) REFERENCES markets(slug)
            );

            CREATE TABLE IF NOT EXISTS live_trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                slug            TEXT,
                timestamp       REAL,
                side            TEXT,
                action          TEXT,
                order_id        TEXT,
                requested_price REAL,
                filled_price    REAL,
                size_usdc       REAL,
                reason          TEXT,
                FOREIGN KEY (slug) REFERENCES markets(slug)
            );

            CREATE TABLE IF NOT EXISTS live_orders (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                client_order_id    TEXT UNIQUE,
                order_id           TEXT UNIQUE,
                slug               TEXT,
                market_id          TEXT,
                token_id           TEXT,
                side               TEXT,
                intent             TEXT,
                order_type         TEXT,
                tif                TEXT,
                requested_price    REAL,
                requested_shares   REAL,
                requested_notional REAL,
                filled_shares      REAL DEFAULT 0,
                avg_fill_price     REAL,
                fee_rate_bps       INTEGER DEFAULT 0,
                status             TEXT,
                error_text         TEXT,
                created_at         REAL,
                updated_at         REAL,
                raw_json           TEXT
            );

            CREATE TABLE IF NOT EXISTS live_fills (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id       TEXT UNIQUE,
                order_id       TEXT,
                slug           TEXT,
                market_id      TEXT,
                token_id       TEXT,
                side           TEXT,
                fill_price     REAL,
                fill_shares    REAL,
                gross_notional REAL,
                fee_amount     REAL,
                fee_asset      TEXT,
                role           TEXT,
                status         TEXT,
                trade_ts       REAL,
                raw_json       TEXT
            );

            CREATE TABLE IF NOT EXISTS live_positions (
                slug              TEXT,
                token_id          TEXT,
                side              TEXT,
                shares            REAL,
                avg_cost          REAL,
                realized_pnl      REAL DEFAULT 0,
                total_fees        REAL DEFAULT 0,
                settlement_status TEXT DEFAULT 'open',
                updated_at        REAL,
                PRIMARY KEY (slug, token_id, side)
            );

            CREATE TABLE IF NOT EXISTS live_reconciliation_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS live_market_state (
                slug               TEXT PRIMARY KEY,
                market_id          TEXT,
                resolved           INTEGER DEFAULT 0,
                resolution_outcome TEXT,
                winning_token_id   TEXT,
                settlement_status  TEXT DEFAULT 'open',
                updated_at         REAL,
                raw_json           TEXT
            );

            CREATE TABLE IF NOT EXISTS strategy_config (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                config_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_ticks_slug       ON price_ticks(slug);
            CREATE INDEX IF NOT EXISTS idx_ticks_time       ON price_ticks(timestamp);
            CREATE INDEX IF NOT EXISTS idx_ticks_slug_ts    ON price_ticks(slug, timestamp);
            CREATE INDEX IF NOT EXISTS idx_trades_slug      ON paper_trades(slug);
            CREATE INDEX IF NOT EXISTS idx_live_trades_slug ON live_trades(slug);
            CREATE INDEX IF NOT EXISTS idx_live_orders_slug_status ON live_orders(slug, status);
            CREATE INDEX IF NOT EXISTS idx_live_fills_order_id ON live_fills(order_id);
            CREATE INDEX IF NOT EXISTS idx_live_positions_slug ON live_positions(slug);
        """)
        self.conn.commit()

        # Schema migrations — safe for existing DBs (SQLite has no IF NOT EXISTS for columns)
        for sql in [
            "ALTER TABLE price_ticks ADD COLUMN up_spread REAL",
            "ALTER TABLE price_ticks ADD COLUMN down_spread REAL",
            "ALTER TABLE price_ticks ADD COLUMN up_change_10s REAL",
            "ALTER TABLE price_ticks ADD COLUMN down_change_10s REAL",
            "ALTER TABLE paper_trades ADD COLUMN shares REAL",
            "ALTER TABLE paper_trades ADD COLUMN seconds_remaining REAL",
            "ALTER TABLE paper_trades ADD COLUMN spread_at_trade REAL",
            "ALTER TABLE paper_trades ADD COLUMN signal_json TEXT",
        ]:
            try:
                self.conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.commit()

    # ── Config ────────────────────────────────────────────────────────────────

    def save_config(self, config: StrategyConfig):
        self.conn.execute(
            "INSERT OR REPLACE INTO strategy_config (id, config_json) VALUES (1, ?)",
            (json.dumps(config.to_dict()),),
        )
        self.conn.commit()

    def load_config(self) -> Optional[StrategyConfig]:
        row = self.conn.execute(
            "SELECT config_json FROM strategy_config WHERE id=1"
        ).fetchone()
        return StrategyConfig.from_dict(json.loads(row["config_json"])) if row else None

    # ── Markets ───────────────────────────────────────────────────────────────

    def upsert_market(self, slug, window_start, window_end,
                      market_id, up_token_id, down_token_id, btc_open=None):
        self.conn.execute("""
            INSERT OR IGNORE INTO markets
            (slug, window_start_ts, window_end_ts, market_id,
             up_token_id, down_token_id, btc_open_price)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (slug, window_start, window_end, market_id,
              up_token_id, down_token_id, btc_open))
        self.conn.commit()

    def update_market_close(self, slug, btc_close, resolution):
        self.conn.execute(
            "UPDATE markets SET btc_close_price=?, resolution=? WHERE slug=?",
            (btc_close, resolution, slug),
        )
        self.conn.commit()

    # ── Price ticks ───────────────────────────────────────────────────────────

    def insert_tick(self, slug, timestamp, seconds_remaining,
                    up_bid, up_ask, up_mid, down_bid, down_ask, down_mid,
                    btc_spot, btc_delta, source="rest",
                    up_spread=None, down_spread=None,
                    up_change_10s=None, down_change_10s=None):
        self.conn.execute("""
            INSERT INTO price_ticks
            (slug, timestamp, seconds_remaining,
             up_best_bid, up_best_ask, up_midpoint,
             down_best_bid, down_best_ask, down_midpoint,
             btc_spot_price, btc_delta_from_open, price_source,
             up_spread, down_spread, up_change_10s, down_change_10s)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (slug, timestamp, seconds_remaining,
              up_bid, up_ask, up_mid,
              down_bid, down_ask, down_mid,
              btc_spot, btc_delta, source,
              up_spread, down_spread, up_change_10s, down_change_10s))
        # Batch commits: flush every 10 ticks rather than every insert.
        # With WebSocket at sub-second cadence this avoids fsyncing constantly.
        # Trades and market updates still commit immediately (see insert_trade,
        # upsert_market) so financial records are never batched.
        self._pending_ticks += 1
        if self._pending_ticks >= 10:
            self.conn.commit()
            self._pending_ticks = 0

    def flush_ticks(self):
        """Force-commit any buffered tick inserts. Call on market close or shutdown."""
        if self._pending_ticks > 0:
            self.conn.commit()
            self._pending_ticks = 0

    # ── Paper trades ──────────────────────────────────────────────────────────

    def insert_trade(self, slug, timestamp, side, action,
                     price, size, reason, pnl, bankroll,
                     shares=None, seconds_remaining=None,
                     spread_at_trade=None, signal_json=None):
        self.conn.execute("""
            INSERT INTO paper_trades
            (slug, timestamp, side, action, price, size, reason, pnl, bankroll_after,
             shares, seconds_remaining, spread_at_trade, signal_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (slug, timestamp, side, action, price, size, reason, pnl, bankroll,
              shares, seconds_remaining, spread_at_trade, signal_json))
        self.conn.commit()

    # ── Live trades ───────────────────────────────────────────────────────────

    def insert_live_trade(self, slug, timestamp, side, action,
                          order_id, requested_price, filled_price, size_usdc, reason):
        self.conn.execute("""
            INSERT INTO live_trades
            (slug, timestamp, side, action, order_id,
             requested_price, filled_price, size_usdc, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (slug, timestamp, side, action, order_id,
              requested_price, filled_price, size_usdc, reason))
        self.conn.commit()

    def update_live_trade_fill(self, order_id: str, filled_price: float):
        self.conn.execute(
            "UPDATE live_trades SET filled_price=? WHERE order_id=?",
            (filled_price, order_id),
        )
        self.conn.commit()

    def upsert_live_order(self, order: dict):
        self.conn.execute("""
            INSERT INTO live_orders
            (client_order_id, order_id, slug, market_id, token_id, side, intent,
             order_type, tif, requested_price, requested_shares, requested_notional,
             filled_shares, avg_fill_price, fee_rate_bps, status, error_text,
             created_at, updated_at, raw_json)
            VALUES
            (:client_order_id, :order_id, :slug, :market_id, :token_id, :side, :intent,
             :order_type, :tif, :requested_price, :requested_shares, :requested_notional,
             :filled_shares, :avg_fill_price, :fee_rate_bps, :status, :error_text,
             :created_at, :updated_at, :raw_json)
            ON CONFLICT(client_order_id) DO UPDATE SET
                order_id=excluded.order_id,
                slug=excluded.slug,
                market_id=excluded.market_id,
                token_id=excluded.token_id,
                side=excluded.side,
                intent=excluded.intent,
                order_type=excluded.order_type,
                tif=excluded.tif,
                requested_price=excluded.requested_price,
                requested_shares=excluded.requested_shares,
                requested_notional=excluded.requested_notional,
                filled_shares=excluded.filled_shares,
                avg_fill_price=excluded.avg_fill_price,
                fee_rate_bps=excluded.fee_rate_bps,
                status=excluded.status,
                error_text=excluded.error_text,
                updated_at=excluded.updated_at,
                raw_json=excluded.raw_json
        """, order)
        self.conn.commit()

    def get_live_orders(self, statuses: Optional[list[str]] = None, slug: Optional[str] = None):
        sql = "SELECT * FROM live_orders"
        params = []
        clauses = []
        if statuses:
            clauses.append("status IN ({})".format(",".join("?" for _ in statuses)))
            params.extend(statuses)
        if slug:
            clauses.append("slug=?")
            params.append(slug)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        return self.conn.execute(sql, params).fetchall()

    def get_live_order(self, order_id: str):
        return self.conn.execute(
            "SELECT * FROM live_orders WHERE order_id=? OR client_order_id=? LIMIT 1",
            (order_id, order_id),
        ).fetchone()

    def insert_live_fill(self, fill: dict) -> bool:
        cur = self.conn.execute("""
            INSERT OR IGNORE INTO live_fills
            (trade_id, order_id, slug, market_id, token_id, side, fill_price,
             fill_shares, gross_notional, fee_amount, fee_asset, role,
             status, trade_ts, raw_json)
            VALUES
            (:trade_id, :order_id, :slug, :market_id, :token_id, :side, :fill_price,
             :fill_shares, :gross_notional, :fee_amount, :fee_asset, :role,
             :status, :trade_ts, :raw_json)
        """, fill)
        self.conn.commit()
        return cur.rowcount > 0

    def get_live_fills(self, order_id: Optional[str] = None):
        if order_id:
            return self.conn.execute(
                "SELECT * FROM live_fills WHERE order_id=? ORDER BY trade_ts",
                (order_id,),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM live_fills ORDER BY trade_ts"
        ).fetchall()

    def upsert_live_position(self, pos: dict):
        self.conn.execute("""
            INSERT INTO live_positions
            (slug, token_id, side, shares, avg_cost, realized_pnl, total_fees,
             settlement_status, updated_at)
            VALUES
            (:slug, :token_id, :side, :shares, :avg_cost, :realized_pnl, :total_fees,
             :settlement_status, :updated_at)
            ON CONFLICT(slug, token_id, side) DO UPDATE SET
                shares=excluded.shares,
                avg_cost=excluded.avg_cost,
                realized_pnl=excluded.realized_pnl,
                total_fees=excluded.total_fees,
                settlement_status=excluded.settlement_status,
                updated_at=excluded.updated_at
        """, pos)
        self.conn.commit()

    def delete_live_position(self, slug: str, token_id: str, side: str):
        self.conn.execute(
            "DELETE FROM live_positions WHERE slug=? AND token_id=? AND side=?",
            (slug, token_id, side),
        )
        self.conn.commit()

    def get_live_positions(self, slug: Optional[str] = None):
        if slug:
            return self.conn.execute(
                "SELECT * FROM live_positions WHERE slug=? ORDER BY side",
                (slug,),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM live_positions ORDER BY slug, side"
        ).fetchall()

    def set_reconciliation_value(self, key: str, value: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO live_reconciliation_state (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    def get_reconciliation_value(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM live_reconciliation_state WHERE key=?",
            (key,),
        ).fetchone()
        return row["value"] if row else None

    def upsert_live_market_state(self, state: dict):
        self.conn.execute("""
            INSERT INTO live_market_state
            (slug, market_id, resolved, resolution_outcome, winning_token_id,
             settlement_status, updated_at, raw_json)
            VALUES
            (:slug, :market_id, :resolved, :resolution_outcome, :winning_token_id,
             :settlement_status, :updated_at, :raw_json)
            ON CONFLICT(slug) DO UPDATE SET
                market_id=excluded.market_id,
                resolved=excluded.resolved,
                resolution_outcome=excluded.resolution_outcome,
                winning_token_id=excluded.winning_token_id,
                settlement_status=excluded.settlement_status,
                updated_at=excluded.updated_at,
                raw_json=excluded.raw_json
        """, state)
        self.conn.commit()

    def get_live_market_state(self, slug: str):
        return self.conn.execute(
            "SELECT * FROM live_market_state WHERE slug=?",
            (slug,),
        ).fetchone()

    def close(self):
        self.conn.close()


# ─── Paper Trading Engine ─────────────────────────────────────────────────────

@dataclass
class Position:
    side:        str
    entry_price: float
    size:        float   # USD spent
    shares:      float   # size / entry_price
    entry_time:  float


class PaperTrader:
    """Simulated execution engine. No real money, no real orders."""

    def __init__(self, config: StrategyConfig, db: Database):
        self.config = config
        self.db = db
        self.bankroll = config.starting_bankroll
        self.positions: dict[str, dict[str, Position]] = {}  # slug → {side → Position}
        self._stopped_out: dict[str, set] = {}  # slug → set of sides stopped out this window
        self._last_sell_time: dict[str, dict[str, float]] = {}  # slug → {side → timestamp}
        self._load_bankroll()

    def _load_bankroll(self):
        row = self.db.conn.execute(
            "SELECT bankroll_after FROM paper_trades ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row and row["bankroll_after"]:
            self.bankroll = row["bankroll_after"]

    def get_positions(self, slug: str) -> list[Position]:
        return list(self.positions.get(slug, {}).values())

    def has_position(self, slug: str, side: str) -> bool:
        return side in self.positions.get(slug, {})

    def evaluate_entry(self, slug: str, side: str, best_ask: float,
                       best_bid: float, seconds_remaining: float,
                       btc_delta: float = 0.0, elapsed_secs: float = 0.0) -> bool:
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
        other = "down" if side == "up" else "up"
        if self.config.btc_momentum_threshold > 0:
            if side == "down" and btc_delta > self.config.btc_momentum_threshold:
                return False
            if side == "up" and btc_delta < -self.config.btc_momentum_threshold:
                return False
        if not self.config.allow_both_sides:
            if self.has_position(slug, other):
                return False
        if self.has_position(slug, side):
            return False
        size = min(self.config.max_position_size, self.bankroll)
        if size < self.config.min_position_usdc:
            return False
        return True

    def execute_buy(self, slug: str, side: str, price: float, now: float,
                    context: Optional[dict] = None):
        size   = min(self.config.max_position_size, self.bankroll)
        shares = size / price
        pos    = Position(side=side, entry_price=price, size=size,
                          shares=shares, entry_time=now)
        self.positions.setdefault(slug, {})[side] = pos
        self.bankroll -= size
        self.db.insert_trade(
            slug, now, side, "buy", price, size, "entry", 0, self.bankroll,
            shares=shares,
            seconds_remaining=context.get("seconds_remaining") if context else None,
            spread_at_trade=context.get("spread") if context else None,
            signal_json=json.dumps(context) if context else None,
        )
        logging.info(
            f"📗 PAPER BUY  {side.upper()} @ ${price:.2f} | "
            f"${size:.2f} → {shares:.1f} shares | bankroll ${self.bankroll:.2f}"
        )

    def evaluate_exit(self, slug: str, side: str, best_bid: float,
                      seconds_remaining: float, btc_delta: float = 0.0) -> Optional[str]:
        if not self.has_position(slug, side):
            return None
        if best_bid >= self.config.exit_threshold:
            return "exit_target"
        if self.config.stop_loss > 0 and best_bid <= self.config.stop_loss:
            if self.config.stop_loss_after_secs == 0 or \
               seconds_remaining <= self.config.stop_loss_after_secs:
                # Don't stop out if BTC is moving in our favor — dip may be temporary
                btc_confirms = (side == "down" and btc_delta < 0) or \
                               (side == "up"   and btc_delta > 0)
                if not btc_confirms:
                    return "stop_loss"
        if seconds_remaining <= self.config.force_exit_before_close_secs:
            neutral = self.config.hold_through_close_neutral_btc_range
            if neutral > 0 and abs(btc_delta) <= neutral:
                return None
            t = self.config.hold_through_close_btc_threshold
            if t > 0:
                if side == "up"   and btc_delta >=  t:
                    return None  # BTC strongly up — let UP resolve at $1.00
                if side == "down" and btc_delta <= -t:
                    return None  # BTC strongly down — let DOWN resolve at $1.00
            return "force_exit"
        return None

    def execute_sell(self, slug: str, side: str, price: float,
                     reason: str, now: float, context: Optional[dict] = None):
        pos_map = self.positions.get(slug, {})
        if side not in pos_map:
            return
        pos      = pos_map[side]
        proceeds = pos.shares * price
        pnl      = proceeds - pos.size
        self.bankroll += proceeds
        del self.positions[slug][side]
        self.db.insert_trade(
            slug, now, side, "sell", price, proceeds, reason, pnl, self.bankroll,
            shares=pos.shares,
            seconds_remaining=context.get("seconds_remaining") if context else None,
            spread_at_trade=context.get("spread") if context else None,
            signal_json=json.dumps(context) if context else None,
        )
        if reason == "stop_loss":
            self._stopped_out.setdefault(slug, set()).add(side)
        self._last_sell_time.setdefault(slug, {})[side] = now

        emoji = "📈" if pnl > 0 else "📉"
        logging.info(
            f"{emoji} PAPER SELL {side.upper()} @ ${price:.2f} | "
            f"{reason} | P&L ${pnl:+.2f} | bankroll ${self.bankroll:.2f}"
        )

    def handle_resolution(self, slug: str, resolution: str, now: float):
        for pos in list(self.positions.get(slug, {}).values()):
            if pos.side == resolution:
                proceeds = pos.shares * 1.0
                pnl      = proceeds - pos.size
            else:
                proceeds = 0.0
                pnl      = -pos.size
            self.bankroll += proceeds
            self.db.insert_trade(
                slug, now, pos.side, "sell",
                1.0 if pos.side == resolution else 0.0,
                proceeds, "resolution", pnl, self.bankroll,
                shares=pos.shares,
            )
            emoji = "💰" if pnl > 0 else "💀"
            logging.info(
                f"{emoji} RESOLUTION {pos.side.upper()} resolved {resolution.upper()} | "
                f"P&L ${pnl:+.2f} | bankroll ${self.bankroll:.2f}"
            )
        self.positions.pop(slug, None)


# ─── Observer ─────────────────────────────────────────────────────────────────

class Observer:
    """Main loop: discovers markets, logs ticks, runs the paper trader."""

    def __init__(self, config: StrategyConfig,
                 db_path: str = "polymarket_observer.db",
                 trader=None):
        self.config  = config
        self.db      = Database(db_path)
        self.db.save_config(config)
        self.poly    = PolymarketClient()
        self.btc     = BTCPriceClient()
        self.trader  = trader if trader is not None else PaperTrader(config, self.db)
        self.running = True
        self.markets_observed = 0
        self._current_tokens: Optional[dict] = None
        self._current_btc_open: Optional[float] = None
        self._last_mids: dict[str, tuple] = {}  # slug → (up_mid, down_mid) from prior tick
        self._window_start_bankroll: float = self.trader.bankroll

        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    def _shutdown(self, signum, frame):
        logging.info("\n🛑 Shutting down...")
        self.running = False

    # ── Extension hooks (override in subclasses) ──────────────────────────────

    def _mode_label(self) -> str:
        return "PAPER TRADING (no real money)"

    def _on_new_market(self, slug: str, tokens: dict):
        """Called once per market window after tokens are resolved. No-op by default."""
        pass

    def _get_prices(self, tokens: dict) -> tuple[Optional[dict], Optional[dict], str]:
        """Fetch current bid/ask for both sides. Override to use WebSocket.
        Returns (up_prices, down_prices, source) where source is 'rest' or 'ws'."""
        return (
            self.poly.get_price(tokens["up_token_id"]),
            self.poly.get_price(tokens["down_token_id"]),
            "rest",
        )

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        logging.info("=" * 70)
        logging.info("🔭 POLYMARKET BTC 5-MIN OBSERVER")
        logging.info(f"   Mode:     {self._mode_label()}")
        logging.info(f"   Entry:    ≤ ${self.config.entry_threshold:.2f}")
        logging.info(f"   Exit:     ≥ ${self.config.exit_threshold:.2f}")
        logging.info(f"   Stop:     "
                     + (f"${self.config.stop_loss:.2f}" if self.config.stop_loss else "disabled"))
        logging.info(f"   Sides:    {'both' if self.config.allow_both_sides else 'one'}")
        logging.info(f"   Window:   +{self.config.entry_delay_secs}s to +{self.config.max_entry_age_secs}s")
        logging.info(f"   BTC dir:  {'aligned only' if self.config.require_btc_alignment else 'contrarian allowed'}")
        logging.info(f"   Bankroll: ${self.trader.bankroll:.2f}")
        logging.info(f"   Poll:     {self.config.poll_interval_secs}s (REST fallback)")
        logging.info("=" * 70)

        last_slug = None

        while self.running:
            try:
                server_time = self.poly.get_server_time()
                window_start, window_end, slug = self.poly.compute_window_times(server_time)

                # ── New market window ─────────────────────────────────────────
                if slug != last_slug:
                    if last_slug:
                        self._finalize_market(last_slug)

                    last_slug = slug
                    self.markets_observed += 1
                    self._current_tokens = None

                    t_start = datetime.fromtimestamp(window_start, tz=timezone.utc)
                    t_end   = datetime.fromtimestamp(window_end,   tz=timezone.utc)
                    logging.info(f"\n{'─' * 70}")
                    logging.info(
                        f"🆕 Market #{self.markets_observed}: {slug} | "
                        f"{t_start:%H:%M:%S} → {t_end:%H:%M:%S} UTC"
                    )

                    event = self.poly.get_market_by_slug(slug)
                    if not event:
                        logging.warning("   ⚠️  Not indexed yet, retrying in 5s...")
                        time.sleep(5)
                        event = self.poly.get_market_by_slug(slug)
                    if not event:
                        logging.warning(f"   ❌ Could not find {slug}. Skipping window.")
                        time.sleep(self.config.poll_interval_secs)
                        continue

                    tokens = self.poly.extract_token_ids(event)
                    if not tokens:
                        logging.warning("   ❌ Could not extract token IDs. Skipping.")
                        time.sleep(self.config.poll_interval_secs)
                        continue

                    btc_open = self.btc.get_btc_price()
                    self.db.upsert_market(
                        slug, window_start, window_end,
                        tokens["market_id"], tokens["up_token_id"], tokens["down_token_id"],
                        btc_open,
                    )
                    self._current_tokens        = tokens
                    self._current_btc_open      = btc_open
                    self._window_start_bankroll = self.trader.bankroll
                    self.trader._stopped_out.clear()

                    # Notify subclasses (WebSocket subscription, LiveTrader registration, etc.)
                    self._on_new_market(slug, tokens)

                    logging.info(
                        f"   BTC open: ${btc_open:,.2f}" if btc_open else "   BTC open: unavailable"
                    )

                # ── Price poll ────────────────────────────────────────────────
                if not self._current_tokens:
                    time.sleep(self.config.poll_interval_secs)
                    continue

                now               = time.time()
                seconds_remaining = window_end - now

                if seconds_remaining < -10:
                    time.sleep(0.5)
                    continue

                up_prices, down_prices, price_source = self._get_prices(self._current_tokens)
                btc_now = self.btc.get_btc_price()

                if up_prices and down_prices:
                    crossed = []
                    if self._is_crossed_quote(up_prices):
                        crossed.append("UP")
                    if self._is_crossed_quote(down_prices):
                        crossed.append("DOWN")
                    if crossed:
                        logging.warning(
                            f"   ⚠️  Skipping crossed quote from {price_source}: {', '.join(crossed)}"
                        )
                        time.sleep(self.config.poll_interval_secs)
                        continue

                    btc_delta  = (btc_now - self._current_btc_open) \
                                 if btc_now and self._current_btc_open else 0.0
                    up_mid     = (up_prices["best_bid"]   + up_prices["best_ask"])   / 2
                    down_mid   = (down_prices["best_bid"] + down_prices["best_ask"]) / 2
                    up_spread   = up_prices["best_ask"]   - up_prices["best_bid"]
                    down_spread = down_prices["best_ask"] - down_prices["best_bid"]

                    last = self._last_mids.get(slug)
                    up_change_10s   = round(up_mid   - last[0], 4) if last else None
                    down_change_10s = round(down_mid - last[1], 4) if last else None
                    self._last_mids[slug] = (up_mid, down_mid)

                    self.db.insert_tick(
                        slug, now, seconds_remaining,
                        up_prices["best_bid"],   up_prices["best_ask"],   up_mid,
                        down_prices["best_bid"], down_prices["best_ask"], down_mid,
                        btc_now, btc_delta, price_source,
                        up_spread=up_spread, down_spread=down_spread,
                        up_change_10s=up_change_10s, down_change_10s=down_change_10s,
                    )

                    marked_value = self._marked_position_value(slug, up_mid, down_mid)
                    window_pnl = (self.trader.bankroll + marked_value) - self._window_start_bankroll
                    pnl_str    = _colored(f"ROI {window_pnl:+.2f}", _GREEN if window_pnl >= 0 else _RED)

                    logging.info(
                        f"   ⏱ {seconds_remaining:5.1f}s | "
                        f"UP  bid={up_prices['best_bid']:.2f} ask={up_prices['best_ask']:.2f} | "
                        f"DN  bid={down_prices['best_bid']:.2f} ask={down_prices['best_ask']:.2f} | "
                        f"BTC ${btc_now:,.2f} ({btc_delta:+.2f}) | {pnl_str}"
                        if btc_now else
                        f"   ⏱ {seconds_remaining:5.1f}s | "
                        f"UP  bid={up_prices['best_bid']:.2f} ask={up_prices['best_ask']:.2f} | "
                        f"DN  bid={down_prices['best_bid']:.2f} ask={down_prices['best_ask']:.2f} | "
                        f"{pnl_str}"
                    )

                    # Exits first, then entries
                    for side, prices in (("up", up_prices), ("down", down_prices)):
                        reason = self.trader.evaluate_exit(
                            slug, side, prices["best_bid"], seconds_remaining,
                            btc_delta=btc_delta
                        )
                        if reason:
                            exit_ctx = {
                                "seconds_remaining": round(seconds_remaining, 1),
                                "spread": round(prices["best_ask"] - prices["best_bid"], 4),
                                "up_mid": round(up_mid, 4),
                                "down_mid": round(down_mid, 4),
                                "btc_delta": round(btc_delta, 2),
                            }
                            self.trader.execute_sell(
                                slug, side, prices["best_bid"], reason, now,
                                context=exit_ctx,
                            )

                    elapsed_secs = 300 - seconds_remaining

                    for side, prices in (("up", up_prices), ("down", down_prices)):
                        if self.trader.evaluate_entry(
                            slug, side, prices["best_ask"], prices["best_bid"],
                            seconds_remaining, btc_delta=btc_delta, elapsed_secs=elapsed_secs
                        ):
                            entry_ctx = {
                                "seconds_remaining": round(seconds_remaining, 1),
                                "spread": round(prices["best_ask"] - prices["best_bid"], 4),
                                "up_mid": round(up_mid, 4),
                                "down_mid": round(down_mid, 4),
                                "btc_delta": round(btc_delta, 2),
                                "up_change_10s": up_change_10s,
                                "down_change_10s": down_change_10s,
                            }
                            self.trader.execute_buy(
                                slug, side, prices["best_ask"], now,
                                context=entry_ctx,
                            )

                time.sleep(self.config.poll_interval_secs)

            except KeyboardInterrupt:
                break
            except Exception as exc:
                logging.error(f"Main loop error: {exc}", exc_info=True)
                time.sleep(5)

        if last_slug:
            self._finalize_market(last_slug)
        self.db.flush_ticks()
        self._print_summary()
        self._on_before_close()
        self.db.close()

    def _on_before_close(self):
        """Called just before db.close() — override in subclasses to stop background threads."""
        pass

    def _marked_position_value(self, slug: str, up_mid: float, down_mid: float) -> float:
        total = 0.0
        for pos in self.trader.get_positions(slug):
            total += pos.shares * (up_mid if pos.side == "up" else down_mid)
        return total

    @staticmethod
    def _is_crossed_quote(prices: dict) -> bool:
        bid = prices.get("best_bid", 0.0) or 0.0
        ask = prices.get("best_ask", 0.0) or 0.0
        return bid > 0 and ask > 0 and ask < bid

    def _market_close_snapshot(self, slug: str) -> tuple[Optional[float], Optional[str], Optional[float]]:
        row = self.db.conn.execute(
            "SELECT btc_open_price, window_end_ts FROM markets WHERE slug=?",
            (slug,),
        ).fetchone()
        if not row or row["btc_open_price"] is None:
            return None, None, None

        tick = self.db.conn.execute("""
            SELECT btc_spot_price, timestamp
            FROM price_ticks
            WHERE slug=? AND btc_spot_price IS NOT NULL AND timestamp <= ?
            ORDER BY timestamp DESC
            LIMIT 1
        """, (slug, row["window_end_ts"])).fetchone()
        if tick and tick["btc_spot_price"] is not None:
            resolution = "up" if tick["btc_spot_price"] >= row["btc_open_price"] else "down"
            return tick["btc_spot_price"], resolution, tick["timestamp"]

        btc_close = self.btc.get_btc_price()
        if btc_close is None:
            return None, None, None
        resolution = "up" if btc_close >= row["btc_open_price"] else "down"
        return btc_close, resolution, None

    def _finalize_market(self, slug: str):
        """Resolve any open positions when a window closes."""
        self.db.flush_ticks()  # commit any buffered ticks before closing the window
        row = self.db.conn.execute(
            "SELECT btc_open_price, window_end_ts FROM markets WHERE slug=?", (slug,)
        ).fetchone()
        btc_close, resolution, close_ts = self._market_close_snapshot(slug)

        if row and row["btc_open_price"] and btc_close is not None and resolution:
            self.db.update_market_close(slug, btc_close, resolution)
            if self.trader.get_positions(slug):
                self.trader.handle_resolution(slug, resolution, time.time())
            window_pnl  = self.trader.bankroll - self._window_start_bankroll
            pnl_str     = _colored(f"{window_pnl:+.2f}", _GREEN if window_pnl >= 0 else _RED)
            arrow       = "▲" if window_pnl >= 0 else "▼"
            source_note = ""
            if close_ts is not None:
                lag = row["window_end_ts"] - close_ts
                source_note = f" | close from last in-window tick ({lag:.1f}s before expiry)"
            else:
                source_note = " | close from spot fallback"
            logging.info(
                f"   ✅ Resolved: {resolution.upper()} | "
                f"BTC ${row['btc_open_price']:,.2f} → ${btc_close:,.2f} | "
                f"Window P&L: {arrow} {pnl_str}  (bankroll ${self.trader.bankroll:.2f})"
                f"{source_note}"
            )

    def _print_summary(self):
        logging.info("\n" + "=" * 70)
        logging.info("📊 SESSION SUMMARY")
        logging.info("=" * 70)
        trades = self.db.conn.execute("""
            SELECT
                SUM(CASE WHEN action='sell' AND pnl > 0  THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN action='sell' AND pnl <= 0 THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN action='sell'              THEN pnl ELSE 0 END) AS total_pnl
            FROM paper_trades
        """).fetchone()
        wins   = trades["wins"]   or 0
        losses = trades["losses"] or 0
        sells  = wins + losses
        if sells:
            logging.info(f"   Round-trips: {sells} ({wins}W / {losses}L) "
                         f"— {wins/sells*100:.1f}% win rate")
            logging.info(f"   Total P&L:   ${trades['total_pnl'] or 0:+.2f}")
        logging.info(f"   Final bankroll: ${self.trader.bankroll:.2f}")
        logging.info(f"   Markets observed: {self.markets_observed}")
        logging.info("=" * 70)


# ─── Analysis ─────────────────────────────────────────────────────────────────

def analyze(db_path: str = "polymarket_observer.db"):
    db = Database(db_path)

    print("\n" + "=" * 70)
    print("📊 POLYMARKET BTC 5-MIN MARKET ANALYSIS")
    print("=" * 70)

    stats = db.conn.execute("""
        SELECT COUNT(*) AS total,
               COUNT(CASE WHEN resolution='up'   THEN 1 END) AS up_wins,
               COUNT(CASE WHEN resolution='down' THEN 1 END) AS down_wins
        FROM markets WHERE resolution IS NOT NULL
    """).fetchone()

    print(f"\nMarkets tracked: {stats['total']}")
    if stats["total"]:
        print(f"  Resolved UP:   {stats['up_wins']}  ({stats['up_wins']/stats['total']*100:.1f}%)")
        print(f"  Resolved DOWN: {stats['down_wins']} ({stats['down_wins']/stats['total']*100:.1f}%)")

    # ── Swing analysis ────────────────────────────────────────────────────────
    print(f"\n{'─' * 70}")
    print("SWING ANALYSIS: Entry ≤X → Exit ≥Y  (how often does this path exist?)")
    print(f"{'─' * 70}")

    for entry_t in (0.30, 0.35, 0.40, 0.45):
        for exit_t in (0.60, 0.65, 0.70, 0.75):
            rows = db.conn.execute("""
                SELECT
                    SUM(CASE WHEN entry_time IS NOT NULL THEN 1 ELSE 0 END) AS entries,
                    SUM(CASE WHEN exit_time  IS NOT NULL THEN 1 ELSE 0 END) AS exits
                FROM (
                    SELECT m.slug,
                           MIN(CASE WHEN t.up_best_ask <= :e  THEN t.timestamp END) AS entry_time,
                           MIN(CASE WHEN t.up_best_bid >= :x
                                    AND t.timestamp > (
                                        SELECT MIN(t2.timestamp) FROM price_ticks t2
                                        WHERE t2.slug = m.slug AND t2.up_best_ask <= :e
                                    ) THEN t.timestamp END) AS exit_time
                    FROM markets m JOIN price_ticks t ON t.slug = m.slug
                    WHERE m.resolution IS NOT NULL
                      AND (SELECT COUNT(*) FROM price_ticks WHERE slug = m.slug) >= 10
                    GROUP BY m.slug
                    UNION ALL
                    SELECT m.slug,
                           MIN(CASE WHEN t.down_best_ask <= :e  THEN t.timestamp END) AS entry_time,
                           MIN(CASE WHEN t.down_best_bid >= :x
                                    AND t.timestamp > (
                                        SELECT MIN(t2.timestamp) FROM price_ticks t2
                                        WHERE t2.slug = m.slug AND t2.down_best_ask <= :e
                                    ) THEN t.timestamp END) AS exit_time
                    FROM markets m JOIN price_ticks t ON t.slug = m.slug
                    WHERE m.resolution IS NOT NULL
                      AND (SELECT COUNT(*) FROM price_ticks WHERE slug = m.slug) >= 10
                    GROUP BY m.slug
                )
            """, {"e": entry_t, "x": exit_t}).fetchone()

            entries = rows["entries"] or 0
            exits   = rows["exits"]   or 0
            if entries:
                hit_rate = exits / entries * 100
                print(f"  ≤${entry_t:.2f} → ≥${exit_t:.2f}: "
                      f"{entries:3d} entries, {exits:3d} hit target  ({hit_rate:.1f}%)")

    # ── BTC vs market divergence ──────────────────────────────────────────────
    print(f"\n{'─' * 70}")
    print("DIVERGENCE: Market swing vs BTC actual move (last 50 markets)")
    print(f"{'─' * 70}")

    for d in db.conn.execute("""
        SELECT m.slug, m.btc_open_price, m.btc_close_price, m.resolution,
               MIN(t.up_best_ask)   AS up_min_ask,
               MAX(t.up_best_bid)   AS up_max_bid,
               MIN(t.down_best_ask) AS dn_min_ask,
               MAX(t.down_best_bid) AS dn_max_bid,
               MAX(ABS(t.btc_delta_from_open)) AS max_btc_swing
        FROM markets m JOIN price_ticks t ON t.slug = m.slug
        WHERE m.resolution IS NOT NULL
        GROUP BY m.slug ORDER BY m.window_start_ts DESC LIMIT 50
    """).fetchall():
        up_range = (d["up_max_bid"] or 0) - (d["up_min_ask"] or 0)
        dn_range = (d["dn_max_bid"] or 0) - (d["dn_min_ask"] or 0)
        btc_pct  = (abs(d["max_btc_swing"] or 0) / d["btc_open_price"] * 100
                    if d["btc_open_price"] else 0)
        print(f"  {d['slug'][-10:]} | {(d['resolution'] or '?'):>4s} | "
              f"UP Δ{up_range:.2f}  DN Δ{dn_range:.2f} | BTC {btc_pct:.3f}%")

    # ── Paper trading results ─────────────────────────────────────────────────
    print(f"\n{'─' * 70}")
    print("PAPER TRADING RESULTS")
    print(f"{'─' * 70}")

    trades = db.conn.execute(
        "SELECT * FROM paper_trades ORDER BY timestamp"
    ).fetchall()

    if not trades:
        print("  No paper trades yet — run observer.py first.")
    else:
        sells  = [t for t in trades if t["action"] == "sell"]
        wins   = [t for t in sells  if t["pnl"] > 0]
        losses = [t for t in sells  if t["pnl"] <= 0]
        if sells:
            total_pnl = sum(t["pnl"] for t in sells)
            print(f"  Buys: {sum(1 for t in trades if t['action']=='buy')}  "
                  f"Sells: {len(sells)}  "
                  f"({len(wins)}W / {len(losses)}L — {len(wins)/len(sells)*100:.1f}% win)")
            print(f"  Total P&L: ${total_pnl:+.2f}")
            if wins:
                print(f"  Avg win:  ${sum(t['pnl'] for t in wins)/len(wins):+.2f}")
            if losses:
                print(f"  Avg loss: ${sum(t['pnl'] for t in losses)/len(losses):+.2f}")
            print()
            for reason in ("exit_target", "stop_loss", "force_exit", "resolution"):
                r = [t for t in sells if t["reason"] == reason]
                if r:
                    print(f"  {reason:15s} {len(r):3d} trades  ${sum(t['pnl'] for t in r):+.2f}")

            buy_queues: dict[tuple[str, str], list] = defaultdict(list)
            round_trips = []
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc

            for trade in trades:
                key = (trade["slug"], trade["side"])
                if trade["action"] == "buy":
                    buy_queues[key].append(trade)
                    continue
                if trade["action"] != "sell" or not buy_queues[key]:
                    continue
                entry = buy_queues[key].pop(0)
                entry_dt = datetime.fromtimestamp(entry["timestamp"], local_tz)
                round_trips.append({
                    "slug": trade["slug"],
                    "side": trade["side"],
                    "entry_hour": entry_dt.hour,
                    "pnl": trade["pnl"] or 0.0,
                    "reason": trade["reason"] or "",
                })

            if round_trips:
                print(f"\n{'─' * 70}")
                print(f"PAPER TRADE TIMING ({local_tz})")
                print(f"{'─' * 70}")

                def _print_bucket(label: str, rows: list[dict]):
                    total = sum(r["pnl"] for r in rows)
                    wins = sum(1 for r in rows if r["pnl"] > 0)
                    print(f"  {label:10s} {len(rows):3d} trades  ${total:+.2f}  "
                          f"avg ${total/len(rows):+.2f}  win {wins/len(rows)*100:4.1f}%")

                session_buckets = {
                    "Overnight": [r for r in round_trips if 1 <= r["entry_hour"] < 4],
                    "Business":  [r for r in round_trips if 8 <= r["entry_hour"] < 16],
                    "Evening":   [r for r in round_trips if 16 <= r["entry_hour"] < 22],
                }
                for label, rows in session_buckets.items():
                    if rows:
                        _print_bucket(label, rows)

                print("\n  By local entry hour:")
                by_hour: dict[int, list[dict]] = defaultdict(list)
                for trip in round_trips:
                    by_hour[trip["entry_hour"]].append(trip)
                for hour in sorted(by_hour):
                    rows = by_hour[hour]
                    total = sum(r["pnl"] for r in rows)
                    wins = sum(1 for r in rows if r["pnl"] > 0)
                    print(f"    {hour:02d}:00  {len(rows):3d} trades  ${total:+.2f}  "
                          f"avg ${total/len(rows):+.2f}  win {wins/len(rows)*100:4.1f}%")

                print("\n  By exit reason:")
                by_reason: dict[str, list[dict]] = defaultdict(list)
                for trip in round_trips:
                    by_reason[trip["reason"]].append(trip)
                for reason in ("exit_target", "stop_loss", "force_exit", "resolution"):
                    rows = by_reason.get(reason, [])
                    if not rows:
                        continue
                    total = sum(r["pnl"] for r in rows)
                    wins = sum(1 for r in rows if r["pnl"] > 0)
                    print(f"    {reason:12s} {len(rows):3d} trades  ${total:+.2f}  "
                          f"avg ${total/len(rows):+.2f}  win {wins/len(rows)*100:4.1f}%")
        print(f"\n  Final bankroll: ${trades[-1]['bankroll_after']:.2f}")

    db.close()


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket BTC 5-Min Observer & Paper Trader"
    )
    parser.add_argument("--analyze",     action="store_true", help="Analyze collected data")
    parser.add_argument("--db",          default="polymarket_observer.db")
    parser.add_argument("--entry",       type=float, default=0.38, help="Buy threshold  (default 0.38)")
    parser.add_argument("--exit",        type=float, default=0.70, help="Sell threshold (default 0.70)")
    parser.add_argument("--stop-loss",   type=float, default=0.0,  help="Stop-loss price (0=off)")
    parser.add_argument("--bankroll",    type=float, default=1000.0)
    parser.add_argument("--max-position", type=float, default=50.0, help="Max USDC per side per market (default: 50)")
    parser.add_argument("--min-position", type=float, default=5.0, help="Min USDC per trade — avoid dust orders (default: 5)")
    parser.add_argument("--poll",        type=float, default=3.0,  help="REST poll interval seconds")
    parser.add_argument("--single-side",    action="store_true",    help="Deprecated: single-side is now the default")
    parser.add_argument("--both-sides",     action="store_true",    help="Allow both UP and DOWN positions in the same market")
    parser.add_argument("--entry-delay",    type=int,   default=60,  help="Seconds to wait before first buy (default 60)")
    parser.add_argument("--max-entry-age",  type=int,   default=150, help="Stop opening new positions after N seconds from window open (default 150, 0=off)")
    parser.add_argument("--btc-momentum",   type=float, default=0.0, help="Skip buy if BTC moved $X against the side (0=off)")
    parser.add_argument("--allow-contrarian", action="store_true", help="Allow entries against BTC's move from window open")
    parser.add_argument("--hold-threshold",    type=float, default=15.0, help="Hold through close if BTC moved $X in your favor (default 15.0, 0=off)")
    parser.add_argument("--hold-neutral-range", type=float, default=5.0,
                        help="Hold through close if BTC is still within $X of the open near expiry (default 5.0, 0=off)")
    parser.add_argument("--cooldown",          type=int,   default=10,  help="Seconds to block re-entry after a sell (default 10, 0=off)")
    parser.add_argument("--stop-loss-after",   type=int,   default=60, help="Only trigger stop_loss in final N seconds of window (default 60, 0=anytime)")
    parser.add_argument("--min-entry",         type=float, default=0.15, help="Reject entries below this price (default 0.15, 0=disabled)")
    parser.add_argument("--only-side",         default="", choices=["", "up", "down"], help="Restrict entries to one side only")
    parser.add_argument("--log-level",      default="INFO")
    args = parser.parse_args()

    fmt = "%(asctime)s %(message)s"

    class _PlainFormatter(logging.Formatter):
        def format(self, record):
            return _ANSI.sub('', super().format(record))

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))

    file_handler = logging.FileHandler("observer.log")
    file_handler.setFormatter(_PlainFormatter(fmt, datefmt="%H:%M:%S"))

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        handlers=[console_handler, file_handler],
    )

    if args.analyze:
        analyze(args.db)
        return

    config = StrategyConfig(
        entry_threshold=args.entry,
        exit_threshold=args.exit,
        stop_loss=args.stop_loss,
        starting_bankroll=args.bankroll,
        max_position_size=args.max_position,
        min_position_usdc=args.min_position,
        poll_interval_secs=args.poll,
        allow_both_sides=args.both_sides,
        entry_delay_secs=args.entry_delay,
        max_entry_age_secs=args.max_entry_age,
        require_btc_alignment=not args.allow_contrarian,
        btc_momentum_threshold=args.btc_momentum,
        hold_through_close_btc_threshold=args.hold_threshold,
        hold_through_close_neutral_btc_range=args.hold_neutral_range,
        stop_loss_after_secs=args.stop_loss_after,
        min_entry_price=args.min_entry,
        post_sell_cooldown_secs=args.cooldown,
        only_side=args.only_side,
    )
    Observer(config, db_path=args.db).run()


if __name__ == "__main__":
    main()
