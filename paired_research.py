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
import json
import logging
import os
import sqlite3
import statistics
import time
from dataclasses import dataclass
from typing import Callable, Optional

from observer import BTCPriceClient, PolymarketClient


DEFAULT_DB = "paired_research.db"


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
                price_source       TEXT DEFAULT 'rest'
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
                market_id=excluded.market_id,
                condition_id=excluded.condition_id,
                up_token_id=excluded.up_token_id,
                down_token_id=excluded.down_token_id,
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
             price_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


class ResearchCollector:
    def __init__(self, db: ResearchDatabase):
        self.db = db
        self.poly = PolymarketClient()
        self.btc = BTCPriceClient()

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

    def run(self, duration_hours: float = 24.0, poll_interval: float = 3.0):
        start = time.time()
        current_slug = None
        current_tokens = None
        btc_open = None
        while time.time() - start < duration_hours * 3600:
            window_start, window_end, slug = self.poly.compute_window_times()
            if slug != current_slug:
                event = self.poly.get_market_by_slug(slug)
                tokens = self.poly.extract_token_ids(event) if event else None
                btc_open = self.btc.get_btc_price()
                if tokens:
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
                    logging.info(f"Tracking research market {slug}")
                current_slug = slug
                current_tokens = tokens

            now = time.time()
            if current_tokens:
                up = self.poly.get_price(current_tokens["up_token_id"])
                down = self.poly.get_price(current_tokens["down_token_id"])
                btc_now = self.btc.get_btc_price()
                if up and down and btc_now:
                    self.db.insert_tick(
                        current_slug,
                        ResearchTick(
                            timestamp=now,
                            seconds_remaining=max(0.0, window_end - now),
                            up_bid=up["best_bid"],
                            up_ask=up["best_ask"],
                            down_bid=down["best_bid"],
                            down_ask=down["best_ask"],
                            btc_spot=btc_now,
                            btc_delta=(btc_now - btc_open) if btc_open else 0.0,
                            price_source="rest",
                        ),
                    )

            # resolve any expired market that hasn't been finalized yet
            for market in self.db.conn.execute(
                """
                SELECT slug, window_end_ts FROM research_markets
                WHERE resolution IS NULL AND window_end_ts <= ?
                """,
                (time.time() - 15.0,),
            ).fetchall():
                winner = self._fetch_resolution(market["slug"])
                btc_close = self.btc.get_btc_price()
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
                    logging.info(f"Resolved {market['slug']} -> {winner}")
            time.sleep(poll_interval)


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


POLICIES: dict[str, Callable[[SimPosition, ResearchTick, dict], dict[str, float]]] = {
    "equal_time": policy_equal_time,
    "combined_cost_threshold": policy_combined_cost_threshold,
    "payout_balanced": policy_payout_balanced,
    "winner_lean_btc": policy_winner_lean_btc,
    "hedge_plus_conviction": policy_hedge_conviction,
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
    last_exec_ts = None

    for tick in ctx.ticks:
        elapsed = 300.0 - tick.seconds_remaining
        if elapsed < start_after or elapsed > stop_after:
            continue
        if last_exec_ts is not None and tick.timestamp - last_exec_ts < step_secs:
            continue
        spends = policy(pos, tick, config)
        if spends.get("up", 0.0) > 0:
            _mark_buy(pos, "up", spends["up"], tick.up_ask, tick.seconds_remaining)
        if spends.get("down", 0.0) > 0:
            _mark_buy(pos, "down", spends["down"], tick.down_ask, tick.seconds_remaining)
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
                "config": config,
            },
            sort_keys=True,
        ),
    }


def run_simulation(db: ResearchDatabase, policy_name: str, config: dict):
    assert policy_name in POLICIES, f"unknown policy {policy_name}"
    rows = []
    for market in db.list_simulatable_markets():
        ctx = db.load_market_context(market["slug"])
        if not ctx or not ctx.ticks:
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
    print(f"  Total gross P&L: ${sum(gross_values):+.2f}")
    print(f"  Total net P&L:   ${sum(net_values):+.2f}")
    print(f"  Avg net/window:  ${statistics.mean(net_values):+.2f}")
    print(f"  Median net/win:  ${statistics.median(net_values):+.2f}")
    print(f"  Worst net win:   ${min(net_values):+.2f}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Paired hold-to-resolution BTC 5m market research")
    parser.add_argument("--db", default=DEFAULT_DB, help="Research SQLite DB path")
    parser.add_argument("--collect", action="store_true", help="Collect a fresh BTC 5m research dataset")
    parser.add_argument("--duration-hours", type=float, default=24.0, help="Collector run duration")
    parser.add_argument("--poll-interval", type=float, default=3.0, help="Collector poll interval in seconds")
    parser.add_argument("--simulate-paired", action="store_true", help="Run paired policy simulation on the research DB")
    parser.add_argument("--policy", choices=sorted(POLICIES.keys()), default="equal_time")
    parser.add_argument("--policy-config", default="", help="JSON string or path for policy configuration")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    db = ResearchDatabase(args.db)
    try:
        if args.collect:
            collector = ResearchCollector(db)
            collector.run(duration_hours=args.duration_hours, poll_interval=args.poll_interval)
        if args.simulate_paired:
            config = _load_policy_config(args.policy_config)
            run_simulation(db, args.policy, config)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
