import tempfile
import unittest
from pathlib import Path

import paper_broker
from config import Settings
from models import PositionSnapshot
from paper_broker import PaperBroker
from risk_manager import evaluate_decisions
from strategy import TradeDecision, decide, trend_ok_for


def settings(**overrides):
    values = {
        "watchlist": ["ETH/USD"],
        "window": 48,
        "trend_window": 192,
        "entry_zscore": 2.0,
        "exit_zscore": 0.0,
        "stop_loss_pct": 8.0,
        "take_profit_pct": 5.0,
        "min_confidence": 50.0,
        "max_position_pct": 15.0,
        "max_total_exposure_pct": 75.0,
        "max_daily_loss_pct": 5.0,
        "max_trades_per_run": 5,
        "trading_fee_pct": 0.8,
        "slippage_pct": 0.05,
        "min_signal_exit_profit_pct": 0.5,
        "trend_tolerance_pct": 0.0,
        "min_edge_pct": 0.0,
    }
    values.update(overrides)
    return Settings(**values)


class RiskManagerExecutionTests(unittest.TestCase):
    def test_sell_is_not_blocked_by_entry_confidence_filter(self):
        positions = {
            "ETH/USD": PositionSnapshot("ETH/USD", qty=2.5, market_value=250.0, avg_entry_price=100.0, unrealized_plpc=0.0)
        }
        decisions = [
            TradeDecision(
                symbol="ETH/USD",
                action="SELL",
                size_pct=100,
                confidence=4,
                reasoning="mean reversion exit",
                rolling_mean=100,
                zscore=0.1,
                price=103,
            )
        ]

        _, sells, skipped = evaluate_decisions(decisions, settings(), equity=1000, cash=500, positions=positions, day_pl_pct=0)

        self.assertEqual([], skipped)
        self.assertEqual(2.5, sells[0].qty)

    def test_full_sell_uses_exact_position_size(self):
        qty = 4.031562639283948e-09
        positions = {
            "ETH/USD": PositionSnapshot("ETH/USD", qty=qty, market_value=0.01, avg_entry_price=100.0, unrealized_plpc=0.0)
        }
        decisions = [
            TradeDecision("ETH/USD", "SELL", 100, 1, "exit dust", 100, 0, 103)
        ]

        _, sells, skipped = evaluate_decisions(decisions, settings(), equity=1000, cash=500, positions=positions, day_pl_pct=0)

        self.assertEqual([], skipped)
        self.assertEqual(qty, sells[0].qty)

    def test_signal_exit_must_clear_fee_adjusted_profit_floor(self):
        positions = {
            "ETH/USD": PositionSnapshot("ETH/USD", qty=2.5, market_value=251.0, avg_entry_price=100.0, unrealized_plpc=0.4)
        }
        decisions = [
            TradeDecision("ETH/USD", "SELL", 100, 80, "weak reversion", 100, 0.2, 100.4)
        ]

        _, sells, skipped = evaluate_decisions(decisions, settings(), equity=1000, cash=500, positions=positions, day_pl_pct=0)

        self.assertEqual([], sells)
        self.assertIn("profit floor", skipped[0].reason)


class MinimumEdgeGateTests(unittest.TestCase):
    """A BUY only makes money if the reversion it reaches for clears the round-trip cost."""

    def _buy(self, edge_pct):
        return TradeDecision(
            symbol="ETH/USD", action="BUY", size_pct=100, confidence=90,
            reasoning="dip", rolling_mean=100, zscore=-2.5, price=100, edge_pct=edge_pct,
        )

    def test_buy_below_round_trip_cost_is_rejected(self):
        # fee 0.8% per side + 0.05% slippage = 1.65% round trip; a 1.0% target cannot pay for it.
        buys, _, skipped = evaluate_decisions(
            [self._buy(1.0)], settings(min_edge_pct=0.0), equity=1000, cash=1000, positions={}, day_pl_pct=0
        )
        self.assertEqual([], buys)
        self.assertIn("minimum edge", skipped[0].reason)

    def test_buy_above_round_trip_cost_is_allowed(self):
        buys, _, skipped = evaluate_decisions(
            [self._buy(3.0)], settings(min_edge_pct=0.0), equity=1000, cash=1000, positions={}, day_pl_pct=0
        )
        self.assertEqual([], skipped)
        self.assertEqual("ETH/USD", buys[0].symbol)

    def test_explicit_min_edge_pct_raises_the_bar_above_the_cost_floor(self):
        buys, _, skipped = evaluate_decisions(
            [self._buy(2.5)], settings(min_edge_pct=4.0), equity=1000, cash=1000, positions={}, day_pl_pct=0
        )
        self.assertEqual([], buys)
        self.assertIn("4.00% minimum edge", skipped[0].reason)

    def test_min_edge_gate_never_blocks_an_exit(self):
        positions = {
            "ETH/USD": PositionSnapshot("ETH/USD", qty=1.0, market_value=110.0, avg_entry_price=100.0, unrealized_plpc=10.0)
        }
        sell = TradeDecision(
            symbol="ETH/USD", action="SELL", size_pct=100, confidence=90,
            reasoning="reverted", rolling_mean=100, zscore=0.2, price=110, edge_pct=0.0,
        )
        _, sells, skipped = evaluate_decisions(
            [sell], settings(min_edge_pct=99.0), equity=1000, cash=500, positions=positions, day_pl_pct=0
        )
        self.assertEqual([], skipped)
        self.assertEqual(1.0, sells[0].qty)


class TrendToleranceTests(unittest.TestCase):
    """The trend filter has to allow a dip to sit slightly under the slow average, or it cancels
    out the entry rule it sits next to and no BUY can ever fire."""

    def test_zero_tolerance_reproduces_the_old_hard_filter(self):
        self.assertTrue(trend_ok_for(price=100.0, trend_mean=100.0, trend_tolerance_pct=0.0))
        self.assertFalse(trend_ok_for(price=99.9, trend_mean=100.0, trend_tolerance_pct=0.0))

    def test_tolerance_allows_a_dip_just_under_the_trend_average(self):
        self.assertTrue(trend_ok_for(price=97.0, trend_mean=100.0, trend_tolerance_pct=4.0))

    def test_tolerance_still_blocks_a_real_downtrend(self):
        self.assertFalse(trend_ok_for(price=80.0, trend_mean=100.0, trend_tolerance_pct=4.0))

    def test_dip_below_trend_produces_a_hold_not_a_buy(self):
        d = decide("ETH/USD", price=80.0, mean=100.0, std=5.0, has_position=False,
                   window=48, entry_zscore=2.0, exit_zscore=0.0, trend_ok=False)
        self.assertEqual("HOLD", d.action)
        self.assertIn("downtrend", d.reasoning)


class PaperBrokerCostTests(unittest.TestCase):
    def test_round_trip_charges_fee_and_slippage(self):
        original_state_path = paper_broker.STATE_PATH
        with tempfile.TemporaryDirectory() as tmp:
            try:
                paper_broker.STATE_PATH = Path(tmp) / "paper_state.json"
                broker = PaperBroker()
                pos = broker.open_position(
                    "ETH/USD",
                    notional_usd=1_000,
                    price=100,
                    stop_loss_pct=8,
                    take_profit_pct=5,
                    trading_fee_pct=0.8,
                    slippage_pct=0.05,
                )
                self.assertAlmostEqual(98_992.0, broker.state["cash"], places=6)
                self.assertAlmostEqual(100.05, pos["entry_price"], places=6)

                event = broker.close_position("ETH/USD", 103, "signal_exit", trading_fee_pct=0.8, slippage_pct=0.05)

                self.assertAlmostEqual(102.9485, event["exit_price"], places=6)
                self.assertLess(event["pl_pct"], 3.0)
                self.assertGreater(event["pl_pct"], 0.0)
                self.assertEqual({}, broker.state["positions"])
            finally:
                paper_broker.STATE_PATH = original_state_path

    def test_prune_dust_positions_removes_sub_dollar_residuals(self):
        original_state_path = paper_broker.STATE_PATH
        with tempfile.TemporaryDirectory() as tmp:
            try:
                paper_broker.STATE_PATH = Path(tmp) / "paper_state.json"
                broker = PaperBroker()
                broker.state["positions"]["ETH/USD"] = {
                    "qty": 0.000001,
                    "entry_price": 100.0,
                    "stop_price": 92.0,
                    "target_price": 105.0,
                    "trade_id": "dust1234",
                    "opened_at": "2026-09-15T00:00:00+00:00",
                    "checked_through": "2026-09-15T00:00:00+00:00",
                }

                events = broker.prune_dust_positions({"ETH/USD": 100.0})

                self.assertEqual("dust_cleanup", events[0]["exit_reason"])
                self.assertEqual({}, broker.state["positions"])
            finally:
                paper_broker.STATE_PATH = original_state_path


if __name__ == "__main__":
    unittest.main()
