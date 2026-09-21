# Strategy review, 2026-09-21 — live-run evidence, a backtest bug, and a wider watchlist

Follow-up to `strategy_review_2026-09-19.md` and `momentum_research_2026-09-19.md`.
Triggered by two asks: review why the completed live runs won or lost, and widen the watchlist
to get closer to trading every day.

Everything below models Kraken's 0.80% taker fee per side + 0.05% slippage, so a round trip
costs ~1.65%. All P/L figures are **net** of both.

---

## 1. What the live runs actually show

559 completed runs, 2,849 log rows, 2026-09-15 → 2026-09-21. Only **14 round trips** closed, and
9 of those are not strategy verdicts (3 `dust_cleanup` at 0.00%, 5 `strategy_switch` flattens from
the momentum experiment). That leaves **6 genuine signal exits**:

| Date | Symbol | Net P/L | Note |
|---|---|---|---|
| 09-16 | ETH | **-2.66%** | old config |
| 09-16 | SOL | **-3.16%** | old config |
| 09-20 | LINK | +1.82% | post-retune |
| 09-21 | ETH | +1.67% | post-retune |
| 09-21 | DOGE | +1.18% | post-retune |
| 09-21 | SOL | +0.99% | post-retune |

Six trades is far too small a sample to conclude anything about profitability, and it is not
treated as one here. But it does show the **payoff shape** the backtest predicted, in live data:

- Winners cluster just above the exit floor: +0.99% to +1.82%. They are **capped by construction** —
  `min_signal_exit_profit_pct` releases the exit the moment the trade clears fees plus 1%, so a
  winner almost never becomes a big winner.
- Losers were 1.5–3x larger than winners, and the mechanism is worse than it looks. Both 09-16
  losses exited on a *reversion signal*: ETH's z-score went from -1.66 at entry to **+4.19** at
  exit while the price fell from $2,490 to $2,424. The z-score reverted because the rolling mean
  collapsed faster than the price did. **"Reverted to the mean" and "made money" are different
  events in a falling market**, and the exit rule only measures the first one.

The skip log over the same period says where the runs went: 469 "no actionable signal", 116
"signal exit below fee-adjusted profit floor", 52 "below the minimum edge".

---

## 2. A bug that had to be fixed before any of this could be measured

`backtest.py` stepped every symbol by **list index**, `for i in range(...)`, indexing each coin's
own bar list at the same position `i`. That assumes every coin has a bar in every 15-minute slot.
They do not — coins have gaps and different listing dates.

Measured on the live five over one year: all five return exactly 35,040 bars, but their **first
timestamps span 27 hours**. So bar `i` was a different calendar moment for each coin, which
corrupts everything portfolio-level (equity marks, exposure caps, the daily-loss breaker, the day
boundary) and makes any "% of days that traded" figure meaningless.

This is the same bug `momentum_trader.aligned_closes()` already fixed on the momentum path on
09-19; it was never fixed on the mean-reversion path. It is now fixed by stepping all symbols on a
shared timestamp grid (union of timestamps, so a short-history coin still contributes on the bars
it has instead of truncating every other coin). **The error grows with watchlist size, so this was
a prerequisite for widening, not a nicety.**

Verified two ways: 36/36 existing tests still pass, and the fixed `backtest.py` now independently
reproduces a separately-written aligned simulator to within 0.7 percentage points
(-20.07% vs -20.79%, identical payoff shape).

> Methodology note: an earlier version of the research harness reported a trailing stop earning
> **+2,340%/yr with +52% average wins**. That was intrabar lookahead — the peak was ratcheted up
> using a bar's high and then that same bar's low was allowed to trigger an exit at
> `peak × (1 - trail)`. Fixed; the trailing stop is in fact clearly *negative* (below). Recorded
> because the failure mode is silent and the result looked like a discovery.

---

## 3. Widening the watchlist

Probed all 622 Kraken USD pairs against Alpaca's free history (Kraken names BTC `XBT` and DOGE
`XDG`, which an earlier pass missed). **32 coins are tradeable on Kraken and have Alpaca backtest
history**; 29 are current and long enough to use. All 29 verified to return live Kraken 15-minute
OHLC.

Widening delivers the calendar coverage that was the goal (1 year, live rules otherwise unchanged):

| Watchlist | Return | Expectancy | Days with a trade | Longest dry spell |
|---|---|---|---|---|
| 5 coins (live today) | -20.79% | -1.38% | **59.0%** | 6 days |
| 10 coins | -13.98% | -0.38% | 84.1% | 3 days |
| 20 coins | -19.06% | -0.64% | 88.9% | 3 days |
| 29 coins | -21.13% | -0.64% | **91.4%** | **3 days** |

It also roughly halves the per-trade bleed, and average win rises from +1.94% to +2.67% — alt
coins simply revert further than BTC/ETH do.

**The current five majors are the worst possible basket for this strategy.** On the same year,
alts-only returned +16.71% where majors returned -16.00%. Mean reversion needs idiosyncratic
noise; BTC and ETH are the most efficiently arbitraged names on the list.

---

## 4. The actual problem, and what fixes it

Baseline diagnosis on the wide universe: avg win **+2.67%**, avg loss **-5.57%**, win rate 60%
→ expectancy **-0.64%**. The average loss is pinned at exactly -5.57% in every single run,
because that is the 4% stop plus 1.65% costs. At a 60% win rate, break-even needs the average
loss better than -4.0%. **The loss side is the whole problem.**

What was tested against it (1 year, 20 coins):

| Change | Result |
|---|---|
| Tighter stop (1.5–3%) | **Much worse** — -66% to -34%. Stops out before reversion happens. |
| Lower take-profit | Worse — caps winners further, loss side untouched. |
| Time stop (0.25–7 days) | Worse at every setting. |
| Trailing stop (1.5–4%) | Worse at every setting — turns reversion into high-turnover chop. |
| Exit z-score | Marginal (+0.03%/trade at most). |
| Volatility-scaled sizing | **Rejected** — cut return from +14.9% to +6.6% without cutting drawdown. The vault's 30–50% drawdown-reduction claim did not transfer. |
| **Wider stop (10–12%)** | **The fix.** See below. |

Widening the stop from 4% to 10% moves expectancy from -0.64% to **+0.34%**, win rate 60% → 81%,
and *reduces* max drawdown from -19.5% to -7.2%. It is counter-intuitive and it survived every
check thrown at it:

- **10 of 10 coin subsets** — live five, majors, alts-only, memes, and six random 8-coin draws.
  Stop-10 beat stop-4 on every one. It is a property of the rule, not of a basket.
- **4 of 4 quarters** in the 1-year window.
- **4 of 4 years** out of sample on 3.5 years of data.
- **24 of 27 neighbouring parameter combinations** have positive expectancy — a plateau, not a spike.

The mechanism is simple: a 4% stop sits inside normal 15-minute noise, so it converts reversions
that would have completed into full-size losses. There is an optimum, not a "remove the stop" —
at a 30%+ stop, capital gets trapped in underwater positions and day coverage collapses to 15%
with a 247-day dry spell.

Two further findings, both consistent across the 1-year and 3.5-year windows:

- **The trend filter is now a net negative.** Turning it off improves every year tested
  (3.5y: -11.38% → +18.71%, expectancy -0.19% → +0.24%). It blocks exactly the deepest dips,
  which are the best mean-reversion entries. Tightening it to 0% tolerance is catastrophic:
  33% day coverage, 17-day dry spells.
- **More concurrent slots.** A wider stop holds positions longer, so the 6 slots
  (12% exposure ÷ 2% per position) fill and block new entries. Moving to 1% × 20% = 20 slots
  lifts expectancy to +0.53% and holds coverage at ~86%.

---

## 5. Out-of-sample result (3.5 years, 14 coins)

| Config | Return | Expectancy | Max DD | Days with a trade |
|---|---|---|---|---|
| Current live (5 coins, stop 4) | **-55.15%** | -1.40% | -56.0% | 64.1% |
| Wide 14, stop 4 | -61.95% | -0.91% | -65.7% | 86.9% |
| Wide 14, stop 10 | -11.38% | -0.19% | -31.4% | 81.0% |
| **Wide 14, stop 10, no trend filter** | **+18.71%** | **+0.24%** | -21.7% | 81.4% |

Buy & hold over the same 3.5 years was +44.83%.

**The honest reading:** these revisions take a strategy that loses roughly 20%/year and make it
roughly break-even to mildly positive (~+5%/yr over 3.5 years), while keeping ~81% of days
active. That is a large improvement and it is robust across years, quarters, and coin subsets.
It is **not** a proven edge: it still trails buy & hold over the full period, and the positive
years are the bear years (it beat buy & hold by 48 points in 2025-03..2026-03 and by 20 points in
2024-03..2025-03, while badly trailing a +92% bull year). Expectancy of +0.24% per round trip
against a 1.65% round-trip cost is a thin margin that could plausibly be a sampling artifact.

Known gaps, not closed: survivorship bias (all 14 coins still exist and were picked in 2026);
the 29-coin universe has only 1 year of history, the 3.5-year check covers 14; and there is no
forward paper record of the revised rules yet.

---

## 6. Recommended change — NOT yet applied

Nothing about the live bot was changed. `trade.yml` still runs `trader.py` with the current
config. Applying this would mean:

| Setting | Now | Proposed |
|---|---|---|
| `WATCHLIST` | 5 coins | 29 coins (all Kraken+Alpaca verified) |
| `stop_loss_pct` | 4 | 10 |
| `trend_tolerance_pct` | 6 | off (or 12) |
| `max_position_pct` | 2 | 1 |
| `max_total_exposure_pct` | 12 | 20 |
| `max_trades_per_run` | 5 | 10 |

Expected: ~90% of days get a trade, longest dry spell ~3 days, roughly break-even rather than
~-20%/yr, max drawdown ~-10% on a 1-year view and ~-22% over 3.5 years.

**Cost of switching:** there are no open positions right now, so unlike the 09-19 momentum
flip-flop (which realized **-$691.75** flattening 5 positions), this change costs nothing to
apply. That will not be true once positions are open again.

Two things worth deciding deliberately rather than by default:

1. **Turning the trend filter off** is the highest-variance item here. It tested better in all
   four years, but its entire purpose was crash protection, and the 3.5-year sample contains no
   2022-style sustained collapse. Setting `trend_tolerance_pct` to 12 instead of off keeps most
   of the gain with some protection retained.
2. **`window` 96 → 384** (24h → 96h) tested better again (3.5y: +22.16%, expectancy +0.44%) but
   cuts day coverage to 67% with 8-day dry spells. It trades activity for edge, which is the
   opposite of the stated goal, so it is not in the proposal above.
