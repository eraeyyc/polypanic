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
from dataclasses import dataclass, asdict
from typing import Optional
import requests


# ─── Constants ────────────────────────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"


# ─── Configuration ────────────────────────────────────────────────────────────

@dataclass
class StrategyConfig:
    """Strategy parameters — tune these after running --analyze."""

    # Entry: buy a side when its best ask is at or below this price
    entry_threshold: float = 0.40

    # Exit: sell when best bid hits this price
    exit_threshold: float = 0.65

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
    allow_both_sides: bool = True

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

    def get_price(self, token_id: str) -> Optional[dict]:
        """Return {"best_bid": float, "best_ask": float} for a token."""
        try:
            buy  = self.session.get(f"{CLOB_API}/price",
                                    params={"token_id": token_id, "side": "BUY"}, timeout=5)
            sell = self.session.get(f"{CLOB_API}/price",
                                    params={"token_id": token_id, "side": "SELL"}, timeout=5)
            return {
                "best_bid": float(buy.json().get("price", 0)),
                "best_ask": float(sell.json().get("price", 0)),
            }
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
    """Multi-source BTC/USD spot price with automatic fallback."""

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

            CREATE TABLE IF NOT EXISTS strategy_config (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                config_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_ticks_slug       ON price_ticks(slug);
            CREATE INDEX IF NOT EXISTS idx_ticks_time       ON price_ticks(timestamp);
            CREATE INDEX IF NOT EXISTS idx_trades_slug      ON paper_trades(slug);
            CREATE INDEX IF NOT EXISTS idx_live_trades_slug ON live_trades(slug);
        """)
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
                    btc_spot, btc_delta, source="rest"):
        self.conn.execute("""
            INSERT INTO price_ticks
            (slug, timestamp, seconds_remaining,
             up_best_bid, up_best_ask, up_midpoint,
             down_best_bid, down_best_ask, down_midpoint,
             btc_spot_price, btc_delta_from_open, price_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (slug, timestamp, seconds_remaining,
              up_bid, up_ask, up_mid,
              down_bid, down_ask, down_mid,
              btc_spot, btc_delta, source))
        self.conn.commit()

    # ── Paper trades ──────────────────────────────────────────────────────────

    def insert_trade(self, slug, timestamp, side, action,
                     price, size, reason, pnl, bankroll):
        self.conn.execute("""
            INSERT INTO paper_trades
            (slug, timestamp, side, action, price, size, reason, pnl, bankroll_after)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (slug, timestamp, side, action, price, size, reason, pnl, bankroll))
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
        self.positions: dict[str, list[Position]] = {}  # slug → [Position]
        self._load_bankroll()

    def _load_bankroll(self):
        row = self.db.conn.execute(
            "SELECT bankroll_after FROM paper_trades ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row and row["bankroll_after"]:
            self.bankroll = row["bankroll_after"]

    def get_positions(self, slug: str) -> list[Position]:
        return self.positions.get(slug, [])

    def has_position(self, slug: str, side: str) -> bool:
        return any(p.side == side for p in self.get_positions(slug))

    def evaluate_entry(self, slug: str, side: str, best_ask: float,
                       seconds_remaining: float, btc_delta: float) -> bool:
        if best_ask <= 0 or best_ask > self.config.entry_threshold:
            return False
        if seconds_remaining < self.config.min_time_remaining_secs:
            return False
        if not self.config.allow_both_sides:
            other = "down" if side == "up" else "up"
            if self.has_position(slug, other):
                return False
        if self.has_position(slug, side):
            return False
        if self.bankroll < self.config.min_position_usdc:
            return False
        return True

    def execute_buy(self, slug: str, side: str, price: float, now: float):
        size   = min(self.config.max_position_size, self.bankroll)
        shares = size / price
        pos    = Position(side=side, entry_price=price, size=size,
                          shares=shares, entry_time=now)
        self.positions.setdefault(slug, []).append(pos)
        self.bankroll -= size
        self.db.insert_trade(slug, now, side, "buy", price, size, "entry", 0, self.bankroll)
        logging.info(
            f"📗 PAPER BUY  {side.upper()} @ ${price:.2f} | "
            f"${size:.2f} → {shares:.1f} shares | bankroll ${self.bankroll:.2f}"
        )

    def evaluate_exit(self, slug: str, side: str, best_bid: float,
                      seconds_remaining: float) -> Optional[str]:
        if not self.has_position(slug, side):
            return None
        if best_bid >= self.config.exit_threshold:
            return "exit_target"
        if self.config.stop_loss > 0 and best_bid <= self.config.stop_loss:
            return "stop_loss"
        if seconds_remaining <= self.config.force_exit_before_close_secs:
            return "force_exit"
        return None

    def execute_sell(self, slug: str, side: str, price: float,
                     reason: str, now: float):
        positions = [p for p in self.get_positions(slug) if p.side == side]
        if not positions:
            return
        pos      = positions[0]
        proceeds = pos.shares * price
        pnl      = proceeds - pos.size
        self.bankroll += proceeds
        self.positions[slug] = [p for p in self.positions[slug] if p.side != side]
        self.db.insert_trade(slug, now, side, "sell", price, proceeds,
                             reason, pnl, self.bankroll)
        emoji = "📈" if pnl > 0 else "📉"
        logging.info(
            f"{emoji} PAPER SELL {side.upper()} @ ${price:.2f} | "
            f"{reason} | P&L ${pnl:+.2f} | bankroll ${self.bankroll:.2f}"
        )

    def handle_resolution(self, slug: str, resolution: str, now: float):
        for pos in self.get_positions(slug):
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

    def _get_prices(self, tokens: dict) -> tuple[Optional[dict], Optional[dict]]:
        """Fetch current bid/ask for both sides. Override to use WebSocket."""
        return (
            self.poly.get_price(tokens["up_token_id"]),
            self.poly.get_price(tokens["down_token_id"]),
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
                    self._current_tokens   = tokens
                    self._current_btc_open = btc_open

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

                up_prices, down_prices = self._get_prices(self._current_tokens)
                btc_now = self.btc.get_btc_price()

                if up_prices and down_prices:
                    btc_delta = (btc_now - self._current_btc_open) \
                                if btc_now and self._current_btc_open else 0.0
                    up_mid   = (up_prices["best_bid"]   + up_prices["best_ask"])   / 2
                    down_mid = (down_prices["best_bid"] + down_prices["best_ask"]) / 2

                    self.db.insert_tick(
                        slug, now, seconds_remaining,
                        up_prices["best_bid"],   up_prices["best_ask"],   up_mid,
                        down_prices["best_bid"], down_prices["best_ask"], down_mid,
                        btc_now, btc_delta,
                    )

                    logging.info(
                        f"   ⏱ {seconds_remaining:5.1f}s | "
                        f"UP  bid={up_prices['best_bid']:.2f} ask={up_prices['best_ask']:.2f} | "
                        f"DN  bid={down_prices['best_bid']:.2f} ask={down_prices['best_ask']:.2f} | "
                        f"BTC ${btc_now:,.2f} ({btc_delta:+.2f})"
                        if btc_now else
                        f"   ⏱ {seconds_remaining:5.1f}s | "
                        f"UP  bid={up_prices['best_bid']:.2f} ask={up_prices['best_ask']:.2f} | "
                        f"DN  bid={down_prices['best_bid']:.2f} ask={down_prices['best_ask']:.2f}"
                    )

                    # Exits first, then entries
                    for side, prices in (("up", up_prices), ("down", down_prices)):
                        reason = self.trader.evaluate_exit(
                            slug, side, prices["best_bid"], seconds_remaining
                        )
                        if reason:
                            self.trader.execute_sell(slug, side, prices["best_bid"], reason, now)

                    for side, prices in (("up", up_prices), ("down", down_prices)):
                        if self.trader.evaluate_entry(
                            slug, side, prices["best_ask"], seconds_remaining, btc_delta
                        ):
                            self.trader.execute_buy(slug, side, prices["best_ask"], now)

                time.sleep(self.config.poll_interval_secs)

            except KeyboardInterrupt:
                break
            except Exception as exc:
                logging.error(f"Main loop error: {exc}", exc_info=True)
                time.sleep(5)

        if last_slug:
            self._finalize_market(last_slug)
        self._print_summary()
        self.db.close()

    def _finalize_market(self, slug: str):
        """Resolve any open positions when a window closes."""
        btc_close = self.btc.get_btc_price()
        row = self.db.conn.execute(
            "SELECT btc_open_price FROM markets WHERE slug=?", (slug,)
        ).fetchone()

        if row and row["btc_open_price"] and btc_close:
            resolution = "up" if btc_close >= row["btc_open_price"] else "down"
            self.db.update_market_close(slug, btc_close, resolution)
            if self.trader.get_positions(slug):
                self.trader.handle_resolution(slug, resolution, time.time())
            logging.info(
                f"   ✅ Resolved: {resolution.upper()} | "
                f"BTC ${row['btc_open_price']:,.2f} → ${btc_close:,.2f}"
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
        print(f"\n  Final bankroll: ${trades[-1]['bankroll_after']:.2f}")

    db.close()


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket BTC 5-Min Observer & Paper Trader"
    )
    parser.add_argument("--analyze",     action="store_true", help="Analyze collected data")
    parser.add_argument("--db",          default="polymarket_observer.db")
    parser.add_argument("--entry",       type=float, default=0.40, help="Buy threshold  (default 0.40)")
    parser.add_argument("--exit",        type=float, default=0.65, help="Sell threshold (default 0.65)")
    parser.add_argument("--stop-loss",   type=float, default=0.0,  help="Stop-loss price (0=off)")
    parser.add_argument("--bankroll",    type=float, default=1000.0)
    parser.add_argument("--poll",        type=float, default=3.0,  help="REST poll interval seconds")
    parser.add_argument("--single-side", action="store_true",      help="Only one position per market")
    parser.add_argument("--log-level",   default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("observer.log"),
        ],
    )

    if args.analyze:
        analyze(args.db)
        return

    config = StrategyConfig(
        entry_threshold=args.entry,
        exit_threshold=args.exit,
        stop_loss=args.stop_loss,
        starting_bankroll=args.bankroll,
        poll_interval_secs=args.poll,
        allow_both_sides=not args.single_side,
    )
    Observer(config, db_path=args.db).run()


if __name__ == "__main__":
    main()
