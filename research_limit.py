"""Research: the live mean-reversion rules executed with LIMIT orders instead of market orders.

Kraken Pro's entry tier charges 0.40% for maker (resting limit) orders vs 0.80% for taker
(market) orders. A dip-buyer is a natural maker: it can rest a bid at the current price and let
the market come to it. This measures whether that halves the cost problem without fooling itself.

Fill rules (deliberately conservative):
- An approved BUY becomes a limit bid at the decision bar's close. It fills on the NEXT bar only
  if that bar's low trades STRICTLY below the bid (touching it is not a guaranteed fill - you may
  be behind others in the queue). Fill at the bid, maker fee, no slippage. Unfilled bids are
  cancelled; the next bar re-decides from scratch.
- An approved signal SELL becomes a limit offer at the decision close; fills next bar only if the
  high trades strictly above it. Unfilled offers are cancelled and re-decided.
- Take-profit is a resting limit offer: fills when a bar's high strictly exceeds it, maker fee.
- Stop-loss stays a MARKET order: taker fee + slippage, filled at the stop level.
- Stop is checked before take-profit within a bar (same pessimism as backtest.py).

The risk manager's cost gates (minimum reversion edge, signal-exit profit floor) keep the live
taker-cost thresholds by default, so only execution changes; `maker_gates=True` lowers them too
(measured worse on a sample month: it lets smaller reversions through and exits them earlier).

Usage: imported by research_mr_years.py (`limit=1` override).
"""
import dataclasses
import math

from backtest import STARTING_CASH
from models import PositionSnapshot
from risk_manager import evaluate_decisions
from strategy import decide, rolling_mean_std_series, trend_ok_for

MAKER_FEE_PCT = 0.40


def simulate_limit(settings, bars_by_symbol, maker_fee_pct=MAKER_FEE_PCT, maker_gates=False):
    req = max(settings.window, settings.trend_window)
    bars_by_symbol = {s: b for s, b in bars_by_symbol.items() if len(b) >= req + 2}
    closes = {s: [b.close for b in v] for s, v in bars_by_symbol.items()}
    ms = {s: rolling_mean_std_series(c, settings.window) for s, c in closes.items()}
    tm = {s: rolling_mean_std_series(c, settings.trend_window)[0] for s, c in closes.items()}
    # maker_gates=False keeps the live (taker-cost) entry/exit thresholds, so only the execution
    # changes - isolates the fee saving from "the cheaper fee also lets smaller trades through".
    gate = dataclasses.replace(settings, trading_fee_pct=maker_fee_pct, slippage_pct=0.0) if maker_gates else settings
    taker, slip, maker = settings.trading_fee_pct / 100, settings.slippage_pct / 100, maker_fee_pct / 100

    grid = sorted({b.timestamp for v in bars_by_symbol.values() for b in v})
    index_at = {}
    for s, v in bars_by_symbol.items():
        lk = {b.timestamp: i for i, b in enumerate(v)}
        index_at[s] = [lk.get(t) for t in grid]

    cash = STARTING_CASH
    pos = {}
    bids, offers = {}, {}  # symbol -> (limit price, notional) / limit price
    trips, reasons = [], {}
    curve, stamps = [], []
    day, day_eq = None, STARTING_CASH
    entry_days, all_days = set(), set()
    marks = {}
    fills_tried = fills_done = 0

    def close(sym, gross_px, fee_rate, reason):
        nonlocal cash
        p = pos.pop(sym)
        gross = p["qty"] * gross_px
        net = gross * (1 - fee_rate)
        cash += net
        basis = p["notional"] + p["fee"]
        trips.append((net - basis) / basis * 100)
        reasons[reason] = reasons.get(reason, 0) + 1

    for gi, ts in enumerate(grid):
        live = {s: index_at[s][gi] for s in bars_by_symbol
                if index_at[s][gi] is not None and index_at[s][gi] >= req}
        if not live:
            continue
        all_days.add(ts.date())

        # 1) resolve orders placed on the previous bar against THIS bar
        for s in list(bids):
            px, notional = bids.pop(s)
            if s not in live or s in pos:
                continue
            fills_tried += 1
            if bars_by_symbol[s][live[s]].low < px:
                fills_done += 1
                fee = notional * maker
                cash -= notional + fee
                pos[s] = {"qty": notional / px, "entry": px, "notional": notional, "fee": fee,
                          "stop": px * (1 - settings.stop_loss_pct / 100),
                          "target": px * (1 + settings.take_profit_pct / 100)}
                entry_days.add(ts.date())
        for s in list(offers):
            px = offers.pop(s)
            if s in live and s in pos and bars_by_symbol[s][live[s]].high > px:
                close(s, px, maker, "signal_exit")

        # 2) brackets on this bar
        for s in list(pos):
            if s not in live:
                continue
            bar = bars_by_symbol[s][live[s]]
            if bar.low <= pos[s]["stop"]:
                close(s, pos[s]["stop"] * (1 - slip), taker, "stop_loss")
            elif bar.high > pos[s]["target"]:
                close(s, pos[s]["target"], maker, "take_profit")

        # 3) decide at this bar's close, place orders for the next bar
        now = {s: closes[s][i] for s, i in live.items()}
        marks.update(now)
        snaps = {s: PositionSnapshot(s, p["qty"], p["qty"] * marks.get(s, p["entry"]), p["entry"],
                                     (marks.get(s, p["entry"]) - p["entry"]) / p["entry"] * 100)
                 for s, p in pos.items()}
        equity = cash + sum(x.market_value for x in snaps.values())
        if day != ts.date():
            day, day_eq = ts.date(), equity
        decisions = []
        for s, i in live.items():
            m, sd, t = ms[s][0][i], ms[s][1][i], tm[s][i]
            if math.isnan(m) or math.isnan(sd) or math.isnan(t):
                continue
            decisions.append(decide(s, now[s], m, sd, s in snaps, settings.window, settings.entry_zscore,
                                    settings.exit_zscore, trend_ok_for(now[s], t, settings.trend_tolerance_pct)))
        buys, sells, _ = evaluate_decisions(decisions, gate, equity, cash, snaps,
                                            (equity - day_eq) / day_eq * 100 if day_eq else 0.0)
        for sl in sells:
            offers[sl.symbol] = now[sl.symbol]
        for b in buys:
            if b.symbol not in pos:
                bids[b.symbol] = (now[b.symbol], b.notional_usd)

        curve.append(cash + sum(p["qty"] * marks.get(s, p["entry"]) for s, p in pos.items()))
        stamps.append(ts)

    final = curve[-1] if curve else STARTING_CASH
    peak, dd = STARTING_CASH, 0.0
    for e in curve:
        peak = max(peak, e)
        dd = min(dd, (e - peak) / peak * 100)
    wins = [p for p in trips if p > 0]
    days = sorted(all_days)
    longest = cur = 0
    for d in days:
        cur = 0 if d in entry_days else cur + 1
        longest = max(longest, cur)
    first = {s: closes[s][req] for s in closes}
    bh = sum(STARTING_CASH / len(closes) / first[s] * closes[s][-1] for s in closes)
    return {
        "total_return_pct": (final - STARTING_CASH) / STARTING_CASH * 100,
        "max_drawdown_pct": dd,
        "completed_trades": len(trips),
        "win_rate_pct": 100 * len(wins) / len(trips) if trips else None,
        "avg_win_pct": sum(wins) / len(wins) if wins else None,
        "avg_loss_pct": (sum(p for p in trips if p <= 0) / (len(trips) - len(wins))) if len(trips) > len(wins) else None,
        "expectancy_pct": sum(trips) / len(trips) if trips else None,
        "days_with_entry_pct": 100 * len(entry_days) / len(days) if days else 0.0,
        "longest_dry_spell_days": longest,
        "buy_hold_return_pct": (bh - STARTING_CASH) / STARTING_CASH * 100,
        "exit_reasons": reasons,
        "bid_fill_rate_pct": 100 * fills_done / fills_tried if fills_tried else None,
        "equity_curve": curve,
        "equity_timestamps": stamps,
    }
