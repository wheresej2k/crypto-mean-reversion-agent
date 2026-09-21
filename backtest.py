"""Backtests the mean-reversion strategy against real historical 15-minute crypto bars, so you can
see how it would have performed over months/years in a few seconds - instead of waiting for the
live bot to trade one bar at a time. Uses the exact same decision rule (strategy.decide) and the
exact same risk_manager.evaluate_decisions() as the live bot - this reflects the real rules, not
a separate rosier simulation.

It also mirrors the real stop-loss/take-profit mechanism the live bot's paper_broker.py uses: a
simulated position closes at the stop or target price the instant a bar's low/high crosses it.

Historical data comes from Alpaca's free, public crypto market data (no API key needed - see the
note in kraken_client.py for why backtesting stays on Alpaca while live trading uses Kraken).
This project needs no Alpaca account at all; the data client below is deliberately unauthenticated.
Bars are 15 minutes (kraken_client.BAR_MINUTES) so this backtest validates the same granularity
the live bot actually trades on.

Usage:
    python backtest.py                 # last 35040 bars (~1 year of 15-min bars)
    python backtest.py --bars 175200   # ~5 years - close to the full history Alpaca has for crypto
"""
import argparse
from datetime import datetime, timedelta, timezone

import math

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from config import load_settings
from kraken_client import BAR_MINUTES
from models import Bar, PositionSnapshot
from risk_manager import evaluate_decisions
from strategy import decide, rolling_mean_std_series, trend_ok_for

STARTING_CASH = 100_000.0
BAR_TIMEFRAME = TimeFrame(BAR_MINUTES, TimeFrameUnit.Minute)


def fetch_recent_bars(data_client, symbol, bars):
    start = datetime.now(timezone.utc) - timedelta(minutes=int(bars * BAR_MINUTES * 1.1) + 60)
    req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=BAR_TIMEFRAME, start=start)
    raw = list(data_client.get_crypto_bars(req)[symbol])[-bars:]
    return [
        Bar(b.timestamp, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))
        for b in raw
    ]


def fetch_all_bars(settings, data_client, bars):
    """Fetches history for every watchlist symbol once, so it can be reused across many
    simulate() calls with different settings (e.g. tune.py's parameter sweep) without re-hitting
    the API for every combination.
    """
    bars_by_symbol = {}
    for symbol in settings.watchlist:
        b = fetch_recent_bars(data_client, symbol, bars)
        if len(b) < bars // 2:
            print(f"  WARNING: not enough history for {symbol} ({len(b)} bars), skipping")
            continue
        bars_by_symbol[symbol] = b
    if not bars_by_symbol:
        raise SystemExit("No symbols had enough historical data to backtest.")
    return bars_by_symbol


def run_backtest(settings, data_client, bars):
    bars_by_symbol = fetch_all_bars(settings, data_client, bars)
    return simulate(settings, bars_by_symbol)


def simulate(settings, bars_by_symbol, mean_std_by_symbol=None, trend_mean_by_symbol=None):
    """`mean_std_by_symbol`/`trend_mean_by_symbol` can be precomputed and passed in - tune.py's
    sweep() does this, since many combinations share the same window/trend_window value and
    recomputing these O(n) series fresh for every one of 1000+ combinations (rather than once per
    distinct value) was wasted work severe enough to cause a real MemoryError during a 15-minute-
    bar, 5-year sweep. Direct callers (backtest.py's own CLI, a single one-off simulate() call)
    can omit both and get the original recompute-internally behavior.
    """
    required_window = max(settings.window, settings.trend_window)
    bars_by_symbol = {
        s: bars for s, bars in bars_by_symbol.items() if len(bars) >= required_window + 1
    }
    if not bars_by_symbol:
        return None

    closes_by_symbol = {s: [b.close for b in bars] for s, bars in bars_by_symbol.items()}
    if mean_std_by_symbol is None:
        mean_std_by_symbol = {s: rolling_mean_std_series(c, settings.window) for s, c in closes_by_symbol.items()}
    if trend_mean_by_symbol is None:
        trend_mean_by_symbol = {s: rolling_mean_std_series(c, settings.trend_window)[0] for s, c in closes_by_symbol.items()}

    # Step every symbol on a SHARED TIMESTAMP GRID, not by list index.
    #
    # This used to be `for i in range(required_window, min(len(bars)))`, indexing every symbol's
    # own list at the same position i. That silently assumed all symbols have a bar at every
    # 15-minute slot, which is false: coins have gaps (thin liquidity, listing dates), so bar i
    # is a DIFFERENT calendar moment for each coin. Measured on the live five over one year,
    # their bar 0 timestamps spanned 27 hours. That corrupts everything portfolio-level - equity,
    # the exposure caps, the daily-loss circuit breaker, the day boundary - because it marks
    # positions at prices from different moments, and it makes any "% of days that traded"
    # statistic meaningless. The error grows with the size of the watchlist, so widening the
    # watchlist made fixing this a prerequisite, not a nicety.
    #
    # momentum_trader.aligned_closes() already fixed the same bug on the momentum path (it
    # intersects dates); this uses the union of timestamps instead, so a coin with a shorter
    # history or an occasional gap still contributes on the bars it does have rather than
    # truncating every other coin down to its overlap.
    grid = sorted({b.timestamp for bars in bars_by_symbol.values() for b in bars})
    index_at = {}
    for s, bars in bars_by_symbol.items():
        lookup = {b.timestamp: i for i, b in enumerate(bars)}
        index_at[s] = [lookup.get(t) for t in grid]

    cash = STARTING_CASH
    positions: dict[str, dict] = {}
    trade_count = 0
    win_count = 0
    loss_count = 0
    # Per-round-trip results, so the summary can show the PAYOFF SHAPE (average win vs average
    # loss), not just a win rate. A win rate on its own is misleading: an exit rule that caps
    # winners at a small profit floor while letting losers run to a full stop can show a 57% win
    # rate and still have deeply negative expectancy.
    trade_pls: list[float] = []
    exit_reasons: dict[str, int] = {}
    equity_curve = []
    day_start_equity = STARTING_CASH
    day_start_date = None
    fee_pct = getattr(settings, "trading_fee_pct", 0.0)
    slippage_pct = getattr(settings, "slippage_pct", 0.0)

    def buy_fill(price):
        return price * (1 + slippage_pct / 100)

    def sell_fill(price):
        return price * (1 - slippage_pct / 100)

    def fee(notional):
        return notional * fee_pct / 100

    entry_days: set = set()
    all_days: set = set()

    for gi, grid_ts in enumerate(grid):
        # Which symbols actually have a closed bar at this moment, and have enough of their own
        # history behind it for the rolling windows to be defined.
        live = {}
        for s in bars_by_symbol:
            idx = index_at[s][gi]
            if idx is not None and idx >= required_window:
                live[s] = idx
        if not live:
            continue
        all_days.add(grid_ts.date())

        # Check every open position's stop-loss/take-profit against this bar's low/high - the
        # exact same trigger condition paper_broker.py's live reconciliation uses.
        for symbol in list(positions.keys()):
            if symbol not in live:
                continue  # no bar for this coin at this moment - cannot mark or trigger it
            bar_now = bars_by_symbol[symbol][live[symbol]]
            pos = positions[symbol]
            if bar_now.low <= pos["stop_price"]:
                exit_price = sell_fill(pos["stop_price"])
            elif bar_now.high >= pos["target_price"]:
                exit_price = sell_fill(pos["target_price"])
            else:
                continue
            gross_proceeds = pos["qty"] * exit_price
            net_proceeds = gross_proceeds - fee(gross_proceeds)
            cash += net_proceeds
            cost_basis = pos["entry_notional"] + pos["entry_fee"]
            pl_pct = (net_proceeds - cost_basis) / cost_basis * 100 if cost_basis else 0.0
            if pl_pct > 0:
                win_count += 1
            else:
                loss_count += 1
            trade_pls.append(pl_pct)
            reason = "stop_loss" if exit_price <= pos["stop_price"] * (1 + slippage_pct / 100) else "take_profit"
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
            del positions[symbol]
            trade_count += 1

        closes_now = {s: closes_by_symbol[s][idx] for s, idx in live.items()}

        def snapshot_positions():
            snapshots = {}
            for symbol, pos in positions.items():
                # A position in a coin with no bar right now keeps its last known entry-based
                # mark rather than crashing; it is re-marked as soon as that coin prints again.
                price = closes_now.get(symbol, pos["entry_price"])
                market_value = pos["qty"] * price
                unrealized_plpc = (price - pos["entry_price"]) / pos["entry_price"] * 100
                snapshots[symbol] = PositionSnapshot(symbol, pos["qty"], market_value, pos["entry_price"], unrealized_plpc)
            return snapshots

        position_snapshots = snapshot_positions()
        equity = cash + sum(p.market_value for p in position_snapshots.values())
        bar_date = grid_ts.date()
        if day_start_date != bar_date:
            day_start_date = bar_date
            day_start_equity = equity
        day_pl_pct = (equity - day_start_equity) / day_start_equity * 100 if day_start_equity else 0.0

        decisions = []
        for symbol, idx in live.items():
            mean, std = mean_std_by_symbol[symbol]
            mean_i, std_i = mean[idx], std[idx]
            trend_mean_i = trend_mean_by_symbol[symbol][idx]
            if math.isnan(mean_i) or math.isnan(std_i) or math.isnan(trend_mean_i):
                continue
            has_position = symbol in position_snapshots
            trend_ok = trend_ok_for(closes_now[symbol], trend_mean_i, getattr(settings, "trend_tolerance_pct", 0.0))
            decisions.append(decide(
                symbol, closes_now[symbol], mean_i, std_i, has_position,
                settings.window, settings.entry_zscore, settings.exit_zscore, trend_ok,
            ))

        approved_buys, approved_sells, _ = evaluate_decisions(decisions, settings, equity, cash, position_snapshots, day_pl_pct)

        for sell in approved_sells:
            pos = positions.pop(sell.symbol, None)
            if pos:
                sell_qty = min(sell.qty, pos["qty"])
                price = sell_fill(closes_now[sell.symbol])
                gross_proceeds = sell_qty * price
                net_proceeds = gross_proceeds - fee(gross_proceeds)
                cash += net_proceeds
                fraction_sold = sell_qty / pos["qty"] if pos["qty"] else 1.0
                cost_basis = (pos["entry_notional"] + pos["entry_fee"]) * fraction_sold
                pl_pct = (net_proceeds - cost_basis) / cost_basis * 100 if cost_basis else 0.0
                if pl_pct > 0:
                    win_count += 1
                else:
                    loss_count += 1
                trade_pls.append(pl_pct)
                exit_reasons["signal_exit"] = exit_reasons.get("signal_exit", 0) + 1
                remaining = pos["qty"] - sell_qty
                if remaining > 1e-9:
                    pos["entry_notional"] *= 1 - fraction_sold
                    pos["entry_fee"] *= 1 - fraction_sold
                    pos["qty"] = remaining
                    positions[sell.symbol] = pos
                trade_count += 1

        for buy in approved_buys:
            # Mirrors trader.py's "one bracket per symbol at a time" rule - don't add to an
            # already-open position instead of merging two stop/target pairs.
            if buy.symbol in positions:
                continue
            price = buy_fill(closes_now[buy.symbol])
            qty = buy.notional_usd / price
            entry_fee = fee(buy.notional_usd)
            cash -= buy.notional_usd + entry_fee
            positions[buy.symbol] = {
                "qty": qty,
                "entry_price": price,
                "entry_notional": buy.notional_usd,
                "entry_fee": entry_fee,
                "stop_price": price * (1 - settings.stop_loss_pct / 100),
                "target_price": price * (1 + settings.take_profit_pct / 100),
            }
            trade_count += 1
            entry_days.add(bar_date)

        position_snapshots = snapshot_positions()
        equity = cash + sum(p.market_value for p in position_snapshots.values())
        equity_curve.append(equity)

    final_equity = equity_curve[-1] if equity_curve else STARTING_CASH
    total_return_pct = (final_equity - STARTING_CASH) / STARTING_CASH * 100

    per_symbol_alloc = STARTING_CASH / len(bars_by_symbol)
    buy_hold_final = sum(
        per_symbol_alloc / closes_by_symbol[s][required_window] * closes_by_symbol[s][-1]
        for s in bars_by_symbol
    )
    buy_hold_return_pct = (buy_hold_final - STARTING_CASH) / STARTING_CASH * 100

    peak = STARTING_CASH
    max_drawdown_pct = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        max_drawdown_pct = min(max_drawdown_pct, (e - peak) / peak * 100)

    completed_trades = win_count + loss_count
    win_rate_pct = (win_count / completed_trades * 100) if completed_trades else None

    wins = [p for p in trade_pls if p > 0]
    losses = [p for p in trade_pls if p <= 0]
    avg_win_pct = sum(wins) / len(wins) if wins else None
    avg_loss_pct = sum(losses) / len(losses) if losses else None
    expectancy_pct = sum(trade_pls) / len(trade_pls) if trade_pls else None

    # Calendar activity, tracked as a first-class result rather than inferred from trade_count:
    # "does this trade most days" is an explicit objective for this bot, and a pure return
    # ranking will always prefer the configuration that barely trades.
    sorted_days = sorted(all_days)
    longest_dry_spell = current_dry = 0
    for d in sorted_days:
        current_dry = 0 if d in entry_days else current_dry + 1
        longest_dry_spell = max(longest_dry_spell, current_dry)

    return {
        "symbols_used": list(bars_by_symbol.keys()),
        "bars_simulated": len(equity_curve),
        "days_covered": len(sorted_days),
        "days_with_entry_pct": (len(entry_days) / len(sorted_days) * 100) if sorted_days else 0.0,
        "longest_dry_spell_days": longest_dry_spell,
        "starting_equity": STARTING_CASH,
        "final_equity": final_equity,
        "total_return_pct": total_return_pct,
        "buy_hold_return_pct": buy_hold_return_pct,
        "trade_count": trade_count,
        "max_drawdown_pct": max_drawdown_pct,
        "win_rate_pct": win_rate_pct,
        "completed_trades": completed_trades,
        "avg_win_pct": avg_win_pct,
        "avg_loss_pct": avg_loss_pct,
        "expectancy_pct": expectancy_pct,
        "exit_reasons": exit_reasons,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=35040, help="How many 15-minute bars of history to simulate (default: 35040 = ~1 year)")
    args = parser.parse_args()

    settings = load_settings()
    data_client = CryptoHistoricalDataClient()  # no keys - Alpaca's crypto market data is public

    years = args.bars * BAR_MINUTES / 60 / 24 / 365.25
    print(f"Backtesting {', '.join(settings.watchlist)} over the last {args.bars} 15-minute bars (~{years:.1f} years)...\n")
    r = run_backtest(settings, data_client, args.bars)

    print(f"Symbols used: {', '.join(r['symbols_used'])}")
    print(f"15-minute bars simulated: {r['bars_simulated']}")
    print(f"Starting equity: ${r['starting_equity']:,.2f}")
    print(f"Final equity:    ${r['final_equity']:,.2f}")
    print(f"Strategy return:      {r['total_return_pct']:+.2f}%")
    print(f"Buy & hold return:    {r['buy_hold_return_pct']:+.2f}%  (equal-weight watchlist, held the whole period)")
    print(f"Worst drawdown:       {r['max_drawdown_pct']:.2f}%")
    print(f"Total trades executed: {r['trade_count']} ({r['completed_trades']} completed round-trips)")
    if r["win_rate_pct"] is not None:
        print(f"Win rate:             {r['win_rate_pct']:.1f}%")
        print(f"Average win:          {r['avg_win_pct']:+.2f}%  (net of fees and slippage)")
        print(f"Average loss:         {r['avg_loss_pct']:+.2f}%  (net of fees and slippage)")
        print(f"Expectancy per trade: {r['expectancy_pct']:+.3f}%")
        print(f"Exits by reason:      {r['exit_reasons']}")
    else:
        print("Win rate:             n/a (no completed round-trip trades)")
    print(f"Days with an entry:   {r['days_with_entry_pct']:.1f}% of {r['days_covered']} days")
    print(f"Longest dry spell:    {r['longest_dry_spell_days']} days with no entry")


if __name__ == "__main__":
    main()
