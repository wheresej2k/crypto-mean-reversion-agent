"""Sweeps combinations of strategy parameters (rolling window, entry/exit z-score thresholds,
stop-loss/take-profit) through the backtest to find the most profitable combination that still
passes a safety filter, instead of just the most profitable combination outright (which tends to
mean "took on more risk and got lucky").

IMPORTANT CAVEAT: picking whatever scored best on historical data is "curve-fitting" - there's a
real risk of tuning to noise that happened to exist in that stretch of history rather than to
anything that will keep working. Treat the winner here as a hypothesis to try on paper, not a
proven result. As a partial check against this, every combination is tested across three
different historical windows (~3 months, ~1 year, ~5 years - close to the full history Alpaca has
for crypto, which starts 2021-01-01).

"Safe" means, across every tested window:
  - its worst drawdown never exceeded SAFE_MAX_DRAWDOWN_PCT
  - its win rate never fell below MIN_WIN_RATE_PCT
Among only the combinations that pass both, the winner is whichever made the most money on
average (see avg_return) - return is NOT itself a pass/fail gate. This is a direct lesson carried
over from the sibling crypto-trading-agent project (2026-09-14): gating on return/profitability
directly breaks in opposite directions depending on whether the test window happened to be a rally
or a crash -

1. "Never net-unprofitable" fails because a trailing ~1-year window can cover a real, severe
   crypto-wide crash - no long-only strategy can guarantee a positive return while the underlying
   assets collapse that hard.
2. "Never underperform buy-and-hold" fails in the OPPOSITE direction - during a strong rally, any
   strategy with stop-losses that isn't 100% invested at all times will naturally lag a naive
   buy-and-hold. That's the literal cost of having downside protection, not a flaw.

Drawdown and win rate are the numbers that actually answer "could this wreck my account" and are
what the hard safety gate is built from. Return still matters - it's how the winner gets picked
among the safe candidates - it just isn't a second gate that a rally or a crash can arbitrarily
fail on its own.

Deliberate design choice: this sweep only touches STRATEGY parameters (window, entry/exit
z-score, stop-loss/take-profit, confidence threshold) - it never touches max_position_pct,
max_total_exposure_pct, or max_daily_loss_pct. Those three are your risk-tolerance choice, not a
"what wins backtests" question, and an automated process silently raising how much of your
account it's willing to risk - even in paper trading - is exactly the kind of self-modifying-risk
behavior this project's design brief calls out as needing a human decision, not an algorithm. If
you want to change your risk tier, do that deliberately in config/params.json yourself.

Usage:
    python tune.py            # always fetches fresh history from Alpaca
    python tune.py --cache    # reuses a local cache of historical bars if present - much faster
                               # for repeated iteration, but the cache can go stale. Only use this
                               # while actively experimenting; a real tuning decision (or the
                               # monthly auto-retune) should use fresh data.
"""
import argparse
import dataclasses
import pickle
from pathlib import Path

from alpaca.data.historical import CryptoHistoricalDataClient

from backtest import fetch_all_bars, simulate
from config import load_settings

WINDOWS = [12, 24, 48]                # hours - half-day, 1-day, 2-day rolling lookback
ENTRY_ZSCORES = [1.5, 2.0, 2.5]        # how far below the rolling mean counts as "a dip"
EXIT_ZSCORES = [-0.5, 0.0, 0.5]        # how far back up counts as "reverted" (a sell target)
STOP_LOSS_PCTS = [6, 8, 10]            # tighter than a trend-following bot's - a dip that keeps
                                        # falling instead of reverting should be cut fast
TAKE_PROFIT_PCTS = [3, 5, 7]           # modest - reversion back to a recent mean is usually a
                                        # small move, not a sustained trend
MIN_CONFIDENCES = [40, 50, 60]

# The safety bar - a combination must never draw down worse than this, and never have a win rate
# below this, in ANY of the three windows tested, to count as "safe". Return is not gated here -
# see the module docstring for why.
SAFE_MAX_DRAWDOWN_PCT = -32.0
MIN_WIN_RATE_PCT = 35.0

WINDOWS_TO_TEST = [("~3 months", 24 * 90), ("~1 year", 24 * 365), ("~5 years", 24 * 1825)]

CACHE_PATH = Path(__file__).parent / ".cache" / "bars_cache.pkl"


def _load_bars_cache():
    if CACHE_PATH.exists():
        with open(CACHE_PATH, "rb") as f:
            return pickle.load(f)
    return None


def _save_bars_cache(bars_by_symbol):
    CACHE_PATH.parent.mkdir(exist_ok=True)
    with open(CACHE_PATH, "wb") as f:
        pickle.dump(bars_by_symbol, f)


def build_combos():
    return [
        (w, ez, xz, sl, tp, mc)
        for w in WINDOWS
        for ez in ENTRY_ZSCORES
        for xz in EXIT_ZSCORES
        for sl in STOP_LOSS_PCTS
        for tp in TAKE_PROFIT_PCTS
        for mc in MIN_CONFIDENCES
    ]


def sweep(base_settings, data_client, bars_by_symbol=None):
    """Runs every combo across every test window. Returns one row per combo: params plus
    per-window return/drawdown/win-rate/buy-hold-return. `bars_by_symbol` can be passed in
    pre-fetched (the monthly auto-retune workflow does this to fetch history exactly once, and
    main() does this too so it can optionally use the local dev cache).
    """
    if bars_by_symbol is None:
        max_hours = max(hours for _, hours in WINDOWS_TO_TEST)
        print(f"Fetching {max_hours} hours of history once for all combinations...")
        bars_by_symbol = fetch_all_bars(base_settings, data_client, max_hours)

    combos = build_combos()
    print(f"Testing {len(combos)} parameter combinations across {len(WINDOWS_TO_TEST)} time windows "
          f"({len(combos) * len(WINDOWS_TO_TEST)} simulations)...\n")

    results = []
    for w, ez, xz, sl, tp, mc in combos:
        settings = dataclasses.replace(
            base_settings,
            window=w, entry_zscore=ez, exit_zscore=xz,
            stop_loss_pct=sl, take_profit_pct=tp, min_confidence=mc,
        )
        window_returns, window_drawdowns, window_winrates, window_buyhold = {}, {}, {}, {}
        for label, hours in WINDOWS_TO_TEST:
            trimmed_bars = {s: bars[-hours:] for s, bars in bars_by_symbol.items()}
            r = simulate(settings, trimmed_bars)
            window_returns[label] = r["total_return_pct"] if r else None
            window_drawdowns[label] = r["max_drawdown_pct"] if r else None
            window_winrates[label] = r["win_rate_pct"] if r else None
            window_buyhold[label] = r["buy_hold_return_pct"] if r else None

        results.append((w, ez, xz, sl, tp, mc, window_returns, window_drawdowns, window_winrates, window_buyhold))

    return results


def is_safe(row):
    window_drawdowns, window_winrates = row[-3], row[-2]
    drawdowns = [v for v in window_drawdowns.values() if v is not None]
    winrates = list(window_winrates.values())

    if len(drawdowns) < len(WINDOWS_TO_TEST):
        return False
    if any(v is None for v in winrates):
        # A window with zero completed round-trip trades has no meaningful win rate - treat that
        # as insufficient evidence rather than letting it silently pass or fail the bar.
        return False

    return min(drawdowns) >= SAFE_MAX_DRAWDOWN_PCT and min(winrates) >= MIN_WIN_RATE_PCT


def avg_return(row):
    window_returns = row[-4]
    values = [v for v in window_returns.values() if v is not None]
    return sum(values) / len(values) if values else -999


def rank_safe_combos(results):
    safe_results = [r for r in results if is_safe(r)]
    safe_results.sort(key=avg_return, reverse=True)
    return safe_results


def _format_window(window_returns, window_drawdowns, window_winrates, window_buyhold, label):
    wr = window_winrates[label]
    return (
        f"{window_returns[label]:+7.2f}% vs b&h {window_buyhold[label]:+6.1f}% "
        f"(dd {window_drawdowns[label]:5.2f}%, wr " + (f"{wr:.1f}%)" if wr is not None else "n/a)")
    )


def diagnose(results):
    """When nothing passes the safety filter, a bare 'nothing passed' isn't enough to act on -
    this reports which specific criterion (drawdown or win rate) is the bottleneck, and shows the
    closest near-misses (with return/buy-hold shown for context, even though neither gates
    safety), so there's something to actually decide from instead of just a dead end.
    """
    drawdown_ok_count = 0
    winrate_ok_count = 0
    for row in results:
        window_drawdowns, window_winrates = row[-3], row[-2]
        drawdowns = [v for v in window_drawdowns.values() if v is not None]
        winrates = [v for v in window_winrates.values() if v is not None]
        if drawdowns and min(drawdowns) >= SAFE_MAX_DRAWDOWN_PCT:
            drawdown_ok_count += 1
        if len(winrates) == len(WINDOWS_TO_TEST) and all(v is not None for v in winrates) and min(winrates) >= MIN_WIN_RATE_PCT:
            winrate_ok_count += 1

    total = len(results)
    print(f"Of {total} combinations tested:")
    print(f"  {drawdown_ok_count} stayed within the {SAFE_MAX_DRAWDOWN_PCT:.0f}% drawdown limit in every window")
    print(f"  {winrate_ok_count} met the {MIN_WIN_RATE_PCT:.0f}% win-rate floor in every window")
    print("(a combination needs both to count as 'safe' - whichever count above is lowest is the actual bottleneck)\n")

    print("Closest near-misses (best average return regardless of safety, for comparison):")
    header = (
        f"{'win':>4} {'entry_z':>7} {'exit_z':>7} {'stop%':>6} {'tp%':>5} {'minconf':>7}  "
        + "  ".join(f"{label:>32}" for label, _ in WINDOWS_TO_TEST)
    )
    print(header)
    by_return = sorted(results, key=avg_return, reverse=True)
    for w, ez, xz, sl, tp, mc, window_returns, window_drawdowns, window_winrates, window_buyhold in by_return[:10]:
        row = "  ".join(
            _format_window(window_returns, window_drawdowns, window_winrates, window_buyhold, label)
            for label, _ in WINDOWS_TO_TEST
        )
        print(f"{w:>4} {ez:>7} {xz:>7} {sl:>6} {tp:>5} {mc:>7}  {row}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cache", action="store_true",
        help="Reuse a local cache of historical bars instead of re-fetching from Alpaca every "
             "run - much faster while iterating, but the cache can go stale. Don't use this for "
             "a real tuning decision, only while actively experimenting.",
    )
    args = parser.parse_args()

    base_settings = load_settings()
    data_client = CryptoHistoricalDataClient()  # no keys - Alpaca's crypto market data is public

    bars_by_symbol = _load_bars_cache() if args.cache else None
    if bars_by_symbol:
        print(f"Using cached historical bars from {CACHE_PATH} (skip --cache for fresh data).\n")
    else:
        max_hours = max(hours for _, hours in WINDOWS_TO_TEST)
        print(f"Fetching {max_hours} hours of history once for all combinations...")
        bars_by_symbol = fetch_all_bars(base_settings, data_client, max_hours)
        if args.cache:
            _save_bars_cache(bars_by_symbol)

    results = sweep(base_settings, data_client, bars_by_symbol=bars_by_symbol)
    safe_results = rank_safe_combos(results)

    print(f"{len(safe_results)} of {len(results)} combinations passed the safety filter "
          f"(drawdown no worse than {SAFE_MAX_DRAWDOWN_PCT:.0f}%, "
          f"win rate at least {MIN_WIN_RATE_PCT:.0f}% in every window).\n")

    if not safe_results:
        print("No combination passed the safety filter.\n")
        diagnose(results)
        return

    header = (
        f"{'win':>4} {'entry_z':>7} {'exit_z':>7} {'stop%':>6} {'tp%':>5} {'minconf':>7}  "
        + "  ".join(f"{label:>32}" for label, _ in WINDOWS_TO_TEST)
    )
    print(header)
    for w, ez, xz, sl, tp, mc, window_returns, window_drawdowns, window_winrates, window_buyhold in safe_results[:15]:
        row = "  ".join(
            _format_window(window_returns, window_drawdowns, window_winrates, window_buyhold, label)
            for label, _ in WINDOWS_TO_TEST
        )
        print(f"{w:>4} {ez:>7} {xz:>7} {sl:>6} {tp:>5} {mc:>7}  {row}")

    best = safe_results[0]
    w, ez, xz, sl, tp, mc = best[:6]
    print("\nMost profitable combination that still passed the safety filter:")
    print(f"  window={w}h, entry_zscore={ez}, exit_zscore={xz}, stop_loss={sl}%, take_profit={tp}%, min_confidence={mc}")
    print("Running this script never changes anything live on its own - this only updates")
    print("config/params.json if you copy these values in yourself, or approve the automated")
    print("monthly retune workflow's pull request. See the caveat at the top of this file.")


if __name__ == "__main__":
    main()
