# Time-series momentum - research results, 2026-09-19

Follow-up to `strategy_review_2026-09-19.md`, which concluded that the live mean-reversion strategy
has **no profitable configuration** at Kraken's 0.80%-per-side taker fee.

Motivated by the crypto research in the Obsidian vault: Liu & Tsyvinski (*Risks and Returns of
Cryptocurrency*) and Liu, Tsyvinski & Wu (*Common Risk Factors in Cryptocurrency*) find that
**time-series momentum** - not mean reversion - is the crypto predictor that holds up.

Nothing here is live. `trader.py` still runs the mean-reversion rules. This is a measurement.

## The headline

Momentum works on this data. Mean reversion does not.

**5 years, BTC/ETH/SOL/LINK/DOGE, 15-minute bars, 0.80% fee + 0.05% slippage**
(lookback 56 days, rebalance every 14 days, regime filter 150 days, max exposure 30%):

| metric | momentum | mean reversion (live config) |
|---|---|---|
| return | **+66.32%** | -59.65% |
| buy & hold | +22.06% | +23.16% |
| max drawdown | -30.25% | -59.73% |
| win rate | **74.6%** | ~54% |
| average win | **+33.89%** | +2.23% |
| average loss | -11.55% | -5.57% |
| **expectancy per trade** | **+22.35%** | **-1.38%** |
| trades/day | 0.163 | 1.31 |

Expectancy of +22% per round trip against a 1.65% round-trip cost. That is the whole point: the
cost is no longer comparable to the size of the move.

## Why it works where mean reversion failed

Three things, in order of importance:

1. **Turnover collapsed.** It rebalances every 14 days, not every 15 minutes. Fees are paid ~0.16
   times a day instead of 1.3 times. At 1.65% a round trip, turnover *is* the strategy's main cost.
2. **Winners are not capped.** There is no take-profit and no stop-loss. The exit is "momentum
   turned negative at the next rebalance". The mean-reversion version capped winners at a
   fee-adjusted profit floor (+1.5% average) while every loser ran a full stop (-5.6% average) -
   a 58% win rate with negative expectancy. Here the average win is +33.9% against a -11.6%
   average loss.
3. **The regime filter.** Holding only coins trading above their own 150-day average is what stops
   an unstopped book from giving everything back in a bear market. Same settings without it:
   -63.6% drawdown instead of -29.9%.

## It is a plateau, not a tuned spike

The single most important test, because a 10-trade artifact is exactly what sank the promising
mean-reversion configuration. All 27 neighbouring combinations (lookback 42/56/70d x rebalance
7/14/21d x regime 120/150/180d, 5 years, 30% exposure):

- **27 of 27 profitable**
- **27 of 27 positive expectancy** (range +12.4% to +26.7%)
- median return **+38.3%**, worst **+9.3%**, best **+108.0%**
- 17 of 27 stayed inside the -38% drawdown bar

Win rates across the grid ran 56-81%. Nothing here depends on hitting one exact setting.

**Wider-universe check.** Re-run on 15 coins instead of 5 (1 year) - the test that collapsed the
mean-reversion candidate from +1.25% to -0.66% expectancy:

| setting | 5-coin expectancy | 15-coin expectancy |
|---|---|---|
| lookback 56d, rebalance 7d | +7.31% | **+7.49%** |
| lookback 56d, rebalance 14d | +10.46% | **+10.14%** |

It holds. Note that short lookbacks (7d, 14d, 28d) did **not** survive the wider universe - they
flipped negative. The edge lives at the multi-week horizon, which is where the research puts it.

## Limits - read before trusting any of the above

1. **It trades about once a week, not daily.** 0.163 trades/day. This directly conflicts with the
   stated goal of daily activity. It cannot do both; low turnover is the reason it works.
2. **Survivorship bias.** BTC, ETH, SOL, LINK and DOGE were picked in 2026, after all five
   survived. A momentum strategy tested only on survivors is flattered, possibly substantially.
   A fair test needs coins that were plausible picks at the *start* of the window, including ones
   that later died.
3. **The 5-year sample is mostly a rising market.** Long-only momentum is helped by that. The
   1-year bear-market slice is much weaker: roughly +4% to +8% on a 6-to-17 trade sample, which is
   too few trades to mean much - though it did beat buy & hold there.
4. **The wider-universe check only covers 1 year.** A 5-year, 15-coin run was started and cut for
   time; it should be finished before this is traded.
5. **Drawdowns are still large.** -30% at 30% exposure, and 10 of the 27 grid points breached the
   -38% safety bar. Raising exposure raises returns and drawdown roughly together.
6. **Long-only, no shorting**, and no forward-tested paper record yet. Every number above is a
   backtest.

## How to run it

```
python momentum_backtest.py                        # 5 years, settings above
python momentum_backtest.py --bars 35040           # 1 year
python momentum_backtest.py --exposure 50 --regime-days 180
```

`momentum_strategy.py` holds the rules and reports a reason per symbol, so it could be wired into a
live bot the same way `strategy.py` is. It deliberately is not wired in yet.

## Recommendation

Do not replace the live mean-reversion bot with this on the strength of a backtest - that is the
mistake the vault's own checklist warns about ("a setup earns size only after journaled evidence
shows positive expectancy after costs"). Two sensible next steps, in order:

1. Close the survivorship and 5-year-wide-universe gaps in limits 2 and 4. If the edge survives
   both, it is real.
2. Run it forward as a **second paper bot** alongside the current one, on its own ledger, and
   compare realized expectancy after a few months. The current bot produces daily data; this one
   would produce weekly data with a real edge. They answer different questions and can run at once.
