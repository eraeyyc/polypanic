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
    _extract_early_momentum_observations,
    assess_market_quality,
    policy_cost_gated_up_bias,
    policy_cost_gated_up_bias_late_guard,
    policy_cost_gated_up_bias_flat_only,
    policy_cost_gated_up_bias_momentum,
    policy_favorite_cost_capped_share_clips,
    policy_market_favorite_share_clips,
    policy_strength_follow_share_clips,
    run_simulation,
    simulate_market,
)
from wallet_analyzer import (
    SnapshotEquity,
    TradeRow,
    WalletAnalyzer,
    _group_reconstructions,
    _estimate_price_sum,
    _classify_burst_effect,
    _lean_alignment,
    build_burst_evolution,
    cluster_trade_bursts,
    evaluate_formula_fits,
    export_reconstructions_csv,
    reconstruct_windows,
    summarize_formula_fits,
    summarize_burst_evolution,
    summarize_rows,
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
    def test_classify_burst_effect_categories(self):
        self.assertEqual(_classify_burst_effect(10.0, -5.0, -0.1), "favor_up")
        self.assertEqual(_classify_burst_effect(-5.0, 10.0, -0.1), "favor_down")
        self.assertEqual(_classify_burst_effect(5.0, 2.0, -0.1), "repair")
        self.assertEqual(_classify_burst_effect(-5.0, -2.0, 0.1), "damage")

    def test_build_burst_evolution_tracks_cumulative_payoff_geometry(self):
        rows = [
            TradeRow(
                timestamp=100,
                slug="btc-updown-5m-0",
                side="BUY",
                outcome="Up",
                price=0.50,
                size=10.0,
                asset="up",
                condition_id="cond",
                transaction_hash="tx1",
            ),
            TradeRow(
                timestamp=102,
                slug="btc-updown-5m-0",
                side="BUY",
                outcome="Up",
                price=0.50,
                size=10.0,
                asset="up",
                condition_id="cond",
                transaction_hash="tx2",
            ),
            TradeRow(
                timestamp=110,
                slug="btc-updown-5m-0",
                side="BUY",
                outcome="Down",
                price=0.25,
                size=20.0,
                asset="down",
                condition_id="cond",
                transaction_hash="tx3",
            ),
        ]
        steps = build_burst_evolution(rows, "btc-updown-5m-0", gap_secs=5)
        self.assertEqual(len(steps), 2)
        self.assertAlmostEqual(steps[0].cumulative_spend, 10.0)
        self.assertAlmostEqual(steps[0].pnl_if_up, 10.0)
        self.assertEqual(steps[0].effect, "favor_up")
        self.assertEqual(steps[0].favored_side, "Up")
        self.assertAlmostEqual(steps[1].cumulative_spend, 15.0)
        self.assertAlmostEqual(steps[1].pnl_if_up, 5.0)
        self.assertAlmostEqual(steps[1].pnl_if_down, 5.0)
        self.assertEqual(steps[1].effect, "favor_down")
        self.assertEqual(steps[1].favored_side, "Flat")

    def test_summarize_burst_evolution_includes_realized_column(self):
        rows = [
            TradeRow(
                timestamp=100,
                slug="btc-updown-5m-0",
                side="BUY",
                outcome="Up",
                price=0.50,
                size=10.0,
                asset="up",
                condition_id="cond",
                transaction_hash="tx1",
            ),
            TradeRow(
                timestamp=110,
                slug="btc-updown-5m-0",
                side="BUY",
                outcome="Down",
                price=0.25,
                size=20.0,
                asset="down",
                condition_id="cond",
                transaction_hash="tx2",
            ),
        ]
        text = summarize_burst_evolution(rows, "btc-updown-5m-0", winner="Down", gap_secs=5)
        self.assertIn("winner=Down", text)
        self.assertIn("realized=$", text)
        self.assertIn("Burst effects:", text)
        self.assertIn("favor_up", text)

    def test_cluster_trade_bursts_merges_nearby_same_side_fills(self):
        rows = [
            TradeRow(
                timestamp=100,
                slug="btc-updown-5m-0",
                side="BUY",
                outcome="Up",
                price=0.50,
                size=10.0,
                asset="up",
                condition_id="cond",
                transaction_hash="tx1",
            ),
            TradeRow(
                timestamp=103,
                slug="btc-updown-5m-0",
                side="BUY",
                outcome="Up",
                price=0.52,
                size=20.0,
                asset="up",
                condition_id="cond",
                transaction_hash="tx2",
            ),
            TradeRow(
                timestamp=110,
                slug="btc-updown-5m-0",
                side="BUY",
                outcome="Down",
                price=0.48,
                size=15.0,
                asset="down",
                condition_id="cond",
                transaction_hash="tx3",
            ),
        ]
        bursts = cluster_trade_bursts(rows, gap_secs=5)
        self.assertEqual(len(bursts), 2)
        self.assertEqual(bursts[0].outcome, "Up")
        self.assertEqual(bursts[0].fills, 2)
        self.assertAlmostEqual(bursts[0].total_size, 30.0)

    def test_summarize_rows_reports_one_buy_burst_per_side(self):
        rows = [
            TradeRow(
                timestamp=100,
                slug="btc-updown-5m-0",
                side="BUY",
                outcome="Up",
                price=0.50,
                size=10.0,
                asset="up",
                condition_id="cond",
                transaction_hash="tx1",
            ),
            TradeRow(
                timestamp=102,
                slug="btc-updown-5m-0",
                side="BUY",
                outcome="Up",
                price=0.51,
                size=8.0,
                asset="up",
                condition_id="cond",
                transaction_hash="tx2",
            ),
            TradeRow(
                timestamp=105,
                slug="btc-updown-5m-0",
                side="BUY",
                outcome="Down",
                price=0.49,
                size=12.0,
                asset="down",
                condition_id="cond",
                transaction_hash="tx3",
            ),
            TradeRow(
                timestamp=107,
                slug="btc-updown-5m-0",
                side="BUY",
                outcome="Down",
                price=0.48,
                size=14.0,
                asset="down",
                condition_id="cond",
                transaction_hash="tx4",
            ),
        ]
        text = summarize_rows("test", rows, burst_gap_secs=5)
        self.assertIn("Windows with exactly one BUY burst per side: 1/1", text)

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
        self.assertEqual(recon[0].first_burst_outcome, "Up")
        self.assertAlmostEqual(recon[0].btc_open_price, 100.0)
        self.assertAlmostEqual(recon[0].btc_first_buy_price, 112.0)
        self.assertAlmostEqual(recon[0].btc_delta_first_buy, 12.0)
        self.assertEqual(recon[0].lean_btc_alignment, "Up-with-BTC")
        self.assertEqual(recon[0].initial_favored_side, "Up")
        self.assertEqual(recon[0].final_favored_side, "Up")
        self.assertRegex(recon[0].local_hour_bucket, r"^\d{2}:00$")
        self.assertEqual(recon[0].streak_alignment, "no-prev")

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

    def test_reconstruct_windows_assigns_previous_winner_streak_and_alignment(self):
        analyzer = WalletAnalyzer("0xabc")
        rows = [
            TradeRow(timestamp=300, slug="btc-updown-5m-300", side="BUY", outcome="Up", price=0.40, size=10.0, asset="u1", condition_id="cond1", transaction_hash="tx1"),
            TradeRow(timestamp=301, slug="btc-updown-5m-300", side="BUY", outcome="Down", price=0.20, size=5.0, asset="d1", condition_id="cond1", transaction_hash="tx2"),
            TradeRow(timestamp=600, slug="btc-updown-5m-600", side="BUY", outcome="Down", price=0.20, size=10.0, asset="d2", condition_id="cond2", transaction_hash="tx3"),
            TradeRow(timestamp=601, slug="btc-updown-5m-600", side="BUY", outcome="Up", price=0.30, size=4.0, asset="u2", condition_id="cond2", transaction_hash="tx4"),
            TradeRow(timestamp=900, slug="btc-updown-5m-900", side="BUY", outcome="Up", price=0.45, size=10.0, asset="u3", condition_id="cond3", transaction_hash="tx5"),
            TradeRow(timestamp=901, slug="btc-updown-5m-900", side="BUY", outcome="Down", price=0.10, size=4.0, asset="d3", condition_id="cond3", transaction_hash="tx6"),
        ]

        with patch.object(
            analyzer,
            "fetch_market_winner",
            side_effect=lambda slug: {
                "btc-updown-5m-300": "Up",
                "btc-updown-5m-600": "Up",
                "btc-updown-5m-900": "Down",
            }[slug],
        ):
            recon = reconstruct_windows(analyzer, rows, limit=3)
        by_slug = {row.slug: row for row in recon}
        self.assertEqual(by_slug["btc-updown-5m-300"].prev_winner_streak, 0)
        self.assertEqual(by_slug["btc-updown-5m-600"].prev_winner, "Up")
        self.assertEqual(by_slug["btc-updown-5m-600"].prev_winner_streak, 1)
        self.assertEqual(by_slug["btc-updown-5m-600"].streak_alignment, "against-prev-streak")
        self.assertEqual(by_slug["btc-updown-5m-900"].prev_winner, "Up")
        self.assertEqual(by_slug["btc-updown-5m-900"].prev_winner_streak, 2)
        self.assertEqual(by_slug["btc-updown-5m-900"].streak_alignment, "with-prev-streak")

    def test_group_reconstructions_supports_streak_and_time_modes(self):
        analyzer = WalletAnalyzer("0xabc")
        rows = [
            TradeRow(timestamp=300, slug="btc-updown-5m-300", side="BUY", outcome="Up", price=0.40, size=10.0, asset="u1", condition_id="cond1", transaction_hash="tx1"),
            TradeRow(timestamp=301, slug="btc-updown-5m-300", side="BUY", outcome="Down", price=0.20, size=5.0, asset="d1", condition_id="cond1", transaction_hash="tx2"),
            TradeRow(timestamp=600, slug="btc-updown-5m-600", side="BUY", outcome="Down", price=0.20, size=10.0, asset="d2", condition_id="cond2", transaction_hash="tx3"),
            TradeRow(timestamp=601, slug="btc-updown-5m-600", side="BUY", outcome="Up", price=0.30, size=4.0, asset="u2", condition_id="cond2", transaction_hash="tx4"),
        ]
        with patch.object(analyzer, "fetch_market_winner", side_effect=["Up", "Down"]):
            recon = reconstruct_windows(analyzer, rows, limit=2)
        self.assertIn("prev-1", _group_reconstructions(recon, "prev_streak"))
        time_groups = _group_reconstructions(recon, "time_of_day")
        self.assertEqual(sum(len(bucket) for bucket in time_groups.values()), 2)
        self.assertTrue(all(key == "unknown" or key.endswith(":00") for key in time_groups))
        cost_streak = _group_reconstructions(recon, "cost_x_streak")
        self.assertEqual(sum(len(bucket) for bucket in cost_streak.values()), 2)
        self.assertTrue(all(" | " in key for key in cost_streak))
        cost_time = _group_reconstructions(recon, "cost_x_time")
        self.assertEqual(sum(len(bucket) for bucket in cost_time.values()), 2)
        self.assertTrue(all(" | " in key for key in cost_time))
        cost_flip = _group_reconstructions(recon, "cost_x_flip")
        self.assertEqual(sum(len(bucket) for bucket in cost_flip.values()), 2)
        self.assertTrue(all(" | " in key for key in cost_flip))

    def test_formula_fit_summary_reports_candidate_results(self):
        rows = [
            TradeRow(
                timestamp=100,
                slug="btc-updown-5m-0",
                side="BUY",
                outcome="Up",
                price=0.20,
                size=100.0,
                asset="up",
                condition_id="cond",
                transaction_hash="tx1",
            ),
            TradeRow(
                timestamp=120,
                slug="btc-updown-5m-0",
                side="BUY",
                outcome="Up",
                price=0.30,
                size=50.0,
                asset="up",
                condition_id="cond",
                transaction_hash="tx2",
            ),
            TradeRow(
                timestamp=300,
                slug="btc-updown-5m-300",
                side="BUY",
                outcome="Down",
                price=0.20,
                size=100.0,
                asset="down",
                condition_id="cond2",
                transaction_hash="tx3",
            ),
        ]
        results = evaluate_formula_fits(rows, limit_windows=2, gap_secs=5)
        self.assertTrue(results)
        self.assertTrue(all(result.choices > 0 for result in results))
        text = summarize_formula_fits(results)
        self.assertIn("linear_bounded", text)
        self.assertIn("quadratic_cost", text)


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

    def test_cost_gated_up_bias_late_guard_skips_down_dominant_late_window(self):
        pos = type("P", (), {"up_shares": 0.0, "down_shares": 0.0, "up_spend": 0.0, "down_spend": 0.0, "combined_spend": 0.0})()
        tick = ResearchTick(
            timestamp=250.0,
            seconds_remaining=40.0,
            up_bid=0.30,
            up_ask=0.32,
            down_bid=0.67,
            down_ask=0.68,
            btc_spot=90.0,
            btc_delta=-10.0,
        )
        spends = policy_cost_gated_up_bias_late_guard(
            pos,
            tick,
            {
                "late_guard_start_secs": 180.0,
                "late_guard_up_ask_max": 0.35,
                "late_guard_down_ask_min": 0.60,
            },
        )
        self.assertEqual(spends, {"up": 0.0, "down": 0.0})

    def test_cost_gated_up_bias_late_guard_keeps_early_window_active(self):
        pos = type("P", (), {"up_shares": 0.0, "down_shares": 0.0, "up_spend": 0.0, "down_spend": 0.0, "combined_spend": 0.0})()
        tick = ResearchTick(
            timestamp=60.0,
            seconds_remaining=240.0,
            up_bid=0.39,
            up_ask=0.40,
            down_bid=0.59,
            down_ask=0.60,
            btc_spot=90.0,
            btc_delta=-10.0,
        )
        spends = policy_cost_gated_up_bias_late_guard(
            pos,
            tick,
            {
                "min_cost_ratio": 0.70,
                "max_cost_ratio": 0.89,
                "target_cost_ratio": 0.80,
                "up_notional": 30.0,
                "late_guard_start_secs": 180.0,
                "late_guard_up_ask_max": 0.35,
                "late_guard_down_ask_min": 0.60,
            },
        )
        self.assertGreater(spends["up"], 0.0)
        self.assertGreater(spends["down"], 0.0)

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

    def test_strength_follow_share_clips_waits_for_clear_strength_signal(self):
        pos = type("P", (), {"up_shares": 0.0, "down_shares": 0.0, "up_spend": 0.0, "down_spend": 0.0, "combined_spend": 0.0})()
        tick = ResearchTick(
            timestamp=10.0,
            seconds_remaining=290.0,
            up_bid=0.49,
            up_ask=0.52,
            down_bid=0.47,
            down_ask=0.48,
            btc_spot=100.0,
            btc_delta=0.0,
        )
        spends = policy_strength_follow_share_clips(
            pos,
            tick,
            {"signal_start_secs": 15.0, "strength_gap": 0.08},
        )
        self.assertEqual(spends, {"up": 0.0, "down": 0.0})

    def test_strength_follow_share_clips_presses_stronger_side_in_fixed_share_clips(self):
        pos = type("P", (), {"up_shares": 0.0, "down_shares": 0.0, "up_spend": 0.0, "down_spend": 0.0, "combined_spend": 0.0})()
        tick = ResearchTick(
            timestamp=40.0,
            seconds_remaining=250.0,
            up_bid=0.58,
            up_ask=0.60,
            down_bid=0.34,
            down_ask=0.38,
            btc_spot=100.0,
            btc_delta=0.0,
        )
        spends = policy_strength_follow_share_clips(
            pos,
            tick,
            {
                "signal_start_secs": 15.0,
                "strength_gap": 0.08,
                "dominant_clip_shares": 58.0,
                "hedge_clip_shares": 20.0,
                "max_projected_cost_ratio": 0.98,
            },
        )
        self.assertAlmostEqual(spends["up"], 34.8)
        self.assertAlmostEqual(spends["down"], 7.6)

    def test_strength_follow_share_clips_blocks_late_reversal(self):
        pos = type("P", (), {"up_shares": 120.0, "down_shares": 60.0, "up_spend": 70.0, "down_spend": 24.0, "combined_spend": 94.0})()
        tick = ResearchTick(
            timestamp=250.0,
            seconds_remaining=35.0,
            up_bid=0.20,
            up_ask=0.22,
            down_bid=0.76,
            down_ask=0.80,
            btc_spot=95.0,
            btc_delta=-5.0,
        )
        spends = policy_strength_follow_share_clips(
            pos,
            tick,
            {"late_reversal_start_secs": 180.0, "late_reversal_gap": 0.18},
        )
        self.assertEqual(spends, {"up": 0.0, "down": 0.0})

    def test_market_favorite_share_clips_chooses_higher_priced_side(self):
        pos = type("P", (), {"up_shares": 0.0, "down_shares": 0.0, "up_spend": 0.0, "down_spend": 0.0, "combined_spend": 0.0})()
        tick = ResearchTick(
            timestamp=40.0,
            seconds_remaining=250.0,
            up_bid=0.78,
            up_ask=0.82,
            down_bid=0.17,
            down_ask=0.19,
            btc_spot=100.0,
            btc_delta=0.0,
        )
        spends = policy_market_favorite_share_clips(
            pos,
            tick,
            {
                "favorite_start_secs": 15.0,
                "favorite_gap": 0.05,
                "dominant_clip_shares": 58.0,
                "hedge_clip_shares": 20.0,
                "max_projected_cost_ratio": 0.98,
            },
        )
        self.assertAlmostEqual(spends["up"], 47.56)
        self.assertAlmostEqual(spends["down"], 3.8)

    def test_market_favorite_share_clips_waits_for_clear_gap(self):
        pos = type("P", (), {"up_shares": 0.0, "down_shares": 0.0, "up_spend": 0.0, "down_spend": 0.0, "combined_spend": 0.0})()
        tick = ResearchTick(
            timestamp=40.0,
            seconds_remaining=250.0,
            up_bid=0.50,
            up_ask=0.52,
            down_bid=0.45,
            down_ask=0.48,
            btc_spot=100.0,
            btc_delta=0.0,
        )
        spends = policy_market_favorite_share_clips(
            pos,
            tick,
            {"favorite_start_secs": 15.0, "favorite_gap": 0.05},
        )
        self.assertEqual(spends, {"up": 0.0, "down": 0.0})

    def test_favorite_cost_capped_share_clips_uses_higher_side_in_spread_band(self):
        pos = type("P", (), {"up_shares": 0.0, "down_shares": 0.0, "up_spend": 0.0, "down_spend": 0.0, "combined_spend": 0.0})()
        tick = ResearchTick(
            timestamp=40.0,
            seconds_remaining=250.0,
            up_bid=0.58,
            up_ask=0.62,
            down_bid=0.33,
            down_ask=0.38,
            btc_spot=100.0,
            btc_delta=0.0,
        )
        spends = policy_favorite_cost_capped_share_clips(
            pos,
            tick,
            {
                "entry_start_secs": 15.0,
                "min_spread_gap": 0.05,
                "max_spread_gap": 0.50,
                "dominant_clip_shares": 58.0,
                "hedge_clip_shares": 20.0,
                "max_projected_cost_ratio": 0.89,
            },
        )
        self.assertAlmostEqual(spends["up"], 35.96)
        self.assertAlmostEqual(spends["down"], 7.6)

    def test_favorite_cost_capped_share_clips_skips_extreme_spread_and_bad_ratio(self):
        pos = type("P", (), {"up_shares": 0.0, "down_shares": 0.0, "up_spend": 0.0, "down_spend": 0.0, "combined_spend": 0.0})()
        tick = ResearchTick(
            timestamp=40.0,
            seconds_remaining=250.0,
            up_bid=0.80,
            up_ask=0.84,
            down_bid=0.12,
            down_ask=0.16,
            btc_spot=100.0,
            btc_delta=0.0,
        )
        spends = policy_favorite_cost_capped_share_clips(
            pos,
            tick,
            {
                "entry_start_secs": 15.0,
                "min_spread_gap": 0.05,
                "max_spread_gap": 0.50,
                "dominant_clip_shares": 58.0,
                "hedge_clip_shares": 20.0,
                "max_projected_cost_ratio": 0.89,
            },
        )
        self.assertEqual(spends, {"up": 0.0, "down": 0.0})

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

    def test_assess_market_quality_rejects_sparse_and_broken_quotes(self):
        sparse = MarketContext(
            slug="btc-updown-5m-sparse",
            winner="Up",
            up_token_id="up",
            down_token_id="down",
            btc_open=100.0,
            ticks=[
                ResearchTick(
                    timestamp=10.0 + i,
                    seconds_remaining=290.0 - i,
                    up_bid=0.49,
                    up_ask=0.50,
                    down_bid=0.49,
                    down_ask=0.50,
                    btc_spot=100.0,
                    btc_delta=0.0,
                )
                for i in range(3)
            ],
        )
        broken = MarketContext(
            slug="btc-updown-5m-broken",
            winner="Up",
            up_token_id="up",
            down_token_id="down",
            btc_open=100.0,
            ticks=[
                ResearchTick(
                    timestamp=20.0 + i,
                    seconds_remaining=280.0 - i,
                    up_bid=0.0,
                    up_ask=0.01,
                    down_bid=0.0,
                    down_ask=0.01,
                    btc_spot=100.0,
                    btc_delta=0.0,
                )
                for i in range(5)
            ],
        )
        self.assertEqual(assess_market_quality(sparse, {"min_ticks": 5}).reason, "low_ticks")
        self.assertEqual(assess_market_quality(broken, {"min_ticks": 5}).reason, "ask_sum_too_low")

    def test_extract_early_momentum_observations_uses_nearest_ticks(self):
        ctx = MarketContext(
            slug="btc-updown-5m-signal",
            winner="Up",
            up_token_id="up",
            down_token_id="down",
            btc_open=100.0,
            ticks=[
                ResearchTick(
                    timestamp=1.0,
                    seconds_remaining=296.0,
                    up_bid=0.49,
                    up_ask=0.50,
                    down_bid=0.49,
                    down_ask=0.50,
                    btc_spot=101.0,
                    btc_delta=1.0,
                ),
                ResearchTick(
                    timestamp=2.0,
                    seconds_remaining=291.0,
                    up_bid=0.49,
                    up_ask=0.50,
                    down_bid=0.49,
                    down_ask=0.50,
                    btc_spot=104.0,
                    btc_delta=4.0,
                ),
                ResearchTick(
                    timestamp=3.0,
                    seconds_remaining=286.0,
                    up_bid=0.49,
                    up_ask=0.50,
                    down_bid=0.49,
                    down_ask=0.50,
                    btc_spot=98.0,
                    btc_delta=-2.0,
                ),
            ],
        )
        observations = _extract_early_momentum_observations(
            ctx,
            checkpoints=[5, 10, 15],
            tolerance_secs=2.0,
        )
        self.assertEqual([o.checkpoint_s for o in observations], [5, 10, 15])
        self.assertAlmostEqual(observations[0].btc_delta, 1.0)
        self.assertAlmostEqual(observations[1].btc_delta, 4.0)
        self.assertAlmostEqual(observations[2].btc_delta, -2.0)


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

    def test_run_simulation_skips_low_quality_windows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = ResearchDatabase(str(Path(tmpdir) / "research.db"))
            try:
                db.upsert_market(
                    {
                        "slug": "valid",
                        "window_start_ts": 0,
                        "window_end_ts": 300,
                        "market_id": "m1",
                        "condition_id": "c1",
                        "up_token_id": "up",
                        "down_token_id": "down",
                        "btc_open_price": 100.0,
                        "btc_close_price": 101.0,
                        "resolution": "Up",
                    }
                )
                for i in range(5):
                    db.insert_tick(
                        "valid",
                        ResearchTick(
                            timestamp=float(i),
                            seconds_remaining=300.0 - i,
                            up_bid=0.49,
                            up_ask=0.50,
                            down_bid=0.49,
                            down_ask=0.50,
                            btc_spot=100.0,
                            btc_delta=0.0,
                        ),
                    )

                db.upsert_market(
                    {
                        "slug": "broken",
                        "window_start_ts": 300,
                        "window_end_ts": 600,
                        "market_id": "m2",
                        "condition_id": "c2",
                        "up_token_id": "up",
                        "down_token_id": "down",
                        "btc_open_price": 100.0,
                        "btc_close_price": 99.0,
                        "resolution": "Down",
                    }
                )
                for i in range(5):
                    db.insert_tick(
                        "broken",
                        ResearchTick(
                            timestamp=300.0 + i,
                            seconds_remaining=300.0 - i,
                            up_bid=0.0,
                            up_ask=0.01,
                            down_bid=0.0,
                            down_ask=0.01,
                            btc_spot=100.0,
                            btc_delta=0.0,
                        ),
                    )

                summary = run_simulation(
                    db,
                    "equal_time",
                    {
                        "notional_each": 10.0,
                        "step_secs": 0.0,
                        "min_ticks": 5,
                        "min_ask_sum": 0.80,
                        "max_ask_sum": 1.20,
                    },
                )
                self.assertEqual(summary["n"], 1)
                self.assertEqual(summary["skipped"], 1)
                rows = db.conn.execute("SELECT slug FROM research_sim_results").fetchall()
                self.assertEqual([row["slug"] for row in rows], ["valid"])
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
