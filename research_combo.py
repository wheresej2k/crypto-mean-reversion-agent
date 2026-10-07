"""Research: combine the two sleeves exactly as they share one live account.

- Mean reversion (alts, 15-minute): its own daily equity per year from research_mr_years.py. It
  sizes off total account equity (1% per position, 20% max exposure), so its daily return IS its
  contribution to the account.
- Trend (BTC/ETH/SOL Donchian, daily): research_daily.simulate() on the majors, a fully-invested-
  when-long sleeve curve. Its contribution is trend_sleeve_pct x its daily return.

Account daily return = r_mean_reversion + (trend_sleeve_pct / 100) x r_trend, compounded within
each calendar year. Years are reported separately because each mean-reversion year is an
independent run starting flat.

Usage:
    python research_combo.py
"""
import json
import math
from pathlib import Path

from research_daily import MAJORS, load_panel, simulate
from trend_strategy import Donchian

CACHE = Path(__file__).parent / ".cache"
SPLITS = (0, 30, 40, 50, 60)


def year_stats(rets):
    eq, peak, dd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) if len(rets) > 1 else 0
    return (eq - 1) * 100, dd * 100, (m / sd * math.sqrt(365) if sd else 0.0)


def main():
    mr = json.load(open(CACHE / "mr_years.json"))["alts"]
    dates, panel = load_panel()
    majors = {s: d for s, d in panel.items() if s in MAJORS}
    curve, _ = simulate(dates, majors, Donchian(majors), start_idx=363)
    trend_ret = {dates[i]: curve[i] / curve[i - 1] - 1 for i in range(364, len(dates))}

    print(f"{'year':6s}{'MR alone':>26s}" + "".join(f"{'MR + trend ' + str(p) + '%':>26s}" for p in SPLITS[1:]) + f"{'trend sleeve alone':>24s}")
    totals = {p: 1.0 for p in SPLITS}
    trend_total = 1.0
    for year in sorted(mr):
        daily = mr[year]["daily"]
        days = sorted(daily)
        mr_ret = {days[i]: daily[days[i]] / daily[days[i - 1]] - 1 for i in range(1, len(days))}
        common = [d for d in days[1:] if d in trend_ret]
        if len(common) < 20:
            continue
        cells = []
        for p in SPLITS:
            rets = [mr_ret[d] + p / 100 * trend_ret[d] for d in common]
            ret, dd, sh = year_stats(rets)
            totals[p] *= 1 + ret / 100
            cells.append(f"{ret:+7.1f}% dd {dd:6.1f}% sh {sh:4.1f}")
        tr, tdd, _ = year_stats([trend_ret[d] for d in common])
        trend_total *= 1 + tr / 100
        print(f"{year:6s}" + "".join(f"{c:>26s}" for c in cells) + f"{tr:+10.1f}% dd {tdd:6.1f}%")
    print(f"{'all':6s}" + "".join(f"{(totals[p] - 1) * 100:+25.1f}%" for p in SPLITS) + f"{(trend_total - 1) * 100:+10.1f}%")


if __name__ == "__main__":
    main()
