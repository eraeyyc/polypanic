import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from paired_research import (
    MarketContext,
    ResearchDatabase,
    ResearchTick,
    policy_cost_gated_up_bias,
    policy_cost_gated_up_bias_flat_only,
    policy_cost_gated_up_bias_momentum,
    simulate_market,
)
from wallet_analyzer import (
    SnapshotEquity,
    TradeRow,
    WalletAnalyzer,
    _group_reconstructions,
    _estimate_price_sum,
    _lean_alignment,
    export_reconstructions_csv,
    reconstruct_windows,
)


class DummyResponse:
    def __init__(self, *, json_data=None, content=b""):
        self._json_data = json_data
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._json_data


def _build_snapshot_zip(positions_rows, equity_rows):
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as zf:
        if positions_rows is not None:
            buf = io.StringIO()
            writer = csv.DictWriter(
                buf,
                fieldnames=["conditionId", "asset", "size", "curPrice", "valuationTime"],
            )
            writer.writeheader()
            writer.writerows(positions_rows)
            zf.writestr("positions.csv", buf.getvalue())
        if equity_rows is not None:
            buf = io.StringIO()
            writer = csv.DictWriter(
                buf,
                fieldnames=["cashBalance", "positionsValue", "equity", "valuationTime"],
            )
            writer.writeheader()
            writer.writerows(equity_rows)
            zf.writestr("equity.csv", buf.getvalue())
    return blob.getvalue()


class WalletAnalyzerTests(unittest.TestCase):
    def test_lean_alignment_labels_with_and_against_btc(self):
        self.assertEqual(_lean_alignment("Up", 12.0), "Up-with-BTC")
        self.assertEqual(_lean_alignment("Up", -12.0), "Up-against-BTC")
        self.assertEqual(_lean_alignment("Down", -12.0), "Down-with-BTC")
        self.assertEqual(_lean_alignment("Down", 12.0), "Down-against-BTC")
        self.assertEqual(_lean_alignment("Up", 1.0), "Up-flat")

    def test_estimate_price_sum_matches_nearby_up_and_down_buys(self):
        rows = [
            TradeRow(
                timestamp=100,
                slug="btc-updown-5m-0",
                side="BUY",
                outcome="Up",
                price=0.287,
                size=100.0,
                asset="up",
                condition_id="cond",
                transaction_hash="tx1",
            ),
            TradeRow(
                timestamp=102,
                slug="btc-updown-5m-0",
                side="BUY",
                outcome="Down",
                price=0.725,
                size=100.0,
                asset="down",
                condition_id="cond",
                transaction_hash="tx2",
            ),
            TradeRow(
                timestamp=150,
                slug="btc-updown-5m-0",
                side="BUY",
                outcome="Up",
                price=0.400,
                size=100.0,
                asset="up",
                condition_id="cond",
                transaction_hash="tx3",
            ),
        ]
        self.assertAlmostEqual(_estimate_price_sum(rows), 1.012)

    def test_fetch_market_winner_accepts_dict_shape(self):
        analyzer = WalletAnalyzer("0xabc")
        analyzer.session.get = lambda *args, **kwargs: DummyResponse(
            json_data={
                "markets": [
                    {
                        "outcomes": json.dumps(["Up", "Down"]),
                        "outcomePrices": json.dumps(["1", "0"]),
                    }
                ]
            }
        )
        self.assertEqual(analyzer.fetch_market_winner("btc-updown-5m-1"), "Up")

    def test_fetch_market_winner_accepts_list_shape(self):
        analyzer = WalletAnalyzer("0xabc")
        analyzer.session.get = lambda *args, **kwargs: DummyResponse(
            json_data=[
                {
                    "markets": [
                        {
                            "outcomes": json.dumps(["Up", "Down"]),
                            "outcomePrices": json.dumps(["0", "1"]),
                        }
                    ]
                }
            ]
        )
        self.assertEqual(analyzer.fetch_market_winner("btc-updown-5m-1"), "Down")

    def test_fetch_accounting_snapshot_parses_positions_and_equity(self):
        analyzer = WalletAnalyzer("0xabc")
        snapshot = _build_snapshot_zip(
            positions_rows=[
                {
                    "conditionId": "cond-1",
                    "asset": "token-up",
                    "size": "12.5",
                    "curPrice": "0.44",
                    "valuationTime": "2026-04-16T10:00:00Z",
                }
            ],
            equity_rows=[
                {
                    "cashBalance": "900.0",
                    "positionsValue": "100.0",
                    "equity": "1000.0",
                    "valuationTime": "2026-04-16T10:00:00Z",
                }
            ],
        )
        analyzer.session.get = lambda *args, **kwargs: DummyResponse(content=snapshot)
        positions, equity = analyzer.fetch_accounting_snapshot()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].asset, "token-up")
        self.assertAlmostEqual(positions[0].size, 12.5)
        self.assertIsInstance(equity, SnapshotEquity)
        self.assertAlmostEqual(equity.equity, 1000.0)

    def test_fetch_accounting_snapshot_handles_flat_account(self):
        analyzer = WalletAnalyzer("0xabc")
        snapshot = _build_snapshot_zip(positions_rows=[], equity_rows=None)
        analyzer.session.get = lambda *args, **kwargs: DummyResponse(content=snapshot)
        positions, equity = analyzer.fetch_accounting_snapshot()
        self.assertEqual(positions, [])
        self.assertIsNone(equity)

    def test_reconstruct_windows_skips_unresolved_slugs(self):
        analyzer = WalletAnalyzer("0xabc")
        rows = [
            TradeRow(
                timestamp=310,
                slug="btc-updown-5m-300",
                side="BUY",
                outcome="Up",
                price=0.40,
                size=10.0,
                asset="up",
                condition_id="cond",
                transaction_hash="tx1",
            ),
            TradeRow(
                timestamp=311,
                slug="btc-updown-5m-300",
                side="BUY",
                outcome="Down",
                price=0.60,
                size=8.0,
                asset="down",
                condition_id="cond",
                transaction_hash="tx2",
            ),
            TradeRow(
                timestamp=610,
                slug="btc-updown-5m-600",
                side="BUY",
                outcome="Up",
                price=0.45,
                size=10.0,
                asset="up",
                condition_id="cond",
                transaction_hash="tx3",
            ),
        ]
        with patch.object(
            analyzer,
            "fetch_market_winner",
            side_effect=lambda slug: None if slug.endswith("600") else "Up",
        ):
            recon = reconstruct_windows(analyzer, rows, limit=10)
        self.assertEqual(len(recon), 1)
        self.assertEqual(recon[0].slug, "btc-updown-5m-300")
        self.assertAlmostEqual(recon[0].gross_pnl, 10.0 - ((10.0 * 0.40) + (8.0 * 0.60)))

    def test_export_reconstructions_csv_writes_expected_columns(self):
        analyzer = WalletAnalyzer("0xabc")
        rows = [
            TradeRow(
                timestamp=310,
                slug="btc-updown-5m-300",
                side="BUY",
                outcome="Up",
                price=0.40,
                size=10.0,
                asset="up",
                condition_id="cond",
                transaction_hash="tx1",
            ),
            TradeRow(
                timestamp=311,
                slug="btc-updown-5m-300",
                side="BUY",
                outcome="Down",
                price=0.60,
                size=8.0,
                asset="down",
                condition_id="cond",
                transaction_hash="tx2",
            ),
        ]
        with patch.object(analyzer, "fetch_market_winner", return_value="Up"):
            recon = reconstruct_windows(analyzer, rows, limit=1)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "recon.csv"
            export_reconstructions_csv(recon, str(path))
            text = path.read_text(encoding="utf-8")
        self.assertIn("combined_cost_ratio", text)
        self.assertIn("winner_overweight", text)

    def test_reconstruct_windows_includes_btc_delta_features(self):
        analyzer = WalletAnalyzer("0xabc")
        rows = [
            TradeRow(
                timestamp=310,
                slug="btc-updown-5m-300",
                side="BUY",
                outcome="Up",
                price=0.40,
                size=10.0,
                asset="up",
                condition_id="cond",
                transaction_hash="tx1",
            ),
            TradeRow(
                timestamp=312,
                slug="btc-updown-5m-300",
                side="BUY",
                outcome="Down",
                price=0.60,
                size=8.0,
                asset="down",
                condition_id="cond",
                transaction_hash="tx2",
            ),
        ]

        class DummyBTCHistory:
            def price_at(self, ts):
                return {300: 100.0, 310: 112.0, 312: 114.0}.get(ts, 100.0)

        with patch.object(analyzer, "fetch_market_winner", return_value="Up"):
            recon = reconstruct_windows(analyzer, rows, limit=1, btc_history=DummyBTCHistory())
        self.assertEqual(len(recon), 1)
        self.assertEqual(recon[0].lean_side, "Up")
        self.assertAlmostEqual(recon[0].btc_open_price, 100.0)
        self.assertAlmostEqual(recon[0].btc_first_buy_price, 112.0)
        self.assertAlmostEqual(recon[0].btc_delta_first_buy, 12.0)
        self.assertEqual(recon[0].lean_btc_alignment, "Up-with-BTC")

    def test_group_reconstructions_supports_cost_by_lean(self):
        analyzer = WalletAnalyzer("0xabc")
        rows = [
            TradeRow(
                timestamp=310,
                slug="btc-updown-5m-300",
                side="BUY",
                outcome="Up",
                price=0.40,
                size=10.0,
                asset="up",
                condition_id="cond",
                transaction_hash="tx1",
            ),
            TradeRow(
                timestamp=312,
                slug="btc-updown-5m-300",
                side="BUY",
                outcome="Down",
                price=0.30,
                size=5.0,
                asset="down",
                condition_id="cond",
                transaction_hash="tx2",
            ),
        ]

        class DummyBTCHistory:
            def price_at(self, ts):
                return {300: 100.0, 310: 110.0}.get(ts, 100.0)

        with patch.object(analyzer, "fetch_market_winner", return_value="Up"):
            recon = reconstruct_windows(analyzer, rows, limit=1, btc_history=DummyBTCHistory())
        groups = _group_reconstructions(recon, "cost_x_lean")
        self.assertIn("<0.80 | Up", groups)


class PairedResearchSimulationTests(unittest.TestCase):
    def test_cost_gated_up_bias_accepts_projected_ratio_in_band(self):
        pos = type("P", (), {"up_shares": 0.0, "down_shares": 0.0, "up_spend": 0.0, "down_spend": 0.0, "combined_spend": 0.0})()
        tick = ResearchTick(
            timestamp=10.0,
            seconds_remaining=290.0,
            up_bid=0.39,
            up_ask=0.40,
            down_bid=0.59,
            down_ask=0.60,
            btc_spot=100.0,
            btc_delta=0.0,
        )
        spends = policy_cost_gated_up_bias(
            pos,
            tick,
            {"min_cost_ratio": 0.70, "max_cost_ratio": 0.89, "target_cost_ratio": 0.80, "up_notional": 30.0},
        )
        self.assertGreater(spends["up"], 0.0)
        self.assertGreater(spends["down"], 0.0)

    def test_cost_gated_up_bias_rejects_projected_ratio_out_of_band(self):
        pos = type("P", (), {"up_shares": 0.0, "down_shares": 0.0, "up_spend": 0.0, "down_spend": 0.0, "combined_spend": 0.0})()
        tick = ResearchTick(
            timestamp=10.0,
            seconds_remaining=290.0,
            up_bid=0.39,
            up_ask=0.40,
            down_bid=0.59,
            down_ask=0.60,
            btc_spot=100.0,
            btc_delta=0.0,
        )
        spends = policy_cost_gated_up_bias(
            pos,
            tick,
            {
                "min_cost_ratio": 0.70,
                "max_cost_ratio": 0.75,
                "target_cost_ratio": 0.72,
                "up_notional": 30.0,
                "min_down_notional": 2.0,
                "max_down_notional": 8.0,
            },
        )
        self.assertEqual(spends, {"up": 0.0, "down": 0.0})

    def test_cost_gated_up_bias_flat_only_skips_non_flat_btc(self):
        pos = type("P", (), {"up_shares": 0.0, "down_shares": 0.0, "up_spend": 0.0, "down_spend": 0.0, "combined_spend": 0.0})()
        tick = ResearchTick(
            timestamp=10.0,
            seconds_remaining=290.0,
            up_bid=0.39,
            up_ask=0.40,
            down_bid=0.59,
            down_ask=0.60,
            btc_spot=120.0,
            btc_delta=12.0,
        )
        spends = policy_cost_gated_up_bias_flat_only(pos, tick, {"flat_btc_abs": 5.0})
        self.assertEqual(spends, {"up": 0.0, "down": 0.0})

    def test_cost_gated_up_bias_momentum_uses_up_bias_in_positive_btc(self):
        pos = type("P", (), {"up_shares": 0.0, "down_shares": 0.0, "up_spend": 0.0, "down_spend": 0.0, "combined_spend": 0.0})()
        tick = ResearchTick(
            timestamp=10.0,
            seconds_remaining=290.0,
            up_bid=0.49,
            up_ask=0.50,
            down_bid=0.49,
            down_ask=0.50,
            btc_spot=125.0,
            btc_delta=25.0,
        )
        spends = policy_cost_gated_up_bias_momentum(
            pos,
            tick,
            {
                "min_cost_ratio": 0.70,
                "max_cost_ratio": 0.95,
                "target_cost_ratio": 0.80,
                "positive_btc_threshold": 20.0,
                "momentum_up_notional": 36.0,
            },
        )
        self.assertEqual(spends["up"], 36.0)
        self.assertGreater(spends["down"], 0.0)

    def test_equal_size_pair_computes_expected_pnl(self):
        ctx = MarketContext(
            slug="btc-updown-5m-1",
            winner="Up",
            up_token_id="up",
            down_token_id="down",
            btc_open=100.0,
            ticks=[
                ResearchTick(
                    timestamp=10.0,
                    seconds_remaining=290.0,
                    up_bid=0.39,
                    up_ask=0.40,
                    down_bid=0.59,
                    down_ask=0.60,
                    btc_spot=100.0,
                    btc_delta=0.0,
                )
            ],
        )
        result = simulate_market(ctx, "equal_time", {"notional_each": 10.0, "step_secs": 0.0})
        self.assertAlmostEqual(result["combined_spend"], 20.0)
        self.assertAlmostEqual(result["up_shares"], 25.0)
        self.assertAlmostEqual(result["down_shares"], 16.666667, places=5)
        self.assertAlmostEqual(result["gross_pnl"], 5.0)
        self.assertAlmostEqual(result["adverse_pnl"], -3.333333, places=5)

    def test_uneven_size_pair_uses_conviction_side(self):
        ctx = MarketContext(
            slug="btc-updown-5m-2",
            winner="Up",
            up_token_id="up",
            down_token_id="down",
            btc_open=100.0,
            ticks=[
                ResearchTick(
                    timestamp=30.0,
                    seconds_remaining=270.0,
                    up_bid=0.49,
                    up_ask=0.50,
                    down_bid=0.49,
                    down_ask=0.50,
                    btc_spot=112.0,
                    btc_delta=12.0,
                )
            ],
        )
        result = simulate_market(
            ctx,
            "hedge_plus_conviction",
            {
                "conviction_notional": 30.0,
                "hedge_notional": 5.0,
                "btc_delta_threshold": 8.0,
                "step_secs": 0.0,
            },
        )
        self.assertAlmostEqual(result["up_spend"], 30.0)
        self.assertAlmostEqual(result["down_spend"], 5.0)
        self.assertGreater(result["up_shares"], result["down_shares"])
        self.assertGreater(result["gross_pnl"], 0.0)

    def test_missing_side_is_left_as_one_sided_exposure(self):
        ctx = MarketContext(
            slug="btc-updown-5m-3",
            winner="Up",
            up_token_id="up",
            down_token_id="down",
            btc_open=100.0,
            ticks=[
                ResearchTick(
                    timestamp=40.0,
                    seconds_remaining=260.0,
                    up_bid=0.49,
                    up_ask=0.50,
                    down_bid=0.0,
                    down_ask=0.50,
                    btc_spot=110.0,
                    btc_delta=10.0,
                )
            ],
        )
        result = simulate_market(
            ctx,
            "hedge_plus_conviction",
            {
                "conviction_notional": 20.0,
                "hedge_notional": 0.0,
                "btc_delta_threshold": 8.0,
                "step_secs": 0.0,
            },
        )
        self.assertAlmostEqual(result["down_spend"], 0.0)
        self.assertAlmostEqual(result["down_shares"], 0.0)
        self.assertGreater(result["up_shares"], 0.0)

    def test_fee_and_slippage_are_applied_deterministically(self):
        ctx = MarketContext(
            slug="btc-updown-5m-4",
            winner="Up",
            up_token_id="up",
            down_token_id="down",
            btc_open=100.0,
            ticks=[
                ResearchTick(
                    timestamp=10.0,
                    seconds_remaining=290.0,
                    up_bid=0.39,
                    up_ask=0.40,
                    down_bid=0.59,
                    down_ask=0.60,
                    btc_spot=100.0,
                    btc_delta=0.0,
                )
            ],
        )
        baseline = simulate_market(ctx, "equal_time", {"notional_each": 10.0, "step_secs": 0.0})
        stressed = simulate_market(
            ctx,
            "equal_time",
            {
                "notional_each": 10.0,
                "step_secs": 0.0,
                "fee_bps": 100.0,
                "slippage_bps": 100.0,
            },
        )
        self.assertLess(stressed["up_shares"], baseline["up_shares"])
        self.assertLess(stressed["down_shares"], baseline["down_shares"])
        self.assertLess(stressed["net_pnl"], baseline["gross_pnl"])


class ResearchDatabaseTests(unittest.TestCase):
    def test_upsert_market_resolution_update_preserves_token_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = ResearchDatabase(str(Path(tmpdir) / "research.db"))
            try:
                db.upsert_market(
                    {
                        "slug": "btc-updown-5m-1",
                        "window_start_ts": 0,
                        "window_end_ts": 300,
                        "market_id": "m1",
                        "condition_id": "c1",
                        "up_token_id": "up-token",
                        "down_token_id": "down-token",
                        "btc_open_price": 100.0,
                        "btc_close_price": None,
                        "resolution": None,
                    }
                )
                db.upsert_market(
                    {
                        "slug": "btc-updown-5m-1",
                        "window_start_ts": None,
                        "window_end_ts": 300,
                        "market_id": "",
                        "condition_id": "",
                        "up_token_id": "",
                        "down_token_id": "",
                        "btc_open_price": None,
                        "btc_close_price": 105.0,
                        "resolution": "Up",
                    }
                )
                row = db.conn.execute(
                    "SELECT up_token_id, down_token_id, btc_close_price, resolution FROM research_markets WHERE slug=?",
                    ("btc-updown-5m-1",),
                ).fetchone()
                self.assertEqual(row["up_token_id"], "up-token")
                self.assertEqual(row["down_token_id"], "down-token")
                self.assertAlmostEqual(row["btc_close_price"], 105.0)
                self.assertEqual(row["resolution"], "Up")
            finally:
                db.close()

    def test_delete_sim_results_for_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = ResearchDatabase(str(Path(tmpdir) / "research.db"))
            try:
                db.insert_sim_result(
                    {
                        "slug": "a",
                        "policy": "equal_time",
                        "up_spend": 1.0,
                        "down_spend": 1.0,
                        "up_shares": 2.0,
                        "down_shares": 2.0,
                        "combined_spend": 2.0,
                        "payout_if_up": 2.0,
                        "payout_if_down": 2.0,
                        "winner": "Up",
                        "gross_pnl": 0.0,
                        "roi": 0.0,
                        "adverse_pnl": 0.0,
                        "net_pnl": 0.0,
                        "first_buy_s": 10,
                        "last_buy_s": 10,
                        "meta_json": "{}",
                    }
                )
                db.insert_sim_result(
                    {
                        "slug": "b",
                        "policy": "payout_balanced",
                        "up_spend": 1.0,
                        "down_spend": 1.0,
                        "up_shares": 2.0,
                        "down_shares": 2.0,
                        "combined_spend": 2.0,
                        "payout_if_up": 2.0,
                        "payout_if_down": 2.0,
                        "winner": "Down",
                        "gross_pnl": 0.0,
                        "roi": 0.0,
                        "adverse_pnl": 0.0,
                        "net_pnl": 0.0,
                        "first_buy_s": 10,
                        "last_buy_s": 10,
                        "meta_json": "{}",
                    }
                )
                db.delete_sim_results("equal_time")
                remaining = db.conn.execute("SELECT policy FROM research_sim_results").fetchall()
                self.assertEqual([row["policy"] for row in remaining], ["payout_balanced"])
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
