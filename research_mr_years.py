"""Research: the LIVE mean-reversion rules (backtest.simulate + config/params.json), run one
calendar year at a time over the cached 15-minute history (research_mr_history.py fills
.cache/m15/), in parallel processes. Each year starts flat with fresh capital, so the years are
independent out-of-sample checks rather than one compounding path.

Writes .cache/mr_years.json: {universe: {year: {"stats": ..., "daily": {date: equity}}}}.

Usage:
    WATCHLIST=... python research_mr_years.py
"""
import dataclasses
import json
import pickle
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtest import simulate
from config import load_settings
from research_mr_history import B

CACHE = Path(__file__).parent / ".cache"
MAJORS = {"BTC/USD", "ETH/USD", "SOL/USD"}
YEARS = [2021, 2022, 2023, 2024, 2025, 2026]


class _Unpickler(pickle.Unpickler):
    # Cache files were written both by research_mr_history.py run as a script (bar class
    # recorded as __main__.B) and by helpers importing it (research_mr_history.B). Same shape.
    def find_class(self, module, name):
        return B if name == "B" else super().find_class(module, name)


def load(symbol):
    with open(CACHE / "m15" / (symbol.replace("/", "_") + ".pkl"), "rb") as f:
        return _Unpickler(f).load()


def run_year(args):
    universe_name, symbols, year, overrides = args
    settings = dataclasses.replace(load_settings(), **overrides)
    warm = timedelta(minutes=15 * (max(settings.window, settings.trend_window) + 5))
    lo = datetime(year, 1, 1, tzinfo=timezone.utc)
    hi = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    bars = {}
    for s in symbols:
        b = [x for x in load(s) if lo - warm <= x.timestamp < hi]
        if len(b) > 2000:  # at least ~3 weeks in this year
            bars[s] = b
    if not bars:
        return universe_name, year, None
    r = simulate(settings, bars)
    daily = {}
    for ts, eq in zip(r["equity_timestamps"], r["equity_curve"]):
        if ts >= lo:
            daily[ts.date().isoformat()] = eq
    keep = ("total_return_pct", "max_drawdown_pct", "completed_trades", "win_rate_pct",
            "avg_win_pct", "avg_loss_pct", "expectancy_pct", "days_with_entry_pct",
            "longest_dry_spell_days", "buy_hold_return_pct")
    stats = {k: r[k] for k in keep}
    stats["coins"] = len(bars)
    return universe_name, year, {"stats": stats, "daily": daily}


def main():
    settings = load_settings()
    syms = [s for s in settings.watchlist if (CACHE / "m15" / (s.replace("/", "_") + ".pkl")).exists()]
    # Live mean reversion trades only the altcoins (the majors belong to the trend sleeve).
    universes = {"alts": [s for s in syms if s not in MAJORS]}
    # Optional overrides, e.g. `trend_tolerance_pct=12`; output file gets a matching suffix.
    overrides = {k: float(v) for k, v in (a.split("=") for a in sys.argv[1:])}
    jobs = [(u, s, y, overrides) for u, s in universes.items() for y in YEARS]
    out = {u: {} for u in universes}
    with ProcessPoolExecutor(max_workers=3) as ex:
        for u, y, res in ex.map(run_year, jobs):
            if res is None:
                continue
            out[u][str(y)] = res
            st = res["stats"]
            print(f"[{u:4s}] {y}  {st['coins']:2d} coins  return {st['total_return_pct']:+7.2f}%  "
                  f"dd {st['max_drawdown_pct']:6.2f}%  trips {st['completed_trades']:5d}  "
                  f"win {st['win_rate_pct'] or 0:5.1f}%  exp {st['expectancy_pct'] or 0:+.3f}%  "
                  f"days {st['days_with_entry_pct']:5.1f}%  b&h {st['buy_hold_return_pct']:+7.2f}%", flush=True)
    suffix = "".join(f"_{k}{v:g}" for k, v in overrides.items())
    json.dump(out, open(CACHE / f"mr_years{suffix}.json", "w"))


if __name__ == "__main__":
    main()
