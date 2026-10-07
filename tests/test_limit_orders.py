import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paper_broker
from limit_orders import FILLED, UNFILLED, WAIT, new_order, resolve
from models import Bar
from paper_broker import PaperBroker

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def bar(i, low, high, close=None):
    c = close if close is not None else (low + high) / 2
    return Bar(T0 + timedelta(minutes=15 * i), c, high, low, c, 1.0)


class ResolveTests(unittest.TestCase):
    def test_waits_until_the_next_bar_has_closed(self):
        o = new_order("buy", 100.0, T0)
        self.assertEqual(resolve(o, [bar(0, 99, 101)]), (WAIT, None))

    def test_buy_fills_only_when_low_trades_strictly_below(self):
        o = new_order("buy", 100.0, T0)
        self.assertEqual(resolve(o, [bar(0, 99, 101), bar(1, 99.9, 102)])[0], FILLED)
        self.assertEqual(resolve(o, [bar(0, 99, 101), bar(1, 100.0, 102)])[0], UNFILLED)  # a touch is not a fill

    def test_sell_fills_only_when_high_trades_strictly_above(self):
        o = new_order("sell", 100.0, T0)
        self.assertEqual(resolve(o, [bar(1, 98, 100.1)])[0], FILLED)
        self.assertEqual(resolve(o, [bar(1, 98, 100.0)])[0], UNFILLED)

    def test_only_the_first_bar_after_the_decision_counts(self):
        # missed runs must not give an order extra chances: bar 2 trades through, bar 1 did not
        o = new_order("buy", 100.0, T0)
        outcome, b = resolve(o, [bar(0, 99, 101), bar(1, 100.5, 102), bar(2, 90, 95)])
        self.assertEqual(outcome, UNFILLED)
        self.assertEqual(b.timestamp, T0 + timedelta(minutes=15))


class FillBarBracketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = paper_broker.STATE_PATH
        paper_broker.STATE_PATH = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        paper_broker.STATE_PATH = self.old
        self.tmp.cleanup()

    def test_a_limit_fill_can_be_stopped_out_on_its_own_fill_bar(self):
        b = PaperBroker()
        order = new_order("buy", 100.0, T0, notional=1000)
        fill_bar = bar(1, 85, 101)  # trades through the bid AND through a 10% stop in the same bar
        self.assertEqual(resolve(order, [fill_bar])[0], FILLED)
        b.open_position("DOGE/USD", 1000, 100.0, 10, 8, 0.40, 0.0, checked_through=order["placed_after"])
        event = b.check_stop_target_hits("DOGE/USD", [fill_bar], 0.80, 0.05)
        self.assertIsNotNone(event)
        self.assertEqual(event["exit_reason"], "stop_loss")

    def test_maker_fill_charges_maker_fee_and_no_slippage(self):
        b = PaperBroker()
        pos = b.open_position("DOGE/USD", 1000, 100.0, 10, 8, 0.40, 0.0)
        self.assertAlmostEqual(pos["entry_price"], 100.0)
        self.assertAlmostEqual(pos["entry_fee"], 4.0)
        self.assertAlmostEqual(b.state["cash"], 100_000 - 1004)


if __name__ == "__main__":
    unittest.main()
