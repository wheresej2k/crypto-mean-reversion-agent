"""Free, rule-based trade signal generator: mean reversion, adapted for crypto bars. Trades on
15-minute bars (see kraken_client.BAR_MINUTES) rather than hourly, so trade decisions themselves
happen roughly every 15 minutes, not just a more frequent check on an hourly signal - window/
entry_zscore/exit_zscore in config/params.json are tuned against this same granularity.

Unlike crypto-trading-agent's trend-following crossover (which waits for a sustained move and
often sits out for days), this strategy watches each coin's rolling average price and its typical
recent range (a Bollinger-Band-style approach: a rolling mean and standard deviation over the last
`window` bars). It buys when price drops meaningfully BELOW that range (a "dip", likely to snap
back) and sells once price has reverted back up near/above the average - taking the reversion as
profit rather than waiting for a trend. This naturally generates far more trade opportunities than
trend-following, since "a temporary dip" happens much more often than "a sustained multi-day move".

Trend filter (added 2026-09-15, borrowed from the sibling trend-following bot's own fix for the
same problem): a BUY only fires if price is ALSO above a much longer moving average
(`trend_window`, well beyond the reversion `window`). A "dip" during a real crash just keeps
falling - buying every dip on the way down repeatedly gets stopped out. The trend filter sits the
bot out in cash during a confirmed downtrend instead, which should improve both safety (avoids the
worst dip-buys) and returns (avoids paying the stop-loss repeatedly on a falling knife). It never
blocks a SELL/exit - only new entries, same as the sibling bot's version.

The stop-loss/take-profit levels (see paper_broker.py's locally-simulated ledger - Kraken has no
spot paper-trading sandbox, so this project trades on real Kraken prices against a local virtual
balance instead of real exchange orders) remain the real safety net if a "dip" just keeps falling
instead of reverting - this strategy's own SELL signal is the target case (reversion happened,
take it), the stop-loss is the fallback case (reversion never came).

This module only ever sees data that already passed data_validator.py - data-quality concerns are
handled entirely upstream, so this stays focused on the signal math.

Two entry points share one decision core (decide()), same pattern as the sibling project:
- generate_signals(): live use, one decision per symbol from the latest bars.
- rolling_mean_std_series() + decide(): backtest.py/tune.py precompute the whole rolling
  mean/stddev history for each symbol in one O(n) pass (running sum and running sum-of-squares,
  not "resum the window every bar") so a multi-year 15-minute-bar sweep stays fast despite being
  4x more timesteps than an equivalent hourly sweep.
"""
import math
from dataclasses import dataclass

from models import Bar

# Scaled so roughly a 2.5-standard-deviation move reaches full (100) confidence.
CONFIDENCE_SCALE = 40


@dataclass
class TradeDecision:
    symbol: str
    action: str  # BUY / SELL / HOLD
    size_pct: float
    confidence: float
    reasoning: str
    rolling_mean: float
    zscore: float
    price: float = 0.0
    # How far price would have to travel, as a % of price, to get back to the rolling mean. This
    # is the GROSS reversion the trade is reaching for, before fees and slippage - the number the
    # min_edge_pct risk filter compares against, so a trade whose whole target is smaller than the
    # round-trip cost is never taken.
    edge_pct: float = 0.0


def rolling_mean_std_series(closes: list[float], window: int) -> tuple[list[float], list[float]]:
    """Rolling mean and standard deviation over the whole list, computed in O(n) via a running
    sum and running sum-of-squares instead of resumming the window at every point. Entries before
    `window` values are available are NaN.
    """
    n = len(closes)
    means = [math.nan] * n
    stds = [math.nan] * n
    if n < window:
        return means, stds

    def _stats(total, total_sq, w):
        mean = total / w
        variance = max(0.0, total_sq / w - mean * mean)
        return mean, math.sqrt(variance)

    running_sum = sum(closes[:window])
    running_sumsq = sum(c * c for c in closes[:window])
    means[window - 1], stds[window - 1] = _stats(running_sum, running_sumsq, window)
    for i in range(window, n):
        old, new = closes[i - window], closes[i]
        running_sum += new - old
        running_sumsq += new * new - old * old
        means[i], stds[i] = _stats(running_sum, running_sumsq, window)
    return means, stds


def decide(
    symbol: str,
    price: float,
    mean: float,
    std: float,
    has_position: bool,
    window: int,
    entry_zscore: float,
    exit_zscore: float,
    trend_ok: bool = True,
) -> TradeDecision:
    """`trend_ok` is the long-term trend filter's verdict (price above the trend_window average) -
    defaults to True so existing callers that don't pass it behave exactly as before. It only ever
    blocks a BUY; a SELL/exit fires on the reversion signal alone regardless of trend_ok.
    """
    if std <= 0 or math.isnan(std):
        return TradeDecision(
            symbol, "HOLD", 100.0, 0.0,
            f"not enough price variation in the last {window} bars to compute a reliable signal",
            mean, 0.0, price, 0.0,
        )

    zscore = (price - mean) / std
    confidence = min(100.0, abs(zscore) * CONFIDENCE_SCALE)

    if zscore <= -entry_zscore and not has_position and trend_ok:
        action = "BUY"
        reasoning = (
            f"price (${price:.4f}) is {abs(zscore):.2f} standard deviations below its {window}-bar "
            f"average (${mean:.4f}) - likely oversold, buying the dip"
        )
    elif zscore <= -entry_zscore and not has_position and not trend_ok:
        action = "HOLD"
        confidence = 0.0
        reasoning = (
            f"price is {abs(zscore):.2f} standard deviations below its {window}-bar average - a dip, "
            f"but price is below the long-term trend filter - sitting out a confirmed downtrend"
        )
    elif zscore >= exit_zscore and has_position:
        action = "SELL"
        reasoning = (
            f"price (${price:.4f}) has reverted to {zscore:+.2f} standard deviations vs its "
            f"{window}-bar average (${mean:.4f}) - taking the reversion, exiting position"
        )
    else:
        action = "HOLD"
        reasoning = f"price is {zscore:+.2f} standard deviations from its {window}-bar average - no actionable signal"

    edge_pct = (mean - price) / price * 100 if price else 0.0
    return TradeDecision(symbol, action, 100.0, confidence, reasoning, mean, zscore, price, edge_pct)


def trend_ok_for(price: float, trend_mean: float, trend_tolerance_pct: float = 0.0) -> bool:
    """The long-term trend filter's verdict, with a tolerance band.

    The original filter was a hard `price >= trend_mean`. That turned out to fight the entry rule
    it sits next to: a mean-reversion BUY needs price to be well BELOW its short-window mean, and
    on a 15-minute chart a dip deep enough to trigger an entry has usually also dragged price a
    little under the much slower trend average. The two conditions were close to mutually
    exclusive, so almost no BUY could ever fire (confirmed against 418 live runs: 7 dips were
    blocked by the filter and only 5 BUYs ever executed).

    `trend_tolerance_pct` lets price sit that far below the trend average and still count as
    "not a confirmed downtrend". 0 reproduces the old hard filter exactly; a large value
    effectively disables the filter. The point of the filter - don't keep buying dips all the way
    down a real crash - is preserved, because a genuine crash puts price far further below the
    trend average than the tolerance band allows.
    """
    return price >= trend_mean * (1 - trend_tolerance_pct / 100)


def generate_signals(
    bars_by_symbol: dict[str, list[Bar]],
    positions: dict,
    window: int,
    entry_zscore: float,
    exit_zscore: float,
    trend_window: int,
    trend_tolerance_pct: float = 0.0,
) -> list[TradeDecision]:
    decisions = []
    for symbol, bars in bars_by_symbol.items():
        closes = [b.close for b in bars]
        recent = closes[-window:]
        mean = sum(recent) / window
        variance = max(0.0, sum(c * c for c in recent) / window - mean * mean)
        std = math.sqrt(variance)
        price = closes[-1]
        trend_recent = closes[-trend_window:]
        trend_mean = sum(trend_recent) / trend_window
        trend_ok = trend_ok_for(price, trend_mean, trend_tolerance_pct)
        has_position = symbol in positions and positions[symbol].qty > 0
        decisions.append(decide(symbol, price, mean, std, has_position, window, entry_zscore, exit_zscore, trend_ok))
    return decisions
