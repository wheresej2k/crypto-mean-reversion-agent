import unittest
from datetime import date, datetime, timezone

from models import Bar
from momentum_strategy import select
from momentum_trader import aligned_closes, due_for_rebalance


def bars(start_day, count, price=100.0, step=1.0):
    return [
        Bar(datetime(2026, 1, 1, tzinfo=timezone.utc).replace(day=1) if False else
            datetime.fromtimestamp((start_day + i) * 86400, tz=timezone.utc),
            price + i * step, price + i * step, price + i * step, price + i * step, 1.0)
        for i in range(count)
    ]


class DateAlignmentTests(unittest.TestCase):
    """Coins have different history lengths. Indexing positionally compares them across different
    dates - the bug that distorted an early version of the momentum backtest.
    """

    def test_aligns_symbols_with_different_start_dates(self):
        # B starts 10 days later than A, so only the overlap should survive.
        data = {"A/USD": bars(100, 40), "B/USD": bars(110, 30)}
        dates, closes = aligned_closes(data)
        self.assertEqual(30, len(dates))
        self.assertEqual(30, len(closes["A/USD"]))
        self.assertEqual(30, len(closes["B/USD"]))

    def test_same_index_is_the_same_calendar_day_for_every_symbol(self):
        data = {"A/USD": bars(100, 40), "B/USD": bars(110, 30)}
        dates, closes = aligned_closes(data)
        for i in (0, 5, 29):
            for symbol, series in data.items():
                on_that_day = next(b.close for b in series if b.timestamp.date() == dates[i])
                self.assertEqual(on_that_day, closes[symbol][i])

    def test_missing_days_are_excluded_not_forward_filled(self):
        a = bars(100, 10)
        b = [x for i, x in enumerate(bars(100, 10)) if i != 4]  # B is missing one day
        dates, closes = aligned_closes({"A/USD": a, "B/USD": b})
        self.assertEqual(9, len(dates))
        self.assertNotIn(a[4].timestamp.date(), dates)

    def test_no_overlap_is_fatal_rather_than_silently_wrong(self):
        with self.assertRaises(SystemExit):
            aligned_closes({"A/USD": bars(100, 5), "B/USD": bars(500, 5)})


class RebalanceScheduleTests(unittest.TestCase):
    def test_first_run_rebalances(self):
        due, why = due_for_rebalance({}, 14, date(2026, 9, 19))
        self.assertTrue(due)
        self.assertIn("first", why)

    def test_waits_until_the_interval_has_passed(self):
        state = {"last_rebalance_date": "2026-09-15"}
        due, why = due_for_rebalance(state, 14, date(2026, 9, 19))
        self.assertFalse(due)
        self.assertIn("4 of 14", why)

    def test_rebalances_once_the_interval_is_reached(self):
        state = {"last_rebalance_date": "2026-09-05"}
        due, _ = due_for_rebalance(state, 14, date(2026, 9, 19))
        self.assertTrue(due)

    def test_rebalances_when_overdue(self):
        state = {"last_rebalance_date": "2026-08-01"}
        due, _ = due_for_rebalance(state, 14, date(2026, 9, 19))
        self.assertTrue(due)


class LiveLabelUnitsTests(unittest.TestCase):
    """The live trader passes DAY counts against daily bars; the backtest passes BAR counts
    against 15-minute bars. Only the reason text depends on which, but a '56-day lookback'
    printed as '1-day' would make the live log untrustworthy.
    """

    def test_daily_bars_label_lookback_in_days(self):
        closes = {"X/USD": [100.0 + i for i in range(200)]}
        pick = select(closes, 199, lookback=56, regime_window=150, bars_per_day=1)[0]
        self.assertIn("56-day return", pick.reason)
        self.assertIn("150-day average", pick.reason)

    def test_fifteen_minute_bars_label_the_same_span_in_days(self):
        closes = {"X/USD": [100.0 + i for i in range(56 * 96 + 150 * 96 + 10)]}
        i = len(closes["X/USD"]) - 1
        pick = select(closes, i, lookback=56 * 96, regime_window=150 * 96, bars_per_day=96)[0]
        self.assertIn("56-day return", pick.reason)
        self.assertIn("150-day average", pick.reason)


if __name__ == "__main__":
    unittest.main()
