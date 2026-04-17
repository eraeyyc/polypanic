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
from dataclasses import dataclass
from typing import Iterable, Optional

import requests


DATA_API = "https://data-api.polymarket.com"
BTC_5M_PREFIX = "btc-updown-5m-"


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
class WindowReconstruction:
    slug: str
    winner: str
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


def summarize_rows(label: str, rows: list[TradeRow]) -> str:
    lines: list[str] = []
    lines.append(f"{label}: {len(rows)} BTC 5m trade rows")
    if not rows:
        return "\n".join(lines)

    by_slug: dict[str, list[TradeRow]] = collections.defaultdict(list)
    side_counts = collections.Counter(r.side for r in rows)
    outcome_counts = collections.Counter(r.outcome for r in rows)
    for row in rows:
        by_slug[row.slug].append(row)

    both_outcomes = 0
    buy_only_windows = 0
    sell_windows = 0
    scale_windows = 0
    sec_values: list[int] = []
    price_values: list[float] = []
    size_values: list[float] = []
    window_shapes = collections.Counter()

    for slug, vals in by_slug.items():
        outcomes = {v.outcome for v in vals if v.outcome}
        sides = {v.side for v in vals if v.side}
        buys = [v for v in vals if v.side == "BUY"]
        sells = [v for v in vals if v.side == "SELL"]
        if len(outcomes) > 1:
            both_outcomes += 1
        if buys and not sells:
            buy_only_windows += 1
        if sells:
            sell_windows += 1
        if len(buys) >= 3:
            scale_windows += 1
        if buys:
            window_shapes["both_outcomes" if len({b.outcome for b in buys}) > 1 else "one_outcome"] += 1
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
        lines.append(f"  {slug}: {preview}")
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


def _estimate_price_sum(vals: list[TradeRow]) -> float:
    by_ts: dict[int, dict[str, list[float]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    for row in vals:
        if row.side != "BUY" or not row.outcome:
            continue
        by_ts[row.timestamp][row.outcome].append(row.price)
    sums: list[float] = []
    for pairs in by_ts.values():
        if "Up" in pairs and "Down" in pairs:
            sums.append(statistics.median(pairs["Up"]) + statistics.median(pairs["Down"]))
    return statistics.median(sums) if sums else 0.0


def reconstruct_windows(
    analyzer: WalletAnalyzer,
    rows: list[TradeRow],
    *,
    limit: int = 25,
    slug_filter: Optional[set[str]] = None,
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
        recon.append(
            WindowReconstruction(
                slug=slug,
                winner=winner,
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
                first_buy_s=min(secs) if secs else None,
                last_buy_s=max(secs) if secs else None,
            )
        )
        if len(recon) >= limit:
            break
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
                f"win={pct(wins, len(bucket_rows)):.1f}%"
            )
    return "\n".join(lines)


def _group_reconstructions(rows: list[WindowReconstruction], mode: str) -> dict[str, list[WindowReconstruction]]:
    groups: dict[str, list[WindowReconstruction]] = collections.defaultdict(list)
    for row in rows:
        if mode == "winner":
            key = f"{row.winner}-overweight" if row.winner_overweight else f"{row.winner}-underweight"
        elif mode == "timing":
            start = row.first_buy_s or 0
            if start < 60:
                key = "early(<60s)"
            elif start < 180:
                key = "mid(60-179s)"
            else:
                key = "late(180+s)"
        elif mode == "combined_cost":
            ratio = row.combined_cost_ratio
            if ratio < 0.85:
                key = "<0.85"
            elif ratio < 0.95:
                key = "0.85-0.949"
            elif ratio < 1.05:
                key = "0.95-1.049"
            else:
                key = ">=1.05"
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
        "slug", "winner",
        "up_shares", "up_spend", "down_shares", "down_spend",
        "combined_spend", "payout_if_up", "payout_if_down",
        "gross_pnl", "roi", "gross_pnl_if_up", "gross_pnl_if_down",
        "combined_cost_ratio", "price_sum", "share_skew", "notional_skew",
        "payout_skew", "winner_overweight", "first_buy_s", "last_buy_s",
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
    parser.add_argument("--summary-by", choices=["skew", "timing", "combined_cost", "winner"], default="", help="Grouped summary mode for reconstruction output")
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
    if args.reconstruct > 0:
        print()
        slug_filter = _load_slug_filter(args.from_slugs) if args.from_slugs else None
        recon = reconstruct_windows(analyzer, all_rows, limit=args.reconstruct, slug_filter=slug_filter)
        if args.export_csv:
            export_reconstructions_csv(recon, args.export_csv)
        print(summarize_reconstructions(recon, summary_by=(args.summary_by or None)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
