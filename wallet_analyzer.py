#!/usr/bin/env python3
"""
Analyze a public Polymarket wallet's BTC 5-minute market activity.

This script pulls public trade/activity data from Polymarket's Data API and
summarizes per-window behavior so we can infer whether a trader is:
  - buying one side or both sides
  - scaling into positions
  - trading early vs late in the 5-minute window
  - using taker-only or mixed visible execution patterns
"""

from __future__ import annotations

import argparse
import collections
import json
import csv
import io
import statistics
import sys
import zipfile
from datetime import UTC, datetime
from dataclasses import dataclass
from typing import Iterable, Optional

import requests


DATA_API = "https://data-api.polymarket.com"
BTC_5M_PREFIX = "btc-updown-5m-"
COINBASE_CANDLES_API = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
LOCAL_TZ = datetime.now().astimezone().tzinfo or UTC


@dataclass
class TradeRow:
    timestamp: int
    slug: str
    side: str
    outcome: str
    price: float
    size: float
    asset: str
    condition_id: str
    transaction_hash: str

    @property
    def window_start_ts(self) -> int:
        try:
            return int(self.slug.rsplit("-", 1)[-1])
        except Exception:
            return 0

    @property
    def seconds_into_window(self) -> Optional[int]:
        start = self.window_start_ts
        if start <= 0:
            return None
        return self.timestamp - start


@dataclass
class SnapshotPosition:
    condition_id: str
    asset: str
    size: float
    cur_price: float
    valuation_time: str


@dataclass
class SnapshotEquity:
    cash_balance: float
    positions_value: float
    equity: float
    valuation_time: str


@dataclass
class TradeBurst:
    slug: str
    side: str
    outcome: str
    start_ts: int
    end_ts: int
    total_size: float
    total_spend: float
    fills: int

    @property
    def avg_price(self) -> float:
        if self.total_size <= 0:
            return 0.0
        return self.total_spend / self.total_size

    @property
    def window_start_ts(self) -> int:
        try:
            return int(self.slug.rsplit("-", 1)[-1])
        except Exception:
            return 0

    @property
    def start_seconds_into_window(self) -> Optional[int]:
        start = self.window_start_ts
        if start <= 0:
            return None
        return self.start_ts - start


@dataclass
class WindowReconstruction:
    slug: str
    winner: str
    lean_side: str
    local_hour_bucket: str
    first_burst_outcome: str
    first_burst_s: Optional[int]
    initial_favored_side: str
    final_favored_side: str
    favored_flip_count: int
    prev_winner: str
    prev_winner_streak: int
    streak_alignment: str
    burst_count: int
    early_bursts: int
    mid_bursts: int
    late_bursts: int
    up_shares: float
    up_spend: float
    down_shares: float
    down_spend: float
    combined_spend: float
    payout_if_up: float
    payout_if_down: float
    gross_pnl: float
    roi: float
    gross_pnl_if_up: float
    gross_pnl_if_down: float
    combined_cost_ratio: float
    price_sum: float
    share_skew: float
    notional_skew: float
    payout_skew: float
    winner_overweight: bool
    first_buy_s: Optional[int]
    last_buy_s: Optional[int]
    btc_open_price: float
    btc_first_buy_price: float
    btc_last_buy_price: float
    btc_delta_first_buy: float
    btc_delta_last_buy: float
    lean_btc_alignment: str


@dataclass
class BurstEvolutionStep:
    idx: int
    burst: TradeBurst
    cumulative_up_shares: float
    cumulative_down_shares: float
    cumulative_spend: float
    pnl_if_up: float
    pnl_if_down: float
    combined_cost_ratio: float
    delta_pnl_if_up: float
    delta_pnl_if_down: float
    delta_cost_ratio: float
    effect: str
    favored_side: str
    favored_changed: bool


@dataclass
class FormulaFitResult:
    name: str
    choices: int
    correct: int
    up_predictions: int
    down_predictions: int
    up_actual_correct: int
    down_actual_correct: int


class BTCHistoryClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "PolypanicWalletAnalyzer/1.0"})
        self._candles: dict[int, float] = {}

    @staticmethod
    def _iso(ts: int) -> str:
        return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")

    def load_range(self, start_ts: int, end_ts: int, granularity: int = 60):
        if end_ts <= start_ts:
            return
        chunk_span = granularity * 280
        current = max(0, start_ts - granularity)
        while current < end_ts + granularity:
            chunk_end = min(end_ts + granularity, current + chunk_span)
            resp = self.session.get(
                COINBASE_CANDLES_API,
                params={
                    "start": self._iso(current),
                    "end": self._iso(chunk_end),
                    "granularity": granularity,
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                for row in data:
                    if not isinstance(row, list) or len(row) < 5:
                        continue
                    ts = int(row[0])
                    close_price = float(row[4])
                    self._candles[ts] = close_price
            current = chunk_end

    def price_at(self, ts: int) -> float:
        if not self._candles:
            return 0.0
        bucket = int(ts - (ts % 60))
        if bucket in self._candles:
            return self._candles[bucket]
        earlier = [key for key in self._candles if key <= bucket]
        if earlier:
            return self._candles[max(earlier)]
        later = [key for key in self._candles if key >= bucket]
        if later:
            return self._candles[min(later)]
        return 0.0


class WalletAnalyzer:
    def __init__(self, user: str):
        self.user = user.lower()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "PolypanicWalletAnalyzer/1.0"})

    def _fetch_rows(
        self,
        endpoint: str,
        *,
        limit: int,
        max_rows: int,
        extra_params: Optional[dict] = None,
    ) -> list[dict]:
        rows: list[dict] = []
        offset = 0
        params = {"user": self.user, "limit": limit}
        if extra_params:
            params.update(extra_params)
        while len(rows) < max_rows:
            params["offset"] = offset
            resp = self.session.get(f"{DATA_API}{endpoint}", params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            rows.extend(data)
            if len(data) < limit:
                break
            offset += limit
        return rows[:max_rows]

    def fetch_trades(self, *, taker_only: bool, max_rows: int) -> list[TradeRow]:
        rows = self._fetch_rows(
            "/trades",
            limit=min(500, max_rows),
            max_rows=max_rows,
            extra_params={"takerOnly": str(taker_only).lower()},
        )
        return self._normalize(rows)

    def fetch_activity(self, *, max_rows: int) -> list[TradeRow]:
        rows = self._fetch_rows(
            "/activity",
            limit=min(500, max_rows),
            max_rows=max_rows,
            extra_params={"type": "TRADE"},
        )
        return self._normalize(rows)

    def fetch_accounting_snapshot(self) -> tuple[list[SnapshotPosition], Optional[SnapshotEquity]]:
        resp = self.session.get(
            f"{DATA_API}/v1/accounting/snapshot",
            params={"user": self.user},
            timeout=30,
        )
        resp.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(resp.content))

        positions: list[SnapshotPosition] = []
        equity: Optional[SnapshotEquity] = None

        if "positions.csv" in zf.namelist():
            rows = csv.DictReader(io.StringIO(zf.read("positions.csv").decode("utf-8")))
            for row in rows:
                positions.append(
                    SnapshotPosition(
                        condition_id=str(row.get("conditionId", "")),
                        asset=str(row.get("asset", "")),
                        size=float(row.get("size", 0.0) or 0.0),
                        cur_price=float(row.get("curPrice", 0.0) or 0.0),
                        valuation_time=str(row.get("valuationTime", "")),
                    )
                )

        if "equity.csv" in zf.namelist():
            rows = list(csv.DictReader(io.StringIO(zf.read("equity.csv").decode("utf-8"))))
            if rows:
                row = rows[0]
                equity = SnapshotEquity(
                    cash_balance=float(row.get("cashBalance", 0.0) or 0.0),
                    positions_value=float(row.get("positionsValue", 0.0) or 0.0),
                    equity=float(row.get("equity", 0.0) or 0.0),
                    valuation_time=str(row.get("valuationTime", "")),
                )

        return positions, equity

    def fetch_market_winner(self, slug: str) -> Optional[str]:
        resp = self.session.get(f"https://gamma-api.polymarket.com/events", params={"slug": slug}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        row = data[0] if isinstance(data, list) and data else data
        if not isinstance(row, dict):
            return None
        markets = row.get("markets") or []
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
            if not isinstance(outcomes, list) or not isinstance(prices, list):
                return None
            for outcome, price in zip(outcomes, prices):
                if float(price) >= 0.999:
                    return str(outcome)
        except Exception:
            return None
        return None

    def _normalize(self, rows: Iterable[dict]) -> list[TradeRow]:
        normalized: list[TradeRow] = []
        for row in rows:
            slug = row.get("slug")
            if not isinstance(slug, str) or not slug.startswith(BTC_5M_PREFIX):
                continue
            normalized.append(
                TradeRow(
                    timestamp=int(float(row.get("timestamp", 0))),
                    slug=slug,
                    side=str(row.get("side", "")),
                    outcome=str(row.get("outcome", "")),
                    price=float(row.get("price", 0.0) or 0.0),
                    size=float(row.get("size", 0.0) or 0.0),
                    asset=str(row.get("asset", "")),
                    condition_id=str(row.get("conditionId", "")),
                    transaction_hash=str(row.get("transactionHash", "")),
                )
            )
        normalized.sort(key=lambda r: (r.slug, r.timestamp, r.transaction_hash))
        return normalized


def median_or_zero(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def pct(n: int, d: int) -> float:
    return (100.0 * n / d) if d else 0.0


def cluster_trade_bursts(rows: list[TradeRow], *, gap_secs: int = 5) -> list[TradeBurst]:
    bursts: list[TradeBurst] = []
    by_key: dict[tuple[str, str, str], list[TradeRow]] = collections.defaultdict(list)
    for row in rows:
        by_key[(row.slug, row.side, row.outcome)].append(row)

    for (slug, side, outcome), group in by_key.items():
        ordered = sorted(group, key=lambda row: (row.timestamp, row.transaction_hash))
        current: Optional[TradeBurst] = None
        for row in ordered:
            if current is None or row.timestamp - current.end_ts > gap_secs:
                current = TradeBurst(
                    slug=slug,
                    side=side,
                    outcome=outcome,
                    start_ts=row.timestamp,
                    end_ts=row.timestamp,
                    total_size=row.size,
                    total_spend=row.size * row.price,
                    fills=1,
                )
                bursts.append(current)
                continue
            current.end_ts = row.timestamp
            current.total_size += row.size
            current.total_spend += row.size * row.price
            current.fills += 1
    bursts.sort(key=lambda burst: (burst.slug, burst.start_ts, burst.outcome, burst.side))
    return bursts


def _classify_burst_effect(delta_pnl_if_up: float, delta_pnl_if_down: float, delta_cost_ratio: float) -> str:
    eps = 1e-9
    if delta_pnl_if_up < -eps and delta_pnl_if_down < -eps:
        return "damage"
    if delta_pnl_if_up > eps and delta_pnl_if_down < -eps:
        return "favor_up"
    if delta_pnl_if_down > eps and delta_pnl_if_up < -eps:
        return "favor_down"
    if delta_pnl_if_up > eps and delta_pnl_if_down > eps:
        return "repair"
    if delta_cost_ratio < -eps and (delta_pnl_if_up > eps or delta_pnl_if_down > eps):
        return "repair"
    return "mixed"


def build_burst_evolution(
    rows: list[TradeRow],
    slug: str,
    *,
    gap_secs: int = 5,
) -> list[BurstEvolutionStep]:
    window_rows = [row for row in rows if row.slug == slug and row.side == "BUY"]
    bursts = [burst for burst in cluster_trade_bursts(window_rows, gap_secs=gap_secs) if burst.side == "BUY"]
    bursts.sort(key=lambda burst: (burst.start_ts, burst.outcome))

    steps: list[BurstEvolutionStep] = []
    up_shares = 0.0
    down_shares = 0.0
    spend = 0.0
    prev_pnl_if_up = 0.0
    prev_pnl_if_down = 0.0
    prev_ratio = 0.0
    prev_favored_side = "Flat"
    for idx, burst in enumerate(bursts, start=1):
        spend += burst.total_spend
        if burst.outcome == "Up":
            up_shares += burst.total_size
        elif burst.outcome == "Down":
            down_shares += burst.total_size
        best_payout = max(up_shares, down_shares)
        ratio = (spend / best_payout) if best_payout > 0 else 0.0
        pnl_if_up = up_shares - spend
        pnl_if_down = down_shares - spend
        delta_pnl_if_up = pnl_if_up - prev_pnl_if_up
        delta_pnl_if_down = pnl_if_down - prev_pnl_if_down
        delta_cost_ratio = ratio - prev_ratio
        favored_side = _favored_side_from_pnl(pnl_if_up, pnl_if_down)
        favored_changed = (
            favored_side != "Flat"
            and prev_favored_side != "Flat"
            and favored_side != prev_favored_side
        )
        steps.append(
            BurstEvolutionStep(
                idx=idx,
                burst=burst,
                cumulative_up_shares=up_shares,
                cumulative_down_shares=down_shares,
                cumulative_spend=spend,
                pnl_if_up=pnl_if_up,
                pnl_if_down=pnl_if_down,
                combined_cost_ratio=ratio,
                delta_pnl_if_up=delta_pnl_if_up,
                delta_pnl_if_down=delta_pnl_if_down,
                delta_cost_ratio=delta_cost_ratio,
                effect=_classify_burst_effect(delta_pnl_if_up, delta_pnl_if_down, delta_cost_ratio),
                favored_side=favored_side,
                favored_changed=favored_changed,
            )
        )
        prev_pnl_if_up = pnl_if_up
        prev_pnl_if_down = pnl_if_down
        prev_ratio = ratio
        if favored_side != "Flat":
            prev_favored_side = favored_side
    return steps


def summarize_burst_evolution(
    rows: list[TradeRow],
    slug: str,
    *,
    winner: Optional[str] = None,
    gap_secs: int = 5,
) -> str:
    steps = build_burst_evolution(rows, slug, gap_secs=gap_secs)
    lines = [f"Burst evolution for {slug}"]
    if winner in {"Up", "Down"}:
        lines[0] += f" | winner={winner}"
    if not steps:
        lines.append("  No BUY bursts found")
        return "\n".join(lines)

    lines.append(f"  BUY bursts: {len(steps)} (gap <= {gap_secs}s)")
    final = steps[-1]
    effect_counts = collections.Counter(step.effect for step in steps)
    favored_flip_count = sum(1 for step in steps if step.favored_changed)
    initial_favored = next((step.favored_side for step in steps if step.favored_side != "Flat"), "Flat")
    lines.append(
        f"  Final geometry: spend=${final.cumulative_spend:,.2f} "
        f"| up_shares={final.cumulative_up_shares:,.1f} "
        f"| down_shares={final.cumulative_down_shares:,.1f} "
        f"| pnl_if_up=${final.pnl_if_up:,.2f} "
        f"| pnl_if_down=${final.pnl_if_down:,.2f} "
        f"| cost_ratio={final.combined_cost_ratio:.3f}"
    )
    lines.append(
        f"  Favored side: initial={initial_favored} final={final.favored_side} flips={favored_flip_count}"
    )
    lines.append(
        "  Burst effects: "
        + ", ".join(f"{name}={count}" for name, count in sorted(effect_counts.items()))
    )
    lines.append("  Steps:")
    for step in steps:
        sec = step.burst.start_seconds_into_window
        sec_label = f"{sec:>3}s" if sec is not None else " ?s"
        realized = ""
        if winner == "Up":
            realized = f" | realized=${step.pnl_if_up:,.2f}"
        elif winner == "Down":
            realized = f" | realized=${step.pnl_if_down:,.2f}"
        lines.append(
            f"    {step.idx:02d}. {sec_label} {step.burst.outcome} "
            f"@${step.burst.avg_price:.3f} x{step.burst.fills} "
            f"({step.burst.total_size:,.1f}sh / ${step.burst.total_spend:,.2f}) "
            f"-> spend=${step.cumulative_spend:,.2f} "
            f"| pnl_if_up=${step.pnl_if_up:,.2f} "
            f"| pnl_if_down=${step.pnl_if_down:,.2f} "
            f"| d_up=${step.delta_pnl_if_up:,.2f} "
            f"| d_down=${step.delta_pnl_if_down:,.2f} "
            f"| d_cost={step.delta_cost_ratio:+.3f} "
            f"| cost={step.combined_cost_ratio:.3f} "
            f"| favored={step.favored_side}"
            f"{' *flip*' if step.favored_changed else ''} "
            f"| {step.effect}{realized}"
        )
    return "\n".join(lines)


def summarize_rows(label: str, rows: list[TradeRow], *, burst_gap_secs: int = 5) -> str:
    lines: list[str] = []
    lines.append(f"{label}: {len(rows)} BTC 5m trade rows")
    if not rows:
        return "\n".join(lines)

    by_slug: dict[str, list[TradeRow]] = collections.defaultdict(list)
    side_counts = collections.Counter(r.side for r in rows)
    outcome_counts = collections.Counter(r.outcome for r in rows)
    bursts = cluster_trade_bursts(rows, gap_secs=burst_gap_secs)
    bursts_by_slug: dict[str, list[TradeBurst]] = collections.defaultdict(list)
    for row in rows:
        by_slug[row.slug].append(row)
    for burst in bursts:
        bursts_by_slug[burst.slug].append(burst)

    both_outcomes = 0
    buy_only_windows = 0
    sell_windows = 0
    scale_windows = 0
    sec_values: list[int] = []
    price_values: list[float] = []
    size_values: list[float] = []
    buy_burst_counts: list[int] = []
    side_burst_counts: list[int] = []
    one_buy_burst_per_side = 0
    early_burst_counts: list[int] = []
    mid_burst_counts: list[int] = []
    late_burst_counts: list[int] = []

    for slug, vals in by_slug.items():
        outcomes = {v.outcome for v in vals if v.outcome}
        buys = [v for v in vals if v.side == "BUY"]
        sells = [v for v in vals if v.side == "SELL"]
        window_bursts = bursts_by_slug.get(slug, [])
        buy_bursts = [b for b in window_bursts if b.side == "BUY"]
        if len(outcomes) > 1:
            both_outcomes += 1
        if buys and not sells:
            buy_only_windows += 1
        if sells:
            sell_windows += 1
        if len(buys) >= 3:
            scale_windows += 1
        if buy_bursts:
            buy_burst_counts.append(len(buy_bursts))
            side_counts_in_window = collections.Counter(b.outcome for b in buy_bursts)
            side_burst_counts.extend(side_counts_in_window.values())
            if side_counts_in_window.get("Up", 0) == 1 and side_counts_in_window.get("Down", 0) == 1:
                one_buy_burst_per_side += 1
            early_burst_counts.append(sum(1 for b in buy_bursts if (b.start_seconds_into_window or 0) < 100))
            mid_burst_counts.append(sum(1 for b in buy_bursts if 100 <= (b.start_seconds_into_window or 0) < 200))
            late_burst_counts.append(sum(1 for b in buy_bursts if (b.start_seconds_into_window or 0) >= 200))
        for v in vals:
            if v.seconds_into_window is not None:
                sec_values.append(v.seconds_into_window)
            price_values.append(v.price)
            size_values.append(v.size)

    lines.append(f"Distinct windows: {len(by_slug)}")
    lines.append(f"Sides: {dict(side_counts)}")
    lines.append(f"Outcomes: {dict(outcome_counts)}")
    lines.append(
        f"Windows with both outcomes traded: {both_outcomes}/{len(by_slug)} "
        f"({pct(both_outcomes, len(by_slug)):.1f}%)"
    )
    lines.append(
        f"Windows with >=3 buys (scaling): {scale_windows}/{len(by_slug)} "
        f"({pct(scale_windows, len(by_slug)):.1f}%)"
    )
    lines.append(
        f"Windows with visible sells: {sell_windows}/{len(by_slug)} "
        f"({pct(sell_windows, len(by_slug)):.1f}%)"
    )
    lines.append(
        f"Windows with buys but no visible sells: {buy_only_windows}/{len(by_slug)} "
        f"({pct(buy_only_windows, len(by_slug)):.1f}%)"
    )
    lines.append(f"BUY bursts (gap <= {burst_gap_secs}s): {len([b for b in bursts if b.side == 'BUY'])} total")
    if buy_burst_counts:
        lines.append(
            f"Buy bursts/window: median {median_or_zero(list(map(float, buy_burst_counts))):.1f} "
            f"(min {min(buy_burst_counts)}, max {max(buy_burst_counts)})"
        )
    if side_burst_counts:
        lines.append(
            f"Buy bursts per side/window: median {median_or_zero(list(map(float, side_burst_counts))):.1f} "
            f"(min {min(side_burst_counts)}, max {max(side_burst_counts)})"
        )
    if buy_burst_counts:
        lines.append(
            f"Burst timing/window: early median {median_or_zero(list(map(float, early_burst_counts))):.1f}, "
            f"mid median {median_or_zero(list(map(float, mid_burst_counts))):.1f}, "
            f"late median {median_or_zero(list(map(float, late_burst_counts))):.1f}"
        )
    lines.append(
        f"Windows with exactly one BUY burst per side: {one_buy_burst_per_side}/{len(by_slug)} "
        f"({pct(one_buy_burst_per_side, len(by_slug)):.1f}%)"
    )
    if sec_values:
        lines.append(
            f"Entry timing: median {median_or_zero(list(map(float, sec_values))):.1f}s into window "
            f"(min {min(sec_values)}s, max {max(sec_values)}s)"
        )
    lines.append(
        f"Trade price: median ${median_or_zero(price_values):.3f} "
        f"(min ${min(price_values):.3f}, max ${max(price_values):.3f})"
    )
    lines.append(
        f"Trade size: median {median_or_zero(size_values):.4f} shares "
        f"(min {min(size_values):.4f}, max {max(size_values):.4f})"
    )

    latest = sorted(by_slug.items(), key=lambda kv: max(v.timestamp for v in kv[1]), reverse=True)[:5]
    lines.append("Recent windows:")
    for slug, vals in latest:
        vals = sorted(vals, key=lambda r: r.timestamp)
        preview = ", ".join(
            f"{v.seconds_into_window:>3}s {v.side} {v.outcome} @{v.price:.3f}"
            for v in vals[:8]
            if v.seconds_into_window is not None
        )
        burst_preview = ", ".join(
            f"{(b.start_seconds_into_window if b.start_seconds_into_window is not None else -1):>3}s "
            f"{b.side} {b.outcome} @{b.avg_price:.3f} x{b.fills}"
            for b in bursts_by_slug.get(slug, [])[:6]
        )
        lines.append(f"  {slug}: {preview}")
        if burst_preview:
            lines.append(f"    bursts: {burst_preview}")
    return "\n".join(lines)


def compare_trade_views(taker_rows: list[TradeRow], activity_rows: list[TradeRow]) -> str:
    lines: list[str] = []
    taker_slugs = {r.slug for r in taker_rows}
    activity_slugs = {r.slug for r in activity_rows}
    lines.append("Cross-check:")
    lines.append(f"  taker windows: {len(taker_slugs)}")
    lines.append(f"  activity windows: {len(activity_slugs)}")
    lines.append(f"  overlap: {len(taker_slugs & activity_slugs)}")
    return "\n".join(lines)


def summarize_snapshot(
    positions: list[SnapshotPosition],
    equity: Optional[SnapshotEquity],
    reference_rows: list[TradeRow],
) -> str:
    lines: list[str] = []
    lines.append("Accounting snapshot:")
    if equity is not None:
        lines.append(
            f"  Equity ${equity.equity:,.2f} = cash ${equity.cash_balance:,.2f} + "
            f"positions ${equity.positions_value:,.2f} @ {equity.valuation_time}"
        )
    else:
        lines.append("  No equity row returned")

    if not positions:
        lines.append("  No open positions in snapshot")
        return "\n".join(lines)

    by_asset = {r.asset: r for r in reference_rows}
    by_condition = collections.defaultdict(list)
    for pos in positions:
        by_condition[pos.condition_id].append(pos)

    lines.append(f"  Open positions: {len(positions)} rows across {len(by_condition)} condition(s)")
    for condition_id, vals in by_condition.items():
        labels = []
        slug = ""
        for v in vals:
            ref = by_asset.get(v.asset)
            outcome = ref.outcome if ref else "?"
            slug = slug or (ref.slug if ref else "")
            labels.append(f"{outcome} {v.size:.4f} @ ${v.cur_price:.3f}")
        lines.append(f"  {slug or condition_id}: " + " | ".join(labels))
    return "\n".join(lines)


def _safe_ratio(a: float, b: float) -> float:
    if abs(b) <= 1e-9:
        return 0.0
    return a / b


def _estimate_price_sum(vals: list[TradeRow], *, tolerance_secs: int = 5) -> float:
    up_rows = [row for row in vals if row.side == "BUY" and row.outcome == "Up"]
    down_rows = [row for row in vals if row.side == "BUY" and row.outcome == "Down"]
    if not up_rows or not down_rows:
        return 0.0

    used_down: set[int] = set()
    sums: list[float] = []

    for up in sorted(up_rows, key=lambda row: row.timestamp):
        best_idx = None
        best_gap = None
        for idx, down in enumerate(down_rows):
            if idx in used_down:
                continue
            gap = abs(up.timestamp - down.timestamp)
            if gap > tolerance_secs:
                continue
            if best_gap is None or gap < best_gap:
                best_gap = gap
                best_idx = idx
        if best_idx is None:
            continue
        used_down.add(best_idx)
        sums.append(up.price + down_rows[best_idx].price)

    return statistics.median(sums) if sums else 0.0


def _bucket_btc_delta(delta: float) -> str:
    if delta <= -20:
        return "<=-20"
    if delta <= -5:
        return "-20..-5"
    if delta < 5:
        return "-5..+5"
    if delta < 20:
        return "+5..+20"
    return ">=+20"


def _bucket_combined_cost(ratio: float) -> str:
    if ratio < 0.80:
        return "<0.80"
    if ratio < 0.90:
        return "0.80-0.899"
    if ratio < 1.00:
        return "0.90-0.999"
    return ">=1.00"


def _lean_side(up_shares: float, down_shares: float) -> str:
    if up_shares > down_shares:
        return "Up"
    if down_shares > up_shares:
        return "Down"
    return "Flat"


def _favored_side_from_pnl(pnl_if_up: float, pnl_if_down: float) -> str:
    if pnl_if_up > pnl_if_down:
        return "Up"
    if pnl_if_down > pnl_if_up:
        return "Down"
    return "Flat"


def _lean_alignment(lean_side: str, btc_delta: float) -> str:
    if abs(btc_delta) < 5:
        return f"{lean_side}-flat"
    if lean_side == "Up":
        return "Up-with-BTC" if btc_delta > 0 else "Up-against-BTC"
    if lean_side == "Down":
        return "Down-with-BTC" if btc_delta < 0 else "Down-against-BTC"
    return "Flat"


def _local_hour_bucket(ts: int) -> str:
    if ts <= 0:
        return "unknown"
    return datetime.fromtimestamp(ts, UTC).astimezone(LOCAL_TZ).strftime("%H:00")


def _streak_alignment(initial_favored_side: str, prev_winner: str) -> str:
    if prev_winner not in {"Up", "Down"} or initial_favored_side not in {"Up", "Down"}:
        return "no-prev"
    if initial_favored_side == prev_winner:
        return "with-prev-streak"
    return "against-prev-streak"


def _bucket_prev_streak(length: int) -> str:
    if length <= 0:
        return "no-prev"
    if length == 1:
        return "prev-1"
    if length == 2:
        return "prev-2"
    return "prev-3+"


def _clamp_price(price: float) -> float:
    return min(0.99, max(0.01, price))


def _post_burst_state(
    up_shares: float,
    down_shares: float,
    spend: float,
    *,
    outcome: str,
    burst_spend: float,
    burst_price: float,
) -> tuple[float, float, float]:
    burst_price = _clamp_price(burst_price)
    shares = burst_spend / burst_price if burst_price > 0 else 0.0
    spend += burst_spend
    if outcome == "Up":
        up_shares += shares
    else:
        down_shares += shares
    return up_shares, down_shares, spend


def _formula_metrics(up_shares: float, down_shares: float, spend: float) -> tuple[float, float, float, float]:
    pnl_if_up = up_shares - spend
    pnl_if_down = down_shares - spend
    best_payout = max(up_shares, down_shares, 1.0)
    cost_ratio = spend / best_payout if best_payout > 0 else 0.0
    return pnl_if_up, pnl_if_down, best_payout, cost_ratio


def _score_linear_bounded(up_shares: float, down_shares: float, spend: float) -> float:
    pnl_if_up, pnl_if_down, _, _ = _formula_metrics(up_shares, down_shares, spend)
    favored = max(pnl_if_up, pnl_if_down)
    other = min(pnl_if_up, pnl_if_down)
    return favored - 0.75 * max(0.0, -other)


def _score_quadratic_downside(up_shares: float, down_shares: float, spend: float) -> float:
    pnl_if_up, pnl_if_down, best_payout, _ = _formula_metrics(up_shares, down_shares, spend)
    favored = max(pnl_if_up, pnl_if_down)
    other = min(pnl_if_up, pnl_if_down)
    downside = max(0.0, -other)
    return favored - (downside * downside / best_payout)


def _score_quadratic_cost(up_shares: float, down_shares: float, spend: float) -> float:
    pnl_if_up, pnl_if_down, best_payout, cost_ratio = _formula_metrics(up_shares, down_shares, spend)
    favored = max(pnl_if_up, pnl_if_down)
    other = min(pnl_if_up, pnl_if_down)
    cost_penalty = 6.0 * best_payout * max(0.0, cost_ratio - 0.85) ** 2
    return favored - 0.5 * max(0.0, -other) - cost_penalty


def _score_target_geometry(up_shares: float, down_shares: float, spend: float) -> float:
    pnl_if_up, pnl_if_down, best_payout, cost_ratio = _formula_metrics(up_shares, down_shares, spend)
    favored = max(pnl_if_up, pnl_if_down)
    other = min(pnl_if_up, pnl_if_down)
    cost_penalty = 4.0 * best_payout * max(0.0, cost_ratio - 0.85) ** 2
    return -abs(favored - 75.0) - 0.5 * abs(other + 75.0) - cost_penalty


FORMULA_SCORERS = {
    "linear_bounded": _score_linear_bounded,
    "quadratic_downside": _score_quadratic_downside,
    "quadratic_cost": _score_quadratic_cost,
    "target_geometry": _score_target_geometry,
}


def evaluate_formula_fits(
    rows: list[TradeRow],
    *,
    limit_windows: int = 50,
    slug_filter: Optional[set[str]] = None,
    gap_secs: int = 5,
) -> list[FormulaFitResult]:
    by_slug: dict[str, list[TradeRow]] = collections.defaultdict(list)
    for row in rows:
        if row.side != "BUY":
            continue
        if slug_filter and row.slug not in slug_filter:
            continue
        by_slug[row.slug].append(row)
    ordered = sorted(by_slug.items(), key=lambda kv: max(r.timestamp for r in kv[1]), reverse=True)[:limit_windows]
    formula_hits = {
        name: {
            "choices": 0,
            "correct": 0,
            "up_predictions": 0,
            "down_predictions": 0,
            "up_actual_correct": 0,
            "down_actual_correct": 0,
        }
        for name in FORMULA_SCORERS
    }
    for slug, vals in ordered:
        bursts = [burst for burst in cluster_trade_bursts(vals, gap_secs=gap_secs) if burst.side == "BUY"]
        bursts.sort(key=lambda burst: (burst.start_ts, burst.outcome))
        up_shares = 0.0
        down_shares = 0.0
        spend = 0.0
        for burst in bursts:
            alt_price = _clamp_price(1.0 - burst.avg_price)
            actual_up, actual_down, actual_spend = _post_burst_state(
                up_shares,
                down_shares,
                spend,
                outcome=burst.outcome,
                burst_spend=burst.total_spend,
                burst_price=burst.avg_price,
            )
            cf_up = _post_burst_state(
                up_shares,
                down_shares,
                spend,
                outcome="Up",
                burst_spend=burst.total_spend,
                burst_price=burst.avg_price if burst.outcome == "Up" else alt_price,
            )
            cf_down = _post_burst_state(
                up_shares,
                down_shares,
                spend,
                outcome="Down",
                burst_spend=burst.total_spend,
                burst_price=burst.avg_price if burst.outcome == "Down" else alt_price,
            )
            for name, scorer in FORMULA_SCORERS.items():
                up_score = scorer(*cf_up)
                down_score = scorer(*cf_down)
                predicted = "Up" if up_score >= down_score else "Down"
                rec = formula_hits[name]
                rec["choices"] += 1
                rec["up_predictions"] += 1 if predicted == "Up" else 0
                rec["down_predictions"] += 1 if predicted == "Down" else 0
                if predicted == burst.outcome:
                    rec["correct"] += 1
                    if burst.outcome == "Up":
                        rec["up_actual_correct"] += 1
                    else:
                        rec["down_actual_correct"] += 1
            up_shares, down_shares, spend = actual_up, actual_down, actual_spend
    return [
        FormulaFitResult(
            name=name,
            choices=stats["choices"],
            correct=stats["correct"],
            up_predictions=stats["up_predictions"],
            down_predictions=stats["down_predictions"],
            up_actual_correct=stats["up_actual_correct"],
            down_actual_correct=stats["down_actual_correct"],
        )
        for name, stats in formula_hits.items()
    ]


def summarize_formula_fits(results: list[FormulaFitResult]) -> str:
    lines = ["Formula fit (approximate counterfactuals via complementary price):"]
    if not results:
        lines.append("  No bursts available")
        return "\n".join(lines)
    for result in sorted(results, key=lambda row: (row.correct / row.choices) if row.choices else 0.0, reverse=True):
        accuracy = pct(result.correct, result.choices)
        lines.append(
            f"  {result.name:18s} accuracy={accuracy:.1f}% "
            f"({result.correct}/{result.choices}) "
            f"| preds Up/Down={result.up_predictions}/{result.down_predictions} "
            f"| correct Up/Down={result.up_actual_correct}/{result.down_actual_correct}"
        )
    return "\n".join(lines)


def reconstruct_windows(
    analyzer: WalletAnalyzer,
    rows: list[TradeRow],
    *,
    limit: int = 25,
    slug_filter: Optional[set[str]] = None,
    btc_history: Optional[BTCHistoryClient] = None,
) -> list[WindowReconstruction]:
    by_slug: dict[str, list[TradeRow]] = collections.defaultdict(list)
    for row in rows:
        if row.side != "BUY":
            continue
        if slug_filter and row.slug not in slug_filter:
            continue
        by_slug[row.slug].append(row)

    ordered = sorted(by_slug.items(), key=lambda kv: max(r.timestamp for r in kv[1]), reverse=True)
    recon: list[WindowReconstruction] = []
    for slug, vals in ordered:
        winner = analyzer.fetch_market_winner(slug)
        if winner not in {"Up", "Down"}:
            continue
        up_rows = [r for r in vals if r.outcome == "Up"]
        down_rows = [r for r in vals if r.outcome == "Down"]
        up_shares = sum(r.size for r in up_rows)
        down_shares = sum(r.size for r in down_rows)
        up_spend = sum(r.size * r.price for r in up_rows)
        down_spend = sum(r.size * r.price for r in down_rows)
        total_spend = up_spend + down_spend
        winning_shares = up_shares if winner == "Up" else down_shares
        gross_pnl = winning_shares - total_spend
        roi = (gross_pnl / total_spend) if total_spend > 0 else 0.0
        gross_pnl_if_up = up_shares - total_spend
        gross_pnl_if_down = down_shares - total_spend
        combined_cost_ratio = total_spend / max(up_shares, down_shares) if max(up_shares, down_shares) > 0 else 0.0
        price_sum = _estimate_price_sum(vals)
        share_skew = _safe_ratio(up_shares, down_shares)
        notional_skew = _safe_ratio(up_spend, down_spend)
        payout_skew = up_shares - down_shares
        winner_overweight = (winner == "Up" and up_shares > down_shares) or (winner == "Down" and down_shares > up_shares)
        secs = [r.seconds_into_window for r in vals if r.seconds_into_window is not None]
        lean_side = _lean_side(up_shares, down_shares)
        first_buy_s = min(secs) if secs else None
        last_buy_s = max(secs) if secs else None
        window_start_ts = vals[0].window_start_ts
        btc_open_price = btc_history.price_at(window_start_ts) if btc_history else 0.0
        first_buy_ts = window_start_ts + first_buy_s if first_buy_s is not None else window_start_ts
        last_buy_ts = window_start_ts + last_buy_s if last_buy_s is not None else window_start_ts
        btc_first_buy_price = btc_history.price_at(first_buy_ts) if btc_history else 0.0
        btc_last_buy_price = btc_history.price_at(last_buy_ts) if btc_history else 0.0
        btc_delta_first_buy = (btc_first_buy_price - btc_open_price) if btc_open_price and btc_first_buy_price else 0.0
        btc_delta_last_buy = (btc_last_buy_price - btc_open_price) if btc_open_price and btc_last_buy_price else 0.0
        evolution = build_burst_evolution(vals, slug)
        initial_favored_side = next((step.favored_side for step in evolution if step.favored_side != "Flat"), "Flat")
        final_favored_side = evolution[-1].favored_side if evolution else "Flat"
        favored_flip_count = sum(1 for step in evolution if step.favored_changed)
        first_burst = evolution[0].burst if evolution else None
        early_bursts = sum(1 for step in evolution if (step.burst.start_seconds_into_window or 0) < 100)
        mid_bursts = sum(1 for step in evolution if 100 <= (step.burst.start_seconds_into_window or 0) < 200)
        late_bursts = sum(1 for step in evolution if (step.burst.start_seconds_into_window or 0) >= 200)
        recon.append(
            WindowReconstruction(
                slug=slug,
                winner=winner,
                lean_side=lean_side,
                local_hour_bucket=_local_hour_bucket(window_start_ts),
                first_burst_outcome=first_burst.outcome if first_burst else "Flat",
                first_burst_s=first_burst.start_seconds_into_window if first_burst else None,
                initial_favored_side=initial_favored_side,
                final_favored_side=final_favored_side,
                favored_flip_count=favored_flip_count,
                prev_winner="None",
                prev_winner_streak=0,
                streak_alignment="no-prev",
                burst_count=len(evolution),
                early_bursts=early_bursts,
                mid_bursts=mid_bursts,
                late_bursts=late_bursts,
                up_shares=up_shares,
                up_spend=up_spend,
                down_shares=down_shares,
                down_spend=down_spend,
                combined_spend=total_spend,
                payout_if_up=up_shares,
                payout_if_down=down_shares,
                gross_pnl=gross_pnl,
                roi=roi,
                gross_pnl_if_up=gross_pnl_if_up,
                gross_pnl_if_down=gross_pnl_if_down,
                combined_cost_ratio=combined_cost_ratio,
                price_sum=price_sum,
                share_skew=share_skew,
                notional_skew=notional_skew,
                payout_skew=payout_skew,
                winner_overweight=winner_overweight,
                first_buy_s=first_buy_s,
                last_buy_s=last_buy_s,
                btc_open_price=btc_open_price,
                btc_first_buy_price=btc_first_buy_price,
                btc_last_buy_price=btc_last_buy_price,
                btc_delta_first_buy=btc_delta_first_buy,
                btc_delta_last_buy=btc_delta_last_buy,
                lean_btc_alignment=_lean_alignment(lean_side, btc_delta_first_buy),
            )
        )
        if len(recon) >= limit:
            break
    chron = sorted(recon, key=lambda row: int(row.slug.rsplit("-", 1)[-1]))
    for idx, row in enumerate(chron):
        if idx == 0:
            continue
        prev_winner = chron[idx - 1].winner
        streak_len = 1
        j = idx - 2
        while j >= 0 and chron[j].winner == prev_winner:
            streak_len += 1
            j -= 1
        row.prev_winner = prev_winner
        row.prev_winner_streak = streak_len
        row.streak_alignment = _streak_alignment(row.initial_favored_side, prev_winner)
    return recon


def summarize_reconstructions(rows: list[WindowReconstruction], summary_by: Optional[str] = None) -> str:
    lines: list[str] = []
    lines.append(f"Per-window reconstruction: {len(rows)} recent windows")
    if not rows:
        return "\n".join(lines)
    pos = [r for r in rows if r.gross_pnl > 0]
    neg = [r for r in rows if r.gross_pnl <= 0]
    lines.append(
        f"  Positive gross windows: {len(pos)}/{len(rows)} "
        f"({pct(len(pos), len(rows)):.1f}%)"
    )
    lines.append(
        f"  Total gross P&L: ${sum(r.gross_pnl for r in rows):,.2f} "
        f"| avg ${statistics.mean(r.gross_pnl for r in rows):,.2f}"
    )
    lines.append(
        f"  Median ROI: {statistics.median(r.roi for r in rows)*100:.1f}% "
        f"| avg ROI: {statistics.mean(r.roi for r in rows)*100:.1f}%"
    )
    lines.append(
        f"  Winner overweighted: {sum(1 for r in rows if r.winner_overweight)}/{len(rows)} "
        f"({pct(sum(1 for r in rows if r.winner_overweight), len(rows)):.1f}%)"
    )
    lines.append(
        f"  First burst side counts: {dict(collections.Counter(r.first_burst_outcome for r in rows))}"
    )
    lines.append(
        f"  Initial favored side counts: {dict(collections.Counter(r.initial_favored_side for r in rows))}"
    )
    lines.append(
        f"  Final favored side counts: {dict(collections.Counter(r.final_favored_side for r in rows))}"
    )
    lines.append(
        f"  Previous streak alignment: {dict(collections.Counter(r.streak_alignment for r in rows))}"
    )
    lines.append(
        f"  Favored flips/window: median {statistics.median(r.favored_flip_count for r in rows):.1f} "
        f"(min {min(r.favored_flip_count for r in rows)}, max {max(r.favored_flip_count for r in rows)})"
    )
    lines.append(
        f"  Previous winner streak/window: median {statistics.median(r.prev_winner_streak for r in rows):.1f} "
        f"(min {min(r.prev_winner_streak for r in rows)}, max {max(r.prev_winner_streak for r in rows)})"
    )
    lines.append(
        f"  Burst timing/window: early median {statistics.median(r.early_bursts for r in rows):.1f}, "
        f"mid median {statistics.median(r.mid_bursts for r in rows):.1f}, "
        f"late median {statistics.median(r.late_bursts for r in rows):.1f}"
    )
    cost_ratios = sorted(r.combined_cost_ratio for r in rows)
    price_sums = [r.price_sum for r in rows if r.price_sum > 0]
    lines.append(
        f"  Combined cost ratio: median {statistics.median(cost_ratios):.3f} "
        f"(min {cost_ratios[0]:.3f}, max {cost_ratios[-1]:.3f})"
    )
    btc_first = [r.btc_delta_first_buy for r in rows if r.btc_open_price > 0 and r.first_buy_s is not None]
    if btc_first:
        lines.append(
            f"  BTC delta at first buy: median {statistics.median(btc_first):+.2f} "
            f"(min {min(btc_first):+.2f}, max {max(btc_first):+.2f})"
        )
    if price_sums:
        lines.append(
            f"  Simultaneous Up+Down price sum: median {statistics.median(price_sums):.3f} "
            f"(min {min(price_sums):.3f}, max {max(price_sums):.3f})"
        )
    lines.append("  Best windows:")
    for r in sorted(rows, key=lambda x: x.gross_pnl, reverse=True)[:5]:
        lines.append(
            f"    {r.slug} winner={r.winner} pnl=${r.gross_pnl:,.2f} roi={r.roi*100:.1f}% "
            f"| up ${r.up_spend:,.2f}/{r.up_shares:,.1f}sh down ${r.down_spend:,.2f}/{r.down_shares:,.1f}sh "
            f"| cost_ratio {r.combined_cost_ratio:.3f} | buys {r.first_buy_s}s→{r.last_buy_s}s"
        )
    lines.append("  Worst windows:")
    for r in sorted(rows, key=lambda x: x.gross_pnl)[:5]:
        lines.append(
            f"    {r.slug} winner={r.winner} pnl=${r.gross_pnl:,.2f} roi={r.roi*100:.1f}% "
            f"| up ${r.up_spend:,.2f}/{r.up_shares:,.1f}sh down ${r.down_spend:,.2f}/{r.down_shares:,.1f}sh "
            f"| cost_ratio {r.combined_cost_ratio:.3f} | buys {r.first_buy_s}s→{r.last_buy_s}s"
        )
    if summary_by:
        lines.append("  Grouped summary:")
        for bucket, bucket_rows in _group_reconstructions(rows, summary_by).items():
            total = sum(r.gross_pnl for r in bucket_rows)
            wins = sum(1 for r in bucket_rows if r.gross_pnl > 0)
            lines.append(
                f"    {bucket:18s} {len(bucket_rows):3d} windows "
                f"pnl=${total:,.2f} avg=${(total/len(bucket_rows)) if bucket_rows else 0:,.2f} "
                f"win={pct(wins, len(bucket_rows)):.1f}% "
                f"overweight={pct(sum(1 for r in bucket_rows if r.winner_overweight), len(bucket_rows)):.1f}% "
                f"cost={statistics.median(r.combined_cost_ratio for r in bucket_rows):.3f}"
            )
    return "\n".join(lines)


def _group_reconstructions(rows: list[WindowReconstruction], mode: str) -> dict[str, list[WindowReconstruction]]:
    groups: dict[str, list[WindowReconstruction]] = collections.defaultdict(list)
    for row in rows:
        if mode == "winner":
            key = f"{row.winner}-overweight" if row.winner_overweight else f"{row.winner}-underweight"
        elif mode == "first_burst":
            key = row.first_burst_outcome
        elif mode == "initial_favored":
            key = row.initial_favored_side
        elif mode == "final_favored":
            key = row.final_favored_side
        elif mode == "prev_streak":
            key = _bucket_prev_streak(row.prev_winner_streak)
        elif mode == "streak_alignment":
            key = row.streak_alignment
        elif mode == "time_of_day":
            key = row.local_hour_bucket
        elif mode == "cost_x_streak":
            key = f"{_bucket_combined_cost(row.combined_cost_ratio)} | {row.streak_alignment}"
        elif mode == "cost_x_time":
            key = f"{_bucket_combined_cost(row.combined_cost_ratio)} | {row.local_hour_bucket}"
        elif mode == "cost_x_flip":
            if row.favored_flip_count == 0:
                flip_bucket = "no-flip"
            elif row.favored_flip_count == 1:
                flip_bucket = "one-flip"
            else:
                flip_bucket = "multi-flip"
            key = f"{_bucket_combined_cost(row.combined_cost_ratio)} | {flip_bucket}"
        elif mode == "favored_flip":
            if row.favored_flip_count == 0:
                key = "no-flip"
            elif row.favored_flip_count == 1:
                key = "one-flip"
            else:
                key = "multi-flip"
        elif mode == "burst_phase":
            if row.late_bursts > row.early_bursts and row.late_bursts > row.mid_bursts:
                key = "late-heavy"
            elif row.early_bursts > row.mid_bursts and row.early_bursts > row.late_bursts:
                key = "early-heavy"
            elif row.mid_bursts > row.early_bursts and row.mid_bursts > row.late_bursts:
                key = "mid-heavy"
            else:
                key = "balanced"
        elif mode == "timing":
            start = row.first_buy_s or 0
            if start < 60:
                key = "early(<60s)"
            elif start < 180:
                key = "mid(60-179s)"
            else:
                key = "late(180+s)"
        elif mode == "lean_side":
            key = row.lean_side
        elif mode == "btc_delta":
            key = _bucket_btc_delta(row.btc_delta_first_buy)
        elif mode == "lean_alignment":
            key = row.lean_btc_alignment
        elif mode == "cost_x_lean":
            key = f"{_bucket_combined_cost(row.combined_cost_ratio)} | {row.lean_side}"
        elif mode == "cost_x_btc":
            key = f"{_bucket_combined_cost(row.combined_cost_ratio)} | {_bucket_btc_delta(row.btc_delta_first_buy)}"
        elif mode == "combined_cost":
            key = _bucket_combined_cost(row.combined_cost_ratio)
        elif mode == "price_sum":
            price_sum = row.price_sum
            if price_sum <= 0:
                key = "unknown"
            elif price_sum < 0.98:
                key = "<0.98"
            elif price_sum < 1.00:
                key = "0.98-0.999"
            elif price_sum < 1.03:
                key = "1.00-1.029"
            else:
                key = ">=1.03"
        else:  # skew
            if row.payout_skew > 100:
                key = "up-heavy"
            elif row.payout_skew < -100:
                key = "down-heavy"
            else:
                key = "roughly-balanced"
        groups[key].append(row)
    return dict(sorted(groups.items()))


def export_reconstructions_csv(rows: list[WindowReconstruction], path: str):
    fields = [
        "slug", "winner", "lean_side", "local_hour_bucket", "first_burst_outcome", "first_burst_s",
        "initial_favored_side", "final_favored_side", "favored_flip_count", "prev_winner",
        "prev_winner_streak", "streak_alignment", "burst_count",
        "early_bursts", "mid_bursts", "late_bursts",
        "up_shares", "up_spend", "down_shares", "down_spend",
        "combined_spend", "payout_if_up", "payout_if_down",
        "gross_pnl", "roi", "gross_pnl_if_up", "gross_pnl_if_down",
        "combined_cost_ratio", "price_sum", "share_skew", "notional_skew",
        "payout_skew", "winner_overweight", "first_buy_s", "last_buy_s",
        "btc_open_price", "btc_first_buy_price", "btc_last_buy_price",
        "btc_delta_first_buy", "btc_delta_last_buy", "lean_btc_alignment",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                field: getattr(row, field)
                for field in fields
            })


def _load_slug_filter(path: str) -> set[str]:
    with open(path, "r", encoding="utf-8") as fh:
        return {line.strip() for line in fh if line.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze a public Polymarket wallet's BTC 5-minute market behavior."
    )
    parser.add_argument("user", help="0x-prefixed Polymarket profile/proxy wallet address")
    parser.add_argument("--max-trades", type=int, default=2000, help="Max /trades rows to fetch")
    parser.add_argument("--max-activity", type=int, default=1000, help="Max /activity rows to fetch")
    parser.add_argument("--reconstruct", type=int, default=0, help="Reconstruct N recent windows with winner/P&L estimates")
    parser.add_argument("--from-slugs", default="", help="Optional file with one slug per line to restrict reconstruction")
    parser.add_argument("--burst-window", default="", help="Print burst-by-burst payoff geometry for one slug")
    parser.add_argument("--burst-gap-secs", type=int, default=5, help="Gap threshold used to cluster fills into bursts")
    parser.add_argument("--fit-formulas", type=int, default=0, help="Evaluate candidate burst-choice formulas across N recent windows")
    parser.add_argument("--summary-by", choices=["skew", "timing", "combined_cost", "price_sum", "winner", "btc_delta", "lean_alignment", "lean_side", "cost_x_lean", "cost_x_btc", "cost_x_streak", "cost_x_time", "cost_x_flip", "favored_flip", "first_burst", "initial_favored", "final_favored", "burst_phase", "prev_streak", "streak_alignment", "time_of_day"], default="", help="Grouped summary mode for reconstruction output")
    parser.add_argument("--export-csv", default="", help="Write reconstructed window metrics to CSV")
    parser.add_argument("--json", action="store_true", help="Emit compact JSON summary")
    args = parser.parse_args()

    analyzer = WalletAnalyzer(args.user)
    taker_rows = analyzer.fetch_trades(taker_only=True, max_rows=args.max_trades)
    all_rows = analyzer.fetch_trades(taker_only=False, max_rows=args.max_trades)
    activity_rows = analyzer.fetch_activity(max_rows=args.max_activity)
    snapshot_positions, snapshot_equity = analyzer.fetch_accounting_snapshot()

    if args.json:
        payload = {
            "user": args.user.lower(),
            "taker_only_rows": len(taker_rows),
            "all_trade_rows": len(all_rows),
            "activity_rows": len(activity_rows),
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Wallet analysis for {args.user.lower()}")
    print("=" * 72)
    print(summarize_rows("Trades (takerOnly=true)", taker_rows))
    print()
    print(summarize_rows("Trades (takerOnly=false)", all_rows))
    print()
    print(summarize_rows("Activity (TRADE)", activity_rows))
    print()
    print(compare_trade_views(taker_rows, activity_rows))
    print()
    print(summarize_snapshot(snapshot_positions, snapshot_equity, all_rows or activity_rows or taker_rows))
    if args.burst_window:
        print()
        print(
            summarize_burst_evolution(
                all_rows,
                args.burst_window,
                winner=analyzer.fetch_market_winner(args.burst_window),
                gap_secs=args.burst_gap_secs,
            )
        )
    if args.reconstruct > 0:
        print()
        slug_filter = _load_slug_filter(args.from_slugs) if args.from_slugs else None
        candidate_rows = [row for row in all_rows if not slug_filter or row.slug in slug_filter]
        btc_history = None
        if candidate_rows:
            start_ts = min(row.window_start_ts for row in candidate_rows if row.window_start_ts > 0)
            end_ts = max(row.timestamp for row in candidate_rows) + 60
            btc_history = BTCHistoryClient()
            btc_history.load_range(start_ts, end_ts)
        recon = reconstruct_windows(
            analyzer,
            all_rows,
            limit=args.reconstruct,
            slug_filter=slug_filter,
            btc_history=btc_history,
        )
        if args.export_csv:
            export_reconstructions_csv(recon, args.export_csv)
        print(summarize_reconstructions(recon, summary_by=(args.summary_by or None)))
    if args.fit_formulas > 0:
        print()
        slug_filter = _load_slug_filter(args.from_slugs) if args.from_slugs else None
        print(
            summarize_formula_fits(
                evaluate_formula_fits(
                    all_rows,
                    limit_windows=args.fit_formulas,
                    slug_filter=slug_filter,
                    gap_secs=args.burst_gap_secs,
                )
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
