"""Trend sleeve: a long-only Donchian-breakout ensemble on the major coins (daily bars).

This is one of the bot's two strategy sleeves (see docs/multi_strategy_2026-10-07.md). Mean
reversion (strategy.py) trades the altcoins on 15-minute bars and does its best work in choppy or
falling markets; this sleeve trades BTC/ETH/SOL on daily bars and does its work in rising ones.
When no major is trending it holds cash, which is a decision, not a failure.

THE RULES (Zarattini, Pagani & Barbon 2025, "Catching Crypto Trends", minus the 5/10-day channels)
--------------------------------------------------------------------------------------------------
For each lookback n in LOOKBACKS, independently per coin:
  - ENTER when today's close is above the highest high of the previous n days.
  - The stop is the midpoint of the previous n days' high/low channel. While long it only ever
    ratchets UP, never down.
  - EXIT when the close falls below that stop.
A coin's signal is the fraction of lookbacks currently long (0 to 1). Its weight is signal x (1 /
90-day realized vol), normalised across all eligible coins - so a calm coin gets more capital than
a wild one, the sleeve is fully invested only when every channel on every coin is long, and it is
in cash when none is.

The 5 and 10-day channels from the paper were dropped because at 1.65% a round trip their
whipsaw turnover costs more than they earn (measured: +24% with them vs +51% without, 2021-12 to
2026-10). That was the only change from the published design.

The ratcheting stops make this path-dependent, so live runs REPLAY the whole fetched history every
time rather than storing stop levels. That keeps it stateless (a missed run can't corrupt it), and
makes the live answer identical to the backtest's by construction - research_daily.py imports this
same class.
"""
import math
from dataclasses import dataclass

LOOKBACKS = (20, 30, 60, 90, 150, 250, 360)
VOL_DAYS = 90


def history(series, t, n):
    """The n values ending at t inclusive, or None if any is missing."""
    if t - n + 1 < 0:
        return None
    w = series[t - n + 1:t + 1]
    return None if any(v is None for v in w) else w


def realized_vol(closes, t, n=VOL_DAYS):
    w = history(closes, t, n + 1)
    if w is None:
        return None
    rets = [math.log(w[i] / w[i - 1]) for i in range(1, len(w))]
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(365)


def panel_from_bars(bars_by_symbol):
    """Align per-symbol daily bars on the union of their dates. Returns (dates, panel) where
    panel[symbol] = {"high", "low", "close"} lists holding None on days that coin has no bar."""
    dates = sorted({b.timestamp.date() for bars in bars_by_symbol.values() for b in bars})
    pos = {d: i for i, d in enumerate(dates)}
    panel = {}
    for sym, bars in bars_by_symbol.items():
        h, l, c = ([None] * len(dates) for _ in range(3))
        for b in bars:
            i = pos[b.timestamp.date()]
            h[i], l[i], c[i] = b.high, b.low, b.close
        panel[sym] = {"high": h, "low": l, "close": c}
    return dates, panel


class Donchian:
    """Stateful: call weights(t) for t = 0, 1, 2, ... in order (the stops ratchet day by day)."""

    def __init__(self, panel, lookbacks=LOOKBACKS, vol_n=VOL_DAYS):
        self.p, self.lbs, self.vol_n = panel, tuple(lookbacks), vol_n
        self.need = max(max(self.lbs), vol_n) + 2
        self.state = {s: {n: None for n in self.lbs} for s in panel}  # None = flat, else stop
        self.signal = {}

    def weights(self, t):
        sig, inv = {}, {}
        for s, d in self.p.items():
            if history(d["close"], t, self.need) is None:
                continue
            c = d["close"][t]
            on = 0
            for n in self.lbs:
                hh = max(d["high"][t - n:t])
                ll = min(d["low"][t - n:t])
                mid = (hh + ll) / 2
                st = self.state[s][n]
                if st is None:
                    if c > hh:
                        self.state[s][n] = mid
                else:
                    st = max(st, mid)
                    self.state[s][n] = None if c < st else st
                on += self.state[s][n] is not None
            vol = realized_vol(d["close"], t, self.vol_n)
            if not vol:
                continue
            inv[s] = 1 / vol
            sig[s] = on / len(self.lbs)
        self.signal = sig
        total_inv = sum(inv.values())
        return {s: sig[s] * inv[s] / total_inv for s in sig if sig[s] > 0} if total_inv else {}


@dataclass
class TrendTarget:
    symbol: str
    weight: float          # fraction of the trend sleeve's capital
    channels_long: int
    channels_total: int
    reason: str


def current_targets(bars_by_symbol, lookbacks=LOOKBACKS, vol_n=VOL_DAYS) -> list[TrendTarget]:
    """Replay the full daily history and return today's target for every symbol."""
    dates, panel = panel_from_bars(bars_by_symbol)
    strat = Donchian(panel, lookbacks, vol_n)
    w = {}
    for t in range(len(dates)):
        w = strat.weights(t)
    out = []
    for s in bars_by_symbol:
        if s not in strat.signal:
            out.append(TrendTarget(s, 0.0, 0, len(strat.lbs),
                                   f"not enough daily history yet (needs {strat.need} days)"))
            continue
        on = round(strat.signal[s] * len(strat.lbs))
        weight = w.get(s, 0.0)
        if on:
            reason = f"{on} of {len(strat.lbs)} breakout channels long - trend up, target {weight * 100:.1f}% of sleeve"
        else:
            reason = f"0 of {len(strat.lbs)} breakout channels long - no uptrend, holding cash"
        out.append(TrendTarget(s, weight, on, len(strat.lbs), reason))
    return out
