"""Free, rule-based trade signal generator: mean reversion, adapted for hourly crypto bars.

Unlike crypto-trading-agent's trend-following crossover (which waits for a sustained move and
often sits out for days), this strategy watches each coin's rolling average price and its typical
recent range (a Bollinger-Band-style approach: a rolling mean and standard deviation over
`window` hours). It buys when price drops meaningfully BELOW that range (a "dip", likely to snap
back) and sells once price has reverted back up near/above the average - taking the reversion as
profit rather than waiting for a trend. This naturally generates far more trade opportunities than
trend-following, since "a temporary dip" happens much more often than "a sustained multi-day move".

The stop-loss/take-profit resting orders (see position_tracker.py, unchanged from the sibling
project) remain the real safety net if a "dip" just keeps falling instead of reverting - this
strategy's own SELL signal is the target case (reversion happened, take it), the stop-loss is the
fallback case (reversion never came).

This module only ever sees data that already passed data_validator.py - data-quality concerns are
handled entirely upstream, so this stays focused on the signal math.

Two entry points share one decision core (decide()), same pattern as the sibling project:
- generate_signals(): live use, one decision per symbol from the latest bars.
- rolling_mean_std_series() + decide(): backtest.py/tune.py precompute the whole rolling
  mean/stddev history for each symbol in one O(n) pass (running sum and running sum-of-squares,
  not "resum the window every hour") so a multi-year hourly sweep stays fast.
"""
import math
from dataclasses import dataclass

from crypto_broker import Bar

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
) -> TradeDecision:
    if std <= 0 or math.isnan(std):
        return TradeDecision(
            symbol, "HOLD", 100.0, 0.0,
            f"not enough price variation in the last {window}h to compute a reliable signal",
            mean, 0.0,
        )

    zscore = (price - mean) / std
    confidence = min(100.0, abs(zscore) * CONFIDENCE_SCALE)

    if zscore <= -entry_zscore and not has_position:
        action = "BUY"
        reasoning = (
            f"price (${price:.4f}) is {abs(zscore):.2f} standard deviations below its {window}h "
            f"average (${mean:.4f}) - likely oversold, buying the dip"
        )
    elif zscore >= exit_zscore and has_position:
        action = "SELL"
        reasoning = (
            f"price (${price:.4f}) has reverted to {zscore:+.2f} standard deviations vs its "
            f"{window}h average (${mean:.4f}) - taking the reversion, exiting position"
        )
    else:
        action = "HOLD"
        reasoning = f"price is {zscore:+.2f} standard deviations from its {window}h average - no actionable signal"

    return TradeDecision(symbol, action, 100.0, confidence, reasoning, mean, zscore)


def generate_signals(
    bars_by_symbol: dict[str, list[Bar]],
    positions: dict,
    window: int,
    entry_zscore: float,
    exit_zscore: float,
) -> list[TradeDecision]:
    decisions = []
    for symbol, bars in bars_by_symbol.items():
        closes = [b.close for b in bars]
        recent = closes[-window:]
        mean = sum(recent) / window
        variance = max(0.0, sum(c * c for c in recent) / window - mean * mean)
        std = math.sqrt(variance)
        price = closes[-1]
        has_position = symbol in positions and positions[symbol].qty > 0
        decisions.append(decide(symbol, price, mean, std, has_position, window, entry_zscore, exit_zscore))
    return decisions
