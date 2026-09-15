"""ACCURATE pillar: validates market data before the strategy ever sees it, instead of silently
trading on bad data. Three checks:

- staleness: is the most recent bar actually recent, or did the data feed silently stop updating?
- gaps: are there missing hourly bars in the middle of the window (a dropped bar can shift the
  moving averages without anyone noticing)?
- bad values: enough history for the long SMA window, no non-positive/NaN closes, no single-bar
  move so extreme it's more likely a data glitch than a real crypto price move.

A symbol that fails any check is dropped from THIS run only (not the whole bot) and the reason is
logged - trading proceeds normally for whichever symbols still look right, rather than the whole
run aborting because one symbol's feed hiccuped.
"""
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from crypto_broker import Bar

MAX_STALENESS_HOURS = 2.5  # a bit over 2x the hourly run cadence - one missed bar is tolerated
MAX_GAP_HOURS = 2.5
MAX_SINGLE_BAR_MOVE_PCT = 40.0  # a bigger single-hour move than this is more likely bad data


@dataclass
class ValidationIssue:
    symbol: str
    reason: str


def validate(
    bars_by_symbol: dict[str, list[Bar]],
    required_window: int,
    now: datetime | None = None,
) -> tuple[dict[str, list[Bar]], list[ValidationIssue]]:
    """`required_window` is the longest lookback the strategy needs (the largest of
    long_sma_window and trend_window) - not necessarily long_sma_window itself.
    """
    now = now or datetime.now(timezone.utc)
    valid: dict[str, list[Bar]] = {}
    issues: list[ValidationIssue] = []

    for symbol, bars in bars_by_symbol.items():
        if len(bars) < required_window + 1:
            issues.append(ValidationIssue(symbol, f"only {len(bars)} hourly bars, need {required_window + 1}"))
            continue

        latest_age_hours = (now - bars[-1].timestamp).total_seconds() / 3600
        if latest_age_hours > MAX_STALENESS_HOURS:
            issues.append(ValidationIssue(symbol, f"latest bar is {latest_age_hours:.1f}h old (stale data feed)"))
            continue

        gap_hours = max(
            (cur.timestamp - prev.timestamp).total_seconds() / 3600 for prev, cur in zip(bars, bars[1:])
        )
        if gap_hours > MAX_GAP_HOURS:
            issues.append(ValidationIssue(symbol, f"missing bar(s): {gap_hours:.1f}h gap between consecutive bars"))
            continue

        if any(b.close <= 0 or math.isnan(b.close) for b in bars):
            issues.append(ValidationIssue(symbol, "non-positive or NaN close price in recent bars"))
            continue

        worst_move_pct = max(
            abs(cur.close - prev.close) / prev.close * 100 for prev, cur in zip(bars, bars[1:])
        )
        if worst_move_pct > MAX_SINGLE_BAR_MOVE_PCT:
            issues.append(
                ValidationIssue(symbol, f"suspicious {worst_move_pct:.0f}% single-bar move (likely bad data)")
            )
            continue

        valid[symbol] = bars

    return valid, issues
