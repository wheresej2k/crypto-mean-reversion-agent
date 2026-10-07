"""Research: run the LIVE mean-reversion rules (backtest.simulate, current config/params.json) over
the longest 15-minute history Alpaca has, including the 2022 crash, and save each run's DAILY
equity curve so research_combo.py can combine it with the daily trend sleeves.

Measurement tool only - touches no live state. Bars are cached in .cache/m15/<coin>.pkl.

Usage:
    WATCHLIST=... python research_mr_history.py
"""
import json
import pickle
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest

from backtest import BAR_TIMEFRAME, simulate
from config import load_settings

CACHE = Path(__file__).parent / ".cache"
START = datetime(2021, 1, 1, tzinfo=timezone.utc)
MAJORS = {"BTC/USD", "ETH/USD", "SOL/USD"}
B = namedtuple("B", "timestamp open high low close volume")


def bars_for(client, symbol):
    path = CACHE / "m15" / (symbol.replace("/", "_") + ".pkl")
    if path.exists():
        return pickle.load(open(path, "rb"))
    req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=BAR_TIMEFRAME, start=START)
    out = [B(b.timestamp, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))
           for b in client.get_crypto_bars(req)[symbol]]
    path.parent.mkdir(parents=True, exist_ok=True)
    pickle.dump(out, open(path, "wb"))
    return out


def daily_curve(r):
    """Last equity of each UTC day."""
    days = {}
    for ts, eq in zip(r["equity_timestamps"], r["equity_curve"]):
        days[ts.date().isoformat()] = eq
    return days


def main():
    settings = load_settings()
    client = CryptoHistoricalDataClient()
    bars = {}
    for s in settings.watchlist:
        bars[s] = bars_for(client, s)
        print(f"{s:10s} {len(bars[s]):7d} bars from {bars[s][0].timestamp.date() if bars[s] else '-'}", flush=True)

    out = {}
    for name, universe in [("all", list(bars)), ("alts", [s for s in bars if s not in MAJORS])]:
        r = simulate(settings, {s: bars[s] for s in universe})
        print(f"\n[{name}] {len(universe)} coins: return {r['total_return_pct']:+.2f}%  "
              f"dd {r['max_drawdown_pct']:.2f}%  trips {r['completed_trades']}  "
              f"win {r['win_rate_pct']:.1f}%  exp {r['expectancy_pct']:+.3f}%  "
              f"days w/ entry {r['days_with_entry_pct']:.1f}%", flush=True)
        out[name] = daily_curve(r)
    json.dump(out, open(CACHE / "mr_curves.json", "w"))


if __name__ == "__main__":
    main()
