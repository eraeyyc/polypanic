#!/usr/bin/env python3
"""
Paired hold-to-resolution research tools for Polymarket BTC 5-minute markets.

This script intentionally lives outside the legacy observer/live-trader strategy
path. It supports:
  - collecting a clean BTC 5-minute market dataset into a dedicated SQLite DB
  - simulating paired buy-both-side policies against recorded ticks + outcomes
  - comparing simple policy families without mutating the legacy strategy code
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import statistics
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from observer import BTCPriceClient, PolymarketClient

try:
    import websockets
    HAS_WS = True
except ImportError:
    HAS_WS = False


DEFAULT_DB = "paired_research.db"
POLYMARKET_MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_BTC_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"


@dataclass
class ResearchTick:
    timestamp: float
    seconds_remaining: float
    up_bid: float
    up_ask: float
    down_bid: float
    down_ask: float
    btc_spot: float
    btc_delta: float
    price_source: str = "rest"
    btc_source_ts: float = 0.0
    up_quote_ts: float = 0.0
    down_quote_ts: float = 0.0


@dataclass
class MarketContext:
    slug: str
    winner: str
    up_token_id: str
    down_token_id: str
    btc_open: float
    ticks: list[ResearchTick]


@dataclass
class SimPosition:
    up_spend: float = 0.0
    down_spend: float = 0.0
    up_shares: float = 0.0
    down_shares: float = 0.0
    first_buy_s: Optional[int] = None
    last_buy_s: Optional[int] = None

    @property
    def combined_spend(self) -> float:
        return self.up_spend + self.down_spend

    @property
    def payout_if_up(self) -> float:
        return self.up_shares

    @property
    def payout_if_down(self) -> float:
        return self.down_shares

    @property
    def combined_cost_ratio(self) -> float:
        best_payout = max(self.up_shares, self.down_shares)
        if best_payout <= 0:
            return 0.0
        return self.combined_spend / best_payout


@dataclass
class MarketQualityCheck:
    ok: bool
    reason: Optional[str] = None


@dataclass
class EarlyMomentumObservation:
    slug: str
    winner: str
    checkpoint_s: int
    elapsed_s: float
    btc_delta: float


@dataclass
class LagObservation:
    slug: str
    winner: str
    direction: str
    btc_event_elapsed_s: float
    poly_event_elapsed_s: float
    lag_secs: float
    btc_delta: float
    ask_gap: float


class ResearchDatabase:
    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS research_markets (
                slug            TEXT PRIMARY KEY,
                window_start_ts INTEGER,
                window_end_ts   INTEGER,
                market_id       TEXT,
                condition_id    TEXT,
                up_token_id     TEXT,
                down_token_id   TEXT,
                btc_open_price  REAL,
                btc_close_price REAL,
                resolution      TEXT,
                created_at      REAL DEFAULT (unixepoch())
            );

            CREATE TABLE IF NOT EXISTS research_ticks (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                slug               TEXT,
                timestamp          REAL,
                seconds_remaining  REAL,
                up_best_bid        REAL,
                up_best_ask        REAL,
                down_best_bid      REAL,
                down_best_ask      REAL,
                btc_spot_price     REAL,
                btc_delta_from_open REAL,
                price_source       TEXT DEFAULT 'rest',
                btc_source_ts      REAL DEFAULT 0,
                up_quote_ts        REAL DEFAULT 0,
                down_quote_ts      REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS research_sim_results (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                slug               TEXT,
                policy             TEXT,
                up_spend           REAL,
                down_spend         REAL,
                up_shares          REAL,
                down_shares        REAL,
                combined_spend     REAL,
                payout_if_up       REAL,
                payout_if_down     REAL,
                winner             TEXT,
                gross_pnl          REAL,
                roi                REAL,
                adverse_pnl        REAL,
                net_pnl            REAL,
                first_buy_s        REAL,
                last_buy_s         REAL,
                meta_json          TEXT,
                created_at         REAL DEFAULT (unixepoch())
            );

            CREATE INDEX IF NOT EXISTS idx_research_ticks_slug_ts
                ON research_ticks(slug, timestamp);
            CREATE INDEX IF NOT EXISTS idx_research_sim_policy
                ON research_sim_results(policy, slug);
            """
        )
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(research_ticks)").fetchall()}
        extra_columns = {
            "btc_source_ts": "ALTER TABLE research_ticks ADD COLUMN btc_source_ts REAL DEFAULT 0",
            "up_quote_ts": "ALTER TABLE research_ticks ADD COLUMN up_quote_ts REAL DEFAULT 0",
            "down_quote_ts": "ALTER TABLE research_ticks ADD COLUMN down_quote_ts REAL DEFAULT 0",
        }
        for column, ddl in extra_columns.items():
            if column not in existing:
                self.conn.execute(ddl)
        self.conn.commit()

    def upsert_market(self, market: dict):
        self.conn.execute(
            """
            INSERT INTO research_markets
            (slug, window_start_ts, window_end_ts, market_id, condition_id,
             up_token_id, down_token_id, btc_open_price, btc_close_price, resolution)
            VALUES
            (:slug, :window_start_ts, :window_end_ts, :market_id, :condition_id,
             :up_token_id, :down_token_id, :btc_open_price, :btc_close_price, :resolution)
            ON CONFLICT(slug) DO UPDATE SET
                market_id=COALESCE(NULLIF(excluded.market_id, ''), research_markets.market_id),
                condition_id=COALESCE(NULLIF(excluded.condition_id, ''), research_markets.condition_id),
                up_token_id=COALESCE(NULLIF(excluded.up_token_id, ''), research_markets.up_token_id),
                down_token_id=COALESCE(NULLIF(excluded.down_token_id, ''), research_markets.down_token_id),
                btc_open_price=COALESCE(research_markets.btc_open_price, excluded.btc_open_price),
                btc_close_price=COALESCE(excluded.btc_close_price, research_markets.btc_close_price),
                resolution=COALESCE(excluded.resolution, research_markets.resolution)
            """,
            market,
        )
        self.conn.commit()

    def insert_tick(self, slug: str, tick: ResearchTick):
        self.conn.execute(
            """
            INSERT INTO research_ticks
            (slug, timestamp, seconds_remaining, up_best_bid, up_best_ask,
             down_best_bid, down_best_ask, btc_spot_price, btc_delta_from_open,
             price_source, btc_source_ts, up_quote_ts, down_quote_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slug,
                tick.timestamp,
                tick.seconds_remaining,
                tick.up_bid,
                tick.up_ask,
                tick.down_bid,
                tick.down_ask,
                tick.btc_spot,
                tick.btc_delta,
                tick.price_source,
                tick.btc_source_ts,
                tick.up_quote_ts,
                tick.down_quote_ts,
            ),
        )
        self.conn.commit()

    def list_simulatable_markets(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM research_markets
            WHERE resolution IN ('Up', 'Down')
              AND btc_open_price IS NOT NULL
              AND EXISTS (SELECT 1 FROM research_ticks t WHERE t.slug = research_markets.slug)
            ORDER BY window_start_ts
            """
        ).fetchall()

    def load_market_context(self, slug: str) -> Optional[MarketContext]:
        market = self.conn.execute(
            "SELECT * FROM research_markets WHERE slug=?",
            (slug,),
        ).fetchone()
        if not market:
            return None
        ticks = [
            ResearchTick(
                timestamp=row["timestamp"],
                seconds_remaining=row["seconds_remaining"],
                up_bid=row["up_best_bid"],
                up_ask=row["up_best_ask"],
                down_bid=row["down_best_bid"],
                down_ask=row["down_best_ask"],
                btc_spot=row["btc_spot_price"],
                btc_delta=row["btc_delta_from_open"],
                price_source=row["price_source"] or "rest",
                btc_source_ts=row["btc_source_ts"] or 0.0,
                up_quote_ts=row["up_quote_ts"] or 0.0,
                down_quote_ts=row["down_quote_ts"] or 0.0,
            )
            for row in self.conn.execute(
                "SELECT * FROM research_ticks WHERE slug=? ORDER BY timestamp",
                (slug,),
            ).fetchall()
        ]
        return MarketContext(
            slug=slug,
            winner=market["resolution"],
            up_token_id=market["up_token_id"],
            down_token_id=market["down_token_id"],
            btc_open=market["btc_open_price"] or 0.0,
            ticks=ticks,
        )

    def insert_sim_result(self, row: dict):
        self.conn.execute(
            """
            INSERT INTO research_sim_results
            (slug, policy, up_spend, down_spend, up_shares, down_shares, combined_spend,
             payout_if_up, payout_if_down, winner, gross_pnl, roi, adverse_pnl, net_pnl,
             first_buy_s, last_buy_s, meta_json)
            VALUES
            (:slug, :policy, :up_spend, :down_spend, :up_shares, :down_shares, :combined_spend,
             :payout_if_up, :payout_if_down, :winner, :gross_pnl, :roi, :adverse_pnl, :net_pnl,
             :first_buy_s, :last_buy_s, :meta_json)
            """,
            row,
        )
        self.conn.commit()

    def delete_sim_results(self, policy: Optional[str] = None):
        if policy:
            self.conn.execute("DELETE FROM research_sim_results WHERE policy=?", (policy,))
        else:
            self.conn.execute("DELETE FROM research_sim_results")
        self.conn.commit()

    def summarize_sim_policy(self, policy: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT COUNT(*) AS n,
                   AVG(gross_pnl) AS avg_gross,
                   SUM(gross_pnl) AS total_gross,
                   AVG(net_pnl) AS avg_net,
                   SUM(net_pnl) AS total_net,
                   AVG(roi) AS avg_roi,
                   MIN(net_pnl) AS worst_net
            FROM research_sim_results
            WHERE policy=?
            """,
            (policy,),
        ).fetchone()

    def close(self):
        self.conn.close()


def _parse_event_ts(value, fallback: float) -> float:
    try:
        ts = float(value)
    except Exception:
        return fallback
    if ts > 1_000_000_000_000:
        return ts / 1000.0
    return ts


class BinanceBTCStream:
    STALE_SEC = 3.0

    def __init__(self, url: str = BINANCE_BTC_WS):
        self.url = url
        self._price = 0.0
        self._event_ts = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if not HAS_WS:
            logging.warning("websockets not installed; Binance BTC stream unavailable")
            return
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="binance-btc-stream", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def get_price(self) -> Optional[dict]:
        with self._lock:
            if self._price <= 0 or (time.time() - self._event_ts) > self.STALE_SEC:
                return None
            return {
                "price": self._price,
                "source_ts": self._event_ts,
                "source": "binance_ws",
            }

    def _run(self):
        asyncio.run(self._run_async())

    async def _run_async(self):
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, ping_interval=20, open_timeout=10) as sock:
                    async for raw in sock:
                        if self._stop.is_set():
                            break
                        payload = json.loads(raw)
                        price = float(payload.get("p", 0.0))
                        if price <= 0:
                            continue
                        event_ts = _parse_event_ts(payload.get("T") or payload.get("E"), time.time())
                        with self._lock:
                            self._price = price
                            self._event_ts = event_ts
            except Exception as exc:
                logging.warning("Binance BTC stream reconnecting after error: %s", exc)
                await asyncio.sleep(1.0)


class PolymarketBestBidAskStream:
    STALE_SEC = 3.0
    PING_INTERVAL = 10.0

    def __init__(self, url: str = POLYMARKET_MARKET_WS):
        self.url = url
        self._quotes: dict[str, dict] = {}
        self._token_ids: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def subscribe(self, token_ids: list[str]):
        self.stop()
        self._token_ids = list(token_ids)
        with self._lock:
            self._quotes.clear()
        if not HAS_WS:
            logging.warning("websockets not installed; Polymarket market stream unavailable")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="polymarket-bba-stream", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def get_quote(self, token_id: str) -> Optional[dict]:
        with self._lock:
            quote = self._quotes.get(token_id)
            if not quote:
                return None
            if (time.time() - quote["source_ts"]) > self.STALE_SEC:
                return None
            return dict(quote)

    def _run(self):
        asyncio.run(self._run_async())

    async def _run_async(self):
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, ping_interval=None, open_timeout=10) as sock:
                    await sock.send(json.dumps({
                        "assets_ids": self._token_ids,
                        "type": "market",
                        "custom_feature_enabled": True,
                    }))
                    last_ping = time.monotonic()
                    while not self._stop.is_set():
                        timeout = max(0.1, self.PING_INTERVAL - (time.monotonic() - last_ping))
                        try:
                            raw = await asyncio.wait_for(sock.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            await sock.send("PING")
                            last_ping = time.monotonic()
                            continue
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", "replace")
                        messages = json.loads(raw)
                        if isinstance(messages, dict):
                            messages = [messages]
                        for message in messages:
                            self._handle_message(message)
            except Exception as exc:
                logging.warning("Polymarket market stream reconnecting after error: %s", exc)
                await asyncio.sleep(1.0)

    def _handle_message(self, message: dict):
        event_type = message.get("event_type")
        if event_type == "book":
            token_id = message.get("asset_id")
            if not token_id:
                return
            bids = [float(level["price"]) for level in (message.get("bids") or []) if float(level.get("size", 0)) > 0]
            asks = [float(level["price"]) for level in (message.get("asks") or []) if float(level.get("size", 0)) > 0]
            if not asks:
                return
            quote = {
                "best_bid": max(bids) if bids else 0.0,
                "best_ask": min(asks),
                "source_ts": _parse_event_ts(message.get("timestamp"), time.time()),
                "source": "poly_ws",
            }
            with self._lock:
                self._quotes[token_id] = quote
            return
        if event_type == "best_bid_ask":
            token_id = message.get("asset_id")
            if not token_id:
                return
            quote = {
                "best_bid": float(message.get("best_bid", 0.0)),
                "best_ask": float(message.get("best_ask", 0.0)),
                "source_ts": _parse_event_ts(message.get("timestamp"), time.time()),
                "source": "poly_ws",
            }
            with self._lock:
                self._quotes[token_id] = quote
            return
        if event_type == "price_change":
            timestamp = _parse_event_ts(message.get("timestamp"), time.time())
            for change in message.get("price_changes") or []:
                token_id = change.get("asset_id")
                if not token_id:
                    continue
                best_bid = float(change.get("best_bid", 0.0))
                best_ask = float(change.get("best_ask", 0.0))
                if best_ask <= 0:
                    continue
                with self._lock:
                    self._quotes[token_id] = {
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                        "source_ts": timestamp,
                        "source": "poly_ws",
                    }


class ResearchCollector:
    def __init__(self, db: ResearchDatabase):
        self.db = db
        self.poly = PolymarketClient()
        self.btc = BTCPriceClient()
        self.btc_ws = BinanceBTCStream()
        self.market_ws = PolymarketBestBidAskStream()

    def _fetch_resolution(self, slug: str) -> Optional[str]:
        event = self.poly.get_market_by_slug(slug)
        if not event:
            return None
        markets = event.get("markets") or []
        if not markets:
            return None
        market = markets[0]
        outcomes = market.get("outcomes")
        prices = market.get("outcomePrices")
        try:
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if isinstance(prices, str):
                prices = json.loads(prices)
            for outcome, price in zip(outcomes or [], prices or []):
                if float(price) >= 0.999:
                    return str(outcome)
        except Exception:
            return None
        return None

    def run(
        self,
        duration_hours: float = 24.0,
        poll_interval: float = 3.0,
        status_interval: float = 30.0,
        btc_stream: str = "rest",
        market_stream: str = "rest",
    ):
        start = time.time()
        current_slug = None
        current_tokens = None
        btc_open = None
        tick_count = 0
        market_tick_count = 0
        last_status_at = 0.0
        last_tick: Optional[ResearchTick] = None
        if btc_stream == "binance_ws":
            self.btc_ws.start()
        try:
            while time.time() - start < duration_hours * 3600:
                window_start, window_end, slug = self.poly.compute_window_times()
                if slug != current_slug:
                    if current_slug and market_tick_count:
                        logging.info(
                            "Completed market %s | %d ticks collected",
                            current_slug,
                            market_tick_count,
                        )
                    event = self.poly.get_market_by_slug(slug)
                    tokens = self.poly.extract_token_ids(event) if event else None
                    btc_open_snapshot = self.btc_ws.get_price() if btc_stream == "binance_ws" else None
                    btc_open = btc_open_snapshot["price"] if btc_open_snapshot else self.btc.get_btc_price()
                    market_tick_count = 0
                    last_tick = None
                    if tokens:
                        if market_stream == "ws":
                            self.market_ws.subscribe([tokens["up_token_id"], tokens["down_token_id"]])
                        self.db.upsert_market(
                            {
                                "slug": slug,
                                "window_start_ts": window_start,
                                "window_end_ts": window_end,
                                "market_id": tokens.get("market_id", ""),
                                "condition_id": tokens.get("condition_id", ""),
                                "up_token_id": tokens["up_token_id"],
                                "down_token_id": tokens["down_token_id"],
                                "btc_open_price": btc_open,
                                "btc_close_price": None,
                                "resolution": None,
                            }
                        )
                        logging.info("Tracking research market %s", slug)
                    current_slug = slug
                    current_tokens = tokens

                now = time.time()
                if current_tokens:
                    if market_stream == "ws":
                        up = self.market_ws.get_quote(current_tokens["up_token_id"])
                        down = self.market_ws.get_quote(current_tokens["down_token_id"])
                    else:
                        up = self.poly.get_price(current_tokens["up_token_id"])
                        down = self.poly.get_price(current_tokens["down_token_id"])
                    btc_snapshot = self.btc_ws.get_price() if btc_stream == "binance_ws" else None
                    if btc_snapshot:
                        btc_now = btc_snapshot["price"]
                        btc_source_ts = btc_snapshot["source_ts"]
                        price_source = f"{market_stream}+{btc_snapshot['source']}"
                    else:
                        btc_now = self.btc.get_btc_price()
                        btc_source_ts = now
                        price_source = f"{market_stream}+rest"
                    if up and down and btc_now:
                        tick = ResearchTick(
                            timestamp=now,
                            seconds_remaining=max(0.0, window_end - now),
                            up_bid=up["best_bid"],
                            up_ask=up["best_ask"],
                            down_bid=down["best_bid"],
                            down_ask=down["best_ask"],
                            btc_spot=btc_now,
                            btc_delta=(btc_now - btc_open) if btc_open else 0.0,
                            price_source=price_source,
                            btc_source_ts=btc_source_ts,
                            up_quote_ts=up.get("source_ts", now),
                            down_quote_ts=down.get("source_ts", now),
                        )
                        self.db.insert_tick(current_slug, tick)
                        tick_count += 1
                        market_tick_count += 1
                        last_tick = tick
                        if status_interval > 0 and (now - last_status_at) >= status_interval:
                            logging.info(
                                "Collecting %s | ticks=%d market_ticks=%d | %.1fs left | "
                                "UP %.2f/%.2f DN %.2f/%.2f | BTC $%.2f (%+.2f) | %s",
                                current_slug,
                                tick_count,
                                market_tick_count,
                                tick.seconds_remaining,
                                tick.up_bid,
                                tick.up_ask,
                                tick.down_bid,
                                tick.down_ask,
                                tick.btc_spot,
                                tick.btc_delta,
                                tick.price_source,
                            )
                            last_status_at = now

                for market in self.db.conn.execute(
                    """
                    SELECT slug, window_end_ts FROM research_markets
                    WHERE resolution IS NULL AND window_end_ts <= ?
                    """,
                    (time.time() - 15.0,),
                ).fetchall():
                    winner = self._fetch_resolution(market["slug"])
                    btc_close_snapshot = self.btc_ws.get_price() if btc_stream == "binance_ws" else None
                    btc_close = btc_close_snapshot["price"] if btc_close_snapshot else self.btc.get_btc_price()
                    if winner:
                        self.db.upsert_market(
                            {
                                "slug": market["slug"],
                                "window_start_ts": None,
                                "window_end_ts": market["window_end_ts"],
                                "market_id": "",
                                "condition_id": "",
                                "up_token_id": "",
                                "down_token_id": "",
                                "btc_open_price": None,
                                "btc_close_price": btc_close,
                                "resolution": winner,
                            }
                        )
                        logging.info("Resolved %s -> %s", market["slug"], winner)
                time.sleep(poll_interval)
        finally:
            self.market_ws.stop()
            self.btc_ws.stop()
            if current_slug and market_tick_count:
                if last_tick is not None:
                    logging.info(
                        "Collector stopped on %s | %d ticks in current market | %.1fs left on last tick",
                        current_slug,
                        market_tick_count,
                        last_tick.seconds_remaining,
                    )
                else:
                    logging.info("Collector stopped on %s | %d ticks in current market", current_slug, market_tick_count)


def _mark_buy(pos: SimPosition, side: str, spend: float, ask: float, seconds_remaining: float):
    if spend <= 0 or ask <= 0:
        return
    shares = spend / ask
    if side == "up":
        pos.up_spend += spend
        pos.up_shares += shares
    else:
        pos.down_spend += spend
        pos.down_shares += shares
    entry_s = int(300 - seconds_remaining)
    pos.first_buy_s = entry_s if pos.first_buy_s is None else min(pos.first_buy_s, entry_s)
    pos.last_buy_s = entry_s if pos.last_buy_s is None else max(pos.last_buy_s, entry_s)


def _apply_slippage(ask: float, slippage_bps: float) -> float:
    if ask <= 0:
        return ask
    return ask * (1.0 + (slippage_bps / 10000.0))


def assess_market_quality(ctx: MarketContext, config: dict) -> MarketQualityCheck:
    min_ticks = int(config.get("min_ticks", 50))
    min_ask_sum = float(config.get("min_ask_sum", 0.80))
    max_ask_sum = float(config.get("max_ask_sum", 1.20))

    if len(ctx.ticks) < min_ticks:
        return MarketQualityCheck(False, "low_ticks")

    ask_sums = [
        tick.up_ask + tick.down_ask
        for tick in ctx.ticks
        if tick.up_ask > 0 and tick.down_ask > 0
    ]
    if len(ask_sums) < min_ticks:
        return MarketQualityCheck(False, "insufficient_valid_quotes")
    if min(ask_sums) < min_ask_sum:
        return MarketQualityCheck(False, "ask_sum_too_low")
    if max(ask_sums) > max_ask_sum:
        return MarketQualityCheck(False, "ask_sum_too_high")
    return MarketQualityCheck(True)


def _winner_for_delta(delta: float, flat_threshold: float = 0.0) -> str:
    if delta > flat_threshold:
        return "Up"
    if delta < -flat_threshold:
        return "Down"
    return "Flat"


def _extract_early_momentum_observations(
    ctx: MarketContext,
    *,
    checkpoints: list[int],
    tolerance_secs: float,
) -> list[EarlyMomentumObservation]:
    observations: list[EarlyMomentumObservation] = []
    if not ctx.ticks or not ctx.btc_open:
        return observations
    for checkpoint in checkpoints:
        best_tick: Optional[ResearchTick] = None
        best_distance: Optional[float] = None
        for tick in ctx.ticks:
            elapsed = 300.0 - tick.seconds_remaining
            distance = abs(elapsed - checkpoint)
            if best_distance is None or distance < best_distance:
                best_tick = tick
                best_distance = distance
        if best_tick is None or best_distance is None or best_distance > tolerance_secs:
            continue
        observations.append(
            EarlyMomentumObservation(
                slug=ctx.slug,
                winner=ctx.winner,
                checkpoint_s=checkpoint,
                elapsed_s=300.0 - best_tick.seconds_remaining,
                btc_delta=best_tick.btc_spot - ctx.btc_open,
            )
        )
    return observations


def _effective_quote_ts(tick: ResearchTick) -> float:
    quote_ts = max(tick.up_quote_ts or 0.0, tick.down_quote_ts or 0.0)
    return quote_ts or tick.timestamp


def _extract_btc_market_lag_observation(
    ctx: MarketContext,
    *,
    btc_delta_threshold: float,
    ask_gap_threshold: float,
    max_elapsed_secs: float,
) -> Optional[LagObservation]:
    if not ctx.ticks:
        return None
    early_ticks = [tick for tick in ctx.ticks if (300.0 - tick.seconds_remaining) <= max_elapsed_secs]
    if not early_ticks:
        return None
    btc_tick = next((tick for tick in early_ticks if abs(tick.btc_delta) >= btc_delta_threshold), None)
    if btc_tick is None or btc_tick.btc_delta == 0:
        return None
    direction = "Up" if btc_tick.btc_delta > 0 else "Down"
    poly_tick = None
    for tick in early_ticks:
        gap = tick.up_ask - tick.down_ask
        if direction == "Up" and gap >= ask_gap_threshold:
            poly_tick = tick
            break
        if direction == "Down" and (-gap) >= ask_gap_threshold:
            poly_tick = tick
            break
    if poly_tick is None:
        return None
    poly_gap = abs(poly_tick.up_ask - poly_tick.down_ask)
    return LagObservation(
        slug=ctx.slug,
        winner=ctx.winner,
        direction=direction,
        btc_event_elapsed_s=300.0 - btc_tick.seconds_remaining,
        poly_event_elapsed_s=300.0 - poly_tick.seconds_remaining,
        lag_secs=_effective_quote_ts(poly_tick) - (btc_tick.btc_source_ts or btc_tick.timestamp),
        btc_delta=btc_tick.btc_delta,
        ask_gap=poly_gap,
    )


def analyze_btc_market_lag(db: ResearchDatabase, config: dict):
    btc_delta_threshold = float(config.get("btc_delta_threshold", 5.0))
    ask_gap_threshold = float(config.get("ask_gap_threshold", 0.05))
    max_elapsed_secs = float(config.get("max_elapsed_secs", 60.0))
    observations: list[LagObservation] = []
    scanned = 0
    for market in db.list_simulatable_markets():
        ctx = db.load_market_context(market["slug"])
        if not ctx or not ctx.ticks:
            continue
        scanned += 1
        obs = _extract_btc_market_lag_observation(
            ctx,
            btc_delta_threshold=btc_delta_threshold,
            ask_gap_threshold=ask_gap_threshold,
            max_elapsed_secs=max_elapsed_secs,
        )
        if obs:
            observations.append(obs)
    if not observations:
        print("No BTC/Polymarket lag observations available.")
        return

    lags = sorted(o.lag_secs for o in observations)
    median_lag = statistics.median(lags)
    avg_lag = statistics.mean(lags)
    p90_index = min(len(lags) - 1, max(0, int(round(0.9 * len(lags))) - 1))
    p90_lag = lags[p90_index]
    lead_count = sum(1 for lag in lags if lag < 0)
    lag_count = sum(1 for lag in lags if lag > 0)
    print(
        "BTC vs Polymarket lag analysis: "
        f"{len(observations)} observations across {scanned} windows"
    )
    print(
        f"  BTC threshold=${btc_delta_threshold:.2f} | ask gap threshold={ask_gap_threshold:.3f} "
        f"| early window <= {max_elapsed_secs:.1f}s"
    )
    print(
        f"  Avg lag={avg_lag:+.3f}s | median={median_lag:+.3f}s | p90={p90_lag:+.3f}s "
        f"| PM leads={lead_count} | PM lags={lag_count}"
    )
    for direction in ("Up", "Down"):
        rows = [o for o in observations if o.direction == direction]
        if not rows:
            continue
        dlags = sorted(o.lag_secs for o in rows)
        print(
            f"  {direction}: n={len(rows)} avg={statistics.mean(dlags):+.3f}s "
            f"median={statistics.median(dlags):+.3f}s"
        )


def analyze_early_btc_signal(db: ResearchDatabase, config: dict):
    checkpoints = [int(v) for v in config.get("checkpoints", [5, 10, 15])]
    thresholds = [float(v) for v in config.get("thresholds", [0, 5, 10, 20, 30])]
    tolerance_secs = float(config.get("tolerance_secs", 4.0))
    observations: list[EarlyMomentumObservation] = []
    skipped = 0
    for market in db.list_simulatable_markets():
        ctx = db.load_market_context(market["slug"])
        if not ctx or not ctx.ticks:
            continue
        obs = _extract_early_momentum_observations(
            ctx,
            checkpoints=checkpoints,
            tolerance_secs=tolerance_secs,
        )
        if not obs:
            skipped += 1
            continue
        observations.extend(obs)

    if not observations:
        print("No early BTC momentum observations available.")
        return

    print(f"Early BTC momentum signal analysis: {len(observations)} observations across {len({o.slug for o in observations})} windows")
    if skipped:
        print(f"  Windows without usable early ticks: {skipped}")
    for checkpoint in checkpoints:
        rows = [o for o in observations if o.checkpoint_s == checkpoint]
        if not rows:
            continue
        print(f"Checkpoint +{checkpoint}s:")
        print(
            f"  Avg |delta|: ${statistics.mean(abs(o.btc_delta) for o in rows):.2f} "
            f"| median actual sample time {statistics.median(o.elapsed_s for o in rows):.1f}s"
        )
        for threshold in thresholds:
            usable = [o for o in rows if abs(o.btc_delta) >= threshold]
            directional = [o for o in usable if _winner_for_delta(o.btc_delta) != 'Flat']
            if not usable:
                continue
            matches = sum(1 for o in directional if _winner_for_delta(o.btc_delta) == o.winner)
            directional_rate = (100.0 * matches / len(directional)) if directional else 0.0
            print(
                f"  |delta| >= ${threshold:.0f}: n={len(usable):3d} "
                f"directional={len(directional):3d} match={directional_rate:5.1f}%"
            )


def _projected_cost_ratio(
    pos: SimPosition,
    *,
    add_up_spend: float,
    add_down_spend: float,
    up_ask: float,
    down_ask: float,
) -> float:
    up_shares = pos.up_shares + ((add_up_spend / up_ask) if up_ask > 0 and add_up_spend > 0 else 0.0)
    down_shares = pos.down_shares + ((add_down_spend / down_ask) if down_ask > 0 and add_down_spend > 0 else 0.0)
    combined_spend = pos.combined_spend + add_up_spend + add_down_spend
    best_payout = max(up_shares, down_shares)
    if best_payout <= 0:
        return 0.0
    return combined_spend / best_payout


def _within_ratio_band(ratio: float, min_ratio: float, max_ratio: float) -> bool:
    if ratio <= 0:
        return False
    return min_ratio <= ratio <= max_ratio


def _pick_up_bias_spends(
    pos: SimPosition,
    tick: ResearchTick,
    *,
    up_spend: float,
    min_ratio: float,
    max_ratio: float,
    target_ratio: float,
    min_down_spend: float,
    max_down_spend: float,
) -> dict[str, float]:
    if tick.up_ask <= 0 or tick.down_ask <= 0 or up_spend <= 0:
        return {"up": 0.0, "down": 0.0}
    best: Optional[tuple[float, float]] = None
    for step in range(13):
        frac = step / 12.0
        down_spend = min_down_spend + ((max_down_spend - min_down_spend) * frac)
        projected = _projected_cost_ratio(
            pos,
            add_up_spend=up_spend,
            add_down_spend=down_spend,
            up_ask=tick.up_ask,
            down_ask=tick.down_ask,
        )
        if not _within_ratio_band(projected, min_ratio, max_ratio):
            continue
        distance = abs(projected - target_ratio)
        if best is None or distance < best[0]:
            best = (distance, down_spend)
    if best is None:
        return {"up": 0.0, "down": 0.0}
    return {"up": up_spend, "down": round(best[1], 6)}


def policy_equal_time(pos: SimPosition, tick: ResearchTick, config: dict) -> dict[str, float]:
    if tick.up_ask <= 0 or tick.down_ask <= 0:
        return {"up": 0.0, "down": 0.0}
    spend = float(config.get("notional_each", 25.0))
    return {"up": spend, "down": spend}


def policy_combined_cost_threshold(pos: SimPosition, tick: ResearchTick, config: dict) -> dict[str, float]:
    threshold = float(config.get("combined_threshold", 0.98))
    if tick.up_ask <= 0 or tick.down_ask <= 0 or tick.up_ask + tick.down_ask > threshold:
        return {"up": 0.0, "down": 0.0}
    spend = float(config.get("notional_each", 25.0))
    return {"up": spend, "down": spend}


def policy_payout_balanced(pos: SimPosition, tick: ResearchTick, config: dict) -> dict[str, float]:
    threshold = float(config.get("combined_threshold", 1.02))
    step = float(config.get("notional_step", 20.0))
    if tick.up_ask <= 0 or tick.down_ask <= 0 or tick.up_ask + tick.down_ask > threshold:
        return {"up": 0.0, "down": 0.0}
    if pos.up_shares < pos.down_shares:
        return {"up": step, "down": 0.0}
    if pos.down_shares < pos.up_shares:
        return {"up": 0.0, "down": step}
    return {"up": step / 2.0, "down": step / 2.0}


def policy_winner_lean_btc(pos: SimPosition, tick: ResearchTick, config: dict) -> dict[str, float]:
    if tick.up_ask <= 0 or tick.down_ask <= 0:
        return {"up": 0.0, "down": 0.0}
    base = float(config.get("base_notional", 10.0))
    extra = float(config.get("lean_notional", 20.0))
    threshold = float(config.get("btc_delta_threshold", 10.0))
    out = {"up": base, "down": base}
    if tick.btc_delta >= threshold:
        out["up"] += extra
    elif tick.btc_delta <= -threshold:
        out["down"] += extra
    return out


def policy_hedge_conviction(pos: SimPosition, tick: ResearchTick, config: dict) -> dict[str, float]:
    threshold = float(config.get("btc_delta_threshold", 8.0))
    conviction = float(config.get("conviction_notional", 30.0))
    hedge = float(config.get("hedge_notional", 8.0))
    if tick.btc_delta >= threshold:
        return {"up": conviction, "down": hedge}
    if tick.btc_delta <= -threshold:
        return {"up": hedge, "down": conviction}
    return {"up": 0.0, "down": 0.0}


def policy_cost_gated_up_bias(pos: SimPosition, tick: ResearchTick, config: dict) -> dict[str, float]:
    min_ratio = float(config.get("min_cost_ratio", 0.70))
    max_ratio = float(config.get("max_cost_ratio", 0.89))
    target_ratio = float(config.get("target_cost_ratio", 0.80))
    up_spend = float(config.get("up_notional", 30.0))
    min_down_spend = float(config.get("min_down_notional", 10.0))
    max_down_spend = float(config.get("max_down_notional", 30.0))
    return _pick_up_bias_spends(
        pos,
        tick,
        up_spend=up_spend,
        min_ratio=min_ratio,
        max_ratio=max_ratio,
        target_ratio=target_ratio,
        min_down_spend=min_down_spend,
        max_down_spend=max_down_spend,
    )


def policy_cost_gated_up_bias_late_guard(pos: SimPosition, tick: ResearchTick, config: dict) -> dict[str, float]:
    elapsed = 300.0 - tick.seconds_remaining
    late_start = float(config.get("late_guard_start_secs", 180.0))
    up_ask_max = float(config.get("late_guard_up_ask_max", 0.35))
    down_ask_min = float(config.get("late_guard_down_ask_min", 0.60))
    if elapsed >= late_start and tick.up_ask > 0 and tick.down_ask > 0:
        if tick.up_ask <= up_ask_max and tick.down_ask >= down_ask_min:
            return {"up": 0.0, "down": 0.0}
    return policy_cost_gated_up_bias(pos, tick, config)


def policy_cost_gated_up_bias_flat_only(pos: SimPosition, tick: ResearchTick, config: dict) -> dict[str, float]:
    flat_abs = float(config.get("flat_btc_abs", 5.0))
    if abs(tick.btc_delta) > flat_abs:
        return {"up": 0.0, "down": 0.0}
    return policy_cost_gated_up_bias(pos, tick, config)


def policy_cost_gated_up_bias_time_filtered(pos: SimPosition, tick: ResearchTick, config: dict) -> dict[str, float]:
    import datetime as _dt
    skip_hours = set(config.get("skip_hours", [17, 18, 19, 20, 21]))
    local_hour = _dt.datetime.fromtimestamp(tick.timestamp).hour
    if local_hour in skip_hours:
        return {"up": 0.0, "down": 0.0}
    return policy_cost_gated_up_bias_flat_only(pos, tick, config)


def policy_cost_gated_up_bias_momentum(pos: SimPosition, tick: ResearchTick, config: dict) -> dict[str, float]:
    min_ratio = float(config.get("min_cost_ratio", 0.70))
    max_ratio = float(config.get("max_cost_ratio", 0.89))
    target_ratio = float(config.get("target_cost_ratio", 0.80))
    flat_abs = float(config.get("flat_btc_abs", 5.0))
    positive_btc = float(config.get("positive_btc_threshold", 20.0))
    flat_up_spend = float(config.get("flat_up_notional", 24.0))
    flat_min_down_spend = float(config.get("flat_min_down_notional", 12.0))
    flat_max_down_spend = float(config.get("flat_max_down_notional", 24.0))
    momentum_up_spend = float(config.get("momentum_up_notional", 36.0))
    momentum_min_down_spend = float(config.get("momentum_min_down_notional", 12.0))
    momentum_max_down_spend = float(config.get("momentum_max_down_notional", 28.0))

    if tick.up_ask <= 0 or tick.down_ask <= 0:
        return {"up": 0.0, "down": 0.0}

    if tick.btc_delta >= positive_btc:
        up_spend = momentum_up_spend
        min_down_spend = momentum_min_down_spend
        max_down_spend = momentum_max_down_spend
    elif abs(tick.btc_delta) <= flat_abs:
        up_spend = flat_up_spend
        min_down_spend = flat_min_down_spend
        max_down_spend = flat_max_down_spend
    else:
        return {"up": 0.0, "down": 0.0}

    return _pick_up_bias_spends(
        pos,
        tick,
        up_spend=up_spend,
        min_ratio=min_ratio,
        max_ratio=max_ratio,
        target_ratio=target_ratio,
        min_down_spend=min_down_spend,
        max_down_spend=max_down_spend,
    )


def policy_strength_follow_share_clips(pos: SimPosition, tick: ResearchTick, config: dict) -> dict[str, float]:
    if tick.up_ask <= 0 or tick.down_ask <= 0:
        return {"up": 0.0, "down": 0.0}

    elapsed = 300.0 - tick.seconds_remaining
    min_start = float(config.get("signal_start_secs", 15.0))
    strength_gap = float(config.get("strength_gap", 0.08))
    dominant_clip_shares = float(config.get("dominant_clip_shares", 58.0))
    hedge_clip_shares = float(config.get("hedge_clip_shares", 20.0))
    max_projected_ratio = float(config.get("max_projected_cost_ratio", 0.98))
    late_start = float(config.get("late_reversal_start_secs", 180.0))
    reversal_gap = float(config.get("late_reversal_gap", 0.18))

    ask_gap = tick.up_ask - tick.down_ask

    if pos.up_shares > pos.down_shares:
        dominant_side = "up"
    elif pos.down_shares > pos.up_shares:
        dominant_side = "down"
    else:
        if elapsed < min_start or abs(ask_gap) < strength_gap:
            return {"up": 0.0, "down": 0.0}
        dominant_side = "up" if ask_gap > 0 else "down"

    # Stop pressing a side late if the opposite side has clearly taken over.
    if elapsed >= late_start:
        if dominant_side == "up" and (tick.down_ask - tick.up_ask) >= reversal_gap:
            return {"up": 0.0, "down": 0.0}
        if dominant_side == "down" and (tick.up_ask - tick.down_ask) >= reversal_gap:
            return {"up": 0.0, "down": 0.0}

    dominant_ask = tick.up_ask if dominant_side == "up" else tick.down_ask
    hedge_ask = tick.down_ask if dominant_side == "up" else tick.up_ask
    dominant_spend = dominant_clip_shares * dominant_ask
    hedge_spend = hedge_clip_shares * hedge_ask

    projected = _projected_cost_ratio(
        pos,
        add_up_spend=dominant_spend if dominant_side == "up" else hedge_spend,
        add_down_spend=hedge_spend if dominant_side == "up" else dominant_spend,
        up_ask=tick.up_ask,
        down_ask=tick.down_ask,
    )
    if projected > max_projected_ratio:
        return {"up": 0.0, "down": 0.0}

    if dominant_side == "up":
        return {"up": round(dominant_spend, 6), "down": round(hedge_spend, 6)}
    return {"up": round(hedge_spend, 6), "down": round(dominant_spend, 6)}


def policy_market_favorite_share_clips(pos: SimPosition, tick: ResearchTick, config: dict) -> dict[str, float]:
    if tick.up_ask <= 0 or tick.down_ask <= 0:
        return {"up": 0.0, "down": 0.0}

    elapsed = 300.0 - tick.seconds_remaining
    min_start = float(config.get("favorite_start_secs", 15.0))
    favorite_gap = float(config.get("favorite_gap", 0.05))
    dominant_clip_shares = float(config.get("dominant_clip_shares", 58.0))
    hedge_clip_shares = float(config.get("hedge_clip_shares", 20.0))
    max_projected_ratio = float(config.get("max_projected_cost_ratio", 0.98))
    late_start = float(config.get("late_reversal_start_secs", 180.0))
    reversal_gap = float(config.get("late_reversal_gap", 0.18))

    if pos.up_shares > pos.down_shares:
        dominant_side = "up"
    elif pos.down_shares > pos.up_shares:
        dominant_side = "down"
    else:
        if elapsed < min_start:
            return {"up": 0.0, "down": 0.0}
        ask_gap = tick.up_ask - tick.down_ask
        if abs(ask_gap) < favorite_gap:
            return {"up": 0.0, "down": 0.0}
        dominant_side = "up" if tick.up_ask > tick.down_ask else "down"

    if elapsed >= late_start:
        if dominant_side == "up" and (tick.down_ask - tick.up_ask) >= reversal_gap:
            return {"up": 0.0, "down": 0.0}
        if dominant_side == "down" and (tick.up_ask - tick.down_ask) >= reversal_gap:
            return {"up": 0.0, "down": 0.0}

    dominant_ask = tick.up_ask if dominant_side == "up" else tick.down_ask
    hedge_ask = tick.down_ask if dominant_side == "up" else tick.up_ask
    dominant_spend = dominant_clip_shares * dominant_ask
    hedge_spend = hedge_clip_shares * hedge_ask
    projected = _projected_cost_ratio(
        pos,
        add_up_spend=dominant_spend if dominant_side == "up" else hedge_spend,
        add_down_spend=hedge_spend if dominant_side == "up" else dominant_spend,
        up_ask=tick.up_ask,
        down_ask=tick.down_ask,
    )
    if projected > max_projected_ratio:
        return {"up": 0.0, "down": 0.0}

    if dominant_side == "up":
        return {"up": round(dominant_spend, 6), "down": round(hedge_spend, 6)}
    return {"up": round(hedge_spend, 6), "down": round(dominant_spend, 6)}


def policy_favorite_cost_capped_share_clips(pos: SimPosition, tick: ResearchTick, config: dict) -> dict[str, float]:
    if tick.up_ask <= 0 or tick.down_ask <= 0:
        return {"up": 0.0, "down": 0.0}

    elapsed = 300.0 - tick.seconds_remaining
    start_secs = float(config.get("entry_start_secs", 15.0))
    min_spread = float(config.get("min_spread_gap", 0.05))
    max_spread = float(config.get("max_spread_gap", 0.50))
    dominant_clip_shares = float(config.get("dominant_clip_shares", 58.0))
    hedge_clip_shares = float(config.get("hedge_clip_shares", 20.0))
    max_projected_ratio = float(config.get("max_projected_cost_ratio", 0.89))
    late_start = float(config.get("late_reversal_start_secs", 180.0))
    reversal_gap = float(config.get("late_reversal_gap", 0.18))

    ask_gap = abs(tick.up_ask - tick.down_ask)
    if pos.up_shares > pos.down_shares:
        dominant_side = "up"
    elif pos.down_shares > pos.up_shares:
        dominant_side = "down"
    else:
        if elapsed < start_secs:
            return {"up": 0.0, "down": 0.0}
        if ask_gap < min_spread or ask_gap > max_spread:
            return {"up": 0.0, "down": 0.0}
        dominant_side = "up" if tick.up_ask > tick.down_ask else "down"

    if elapsed >= late_start:
        if dominant_side == "up" and (tick.down_ask - tick.up_ask) >= reversal_gap:
            return {"up": 0.0, "down": 0.0}
        if dominant_side == "down" and (tick.up_ask - tick.down_ask) >= reversal_gap:
            return {"up": 0.0, "down": 0.0}

    dominant_ask = tick.up_ask if dominant_side == "up" else tick.down_ask
    hedge_ask = tick.down_ask if dominant_side == "up" else tick.up_ask
    dominant_spend = dominant_clip_shares * dominant_ask
    hedge_spend = hedge_clip_shares * hedge_ask
    projected = _projected_cost_ratio(
        pos,
        add_up_spend=dominant_spend if dominant_side == "up" else hedge_spend,
        add_down_spend=hedge_spend if dominant_side == "up" else dominant_spend,
        up_ask=tick.up_ask,
        down_ask=tick.down_ask,
    )
    if projected > max_projected_ratio:
        return {"up": 0.0, "down": 0.0}

    if dominant_side == "up":
        return {"up": round(dominant_spend, 6), "down": round(hedge_spend, 6)}
    return {"up": round(hedge_spend, 6), "down": round(dominant_spend, 6)}


def policy_pair_under_40_complete_by_45(pos: SimPosition, tick: ResearchTick, config: dict) -> dict[str, float]:
    if tick.up_ask <= 0 or tick.down_ask <= 0:
        return {"up": 0.0, "down": 0.0}

    spend = float(config.get("notional_each", 25.0))
    entry_threshold = float(config.get("entry_threshold", 0.40))
    completion_threshold = float(config.get("completion_threshold", 0.45))

    has_up = pos.up_shares > 0
    has_down = pos.down_shares > 0
    if has_up and has_down:
        return {"up": 0.0, "down": 0.0}

    buy_up = False
    buy_down = False

    if not has_up and not has_down:
        if tick.up_ask <= entry_threshold:
            buy_up = True
        if tick.down_ask <= entry_threshold:
            buy_down = True
    else:
        if not has_up and tick.up_ask <= completion_threshold:
            buy_up = True
        if not has_down and tick.down_ask <= completion_threshold:
            buy_down = True

    return {
        "up": spend if buy_up else 0.0,
        "down": spend if buy_down else 0.0,
    }


POLICIES: dict[str, Callable[[SimPosition, ResearchTick, dict], dict[str, float]]] = {
    "equal_time": policy_equal_time,
    "combined_cost_threshold": policy_combined_cost_threshold,
    "payout_balanced": policy_payout_balanced,
    "winner_lean_btc": policy_winner_lean_btc,
    "hedge_plus_conviction": policy_hedge_conviction,
    "cost_gated_up_bias": policy_cost_gated_up_bias,
    "cost_gated_up_bias_late_guard": policy_cost_gated_up_bias_late_guard,
    "cost_gated_up_bias_flat_only": policy_cost_gated_up_bias_flat_only,
    "cost_gated_up_bias_time_filtered": policy_cost_gated_up_bias_time_filtered,
    "cost_gated_up_bias_momentum": policy_cost_gated_up_bias_momentum,
    "favorite_cost_capped_share_clips": policy_favorite_cost_capped_share_clips,
    "market_favorite_share_clips": policy_market_favorite_share_clips,
    "pair_under_40_complete_by_45": policy_pair_under_40_complete_by_45,
    "strength_follow_share_clips": policy_strength_follow_share_clips,
}


def _load_policy_config(raw: str) -> dict:
    if not raw:
        return {}
    if os.path.exists(raw):
        with open(raw, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(raw)


def simulate_market(ctx: MarketContext, policy_name: str, config: dict) -> dict:
    policy = POLICIES[policy_name]
    pos = SimPosition()
    step_secs = float(config.get("step_secs", 20.0))
    start_after = float(config.get("start_after_secs", 0.0))
    stop_after = float(config.get("stop_after_secs", 300.0))
    fee_bps = float(config.get("fee_bps", 0.0))
    slippage_bps = float(config.get("slippage_bps", 0.0))
    last_exec_ts = None

    for tick in ctx.ticks:
        elapsed = 300.0 - tick.seconds_remaining
        if elapsed < start_after or elapsed > stop_after:
            continue
        if last_exec_ts is not None and tick.timestamp - last_exec_ts < step_secs:
            continue
        spends = policy(pos, tick, config)
        if spends.get("up", 0.0) > 0:
            _mark_buy(
                pos,
                "up",
                spends["up"],
                _apply_slippage(tick.up_ask, slippage_bps),
                tick.seconds_remaining,
            )
        if spends.get("down", 0.0) > 0:
            _mark_buy(
                pos,
                "down",
                spends["down"],
                _apply_slippage(tick.down_ask, slippage_bps),
                tick.seconds_remaining,
            )
        if spends.get("up", 0.0) > 0 or spends.get("down", 0.0) > 0:
            last_exec_ts = tick.timestamp

    combined_spend = pos.combined_spend
    gross_pnl_if_up = pos.up_shares - combined_spend
    gross_pnl_if_down = pos.down_shares - combined_spend
    gross_pnl = gross_pnl_if_up if ctx.winner == "Up" else gross_pnl_if_down
    adverse_pnl = min(gross_pnl_if_up, gross_pnl_if_down)
    fee_cost = combined_spend * (fee_bps / 10000.0)
    net_pnl = gross_pnl - fee_cost
    roi = (gross_pnl / combined_spend) if combined_spend > 0 else 0.0
    return {
        "slug": ctx.slug,
        "policy": policy_name,
        "up_spend": round(pos.up_spend, 6),
        "down_spend": round(pos.down_spend, 6),
        "up_shares": round(pos.up_shares, 6),
        "down_shares": round(pos.down_shares, 6),
        "combined_spend": round(combined_spend, 6),
        "payout_if_up": round(pos.payout_if_up, 6),
        "payout_if_down": round(pos.payout_if_down, 6),
        "winner": ctx.winner,
        "gross_pnl": round(gross_pnl, 6),
        "roi": round(roi, 6),
        "adverse_pnl": round(adverse_pnl, 6),
        "net_pnl": round(net_pnl, 6),
        "first_buy_s": pos.first_buy_s,
        "last_buy_s": pos.last_buy_s,
        "meta_json": json.dumps(
            {
                "gross_pnl_if_up": round(gross_pnl_if_up, 6),
                "gross_pnl_if_down": round(gross_pnl_if_down, 6),
                "combined_cost_ratio": round(combined_spend / max(pos.up_shares, pos.down_shares), 6)
                if max(pos.up_shares, pos.down_shares) > 0 else 0.0,
                "fee_bps": fee_bps,
                "slippage_bps": slippage_bps,
                "config": config,
            },
            sort_keys=True,
        ),
    }


def _print_policy_ranking(rows: list[dict]):
    if not rows:
        return
    print("Policy ranking:")
    ranked = sorted(
        rows,
        key=lambda row: (row["total_net"], row["avg_net"], row["worst_net"]),
        reverse=True,
    )
    for row in ranked:
        print(
            f"  {row['policy']:22s} windows={row['n']:4d} "
            f"gross=${row['total_gross']:+.2f} net=${row['total_net']:+.2f} "
            f"avg_net=${row['avg_net']:+.2f} avg_roi={row['avg_roi']*100:.2f}% "
            f"worst=${row['worst_net']:+.2f}"
        )


def run_simulation(db: ResearchDatabase, policy_name: str, config: dict):
    assert policy_name in POLICIES, f"unknown policy {policy_name}"
    db.delete_sim_results(policy_name)
    rows = []
    skipped = 0
    skip_reasons: dict[str, int] = {}
    for market in db.list_simulatable_markets():
        ctx = db.load_market_context(market["slug"])
        if not ctx or not ctx.ticks:
            continue
        quality = assess_market_quality(ctx, config)
        if not quality.ok:
            skipped += 1
            skip_reasons[quality.reason or "unknown"] = skip_reasons.get(quality.reason or "unknown", 0) + 1
            continue
        result = simulate_market(ctx, policy_name, config)
        db.insert_sim_result(result)
        rows.append(result)

    if not rows:
        print("No resolved research markets available to simulate.")
        return
    net_values = [row["net_pnl"] for row in rows]
    gross_values = [row["gross_pnl"] for row in rows]
    print(f"Policy {policy_name}: {len(rows)} windows")
    if skipped:
        reasons = ", ".join(f"{name}={count}" for name, count in sorted(skip_reasons.items()))
        print(f"  Skipped windows:  {skipped} ({reasons})")
    print(f"  Total gross P&L: ${sum(gross_values):+.2f}")
    print(f"  Total net P&L:   ${sum(net_values):+.2f}")
    print(f"  Avg net/window:  ${statistics.mean(net_values):+.2f}")
    print(f"  Median net/win:  ${statistics.median(net_values):+.2f}")
    print(f"  Worst net win:   ${min(net_values):+.2f}")
    return {
        "policy": policy_name,
        "n": len(rows),
        "skipped": skipped,
        "total_gross": sum(gross_values),
        "total_net": sum(net_values),
        "avg_net": statistics.mean(net_values),
        "avg_roi": statistics.mean(row["roi"] for row in rows),
        "worst_net": min(net_values),
    }


def run_simulation_suite(db: ResearchDatabase, config: dict):
    summaries = []
    for policy_name in sorted(POLICIES):
        summary = run_simulation(db, policy_name, config)
        if summary:
            summaries.append(summary)
    print()
    _print_policy_ranking(summaries)


def main() -> int:
    parser = argparse.ArgumentParser(description="Paired hold-to-resolution BTC 5m market research")
    parser.add_argument("--db", default=DEFAULT_DB, help="Research SQLite DB path")
    parser.add_argument("--collect", action="store_true", help="Collect a fresh BTC 5m research dataset")
    parser.add_argument("--duration-hours", type=float, default=24.0, help="Collector run duration")
    parser.add_argument("--poll-interval", type=float, default=3.0, help="Collector poll interval in seconds")
    parser.add_argument("--status-interval", type=float, default=30.0, help="Collector heartbeat interval in seconds")
    parser.add_argument("--btc-stream", choices=["rest", "binance_ws"], default="rest", help="BTC spot source for collection")
    parser.add_argument("--market-stream", choices=["rest", "ws"], default="rest", help="Polymarket quote source for collection")
    parser.add_argument("--simulate-paired", action="store_true", help="Run paired policy simulation on the research DB")
    parser.add_argument("--analyze-early-btc", action="store_true", help="Analyze early BTC momentum checkpoints against resolved winners")
    parser.add_argument("--analyze-btc-lag", action="store_true", help="Analyze whether Polymarket quote state lags BTC moves")
    parser.add_argument("--policy", choices=["all"] + sorted(POLICIES.keys()), default="equal_time")
    parser.add_argument("--policy-config", default="", help="JSON string or path for policy configuration")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    db = ResearchDatabase(args.db)
    try:
        if args.collect:
            collector = ResearchCollector(db)
            collector.run(
                duration_hours=args.duration_hours,
                poll_interval=args.poll_interval,
                status_interval=args.status_interval,
                btc_stream=args.btc_stream,
                market_stream=args.market_stream,
            )
        if args.simulate_paired:
            config = _load_policy_config(args.policy_config)
            if args.policy == "all":
                run_simulation_suite(db, config)
            else:
                run_simulation(db, args.policy, config)
        if args.analyze_early_btc:
            config = _load_policy_config(args.policy_config)
            analyze_early_btc_signal(db, config)
        if args.analyze_btc_lag:
            config = _load_policy_config(args.policy_config)
            analyze_btc_market_lag(db, config)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
