"""Research harness for DAILY-bar strategy sleeves - a measurement tool, not part of any workflow.

Every candidate strategy is a function that, given the data up to and including day t, returns a
target weight per coin (fraction of the sleeve's own equity, long-only, sum <= 1). simulate() then
trades toward those weights at day t's close, charging the live bot's real costs (0.80% fee per
side + 0.05% slippage, ~1.65% round trip) on every dollar traded.

No lookahead: a strategy only ever sees rows [0..t]. A coin is only tradeable once it has enough of
its own history for the strategy's longest window, so late listings enter on their own schedule.

Data: Alpaca's free public daily crypto bars, cached in .cache/daily.json (2021-01-01 onward for the
oldest coins). Survivorship caveat: the 32 coins were chosen in 2026, so coins that died since 2021
are absent. That biases every result here upward, roughly equally across strategies.

Usage:
    python research_daily.py              # run every strategy, per-year table
"""
import json
import math
from pathlib import Path

from trend_strategy import LOOKBACKS, Donchian, history, realized_vol  # noqa: F401 - one implementation, live and research

CACHE = Path(__file__).parent / ".cache" / "daily.json"
FEE_PCT = 0.80
SLIPPAGE_PCT = 0.05
MAJORS = ("BTC/USD", "ETH/USD", "SOL/USD")


# ---------------------------------------------------------------- data

def load_panel(path: Path = CACHE):
    """Returns (dates, panel) where panel[symbol] = dict of aligned lists (None where the coin had
    no bar that day). Aligned on the union of dates - see backtest.py on why index alignment
    matters."""
    raw = json.load(open(path))
    dates = sorted({row[0] for rows in raw.values() for row in rows})
    pos = {d: i for i, d in enumerate(dates)}
    panel = {}
    for sym, rows in raw.items():
        o, h, l, c = ([None] * len(dates) for _ in range(4))
        for d, op, hi, lo, cl, _v in rows:
            i = pos[d]
            o[i], h[i], l[i], c[i] = op, hi, lo, cl
        panel[sym] = {"open": o, "high": h, "low": l, "close": c, "first": pos[rows[0][0]]}
    return dates, panel


# ---------------------------------------------------------------- strategies
# Each strategy is a class with weights(t) -> {symbol: weight}. They may keep internal state
# (e.g. Donchian's ratcheting stops), so a fresh instance is built per simulation.

class TSMOM:
    """Time-series momentum, as researched 2026-09-19 (momentum_strategy.py): every `rebalance`
    days hold, equal-weight, each coin whose `lookback`-day return is positive and whose price is
    above its `regime`-day average. Between rebalances, keep the previous targets."""

    def __init__(self, panel, lookback=56, rebalance=14, regime=150):
        self.p, self.lb, self.rb, self.rg = panel, lookback, rebalance, regime
        self.need = max(lookback, regime) + 1
        self.last = {}
        self.counter = 0

    def weights(self, t):
        if self.counter % self.rb == 0:
            picks = []
            for s, d in self.p.items():
                c = d["close"]
                w = history(c, t, self.need)
                if w is None:
                    continue
                if w[-1] / w[-1 - self.lb] - 1 > 0 and w[-1] >= sum(w[-1 - self.rg:-1]) / self.rg:
                    picks.append(s)
            self.last = {s: 1 / len(picks) for s in picks} if picks else {}
        self.counter += 1
        return self.last


class RSI2Pullback:
    """Connors-style short-term pullback inside a long-term uptrend: only coins above their
    `trend`-day average are eligible; buy when 2-day RSI drops below `entry_rsi`; exit when the
    close recovers above its `exit_ma`-day average, or falls out of the uptrend. Equal slots of
    1/`slots` of the sleeve."""

    def __init__(self, panel, trend=200, entry_rsi=10, exit_ma=5, slots=10):
        self.p, self.trend, self.er, self.xm, self.slots = panel, trend, entry_rsi, exit_ma, slots
        self.held = set()

    @staticmethod
    def rsi2(closes, t):
        w = history(closes, t, 3)
        if w is None:
            return None
        g = [max(0, w[i] - w[i - 1]) for i in (1, 2)]
        l = [max(0, w[i - 1] - w[i]) for i in (1, 2)]
        if sum(l) == 0:
            return 100.0
        return 100 - 100 / (1 + sum(g) / sum(l))

    def weights(self, t):
        cands = []
        for s, d in self.p.items():
            c = d["close"]
            w = history(c, t, self.trend)
            if w is None:
                self.held.discard(s)
                continue
            px, sma = w[-1], sum(w) / len(w)
            if s in self.held:
                if px > sum(w[-self.xm:]) / self.xm or px < sma:
                    self.held.discard(s)
            elif px > sma:
                r = self.rsi2(c, t)
                if r is not None and r < self.er:
                    cands.append((r, s))
        for _, s in sorted(cands):
            if len(self.held) < self.slots:
                self.held.add(s)
        return {s: 1 / self.slots for s in self.held}


# ---------------------------------------------------------------- simulator

def simulate(dates, panel, strategy, drift_band=0.25, start_idx=0):
    """Daily simulation of one sleeve starting with 1.0 of equity. Trades toward target weights at
    the close, but only resizes an existing holding when it has drifted more than `drift_band`
    (relative) from target - turnover control, which matters enormously at 1.65% a round trip.
    Entries and full exits always execute. Returns the daily equity curve and round-trip P/Ls."""
    cost = (FEE_PCT + SLIPPAGE_PCT) / 100
    cash, units, basis = 1.0, {}, {}
    curve, trips = [], []
    for t in range(len(dates)):
        target = strategy.weights(t)
        if t < start_idx:
            curve.append(1.0)
            continue
        px = {s: d["close"][t] for s, d in panel.items() if d["close"][t] is not None}
        equity = cash + sum(u * px.get(s, 0) for s, u in units.items())
        # sells first (frees cash), then buys
        for s in list(units):
            if s not in px:
                continue
            tw = target.get(s, 0.0)
            cur = units[s] * px[s]
            want = tw * equity
            if tw == 0 or (cur - want) / max(want, 1e-12) > drift_band:
                sell_val = cur if tw == 0 else cur - want
                frac = sell_val / cur
                proceeds = sell_val * (1 - cost)
                cash += proceeds
                if tw == 0:
                    trips.append(proceeds / basis[s] - 1)
                    units.pop(s); basis.pop(s)
                else:
                    units[s] *= 1 - frac
                    basis[s] *= 1 - frac
        for s, tw in sorted(target.items(), key=lambda kv: -kv[1]):
            if s not in px or tw <= 0:
                continue
            cur = units.get(s, 0.0) * px[s]
            want = tw * equity
            if cur == 0 or (want - cur) / want > drift_band:
                spend = min(want - cur, cash / (1 + cost))
                if spend <= 1e-9:
                    continue
                cash -= spend * (1 + cost)
                units[s] = units.get(s, 0.0) + spend / px[s]
                basis[s] = basis.get(s, 0.0) + spend * (1 + cost)
        curve.append(cash + sum(u * px.get(s, 0) for s, u in units.items()))
    return curve, trips


def stats(curve, dates, i0=0, i1=None):
    i1 = len(curve) - 1 if i1 is None else i1
    seg = curve[i0:i1 + 1]
    rets = [seg[i] / seg[i - 1] - 1 for i in range(1, len(seg))]
    total = seg[-1] / seg[0] - 1
    peak, dd = seg[0], 0.0
    for e in seg:
        peak = max(peak, e)
        dd = min(dd, e / peak - 1)
    m = sum(rets) / len(rets) if rets else 0
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) if len(rets) > 1 else 0
    sharpe = m / sd * math.sqrt(365) if sd else 0
    return {"ret": total * 100, "dd": dd * 100, "sharpe": sharpe}


def year_slices(dates, start_idx):
    out, cur = [], None
    for i in range(start_idx, len(dates)):
        y = dates[i][:4]
        if y != cur:
            if out:
                out[-1][2] = i - 1
            out.append([y, i, None])
            cur = y
    out[-1][2] = len(dates) - 1
    for o in out:
        if o[1] > start_idx:
            o[1] -= 1  # start from previous close so each year's return is close-to-close
    return out


def buy_hold_curve(dates, panel, start_idx):
    """Equal-weight all coins available at each point, rebalanced daily, no costs - the market."""
    curve = [1.0]
    for t in range(start_idx + 1, len(dates)):
        rets = [d["close"][t] / d["close"][t - 1] - 1 for d in panel.values()
                if d["close"][t] is not None and d["close"][t - 1] is not None]
        curve.append(curve[-1] * (1 + (sum(rets) / len(rets) if rets else 0)))
    return [1.0] * start_idx + curve


def report(name, curve, trips, dates, start_idx):
    full = stats(curve, dates, start_idx)
    wins = [p for p in trips if p > 0]
    line = f"{name:28s} total {full['ret']:+8.1f}%  dd {full['dd']:6.1f}%  sharpe {full['sharpe']:5.2f}"
    if trips:
        line += (f"  trips {len(trips):4d}  win {100 * len(wins) / len(trips):4.0f}%"
                 f"  exp {100 * sum(trips) / len(trips):+6.2f}%")
    print(line)
    print("   by year: " + "  ".join(
        f"{y} {stats(curve, dates, a, b)['ret']:+6.1f}%" for y, a, b in year_slices(dates, start_idx)))


def main():
    dates, panel = load_panel()
    start = 361 + 2  # the longest warm-up (Donchian's 360-day channel) - same start for all
    print(f"Daily sleeves, {dates[start]} -> {dates[-1]}, costs {2 * FEE_PCT + SLIPPAGE_PCT:.2f}% "
          f"round trip\n")
    bh = buy_hold_curve(dates, panel, start)
    report("Buy & hold (equal weight)", bh, [], dates, start)
    for name, strat in [
        ("TSMOM 56/14/150", TSMOM(panel)),
        ("Donchian ensemble (paper)", Donchian(panel, lookbacks=(5, 10) + LOOKBACKS)),
        ("Donchian, no 5/10-day", Donchian(panel)),
        ("RSI2 pullback in uptrend", RSI2Pullback(panel)),
    ]:
        curve, trips = simulate(dates, panel, strat, start_idx=start)
        report(name, curve, trips, dates, start)

    # The trend sleeve as deployed: majors only. Trend following on the altcoins does not hold up
    # (they bleed against BTC); on the majors it does, which matches the literature.
    majors = {s: d for s, d in panel.items() if s in MAJORS}
    print()
    report("Buy & hold majors", buy_hold_curve(dates, majors, start), [], dates, start)
    for name, strat in [
        ("TSMOM majors", TSMOM(majors)),
        ("Donchian majors (LIVE sleeve)", Donchian(majors)),
    ]:
        curve, trips = simulate(dates, majors, strat, start_idx=start)
        report(name, curve, trips, dates, start)


if __name__ == "__main__":
    main()
