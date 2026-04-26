#!/usr/bin/env python3
"""
Import market_data_2sec_weekly5_with_resolutions.csv into a ResearchDatabase.

Column mapping:
  slug          → slug
  start_time    → window_start_ts (also btc_open_price via btc_strike)
  elapsed       → seconds elapsed; seconds_remaining = 300 - elapsed
  ask_YES/bid_YES → up_ask / up_bid
  ask_NO/bid_NO   → down_ask / down_bid
  btc_strike    → btc_open_price
  btc_current   → btc_spot
  btc_gap       → btc_delta (= btc_current - btc_strike)
  timestamp_log → tick timestamp
  winner        → resolution (only written when resolved=True)
"""

import argparse
import csv
import sys
from collections import defaultdict

sys.path.insert(0, ".")
from paired_research import ResearchDatabase, ResearchTick


def import_csv(csv_path: str, db_path: str, overwrite: bool = False):
    print(f"Loading {csv_path} ...")
    by_slug: dict[str, list[dict]] = defaultdict(list)
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            by_slug[row["slug"]].append(row)

    print(f"  {sum(len(v) for v in by_slug.values()):,} rows across {len(by_slug)} markets")

    db = ResearchDatabase(db_path)
    if overwrite:
        db.conn.executescript(
            "DELETE FROM research_ticks; "
            "DELETE FROM research_markets; "
            "DELETE FROM research_sim_results;"
        )
        db.conn.commit()
        print("  Cleared existing data.")

    inserted_markets = 0
    inserted_ticks = 0
    skipped = 0

    for slug, rows in sorted(by_slug.items(), key=lambda kv: kv[0]):
        rows_sorted = sorted(rows, key=lambda r: int(r["elapsed"]))
        first = rows_sorted[0]
        last = rows_sorted[-1]

        window_start = int(first["start_time"])
        window_end = window_start + 300
        btc_open = float(first["btc_strike"])
        resolved = last["resolved"].strip().lower() == "true"
        winner = last["winner"].strip() if resolved else None

        if not resolved:
            skipped += 1
            continue

        db.upsert_market({
            "slug": slug,
            "window_start_ts": window_start,
            "window_end_ts": window_end,
            "market_id": "",
            "condition_id": "",
            "up_token_id": f"{slug}-yes",
            "down_token_id": f"{slug}-no",
            "btc_open_price": btc_open,
            "btc_close_price": float(last["btc_current"]),
            "resolution": winner,
        })
        inserted_markets += 1

        for row in rows_sorted:
            elapsed = int(row["elapsed"])
            seconds_remaining = max(0.0, 300.0 - elapsed)
            tick = ResearchTick(
                timestamp=float(row["timestamp_log"]),
                seconds_remaining=seconds_remaining,
                up_bid=float(row["bid_YES"]),
                up_ask=float(row["ask_YES"]),
                down_bid=float(row["bid_NO"]),
                down_ask=float(row["ask_NO"]),
                btc_spot=float(row["btc_current"]),
                btc_delta=float(row["btc_gap"]),
            )
            db.insert_tick(slug, tick)
            inserted_ticks += 1

        if inserted_markets % 100 == 0:
            print(f"  ... {inserted_markets} markets, {inserted_ticks:,} ticks")

    db.close()
    print(f"Done. Imported {inserted_markets} markets, {inserted_ticks:,} ticks. Skipped {skipped} unresolved.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", help="Path to CSV file")
    parser.add_argument("--db", default="backtest_historical.db")
    parser.add_argument("--overwrite", action="store_true", help="Clear DB before importing")
    args = parser.parse_args()
    import_csv(args.csv, args.db, overwrite=args.overwrite)
