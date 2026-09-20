import unittest

from momentum_strategy import (BARS_PER_DAY, above_regime_average, select, trailing_return)


def ramp(n, start=100.0, step=1.0):
    return [start + i * step for i in range(n)]


class TrailingReturnTests(unittest.TestCase):
    def test_measures_return_over_the_lookback(self):
        closes = [100.0] * 10 + [110.0]
        self.assertAlmostEqual(0.10, trailing_return(closes, 10, 10))

    def test_returns_none_without_enough_history(self):
        # Must be None, not 0.0 - a caller treating "no history" as "flat" would silently trade
        # on data it does not have.
        self.assertIsNone(trailing_return(ramp(5), 3, 10))

    def test_handles_a_negative_move(self):
        closes = [100.0] * 10 + [80.0]
        self.assertAlmostEqual(-0.20, trailing_return(closes, 10, 10))


class RegimeFilterTests(unittest.TestCase):
    def test_price_above_its_average_passes(self):
        self.assertTrue(above_regime_average(ramp(50), 49, 20))

    def test_price_below_its_average_fails(self):
        falling = list(reversed(ramp(50)))
        self.assertFalse(above_regime_average(falling, 49, 20))

    def test_zero_window_disables_the_filter(self):
        self.assertTrue(above_regime_average(list(reversed(ramp(50))), 49, 0))

    def test_insufficient_history_is_none_not_true(self):
        self.assertIsNone(above_regime_average(ramp(10), 9, 20))


class SelectTests(unittest.TestCase):
    def test_picks_positive_momentum_in_an_uptrend(self):
        closes = {"BTC/USD": ramp(200)}
        picks = select(closes, 199, lookback=50, regime_window=100)
        self.assertTrue(picks[0].selected)
        self.assertGreater(picks[0].trailing_return_pct, 0)

    def test_rejects_negative_momentum(self):
        closes = {"BTC/USD": list(reversed(ramp(200)))}
        picks = select(closes, 199, lookback=50, regime_window=0)
        self.assertFalse(picks[0].selected)
        self.assertIn("not positive momentum", picks[0].reason)

    def test_regime_filter_blocks_a_bounce_inside_a_downtrend(self):
        # Long decline, then a short sharp bounce: momentum over a short lookback is positive,
        # but price is still far below its long-run average. This is the case the regime filter
        # exists for, and the one that cost the unfiltered version ~34 points of drawdown.
        closes = {"BTC/USD": list(reversed(ramp(300, start=100.0, step=2.0))) + ramp(20, start=110.0, step=3.0)}
        i = len(closes["BTC/USD"]) - 1
        unfiltered = select(closes, i, lookback=15, regime_window=0)[0]
        filtered = select(closes, i, lookback=15, regime_window=200)[0]
        self.assertTrue(unfiltered.selected)
        self.assertFalse(filtered.selected)
        self.assertIn("bear regime", filtered.reason)

    def test_max_names_keeps_only_the_strongest(self):
        closes = {
            "STRONG": ramp(200, start=100.0, step=3.0),
            "WEAK": ramp(200, start=100.0, step=0.2),
        }
        picks = {p.symbol: p for p in select(closes, 199, lookback=50, regime_window=100, max_names=1)}
        self.assertTrue(picks["STRONG"].selected)
        self.assertFalse(picks["WEAK"].selected)
        self.assertIn("outside the top 1", picks["WEAK"].reason)

    def test_every_symbol_is_accounted_for(self):
        closes = {"A": ramp(200), "B": list(reversed(ramp(200))), "C": ramp(30)}
        picks = select(closes, 199, lookback=50, regime_window=100)
        self.assertEqual({"A", "B", "C"}, {p.symbol for p in picks})
        self.assertTrue(all(p.reason for p in picks))

    def test_uses_no_data_after_the_decision_index(self):
        rising = ramp(200)
        crash = rising + [1.0] * 50
        early = select({"X": rising}, 199, lookback=50, regime_window=100)[0]
        late = select({"X": crash}, 199, lookback=50, regime_window=100)[0]
        self.assertEqual(early.selected, late.selected)
        self.assertAlmostEqual(early.trailing_return_pct, late.trailing_return_pct)


if __name__ == "__main__":
    unittest.main()
