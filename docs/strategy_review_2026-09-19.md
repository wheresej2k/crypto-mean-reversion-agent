# Strategy review - 2026-09-19

Covers `logs/trade_log.csv` through run 418 (`2026-09-20T00:15:41+00:00`, 2,135 rows), plus a
parameter sweep over 5 years of 15-minute bars for BTC, ETH, SOL, LINK and DOGE.

## 1. Why the bot stopped trading

It was not waiting for a good setup. **The entry threshold was mathematically unreachable.**

`entry_zscore` was `3.5`, so a BUY needed price to sit 3.5 standard deviations below its 24-hour
average. Across all 1,922 z-score observations in the live log:

| threshold | times reached | share |
|---|---|---|
| z <= -1.0 | 224 | 11.65% |
| z <= -1.5 | 87 | 4.53% |
| z <= -2.0 | 27 | 1.40% |
| z <= -2.5 | 4 | 0.21% |
| z <= -3.0 | **0** | **0.00%** |
| z <= -3.5 | **0** | **0.00%** |

The lowest reading ever recorded was **-2.95**. The threshold was never going to fire. Over 5 years
of backtest the same setting produced **9 round trips**.

A second problem compounded it: a BUY also required `price >= trend_mean` (48-hour average). A dip
deep enough to trigger an entry has usually already pulled price slightly under the slow average,
so the two conditions were close to mutually exclusive.

## 2. The bigger finding: the payoff shape was upside down

Loosening the threshold alone would have made things worse. Instrumenting the backtest to report
average win against average loss shows why:

```
window=96 entry_zscore=2.0            77 round trips
win rate 58.4%   avg win +1.53%   avg loss -5.57%   expectancy -1.424% per trade
exits: signal_exit 43, stop_loss 32, take_profit 2
```

A 58% win rate looks healthy and is completely misleading. Winners were capped near the
fee-adjusted profit floor (~+1.5%) while every loser ran the full 4% stop plus 1.65% in costs
(-5.57%). `take_profit` fired twice in a year. **Every trade lost 1.4% on average.**

The root cause is arithmetic, not tuning. Kraken Pro's lowest-volume spot taker fee is 0.80% per
side, so a round trip costs ~1.65% with slippage. The average reversion available on a 15-minute
chart for these coins is smaller than that.

## 3. What the sweep found

Roughly 2,000 configurations were tested across 3-month, 1-year and 5-year windows.

- **900 combinations** of stop-loss, take-profit, exit z-score, profit floor and trend tolerance:
  **not one was profitable.** Best result was -3.01%.
- **Every configuration trading once a day or more lost 40%+ over one year and 98%+ over five.**
- The only positive-expectancy family required a minimum-edge filter and traded **0.03 round trips
  per day** - about 8 per year.
- That positive result did **not** survive scrutiny. Re-run on a 15-coin universe instead of 5, its
  expectancy fell from +1.25% to -0.66%. It was a 10-trade artifact - exactly the data-mining trap
  `Trading System Checklist - Evidence Based` and the Aronson notes in the vault warn about.
- **The fee tier is the largest single lever.** Moving 0.80% -> 0.40% per side (resting limit
  orders instead of market orders) improves expectancy by ~0.77% per round trip, more than any
  strategy parameter tested.

Conclusion: **at 0.80% per side there is no profitable setting of this strategy.** Frequency and
expectancy are directly opposed, and no amount of tuning reconciles them.

## 4. What changed

The decision, made explicitly: **trade daily for the data, accept the bleed, and shrink position
size so the experiment survives long enough to produce that data.**

| Setting | Was | Now | Why |
|---|---|---|---|
| `entry_zscore` | 3.5 | **1.0** | 3.5 was never reachable; 1.0 fires regularly |
| `trend_tolerance_pct` | (none) | **6** | lets a dip sit 6% under the slow average instead of cancelling every entry; still blocks real crashes |
| `min_edge_pct` | (none) | **3** | new cost gate: never take a dip whose whole reversion target is smaller than the round-trip fee |
| `max_position_pct` | 15 | **2** | the survival knob - same trade flow, ~1/7th the bleed rate |
| `max_total_exposure_pct` | 75 | **12** | matches the smaller position size |
| `min_confidence` | 50 | **30** | at z=-1.0 confidence is 40, so 50 would have blocked every entry the new threshold creates |

Code changes:

- `strategy.py` - `trend_ok_for()` adds the tolerance band; `TradeDecision` now carries `edge_pct`.
- `risk_manager.py` - hard cost gate. A BUY is rejected unless its reversion target clears
  `2 x trading_fee_pct + slippage_pct`, regardless of configuration. Never blocks an exit.
- `backtest.py` - now reports average win, average loss, expectancy and exits by reason. A win rate
  on its own hides the failure mode described in section 2.
- `build_dashboard.py` - the same payoff stats now render on the live dashboard.
- `trader.py` - `--dry-run` no longer writes skip rows into the real trade log. Local test runs were
  silently polluting the history the strategy gets judged against.
- `tests/test_execution.py` - 8 new tests covering the cost gate and the tolerance band.

## 5. What to expect - stated plainly

Backtested on the new settings, 5 coins, 2% position size, 0.80% fee:

| window | return | max drawdown | round trips | per day |
|---|---|---|---|---|
| 3 months | -0.50% | -0.84% | 55 | 0.64 |
| 1 year | -12.41% | -12.71% | 476 | 1.31 |
| 5 years | -59.65% | -59.73% | 2,768 | 1.75 |

That 5-year figure annualizes to roughly **-19% per year**. This is a research budget, not a
strategy that is expected to make money.

**It will not literally trade every single day.** Dips cluster. Measured against calendar days:

| window | days with at least one entry signal | longest dry spell |
|---|---|---|
| 5 years | 75.5% | 10 days |
| 1 year | 48.5% | 12 days |
| 3 months | 33.0% | 12 days |

The last year was a -48% bear market, where the trend filter correctly sits the bot out - which is
why coverage is lower than the 5-year figure. The average of 1.31 trades/day is real, but it comes
in bursts; the median quiet day has no signal at all.

If you want more calendar coverage, the dial is `min_edge_pct`, not the trend filter. Lowering it
from 3 to 2 raises last-year coverage from 48.5% to 72.9% and cuts the longest gap from 12 days to
8, at a cost of roughly -15.4% for the year instead of -12.4%. That is a one-number change in
`config/params.json`.

## 6. Worth doing next

1. **Limit orders instead of market orders.** Halving the fee is worth more than every parameter in
   the sweep combined. It does not make the strategy profitable, but it roughly halves the bleed.
2. **Test a momentum strategy on this data.** `Crypto Trading - High Quality Research` in the vault
   notes that Liu and Tsyvinski find time-series *momentum* is the crypto-specific predictor that
   holds up, and Liu/Tsyvinski/Wu find market, size and momentum factors price the cross-section.
   Mean reversion is not what that research supports. A momentum rule with a trailing stop produces
   the opposite payoff shape - small frequent losses, occasional large wins - which is the shape
   that survives a 1.65% round-trip cost.
3. **Let the log answer it.** The dashboard now shows expectancy per trade. That single number, on
   real forward-traded data, is the thing to watch. If it is not above zero after a few hundred
   round trips, no amount of parameter tuning will fix it.
