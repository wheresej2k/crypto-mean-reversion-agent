import tempfile
import unittest
from pathlib import Path

import paper_broker
from config import Settings
from models import PositionSnapshot
from paper_broker import PaperBroker
from risk_manager import evaluate_decisions
from strategy import TradeDecision


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
