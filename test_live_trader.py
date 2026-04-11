import unittest

from observer import Database
from trader import (
    LiveOrderState,
    LivePositionState,
    LiveTrader,
    _round_down_to_tick,
    _round_up_to_tick,
)


class DummyClob:
    pass


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


if __name__ == "__main__":
    unittest.main()
