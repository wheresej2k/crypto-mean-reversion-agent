import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paper_broker
from models import Bar
from paper_broker import MEAN_REVERSION, TREND, PaperBroker, strategy_of
from trend_strategy import Donchian, TrendTarget, current_targets, panel_from_bars
from trend_trader import plan_orders


def daily(closes, start=datetime(2024, 1, 1, tzinfo=timezone.utc)):
    return [Bar(start + timedelta(days=i), c, c * 1.001, c * 0.999, c, 1.0) for i, c in enumerate(closes)]


class DonchianTests(unittest.TestCase):
    def test_flat_until_enough_history(self):
        targets = current_targets({"BTC/USD": daily([100.0 + i for i in range(100)])})
        self.assertEqual(targets[0].weight, 0.0)
        self.assertIn("not enough daily history", targets[0].reason)

    def test_steady_uptrend_is_fully_long(self):
        # gentle noise so realized vol is non-zero
        closes = [100 * 1.003 ** i * (1.002 if i % 2 else 0.998) for i in range(500)]
        t = current_targets({"BTC/USD": daily(closes)})[0]
        self.assertEqual(t.channels_long, t.channels_total)
        self.assertAlmostEqual(t.weight, 1.0)

    def test_crash_exits_every_channel(self):
        up = [100 * 1.003 ** i * (1.002 if i % 2 else 0.998) for i in range(450)]
        down = [up[-1] * 0.97 ** i for i in range(1, 60)]
        t = current_targets({"BTC/USD": daily(up + down)})[0]
        self.assertEqual(t.channels_long, 0)
        self.assertEqual(t.weight, 0.0)

    def test_weights_never_exceed_one_and_favour_calmer_coin(self):
        calm = [100 * 1.003 ** i * (1.002 if i % 2 else 0.998) for i in range(500)]
        wild = [100 * 1.003 ** i * (1.03 if i % 2 else 0.97) for i in range(500)]
        ts = {t.symbol: t for t in current_targets({"A/USD": daily(calm), "B/USD": daily(wild)})}
        self.assertLessEqual(ts["A/USD"].weight + ts["B/USD"].weight, 1.0 + 1e-9)
        self.assertGreater(ts["A/USD"].weight, ts["B/USD"].weight)

    def test_live_replay_matches_stepwise_research_class(self):
        closes = [100 * (1 + 0.002 * ((i * 7) % 11 - 5)) * 1.001 ** i for i in range(600)]
        bars = {"BTC/USD": daily(closes), "ETH/USD": daily([c * 0.05 for c in closes[::-1]])}
        dates, panel = panel_from_bars(bars)
        strat = Donchian(panel)
        for t in range(len(dates)):
            w = strat.weights(t)
        live = {t.symbol: t.weight for t in current_targets(bars)}
        for s in bars:
            self.assertAlmostEqual(live[s], w.get(s, 0.0))


class PlanOrdersTests(unittest.TestCase):
    def tgt(self, s, w):
        return TrendTarget(s, w, 0, 7, "r")

    def test_entry_exit_and_drift_band(self):
        prices = {"BTC/USD": 100.0, "ETH/USD": 10.0, "SOL/USD": 1.0}
        holdings = {"ETH/USD": 50.0, "SOL/USD": 100.0}  # ETH $500, SOL $100
        targets = [self.tgt("BTC/USD", 0.5), self.tgt("ETH/USD", 0.0), self.tgt("SOL/USD", 0.11)]
        orders = {o[0]: o for o in plan_orders(targets, holdings, prices, 1000.0, 0.25)}
        self.assertEqual(orders["BTC/USD"][1:3], ("BUY", 500.0))
        self.assertEqual(orders["ETH/USD"][1:3], ("CLOSE", 500.0))
        self.assertNotIn("SOL/USD", orders)  # $100 held vs $110 target: inside the band

    def test_sells_come_before_buys(self):
        prices = {"A/USD": 1.0, "B/USD": 1.0}
        orders = plan_orders([self.tgt("A/USD", 0.9), self.tgt("B/USD", 0.0)], {"B/USD": 500.0}, prices, 1000.0, 0.25)
        self.assertEqual([o[1] for o in orders], ["CLOSE", "BUY"])

    def test_trim_when_far_above_target(self):
        orders = plan_orders([self.tgt("A/USD", 0.2)], {"A/USD": 500.0}, {"A/USD": 1.0}, 1000.0, 0.25)
        self.assertEqual(orders[0][1:3], ("SELL", 300.0))


class SleeveBrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = paper_broker.STATE_PATH
        paper_broker.STATE_PATH = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        paper_broker.STATE_PATH = self.old
        self.tmp.cleanup()

    def test_positions_are_tagged_and_untagged_means_mean_reversion(self):
        b = PaperBroker()
        b.open_position("BTC/USD", 1000, 100.0, 100, 1e6, strategy=TREND)
        b.open_position("DOGE/USD", 1000, 1.0, 10, 8)
        self.assertEqual(strategy_of(b.state["positions"]["BTC/USD"]), TREND)
        self.assertEqual(strategy_of(b.state["positions"]["DOGE/USD"]), MEAN_REVERSION)
        del b.state["positions"]["DOGE/USD"]["strategy"]
        self.assertEqual(strategy_of(b.state["positions"]["DOGE/USD"]), MEAN_REVERSION)

    def test_add_to_position_averages_entry_and_charges_fee(self):
        b = PaperBroker()
        b.open_position("BTC/USD", 1000, 100.0, 100, 1e6, trading_fee_pct=1.0, strategy=TREND)
        b.add_to_position("BTC/USD", 1000, 200.0, trading_fee_pct=1.0)
        pos = b.state["positions"]["BTC/USD"]
        self.assertAlmostEqual(pos["qty"], 15.0)
        self.assertAlmostEqual(pos["entry_price"], 2000 / 15)
        self.assertAlmostEqual(pos["entry_notional"], 2000)
        self.assertAlmostEqual(pos["entry_fee"], 20)
        self.assertAlmostEqual(b.state["cash"], 100_000 - 2020)


if __name__ == "__main__":
    unittest.main()
