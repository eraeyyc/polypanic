"""
Comprehensive edge-case and property-based tests for paired_research.py.

Covers: SimPosition properties, _mark_buy, _apply_slippage, _projected_cost_ratio,
_within_ratio_band, _winner_for_delta, _parse_event_ts, all policy zero-ask guards,
policy threshold boundaries, simulate_market filtering/step logic, backtest harness,
and Hypothesis property tests that hold over arbitrary valid inputs.
"""

import tempfile
import unittest
from pathlib import Path

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from paired_research import (
    POLICIES,
    MarketContext,
    policy_cost_gated_up_bias_time_filtered,
    MarketQualityCheck,
    ResearchDatabase,
    ResearchTick,
    SimPosition,
    _apply_slippage,
    _effective_quote_ts,
    _mark_buy,
    _parse_event_ts,
    _pick_up_bias_spends,
    _projected_cost_ratio,
    _winner_for_delta,
    _within_ratio_band,
    assess_market_quality,
    policy_combined_cost_threshold,
    policy_cost_gated_up_bias,
    policy_cost_gated_up_bias_flat_only,
    policy_cost_gated_up_bias_late_guard,
    policy_cost_gated_up_bias_momentum,
    policy_equal_time,
    policy_favorite_cost_capped_share_clips,
    policy_hedge_conviction,
    policy_market_favorite_share_clips,
    policy_pair_under_40_complete_by_45,
    policy_payout_balanced,
    policy_strength_follow_share_clips,
    policy_winner_lean_btc,
    run_simulation,
    simulate_market,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_pos():
    return SimPosition()


def _pos(up_shares=0.0, down_shares=0.0, up_spend=0.0, down_spend=0.0):
    p = SimPosition()
    p.up_shares = up_shares
    p.down_shares = down_shares
    p.up_spend = up_spend
    p.down_spend = down_spend
    return p


def _tick(
    timestamp=10.0,
    seconds_remaining=290.0,
    up_bid=0.49,
    up_ask=0.50,
    down_bid=0.49,
    down_ask=0.50,
    btc_spot=100.0,
    btc_delta=0.0,
    btc_source_ts=0.0,
    up_quote_ts=0.0,
    down_quote_ts=0.0,
):
    return ResearchTick(
        timestamp=timestamp,
        seconds_remaining=seconds_remaining,
        up_bid=up_bid,
        up_ask=up_ask,
        down_bid=down_bid,
        down_ask=down_ask,
        btc_spot=btc_spot,
        btc_delta=btc_delta,
        btc_source_ts=btc_source_ts,
        up_quote_ts=up_quote_ts,
        down_quote_ts=down_quote_ts,
    )


def _ctx(ticks, winner="Up", slug="btc-updown-5m-0", btc_open=100.0):
    return MarketContext(
        slug=slug,
        winner=winner,
        up_token_id="up",
        down_token_id="down",
        btc_open=btc_open,
        ticks=ticks,
    )


# ---------------------------------------------------------------------------
# SimPosition properties
# ---------------------------------------------------------------------------

class SimPositionTests(unittest.TestCase):
    def test_combined_spend_sums_both_sides(self):
        p = _pos(up_spend=10.0, down_spend=5.0)
        self.assertAlmostEqual(p.combined_spend, 15.0)

    def test_combined_cost_ratio_both_sides_zero(self):
        p = _empty_pos()
        self.assertAlmostEqual(p.combined_cost_ratio, 0.0)

    def test_combined_cost_ratio_only_up(self):
        p = _pos(up_shares=20.0, up_spend=10.0, down_spend=2.0)
        # ratio = 12 / 20
        self.assertAlmostEqual(p.combined_cost_ratio, 0.6)

    def test_combined_cost_ratio_uses_larger_side(self):
        p = _pos(up_shares=10.0, down_shares=20.0, up_spend=5.0, down_spend=5.0)
        # ratio = 10 / 20 = 0.5
        self.assertAlmostEqual(p.combined_cost_ratio, 0.5)

    def test_payout_if_up_equals_up_shares(self):
        p = _pos(up_shares=15.0)
        self.assertAlmostEqual(p.payout_if_up, 15.0)

    def test_payout_if_down_equals_down_shares(self):
        p = _pos(down_shares=25.0)
        self.assertAlmostEqual(p.payout_if_down, 25.0)


# ---------------------------------------------------------------------------
# _mark_buy
# ---------------------------------------------------------------------------

class MarkBuyTests(unittest.TestCase):
    def test_mark_buy_up_increases_shares_proportionally(self):
        pos = _empty_pos()
        _mark_buy(pos, "up", 10.0, 0.50, 290.0)
        self.assertAlmostEqual(pos.up_shares, 20.0)
        self.assertAlmostEqual(pos.up_spend, 10.0)

    def test_mark_buy_down_increases_shares_proportionally(self):
        pos = _empty_pos()
        _mark_buy(pos, "down", 8.0, 0.40, 280.0)
        self.assertAlmostEqual(pos.down_shares, 20.0)
        self.assertAlmostEqual(pos.down_spend, 8.0)

    def test_mark_buy_zero_spend_is_noop(self):
        pos = _empty_pos()
        _mark_buy(pos, "up", 0.0, 0.50, 290.0)
        self.assertAlmostEqual(pos.up_shares, 0.0)
        self.assertIsNone(pos.first_buy_s)

    def test_mark_buy_zero_ask_is_noop(self):
        pos = _empty_pos()
        _mark_buy(pos, "up", 10.0, 0.0, 290.0)
        self.assertAlmostEqual(pos.up_shares, 0.0)
        self.assertIsNone(pos.first_buy_s)

    def test_mark_buy_tracks_first_and_last_buy_s(self):
        pos = _empty_pos()
        _mark_buy(pos, "up", 10.0, 0.50, 280.0)  # elapsed = 300 - 280 = 20
        _mark_buy(pos, "up", 10.0, 0.50, 260.0)  # elapsed = 40
        self.assertEqual(pos.first_buy_s, 20)
        self.assertEqual(pos.last_buy_s, 40)

    def test_mark_buy_first_buy_s_does_not_grow_on_earlier_fill(self):
        pos = _empty_pos()
        _mark_buy(pos, "up", 10.0, 0.50, 260.0)  # elapsed=40
        _mark_buy(pos, "up", 10.0, 0.50, 280.0)  # elapsed=20 — earlier, but after first call
        self.assertEqual(pos.first_buy_s, 20)
        self.assertEqual(pos.last_buy_s, 40)


# ---------------------------------------------------------------------------
# _apply_slippage
# ---------------------------------------------------------------------------

class ApplySlippageTests(unittest.TestCase):
    def test_zero_slippage_returns_ask_unchanged(self):
        self.assertAlmostEqual(_apply_slippage(0.50, 0.0), 0.50)

    def test_positive_slippage_increases_ask(self):
        result = _apply_slippage(0.50, 100.0)  # 100 bps = 1%
        self.assertAlmostEqual(result, 0.505)

    def test_zero_ask_returns_zero(self):
        self.assertAlmostEqual(_apply_slippage(0.0, 50.0), 0.0)

    def test_slippage_is_proportional_to_bps(self):
        r1 = _apply_slippage(1.0, 50.0)
        r2 = _apply_slippage(1.0, 100.0)
        self.assertAlmostEqual(r2 - 1.0, 2.0 * (r1 - 1.0))


# ---------------------------------------------------------------------------
# _projected_cost_ratio
# ---------------------------------------------------------------------------

class ProjectedCostRatioTests(unittest.TestCase):
    def test_empty_position_no_add_returns_zero(self):
        pos = _empty_pos()
        r = _projected_cost_ratio(pos, add_up_spend=0.0, add_down_spend=0.0, up_ask=0.5, down_ask=0.5)
        self.assertAlmostEqual(r, 0.0)

    def test_adds_up_spend_and_shares(self):
        pos = _empty_pos()
        # adding $10 at ask 0.5 = 20 up shares; no down; ratio = 10/20 = 0.5
        r = _projected_cost_ratio(pos, add_up_spend=10.0, add_down_spend=0.0, up_ask=0.5, down_ask=0.5)
        self.assertAlmostEqual(r, 0.5)

    def test_existing_position_is_included(self):
        pos = _pos(up_shares=20.0, up_spend=10.0)
        # adding another $10 at ask 0.5 = 40 up shares; ratio = 20/40 = 0.5
        r = _projected_cost_ratio(pos, add_up_spend=10.0, add_down_spend=0.0, up_ask=0.5, down_ask=0.5)
        self.assertAlmostEqual(r, 0.5)

    def test_zero_ask_skips_share_calculation(self):
        pos = _empty_pos()
        r = _projected_cost_ratio(pos, add_up_spend=10.0, add_down_spend=0.0, up_ask=0.0, down_ask=0.5)
        self.assertAlmostEqual(r, 0.0)

    def test_down_dominant_uses_down_shares_for_ratio(self):
        pos = _empty_pos()
        # $5 up at 0.5 = 10 up shares; $10 down at 0.25 = 40 down shares
        # combined_spend=15, best_payout=40, ratio=0.375
        r = _projected_cost_ratio(pos, add_up_spend=5.0, add_down_spend=10.0, up_ask=0.5, down_ask=0.25)
        self.assertAlmostEqual(r, 0.375)


# ---------------------------------------------------------------------------
# _within_ratio_band
# ---------------------------------------------------------------------------

class WithinRatioBandTests(unittest.TestCase):
    def test_zero_ratio_returns_false(self):
        self.assertFalse(_within_ratio_band(0.0, 0.70, 0.89))

    def test_exactly_at_min_returns_true(self):
        self.assertTrue(_within_ratio_band(0.70, 0.70, 0.89))

    def test_exactly_at_max_returns_true(self):
        self.assertTrue(_within_ratio_band(0.89, 0.70, 0.89))

    def test_below_min_returns_false(self):
        self.assertFalse(_within_ratio_band(0.69, 0.70, 0.89))

    def test_above_max_returns_false(self):
        self.assertFalse(_within_ratio_band(0.90, 0.70, 0.89))

    def test_midpoint_returns_true(self):
        self.assertTrue(_within_ratio_band(0.80, 0.70, 0.89))


# ---------------------------------------------------------------------------
# _winner_for_delta
# ---------------------------------------------------------------------------

class WinnerForDeltaTests(unittest.TestCase):
    def test_zero_delta_with_zero_threshold_returns_flat(self):
        # With flat_threshold=0.0: >0 → Up, <0 → Down, ==0 → Flat
        self.assertEqual(_winner_for_delta(0.0, flat_threshold=0.0), "Flat")

    def test_small_positive_delta_returns_up_at_zero_threshold(self):
        self.assertEqual(_winner_for_delta(0.001, flat_threshold=0.0), "Up")

    def test_negative_delta_at_threshold_boundary(self):
        # -5.0 < -4.99 threshold? no: threshold is 4.99, -5.0 < -4.99 → Down
        self.assertEqual(_winner_for_delta(-5.0, flat_threshold=4.99), "Down")

    def test_positive_delta_within_flat_band_returns_flat(self):
        self.assertEqual(_winner_for_delta(3.0, flat_threshold=5.0), "Flat")

    def test_default_flat_threshold_zero(self):
        self.assertEqual(_winner_for_delta(0.1), "Up")
        self.assertEqual(_winner_for_delta(-0.1), "Down")


# ---------------------------------------------------------------------------
# _parse_event_ts
# ---------------------------------------------------------------------------

class ParseEventTsTests(unittest.TestCase):
    def test_normal_seconds_returned_as_is(self):
        self.assertAlmostEqual(_parse_event_ts(1_700_000_000, fallback=0.0), 1_700_000_000.0)

    def test_milliseconds_divided_by_1000(self):
        ms = 1_700_000_000_000
        result = _parse_event_ts(ms, fallback=0.0)
        self.assertAlmostEqual(result, 1_700_000_000.0)

    def test_invalid_value_returns_fallback(self):
        self.assertAlmostEqual(_parse_event_ts("not-a-number", fallback=42.0), 42.0)

    def test_none_returns_fallback(self):
        self.assertAlmostEqual(_parse_event_ts(None, fallback=99.0), 99.0)


# ---------------------------------------------------------------------------
# _effective_quote_ts
# ---------------------------------------------------------------------------

class EffectiveQuoteTsTests(unittest.TestCase):
    def test_both_zero_falls_back_to_timestamp(self):
        t = _tick(timestamp=50.0, up_quote_ts=0.0, down_quote_ts=0.0)
        self.assertAlmostEqual(_effective_quote_ts(t), 50.0)

    def test_uses_max_of_up_and_down_quote_ts(self):
        t = _tick(timestamp=50.0, up_quote_ts=60.0, down_quote_ts=55.0)
        self.assertAlmostEqual(_effective_quote_ts(t), 60.0)

    def test_only_up_quote_ts_set(self):
        t = _tick(timestamp=50.0, up_quote_ts=70.0, down_quote_ts=0.0)
        self.assertAlmostEqual(_effective_quote_ts(t), 70.0)


# ---------------------------------------------------------------------------
# Zero-ask guards across all policies
# ---------------------------------------------------------------------------

_ZERO_ASK_TICK = _tick(up_ask=0.0, down_ask=0.0)
_ZERO_UP_ASK_TICK = _tick(up_ask=0.0)
_ZERO_DOWN_ASK_TICK = _tick(down_ask=0.0)


class PolicyZeroAskGuardTests(unittest.TestCase):
    """All policy functions must return {up:0,down:0} when either ask is 0."""

    def _check_all_zero(self, tick, config=None):
        pos = _empty_pos()
        cfg = config or {}
        for name, fn in POLICIES.items():
            with self.subTest(policy=name):
                result = fn(pos, tick, cfg)
                self.assertEqual(result["up"], 0.0, f"{name} up not zero on zero-ask tick")
                self.assertEqual(result["down"], 0.0, f"{name} down not zero on zero-ask tick")

    def test_both_asks_zero(self):
        self._check_all_zero(_ZERO_ASK_TICK)

    def test_up_ask_zero(self):
        self._check_all_zero(_ZERO_UP_ASK_TICK)

    def test_down_ask_zero(self):
        self._check_all_zero(_ZERO_DOWN_ASK_TICK)


# ---------------------------------------------------------------------------
# Policy: equal_time
# ---------------------------------------------------------------------------

class PolicyEqualTimeTests(unittest.TestCase):
    def test_returns_configured_notional_both_sides(self):
        spends = policy_equal_time(_empty_pos(), _tick(), {"notional_each": 15.0})
        self.assertAlmostEqual(spends["up"], 15.0)
        self.assertAlmostEqual(spends["down"], 15.0)

    def test_default_notional_is_25(self):
        spends = policy_equal_time(_empty_pos(), _tick(), {})
        self.assertAlmostEqual(spends["up"], 25.0)
        self.assertAlmostEqual(spends["down"], 25.0)


# ---------------------------------------------------------------------------
# Policy: combined_cost_threshold
# ---------------------------------------------------------------------------

class PolicyCombinedCostThresholdTests(unittest.TestCase):
    def test_rejects_when_ask_sum_exceeds_threshold(self):
        t = _tick(up_ask=0.55, down_ask=0.55)  # sum = 1.10 > 0.98
        spends = policy_combined_cost_threshold(_empty_pos(), t, {"combined_threshold": 0.98})
        self.assertEqual(spends, {"up": 0.0, "down": 0.0})

    def test_accepts_when_ask_sum_at_threshold(self):
        t = _tick(up_ask=0.49, down_ask=0.49)  # sum = 0.98
        spends = policy_combined_cost_threshold(_empty_pos(), t, {"combined_threshold": 0.98})
        self.assertGreater(spends["up"], 0.0)

    def test_accepts_when_ask_sum_below_threshold(self):
        t = _tick(up_ask=0.40, down_ask=0.40)
        spends = policy_combined_cost_threshold(_empty_pos(), t, {"combined_threshold": 0.98})
        self.assertGreater(spends["up"], 0.0)


# ---------------------------------------------------------------------------
# Policy: payout_balanced
# ---------------------------------------------------------------------------

class PolicyPayoutBalancedTests(unittest.TestCase):
    def test_flat_position_buys_both_sides_equally(self):
        t = _tick(up_ask=0.49, down_ask=0.49)
        spends = policy_payout_balanced(_empty_pos(), t, {"combined_threshold": 1.02, "notional_step": 20.0})
        self.assertAlmostEqual(spends["up"], 10.0)
        self.assertAlmostEqual(spends["down"], 10.0)

    def test_more_up_shares_buys_down_only(self):
        pos = _pos(up_shares=30.0, down_shares=10.0)
        t = _tick(up_ask=0.49, down_ask=0.49)
        spends = policy_payout_balanced(pos, t, {"combined_threshold": 1.02, "notional_step": 20.0})
        self.assertAlmostEqual(spends["up"], 0.0)
        self.assertGreater(spends["down"], 0.0)

    def test_more_down_shares_buys_up_only(self):
        pos = _pos(up_shares=10.0, down_shares=30.0)
        t = _tick(up_ask=0.49, down_ask=0.49)
        spends = policy_payout_balanced(pos, t, {"combined_threshold": 1.02, "notional_step": 20.0})
        self.assertGreater(spends["up"], 0.0)
        self.assertAlmostEqual(spends["down"], 0.0)

    def test_rejects_when_ask_sum_too_high(self):
        t = _tick(up_ask=0.55, down_ask=0.55)  # sum = 1.10 > 1.02
        spends = policy_payout_balanced(_empty_pos(), t, {"combined_threshold": 1.02})
        self.assertEqual(spends, {"up": 0.0, "down": 0.0})


# ---------------------------------------------------------------------------
# Policy: winner_lean_btc
# ---------------------------------------------------------------------------

class PolicyWinnerLeanBtcTests(unittest.TestCase):
    def test_flat_btc_buys_base_both_sides(self):
        t = _tick(btc_delta=0.0)
        spends = policy_winner_lean_btc(_empty_pos(), t, {"base_notional": 10.0, "lean_notional": 20.0, "btc_delta_threshold": 10.0})
        self.assertAlmostEqual(spends["up"], 10.0)
        self.assertAlmostEqual(spends["down"], 10.0)

    def test_positive_btc_adds_lean_to_up(self):
        t = _tick(btc_delta=15.0)
        spends = policy_winner_lean_btc(_empty_pos(), t, {"base_notional": 10.0, "lean_notional": 20.0, "btc_delta_threshold": 10.0})
        self.assertAlmostEqual(spends["up"], 30.0)
        self.assertAlmostEqual(spends["down"], 10.0)

    def test_negative_btc_adds_lean_to_down(self):
        t = _tick(btc_delta=-15.0)
        spends = policy_winner_lean_btc(_empty_pos(), t, {"base_notional": 10.0, "lean_notional": 20.0, "btc_delta_threshold": 10.0})
        self.assertAlmostEqual(spends["up"], 10.0)
        self.assertAlmostEqual(spends["down"], 30.0)

    def test_btc_delta_at_threshold_triggers_lean(self):
        t = _tick(btc_delta=10.0)
        spends = policy_winner_lean_btc(_empty_pos(), t, {"base_notional": 5.0, "lean_notional": 10.0, "btc_delta_threshold": 10.0})
        self.assertAlmostEqual(spends["up"], 15.0)


# ---------------------------------------------------------------------------
# Policy: hedge_conviction
# ---------------------------------------------------------------------------

class PolicyHedgeConvictionTests(unittest.TestCase):
    def test_flat_btc_returns_zeros(self):
        t = _tick(btc_delta=0.0)
        spends = policy_hedge_conviction(_empty_pos(), t, {"btc_delta_threshold": 8.0})
        self.assertEqual(spends, {"up": 0.0, "down": 0.0})

    def test_btc_between_thresholds_returns_zeros(self):
        t = _tick(btc_delta=5.0)
        spends = policy_hedge_conviction(_empty_pos(), t, {"btc_delta_threshold": 8.0})
        self.assertEqual(spends, {"up": 0.0, "down": 0.0})

    def test_strong_positive_btc_convicts_up(self):
        t = _tick(btc_delta=12.0)
        spends = policy_hedge_conviction(_empty_pos(), t, {"btc_delta_threshold": 8.0, "conviction_notional": 30.0, "hedge_notional": 5.0})
        self.assertAlmostEqual(spends["up"], 30.0)
        self.assertAlmostEqual(spends["down"], 5.0)

    def test_strong_negative_btc_convicts_down(self):
        t = _tick(btc_delta=-12.0)
        spends = policy_hedge_conviction(_empty_pos(), t, {"btc_delta_threshold": 8.0, "conviction_notional": 30.0, "hedge_notional": 5.0})
        self.assertAlmostEqual(spends["up"], 5.0)
        self.assertAlmostEqual(spends["down"], 30.0)


# ---------------------------------------------------------------------------
# Policy: cost_gated_up_bias_momentum — negative BTC skipped
# ---------------------------------------------------------------------------

class PolicyCostGatedMomentumTests(unittest.TestCase):
    def test_negative_btc_outside_flat_band_skips(self):
        pos = _empty_pos()
        t = _tick(btc_delta=-15.0, up_ask=0.40, down_ask=0.60)
        spends = policy_cost_gated_up_bias_momentum(
            pos, t,
            {"flat_btc_abs": 5.0, "positive_btc_threshold": 20.0},
        )
        self.assertEqual(spends, {"up": 0.0, "down": 0.0})

    def test_positive_btc_below_threshold_in_flat_band_uses_flat_config(self):
        pos = _empty_pos()
        t = _tick(btc_delta=2.0, up_ask=0.40, down_ask=0.60)
        spends = policy_cost_gated_up_bias_momentum(
            pos, t,
            {
                "flat_btc_abs": 5.0,
                "positive_btc_threshold": 20.0,
                "min_cost_ratio": 0.70,
                "max_cost_ratio": 0.89,
                "target_cost_ratio": 0.80,
                "flat_up_notional": 24.0,
                "flat_min_down_notional": 12.0,
                "flat_max_down_notional": 24.0,
            },
        )
        self.assertGreater(spends["up"], 0.0)


# ---------------------------------------------------------------------------
# assess_market_quality — ask_sum_too_high
# ---------------------------------------------------------------------------

class AssessMarketQualityExtendedTests(unittest.TestCase):
    def test_ask_sum_too_high_rejected(self):
        ctx = _ctx(
            ticks=[
                _tick(timestamp=float(i), seconds_remaining=300.0 - i, up_ask=0.65, down_ask=0.65)
                for i in range(6)
            ]
        )
        result = assess_market_quality(ctx, {"min_ticks": 5, "min_ask_sum": 0.80, "max_ask_sum": 1.20})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "ask_sum_too_high")

    def test_sufficient_valid_ticks_passes(self):
        ctx = _ctx(
            ticks=[
                _tick(timestamp=float(i), seconds_remaining=300.0 - i, up_ask=0.50, down_ask=0.50)
                for i in range(10)
            ]
        )
        result = assess_market_quality(ctx, {"min_ticks": 5, "min_ask_sum": 0.80, "max_ask_sum": 1.20})
        self.assertTrue(result.ok)

    def test_insufficient_valid_quotes_below_min_ticks(self):
        ctx = _ctx(
            ticks=[
                _tick(timestamp=float(i), seconds_remaining=300.0 - i, up_ask=0.0, down_ask=0.0)
                for i in range(10)
            ]
        )
        result = assess_market_quality(ctx, {"min_ticks": 5, "min_ask_sum": 0.80, "max_ask_sum": 1.20})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "insufficient_valid_quotes")


# ---------------------------------------------------------------------------
# simulate_market — step_secs throttling and start/stop filtering
# ---------------------------------------------------------------------------

class SimulateMarketFilteringTests(unittest.TestCase):
    def _multi_tick_ctx(self, n=10):
        ticks = [
            _tick(timestamp=float(i * 5), seconds_remaining=300.0 - (i * 5), up_ask=0.50, down_ask=0.50)
            for i in range(n)
        ]
        return _ctx(ticks)

    def test_step_secs_limits_executions(self):
        ctx = self._multi_tick_ctx(10)
        # With step_secs=30, only ticks >=30s apart execute.
        result = simulate_market(ctx, "equal_time", {"notional_each": 10.0, "step_secs": 30.0})
        # 10 ticks × 5s spacing = 0..45s; step_secs=30 → at most 2 executions
        self.assertLessEqual(result["up_spend"], 20.0)

    def test_start_after_secs_skips_early_ticks(self):
        ctx = self._multi_tick_ctx(10)
        # elapsed of first tick = 0. start_after=10 skips it.
        result_all = simulate_market(ctx, "equal_time", {"notional_each": 10.0, "step_secs": 0.0})
        result_late = simulate_market(ctx, "equal_time", {"notional_each": 10.0, "step_secs": 0.0, "start_after_secs": 10.0})
        self.assertLess(result_late["up_spend"], result_all["up_spend"])

    def test_stop_after_secs_skips_late_ticks(self):
        ctx = self._multi_tick_ctx(10)
        result_all = simulate_market(ctx, "equal_time", {"notional_each": 10.0, "step_secs": 0.0})
        result_early = simulate_market(ctx, "equal_time", {"notional_each": 10.0, "step_secs": 0.0, "stop_after_secs": 20.0})
        self.assertLess(result_early["up_spend"], result_all["up_spend"])

    def test_no_ticks_returns_zero_pnl(self):
        ctx = _ctx(ticks=[])
        result = simulate_market(ctx, "equal_time", {"notional_each": 10.0, "step_secs": 0.0})
        self.assertAlmostEqual(result["combined_spend"], 0.0)
        self.assertAlmostEqual(result["gross_pnl"], 0.0)

    def test_gross_pnl_correct_for_down_winner(self):
        ctx = _ctx(
            winner="Down",
            ticks=[_tick(up_ask=0.40, down_ask=0.60)],
        )
        result = simulate_market(ctx, "equal_time", {"notional_each": 10.0, "step_secs": 0.0})
        # up_shares = 10/0.4 = 25; down_shares = 10/0.6 = 16.67
        # gross_pnl (Down winner) = down_shares - combined_spend = 16.67 - 20 = -3.33
        # adverse_pnl = min(pnl_if_up, pnl_if_down) = min(+5.0, -3.33) = -3.33
        self.assertAlmostEqual(result["gross_pnl"], 10.0 / 0.6 - 20.0, places=4)
        self.assertAlmostEqual(result["adverse_pnl"], 10.0 / 0.6 - 20.0, places=4)

    def test_roi_is_gross_pnl_over_spend(self):
        ctx = _ctx(ticks=[_tick(up_ask=0.50, down_ask=0.50)])
        result = simulate_market(ctx, "equal_time", {"notional_each": 10.0, "step_secs": 0.0})
        expected_roi = result["gross_pnl"] / result["combined_spend"]
        self.assertAlmostEqual(result["roi"], expected_roi, places=5)

    def test_net_pnl_deducts_fee(self):
        ctx = _ctx(ticks=[_tick(up_ask=0.50, down_ask=0.50)])
        result = simulate_market(ctx, "equal_time", {"notional_each": 10.0, "step_secs": 0.0, "fee_bps": 100.0})
        # fee = combined_spend * 0.01
        expected_net = result["gross_pnl"] - result["combined_spend"] * 0.01
        self.assertAlmostEqual(result["net_pnl"], expected_net, places=5)


# ---------------------------------------------------------------------------
# _pick_up_bias_spends — no valid down_spend in band
# ---------------------------------------------------------------------------

class PickUpBiasSpendTests(unittest.TestCase):
    def test_no_valid_down_spend_returns_zeros(self):
        pos = _empty_pos()
        t = _tick(up_ask=0.10, down_ask=0.10)  # sum so low that any combo blows past ratio cap
        result = _pick_up_bias_spends(
            pos, t,
            up_spend=100.0,
            min_ratio=0.99,
            max_ratio=0.99,
            target_ratio=0.99,
            min_down_spend=0.0,
            max_down_spend=0.01,
        )
        self.assertEqual(result, {"up": 0.0, "down": 0.0})


# ---------------------------------------------------------------------------
# Backtest harness: run_simulation over a synthetic in-memory DB
# ---------------------------------------------------------------------------

class BacktestHarnessTests(unittest.TestCase):
    """
    Builds a synthetic 30-window DB (equivalent to 30 × 5min = 2.5 hours of
    BTC 5m market data) and runs the full simulation suite against it.

    This serves as the 'backtest harness against cached data' requested in the
    TDD loop. It validates:
      - PnL arithmetic is correct end-to-end
      - All policies run without crashing
      - Summaries are internally consistent
    """

    def _build_db(self, n_windows=30):
        d = tempfile.mkdtemp()
        db = ResearchDatabase(str(Path(d) / "backtest.db"))
        base_ts = 1_700_000_000
        for i in range(n_windows):
            window_start = base_ts + i * 300
            window_end = window_start + 300
            slug = f"btc-updown-5m-{window_start}"
            winner = "Up" if i % 2 == 0 else "Down"
            btc_open = 30000.0 + i * 10
            db.upsert_market({
                "slug": slug,
                "window_start_ts": window_start,
                "window_end_ts": window_end,
                "market_id": f"m{i}",
                "condition_id": f"c{i}",
                "up_token_id": f"up{i}",
                "down_token_id": f"dn{i}",
                "btc_open_price": btc_open,
                "btc_close_price": btc_open + (5.0 if winner == "Up" else -5.0),
                "resolution": winner,
            })
            btc_delta = 5.0 if winner == "Up" else -5.0
            for j in range(10):
                db.insert_tick(slug, ResearchTick(
                    timestamp=float(window_start + j * 30),
                    seconds_remaining=300.0 - j * 30,
                    up_bid=0.49,
                    up_ask=0.50,
                    down_bid=0.49,
                    down_ask=0.50,
                    btc_spot=btc_open + btc_delta * (j / 10),
                    btc_delta=btc_delta * (j / 10),
                ))
        return db

    def test_equal_time_runs_without_crash(self):
        db = self._build_db()
        try:
            summary = run_simulation(db, "equal_time", {
                "notional_each": 10.0,
                "step_secs": 60.0,
                "min_ticks": 5,
                "min_ask_sum": 0.80,
                "max_ask_sum": 1.20,
            })
            self.assertIsNotNone(summary)
            self.assertEqual(summary["n"], 30)
            self.assertEqual(summary["skipped"], 0)
        finally:
            db.close()

    def test_pnl_consistent_with_per_window_results(self):
        db = self._build_db()
        try:
            summary = run_simulation(db, "equal_time", {
                "notional_each": 10.0,
                "step_secs": 0.0,
                "min_ticks": 5,
                "min_ask_sum": 0.80,
                "max_ask_sum": 1.20,
            })
            rows = db.conn.execute(
                "SELECT gross_pnl, net_pnl FROM research_sim_results WHERE policy='equal_time'"
            ).fetchall()
            self.assertAlmostEqual(
                sum(r["gross_pnl"] for r in rows),
                summary["total_gross"],
                places=4,
            )
            self.assertAlmostEqual(
                sum(r["net_pnl"] for r in rows),
                summary["total_net"],
                places=4,
            )
        finally:
            db.close()

    def test_all_policies_run_without_crash(self):
        db = self._build_db()
        try:
            for policy_name in POLICIES:
                with self.subTest(policy=policy_name):
                    summary = run_simulation(db, policy_name, {
                        "notional_each": 10.0,
                        "step_secs": 0.0,
                        "min_ticks": 5,
                        "min_ask_sum": 0.80,
                        "max_ask_sum": 1.20,
                    })
                    # Every policy must either produce results or cleanly skip
                    self.assertIsNotNone(summary)
        finally:
            db.close()

    def test_backtest_report(self):
        """Print a readable PnL report for the 30-window synthetic backtest."""
        db = self._build_db(30)
        try:
            results = []
            for policy_name in sorted(POLICIES):
                summary = run_simulation(db, policy_name, {
                    "notional_each": 10.0,
                    "step_secs": 0.0,
                    "fee_bps": 7.2,
                    "slippage_bps": 10.0,
                    "min_ticks": 5,
                    "min_ask_sum": 0.80,
                    "max_ask_sum": 1.20,
                })
                if summary:
                    results.append(summary)
            self.assertTrue(results, "No simulation results produced")
            # Basic sanity: net <= gross for every policy
            for r in results:
                self.assertLessEqual(r["total_net"], r["total_gross"] + 1e-6,
                                     f"net > gross for {r['policy']}")
        finally:
            db.close()

    def test_30_day_equivalent_volume_benchmark(self):
        """
        Synthetic 30-day equivalent: 30 days × 288 5-min windows/day = 8640 windows.
        Validates that the simulation scales without numerical issues.
        Run only 864 windows (10% sample) for test speed.
        """
        db = self._build_db(n_windows=864)
        try:
            summary = run_simulation(db, "equal_time", {
                "notional_each": 10.0,
                "step_secs": 0.0,
                "min_ticks": 5,
                "min_ask_sum": 0.80,
                "max_ask_sum": 1.20,
            })
            self.assertEqual(summary["n"], 864)
            # With equal asks (0.5/0.5), equal Up/Down outcomes: gross_pnl ≈ 0
            self.assertAlmostEqual(abs(summary["total_gross"]) / (summary["n"] * 20), 0.0, places=6)
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------------

_pos_strategy = st.builds(
    lambda us, ds, usp, dsp: _pos(us, ds, usp, dsp),
    us=st.floats(min_value=0.0, max_value=1e6, allow_nan=False),
    ds=st.floats(min_value=0.0, max_value=1e6, allow_nan=False),
    usp=st.floats(min_value=0.0, max_value=1e6, allow_nan=False),
    dsp=st.floats(min_value=0.0, max_value=1e6, allow_nan=False),
)

_valid_ask = st.floats(min_value=0.01, max_value=0.99, allow_nan=False)
_valid_bps = st.floats(min_value=0.0, max_value=200.0, allow_nan=False)


class HypothesisPropertyTests(unittest.TestCase):
    @given(ask=_valid_ask, bps=_valid_bps)
    def test_slippage_never_reduces_ask(self, ask, bps):
        self.assertGreaterEqual(_apply_slippage(ask, bps), ask)

    @given(pos=_pos_strategy)
    def test_combined_cost_ratio_non_negative(self, pos):
        self.assertGreaterEqual(pos.combined_cost_ratio, 0.0)

    @given(pos=_pos_strategy)
    def test_combined_spend_equals_sum_of_sides(self, pos):
        self.assertAlmostEqual(pos.combined_spend, pos.up_spend + pos.down_spend)

    @given(
        add_up=st.floats(min_value=0.0, max_value=1e4, allow_nan=False),
        add_down=st.floats(min_value=0.0, max_value=1e4, allow_nan=False),
        up_ask=_valid_ask,
        down_ask=_valid_ask,
    )
    def test_projected_cost_ratio_non_negative(self, add_up, add_down, up_ask, down_ask):
        pos = _empty_pos()
        r = _projected_cost_ratio(pos, add_up_spend=add_up, add_down_spend=add_down, up_ask=up_ask, down_ask=down_ask)
        self.assertGreaterEqual(r, 0.0)

    @given(
        up_ask=_valid_ask,
        down_ask=_valid_ask,
        btc_delta=st.floats(min_value=-500.0, max_value=500.0, allow_nan=False),
    )
    @settings(max_examples=100)
    def test_all_policies_return_non_negative_spends(self, up_ask, down_ask, btc_delta):
        pos = _empty_pos()
        tick = _tick(
            up_ask=up_ask, down_ask=down_ask,
            up_bid=max(0.0, up_ask - 0.01),
            down_bid=max(0.0, down_ask - 0.01),
            btc_delta=btc_delta,
            seconds_remaining=150.0,
        )
        for name, fn in POLICIES.items():
            with self.subTest(policy=name):
                result = fn(pos, tick, {})
                self.assertGreaterEqual(result["up"], 0.0, f"{name}.up < 0")
                self.assertGreaterEqual(result["down"], 0.0, f"{name}.down < 0")

    @given(
        up_ask=_valid_ask,
        down_ask=_valid_ask,
    )
    @settings(max_examples=50)
    def test_simulate_market_combined_spend_non_negative(self, up_ask, down_ask):
        ctx = _ctx(ticks=[_tick(up_ask=up_ask, down_ask=down_ask)])
        for name in POLICIES:
            result = simulate_market(ctx, name, {"notional_each": 10.0, "step_secs": 0.0})
            self.assertGreaterEqual(result["combined_spend"], 0.0)

    @given(
        ratio=st.floats(min_value=0.0, max_value=2.0, allow_nan=False),
        lo=st.floats(min_value=0.01, max_value=0.99, allow_nan=False),
    )
    def test_within_ratio_band_symmetric(self, ratio, lo):
        hi = lo + 0.20
        result = _within_ratio_band(ratio, lo, hi)
        # If it passes, ratio must be in [lo, hi] and > 0.
        if result:
            self.assertGreater(ratio, 0.0)
            self.assertGreaterEqual(ratio, lo)
            self.assertLessEqual(ratio, hi)


# ---------------------------------------------------------------------------
# Policy: cost_gated_up_bias_time_filtered
# ---------------------------------------------------------------------------

class PolicyTimeFilteredTests(unittest.TestCase):
    def _tick_at_hour(self, hour: int) -> ResearchTick:
        import datetime
        # Build a timestamp whose local hour matches the requested hour.
        # Use a fixed date (2026-04-20 00:00 local) and add the hour offset.
        base = datetime.datetime(2026, 4, 20, 0, 0, 0)
        ts = (base + datetime.timedelta(hours=hour)).timestamp()
        return _tick(timestamp=ts, up_ask=0.40, down_ask=0.60, btc_delta=0.0)

    def test_skips_entry_in_blocked_hours(self):
        pos = _empty_pos()
        for h in [17, 18, 19, 20, 21]:
            with self.subTest(hour=h):
                t = self._tick_at_hour(h)
                spends = policy_cost_gated_up_bias_time_filtered(
                    pos, t,
                    {
                        "skip_hours": [17, 18, 19, 20, 21],
                        "min_cost_ratio": 0.70, "max_cost_ratio": 0.89,
                        "target_cost_ratio": 0.80, "flat_btc_abs": 15.0,
                    },
                )
                self.assertEqual(spends, {"up": 0.0, "down": 0.0}, f"hour {h} should be blocked")

    def test_allows_entry_outside_blocked_hours(self):
        pos = _empty_pos()
        for h in [0, 2, 11, 14]:
            with self.subTest(hour=h):
                t = self._tick_at_hour(h)
                spends = policy_cost_gated_up_bias_time_filtered(
                    pos, t,
                    {
                        "skip_hours": [17, 18, 19, 20, 21],
                        "min_cost_ratio": 0.70, "max_cost_ratio": 0.89,
                        "target_cost_ratio": 0.80, "flat_btc_abs": 15.0,
                    },
                )
                self.assertGreater(spends["up"] + spends["down"], 0.0, f"hour {h} should be allowed")

    def test_empty_skip_hours_falls_through_to_flat_only(self):
        pos = _empty_pos()
        t = self._tick_at_hour(19)
        spends = policy_cost_gated_up_bias_time_filtered(
            pos, t,
            {
                "skip_hours": [],
                "min_cost_ratio": 0.70, "max_cost_ratio": 0.89,
                "target_cost_ratio": 0.80, "flat_btc_abs": 15.0,
            },
        )
        self.assertGreater(spends["up"] + spends["down"], 0.0)

    def test_respects_flat_btc_filter_within_allowed_hours(self):
        pos = _empty_pos()
        t = self._tick_at_hour(14)
        t_moving = ResearchTick(
            timestamp=t.timestamp, seconds_remaining=t.seconds_remaining,
            up_bid=t.up_bid, up_ask=t.up_ask,
            down_bid=t.down_bid, down_ask=t.down_ask,
            btc_spot=t.btc_spot, btc_delta=20.0,  # outside flat band
        )
        spends = policy_cost_gated_up_bias_time_filtered(
            pos, t_moving,
            {"skip_hours": [17, 18, 19, 20, 21], "flat_btc_abs": 5.0},
        )
        self.assertEqual(spends, {"up": 0.0, "down": 0.0})

    def test_default_skip_hours_blocks_17_to_21(self):
        pos = _empty_pos()
        for h in [17, 18, 19, 20, 21]:
            with self.subTest(hour=h):
                t = self._tick_at_hour(h)
                spends = policy_cost_gated_up_bias_time_filtered(pos, t, {})
                self.assertEqual(spends, {"up": 0.0, "down": 0.0})


if __name__ == "__main__":
    unittest.main()
