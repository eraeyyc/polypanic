import unittest
from unittest.mock import patch

from observer import Database
from trader import (
    LiveOrderState,
    LivePositionState,
    LiveTrader,
    TERMINAL_ORDER_STATUSES,
    _round_down_to_tick,
    _round_up_to_tick,
)


class DummyClob:
    def __init__(self):
        self.fail_post_order = False

    def get_order_book(self, token_id):
        class _Book:
            tick_size = "0.01"
            min_order_size = "1"
        return _Book()

    def get_fee_rate_bps(self, token_id):
        return 72

    def calculate_market_price(self, token_id, side, amount, order_type):
        return 0.38

    def get_balance_allowance(self, params):
        return {"balance": "1000", "allowance": "1000"}

    def create_market_order(self, args):
        return {"signed": True, "args": args}

    def post_order(self, signed, order_type):
        if self.fail_post_order:
            raise RuntimeError("boom")
        return {"orderID": "oid-live", "status": "submitted"}

    def get_address(self):
        return "0xdeadbeef"


class TickRoundingTests(unittest.TestCase):
    def test_round_up_to_tick(self):
        self.assertEqual(_round_up_to_tick(0.6831, 0.01), 0.69)
        self.assertEqual(_round_up_to_tick(0.6831, 0.0001), 0.6831)

    def test_round_down_to_tick(self):
        self.assertEqual(_round_down_to_tick(0.6839, 0.01), 0.68)
        self.assertEqual(_round_down_to_tick(0.6839, 0.0001), 0.6839)


class LiveDatabaseTests(unittest.TestCase):
    def test_live_order_round_trip(self):
        db = Database(":memory:")
        order = LiveOrderState(
            client_order_id="cid-1",
            order_id="oid-1",
            slug="btc-updown-5m-1",
            market_id="cond-1",
            token_id="token-1",
            side="up",
            intent="buy",
            order_type="market",
            tif="FAK",
            requested_price=0.38,
            requested_shares=131.57,
            requested_notional=50.0,
            fee_rate_bps=72,
            created_at=1.0,
            updated_at=2.0,
            status="submitted",
        )
        db.upsert_live_order(order.to_record())
        row = db.get_live_order("oid-1")
        self.assertIsNotNone(row)
        self.assertEqual(row["client_order_id"], "cid-1")
        self.assertEqual(row["status"], "submitted")
        db.close()

    def test_live_position_round_trip(self):
        db = Database(":memory:")
        pos = LivePositionState(
            slug="btc-updown-5m-1",
            token_id="token-1",
            side="up",
            shares=10.0,
            avg_cost=0.41,
            realized_pnl=3.5,
            total_fees=0.2,
            updated_at=2.0,
        )
        db.upsert_live_position(pos.to_record())
        rows = db.get_live_positions("btc-updown-5m-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shares"], 10.0)
        self.assertEqual(rows[0]["avg_cost"], 0.41)
        db.close()


class LiveAccountingTests(unittest.TestCase):
    def _build_trader(self):
        from observer import StrategyConfig

        return LiveTrader(StrategyConfig(), Database(":memory:"), DummyClob())

    def test_fee_calculation_for_buy_and_sell(self):
        trader = self._build_trader()
        buy_fee_amount, buy_fee_asset, buy_fee_shares = trader._compute_fee(
            shares=100.0, price=0.40, action="buy", fee_rate_bps=72
        )
        sell_fee_amount, sell_fee_asset, sell_fee_shares = trader._compute_fee(
            shares=100.0, price=0.70, action="sell", fee_rate_bps=72
        )
        self.assertAlmostEqual(buy_fee_amount, 0.1728)
        self.assertEqual(buy_fee_asset, "SHARES")
        self.assertAlmostEqual(buy_fee_shares, 0.432)
        self.assertAlmostEqual(sell_fee_amount, 0.1512)
        self.assertEqual(sell_fee_asset, "USDC")
        self.assertEqual(sell_fee_shares, 0.0)
        trader.db.close()

    def test_fill_application_updates_position_and_realized_pnl(self):
        trader = self._build_trader()
        buy_order = LiveOrderState(
            client_order_id="cid-buy",
            order_id="oid-buy",
            slug="btc-updown-5m-1",
            market_id="cond-1",
            token_id="token-1",
            side="up",
            intent="buy",
            order_type="market",
            tif="FAK",
            requested_price=0.40,
            requested_shares=100.0,
            requested_notional=40.0,
            fee_rate_bps=72,
            created_at=1.0,
        )
        trader._apply_fill_to_positions(
            buy_order,
            fill_shares=100.0,
            fill_price=0.40,
            fee_amount=0.1728,
            fee_asset="SHARES",
        )
        pos = trader.actual_positions["btc-updown-5m-1:up"]
        self.assertAlmostEqual(pos.shares, 99.568)
        self.assertAlmostEqual(pos.avg_cost, 40.0 / 99.568, places=6)
        self.assertAlmostEqual(pos.total_fees, 0.1728)

        sell_order = LiveOrderState(
            client_order_id="cid-sell",
            order_id="oid-sell",
            slug="btc-updown-5m-1",
            market_id="cond-1",
            token_id="token-1",
            side="up",
            intent="exit_target",
            order_type="market",
            tif="FAK",
            requested_price=0.70,
            requested_shares=50.0,
            requested_notional=35.0,
            fee_rate_bps=72,
            created_at=2.0,
        )
        trader._apply_fill_to_positions(
            sell_order,
            fill_shares=50.0,
            fill_price=0.70,
            fee_amount=0.1512,
            fee_asset="USDC",
        )
        pos = trader.actual_positions["btc-updown-5m-1:up"]
        self.assertAlmostEqual(pos.shares, 49.568)
        self.assertAlmostEqual(pos.realized_pnl, 35.0 - 0.1512 - (50.0 * (40.0 / 99.568)), places=6)
        self.assertAlmostEqual(pos.total_fees, 0.324, places=6)
        trader.db.close()


class LiveStateHandlingTests(unittest.TestCase):
    def _build_trader(self):
        from observer import StrategyConfig

        return LiveTrader(StrategyConfig(), Database(":memory:"), DummyClob())

    def test_closed_orders_are_terminal_for_capacity_and_side_checks(self):
        trader = self._build_trader()
        closed = LiveOrderState(
            client_order_id="cid-closed",
            order_id="oid-closed",
            slug="btc-updown-5m-1",
            market_id="cond-1",
            token_id="token-1",
            side="up",
            intent="buy",
            order_type="market",
            tif="FAK",
            requested_price=0.38,
            requested_shares=10.0,
            requested_notional=3.8,
            fee_rate_bps=72,
            created_at=1.0,
            status="closed",
        )
        trader.open_orders[closed.order_id] = closed
        self.assertIn("closed", TERMINAL_ORDER_STATUSES)
        self.assertIsNone(trader._open_order_for("btc-updown-5m-1", "up"))
        self.assertTrue(trader._ensure_buy_capacity(5.0))
        trader.db.close()

    def test_market_data_health_can_clear_transient_kill_switch(self):
        trader = self._build_trader()
        trader._consecutive_errors = 0
        for _ in range(trader.config.max_consecutive_live_errors):
            trader.set_market_data_health(False)
        self.assertTrue(trader._kill_switch)
        trader.set_market_data_health(True)
        self.assertFalse(trader._kill_switch)
        self.assertTrue(trader._market_data_ok)
        trader.db.close()

    def test_failed_buy_submission_is_marked_failed(self):
        trader = self._build_trader()
        trader.register_market("btc-updown-5m-1", "token-up", "token-down", "market-1", "cond-1")
        trader._market_data_ok = True
        trader.clob.fail_post_order = True

        trader.execute_buy("btc-updown-5m-1", "up", 0.38, now=1.0, context={"test": True})

        orders = trader.db.get_live_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status"], "failed")
        self.assertIn("boom", orders[0]["error_text"])
        self.assertEqual(trader.open_orders, {})
        trader.db.close()

    def test_missing_data_api_position_clears_stale_local_inventory(self):
        trader = self._build_trader()
        slug = "btc-updown-5m-100"
        trader.register_market(slug, "token-up", "token-down", "market-1", "cond-1")
        stale = LivePositionState(
            slug=slug,
            token_id="token-up",
            side="up",
            shares=13.0,
            avg_cost=0.35,
            updated_at=10.0,
        )
        trader.actual_positions[f"{slug}:up"] = stale
        trader.db.upsert_live_position(stale.to_record())

        with patch.object(trader.data_api, "get_positions", return_value=[]), \
             patch("trader._now_ts", return_value=500.0):
            trader._sync_positions_from_data_api()

        self.assertNotIn(f"{slug}:up", trader.actual_positions)
        self.assertEqual(trader.db.get_live_positions(slug), [])
        trader.db.close()

    def test_missing_data_api_position_does_not_clear_active_window_inventory(self):
        trader = self._build_trader()
        slug = "btc-updown-5m-100"
        trader.register_market(slug, "token-up", "token-down", "market-1", "cond-1")
        stale = LivePositionState(
            slug=slug,
            token_id="token-up",
            side="up",
            shares=13.0,
            avg_cost=0.35,
            updated_at=10.0,
        )
        trader.actual_positions[f"{slug}:up"] = stale
        trader.db.upsert_live_position(stale.to_record())

        with patch.object(trader.data_api, "get_positions", return_value=[]), \
             patch("trader._now_ts", return_value=250.0):
            trader._sync_positions_from_data_api()

        self.assertIn(f"{slug}:up", trader.actual_positions)
        self.assertEqual(len(trader.db.get_live_positions(slug)), 1)
        trader.db.close()

    def test_entry_rejection_logs_reason(self):
        trader = self._build_trader()
        with self.assertLogs(level="INFO") as logs:
            accepted = trader.evaluate_entry(
                "btc-updown-5m-1",
                "down",
                best_ask=0.20,
                best_bid=0.19,
                seconds_remaining=120.0,
                btc_delta=25.0,
                elapsed_secs=90.0,
            )
        self.assertFalse(accepted)
        self.assertTrue(any("BTC misaligned for DOWN" in line for line in logs.output))
        trader.db.close()

    def test_neutral_btc_range_holds_through_close(self):
        trader = self._build_trader()
        pos = LivePositionState(
            slug="btc-updown-5m-1",
            token_id="token-1",
            side="down",
            shares=10.0,
            avg_cost=0.35,
            updated_at=1.0,
        )
        trader.actual_positions["btc-updown-5m-1:down"] = pos
        reason = trader.evaluate_exit(
            "btc-updown-5m-1",
            "down",
            best_bid=0.12,
            seconds_remaining=10.0,
            btc_delta=-2.5,
        )
        self.assertIsNone(reason)
        trader.db.close()


if __name__ == "__main__":
    unittest.main()
