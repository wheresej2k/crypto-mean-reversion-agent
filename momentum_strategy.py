"""Time-series momentum - a RESEARCH strategy, not the live one.

Nothing in this module is wired into trader.py. The live bot still runs the mean-reversion rules in
strategy.py. This exists so the momentum idea can be measured on the same data, under the same
costs, before anyone decides whether to trade it. See docs/momentum_research_2026-09-19.md.

WHY THIS EXISTS
---------------
The mean-reversion strategy has no profitable configuration at Kraken's 0.80%-per-side taker fee
(docs/strategy_review_2026-09-19.md). A round trip costs ~1.65% and the average 15-minute reversion
on these coins is smaller than that, so the edge is consumed by fees before it exists.

The crypto research in the user's vault (Liu & Tsyvinski, "Risks and Returns of Cryptocurrency";
Liu, Tsyvinski & Wu, "Common Risk Factors in Cryptocurrency") finds that **time-series momentum**,
not mean reversion, is the crypto predictor that holds up. Its payoff shape is the inverse: a
minority of trades produce large winners while losers are cut small. That shape survives a 1.65%
round trip, because the winners are not capped.

THE RULES
---------
Every `rebalance` bars (not every bar - turnover is what kills a strategy at these fees):

1. Measure each coin's trailing `lookback`-bar return.
2. Keep the coins whose return is positive AND whose price is above its own `regime_window`
   average. The regime filter is what stops an unstopped momentum book from giving its gains back
   during a crypto bear market.
3. Hold those coins equal-weight up to `max_exposure_pct` of equity. Everything else sits in cash.

There is deliberately **no take-profit and no stop-loss**. The exit is "momentum turned negative at
the next rebalance". Capping winners is precisely what broke the mean-reversion version: it
produced a 58% win rate with -1.42% expectancy per trade.

MEASURED BEHAVIOUR (5 years of 15-minute bars, BTC/ETH/SOL/LINK/DOGE, 0.80% fee + 0.05% slippage,
lookback 56d / rebalance 14d / regime 150d / 30% max exposure):

    return +62.76%   buy & hold +23.16%   max drawdown -29.87%
    127 round trips   win rate 75%   expectancy +22.5% per round trip

All 27 neighbouring parameter combinations tested profitable with positive expectancy, so this is
a plateau rather than a single tuned spike. Read the "Limits" section of the research doc before
trusting it - in particular it trades roughly **once a week, not daily**.
"""
import math
from dataclasses import dataclass

BARS_PER_DAY = 96  # 15-minute bars


@dataclass
class MomentumPick:
    symbol: str
    trailing_return_pct: float
    above_regime: bool
    selected: bool
    reason: str


def trailing_return(closes: list[float], index: int, lookback: int) -> float | None:
    """Return over the last `lookback` bars ending at `index`, as a fraction. None if not enough
    history - callers must treat that as "no opinion", never as zero.
    """
    if index < lookback or index >= len(closes):
        return None
    past = closes[index - lookback]
    if past <= 0:
        return None
    return closes[index] / past - 1


def above_regime_average(closes: list[float], index: int, regime_window: int) -> bool | None:
    """Is price above its own trailing `regime_window` average? None if not enough history.

    `regime_window` of 0 disables the filter (always True), which is supported but measurably
    worse: without it the same settings drew down -63.6% instead of -29.9% over five years.
    """
    if regime_window <= 0:
        return True
    if index < regime_window or index >= len(closes):
        return None
    window = closes[index - regime_window:index]
    return closes[index] >= sum(window) / len(window)


def select(
    closes_by_symbol: dict[str, list[float]],
    index: int,
    lookback: int,
    regime_window: int,
    threshold: float = 0.0,
    max_names: int | None = None,
    bars_per_day: int = 1,
) -> list[MomentumPick]:
    """Decide what to hold at `index`. Returns a pick per symbol, including the rejected ones and
    why, so a live run can log its reasoning the same way the mean-reversion bot did.

    `lookback` and `regime_window` are counts of BARS, whatever size those bars are.
    `bars_per_day` only converts them into days for the human-readable reason strings: the live
    trader runs on daily bars and passes 1, while momentum_backtest.py runs on 15-minute bars and
    passes 96. Getting it wrong mislabels the reasons (a 56-day lookback printed as "1-day") but
    never changes a decision.

    Uses only data at or before `index` - no lookahead.
    """
    lookback_days = lookback / bars_per_day
    regime_days = regime_window / bars_per_day
    picks: list[MomentumPick] = []
    scored: list[tuple[float, str]] = []

    for symbol, closes in closes_by_symbol.items():
        r = trailing_return(closes, index, lookback)
        if r is None:
            picks.append(MomentumPick(symbol, 0.0, False, False, "not enough history yet"))
            continue
        regime = above_regime_average(closes, index, regime_window)
        if regime is None:
            picks.append(MomentumPick(symbol, r * 100, False, False,
                                      "not enough history for the regime filter"))
            continue
        if r <= threshold:
            picks.append(MomentumPick(
                symbol, r * 100, regime, False,
                f"{lookback_days:.0f}-day return {r * 100:+.2f}% is not positive momentum"))
            continue
        if not regime:
            picks.append(MomentumPick(
                symbol, r * 100, False, False,
                f"momentum is {r * 100:+.2f}% but price is below its "
                f"{regime_days:.0f}-day average - sitting out a bear regime"))
            continue
        scored.append((r, symbol))

    scored.sort(reverse=True)
    kept = scored[:max_names] if max_names else scored
    kept_symbols = {s for _, s in kept}

    for r, symbol in scored:
        if symbol in kept_symbols:
            picks.append(MomentumPick(
                symbol, r * 100, True, True,
                f"{lookback_days:.0f}-day return {r * 100:+.2f}% and above its "
                f"{regime_days:.0f}-day average"))
        else:
            picks.append(MomentumPick(symbol, r * 100, True, False,
                                      f"positive momentum but outside the top {max_names}"))
    return picks
