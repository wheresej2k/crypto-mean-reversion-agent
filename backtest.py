"""Backtests the mean-reversion strategy against real historical hourly crypto bars, so you can
see how it would have performed over months/years in a few seconds - instead of waiting for the
live bot to trade one hour at a time. Uses the exact same decision rule (strategy.decide) and the
exact same risk_manager.evaluate_decisions() as the live bot - this reflects the real rules, not
a separate rosier simulation.

It also mirrors the real stop-loss/take-profit mechanism the live bot's paper_broker.py uses: a
simulated position closes at the stop or target price the instant an hourly bar's low/high
crosses it.

Historical data comes from Alpaca's free, public crypto market data (no API key needed - see the
note in kraken_client.py for why backtesting stays on Alpaca while live trading uses Kraken).
This project needs no Alpaca account at all; the data client below is deliberately unauthenticated.

Usage:
    python backtest.py                 # last 8760 hours (~1 year)
    python backtest.py --hours 43800   # ~5 years - close to the full history Alpaca has for crypto
"""
import argparse
from datetime import datetime, timedelta, timezone

import math

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import load_settings
from models import Bar, PositionSnapshot
from risk_manager import evaluate_decisions
from strategy import decide, rolling_mean_std_series

STARTING_CASH = 100_000.0


def fetch_hourly_bars(data_client, symbol, hours):
    start = datetime.now(timezone.utc) - timedelta(hours=int(hours * 1.1) + 48)
    req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Hour, start=start)
    raw = list(data_client.get_crypto_bars(req)[symbol])[-hours:]
    return [
        Bar(b.timestamp, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))
        for b in raw
    ]


def fetch_all_bars(settings, data_client, hours):
    """Fetches history for every watchlist symbol once, so it can be reused across many
    simulate() calls with different settings (e.g. tune.py's parameter sweep) without re-hitting
    the API for every combination.
    """
    bars_by_symbol = {}
    for symbol in settings.watchlist:
        bars = fetch_hourly_bars(data_client, symbol, hours)
        if len(bars) < hours // 2:
            print(f"  WARNING: not enough history for {symbol} ({len(bars)} hours), skipping")
            continue
        bars_by_symbol[symbol] = bars
    if not bars_by_symbol:
        raise SystemExit("No symbols had enough historical data to backtest.")
    return bars_by_symbol


def run_backtest(settings, data_client, hours):
    bars_by_symbol = fetch_all_bars(settings, data_client, hours)
    return simulate(settings, bars_by_symbol)


def simulate(settings, bars_by_symbol):
    required_window = settings.window
    bars_by_symbol = {
        s: bars for s, bars in bars_by_symbol.items() if len(bars) >= required_window + 1
    }
    if not bars_by_symbol:
        return None

    # Precompute the full rolling mean/stddev history once per symbol (see
    # strategy.rolling_mean_std_series) instead of recomputing a window's stats at every
    # timestep - this is what keeps a multi-year hourly sweep in tune.py fast rather than
    # impractically slow.
    closes_by_symbol = {s: [b.close for b in bars] for s, bars in bars_by_symbol.items()}
    mean_std_by_symbol = {s: rolling_mean_std_series(c, settings.window) for s, c in closes_by_symbol.items()}

    num_hours = min(len(b) for b in bars_by_symbol.values())
    start_index = required_window

    cash = STARTING_CASH
    positions: dict[str, dict] = {}
    trade_count = 0
    win_count = 0
    loss_count = 0
    equity_curve = []
    prev_equity = STARTING_CASH

    for i in range(start_index, num_hours):
        # Check every open position's stop-loss/take-profit against this hour's low/high - the
        # exact same trigger condition the two real resting orders use.
        for symbol in list(positions.keys()):
            bar_now = bars_by_symbol[symbol][i]
            pos = positions[symbol]
            if bar_now.low <= pos["stop_price"]:
                exit_price = pos["stop_price"]
            elif bar_now.high >= pos["target_price"]:
                exit_price = pos["target_price"]
            else:
                continue
            cash += pos["qty"] * exit_price
            pl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
            if pl_pct > 0:
                win_count += 1
            else:
                loss_count += 1
            del positions[symbol]
            trade_count += 1

        closes_now = {s: closes_by_symbol[s][i] for s in bars_by_symbol}
        position_snapshots = {}
        for symbol, pos in positions.items():
            price = closes_now[symbol]
            market_value = pos["qty"] * price
            unrealized_plpc = (price - pos["entry_price"]) / pos["entry_price"] * 100
            position_snapshots[symbol] = PositionSnapshot(symbol, pos["qty"], market_value, pos["entry_price"], unrealized_plpc)

        equity = cash + sum(p.market_value for p in position_snapshots.values())
        day_pl_pct = (equity - prev_equity) / prev_equity * 100 if prev_equity else 0.0

        decisions = []
        for symbol in bars_by_symbol:
            mean, std = mean_std_by_symbol[symbol]
            mean_i, std_i = mean[i], std[i]
            if math.isnan(mean_i) or math.isnan(std_i):
                continue
            has_position = symbol in position_snapshots
            decisions.append(decide(
                symbol, closes_now[symbol], mean_i, std_i, has_position,
                settings.window, settings.entry_zscore, settings.exit_zscore,
            ))

        approved_buys, approved_sells, _ = evaluate_decisions(decisions, settings, equity, cash, position_snapshots, day_pl_pct)

        for sell in approved_sells:
            pos = positions.pop(sell.symbol, None)
            if pos:
                sell_qty = min(sell.qty, pos["qty"])
                price = closes_now[sell.symbol]
                cash += sell_qty * price
                pl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
                if pl_pct > 0:
                    win_count += 1
                else:
                    loss_count += 1
                remaining = pos["qty"] - sell_qty
                if remaining > 1e-9:
                    pos["qty"] = remaining
                    positions[sell.symbol] = pos
                trade_count += 1

        for buy in approved_buys:
            # Mirrors trader.py's "one bracket per symbol at a time" rule - don't add to an
            # already-open position instead of merging two stop/target pairs.
            if buy.symbol in positions:
                continue
            price = closes_now[buy.symbol]
            qty = buy.notional_usd / price
            cash -= buy.notional_usd
            positions[buy.symbol] = {
                "qty": qty,
                "entry_price": price,
                "stop_price": price * (1 - settings.stop_loss_pct / 100),
                "target_price": price * (1 + settings.take_profit_pct / 100),
            }
            trade_count += 1

        prev_equity = equity
        equity_curve.append(equity)

    final_equity = equity_curve[-1] if equity_curve else STARTING_CASH
    total_return_pct = (final_equity - STARTING_CASH) / STARTING_CASH * 100

    per_symbol_alloc = STARTING_CASH / len(bars_by_symbol)
    buy_hold_final = sum(
        per_symbol_alloc / closes_by_symbol[s][start_index] * closes_by_symbol[s][num_hours - 1]
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

    return {
        "symbols_used": list(bars_by_symbol.keys()),
        "hours_simulated": num_hours - start_index,
        "starting_equity": STARTING_CASH,
        "final_equity": final_equity,
        "total_return_pct": total_return_pct,
        "buy_hold_return_pct": buy_hold_return_pct,
        "trade_count": trade_count,
        "max_drawdown_pct": max_drawdown_pct,
        "win_rate_pct": win_rate_pct,
        "completed_trades": completed_trades,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=8760, help="How many hourly bars of history to simulate (default: 8760 = ~1 year)")
    args = parser.parse_args()

    settings = load_settings()
    data_client = CryptoHistoricalDataClient()  # no keys - Alpaca's crypto market data is public

    print(f"Backtesting {', '.join(settings.watchlist)} over the last {args.hours} hourly bars (~{args.hours / 24 / 365.25:.1f} years)...\n")
    r = run_backtest(settings, data_client, args.hours)

    print(f"Symbols used: {', '.join(r['symbols_used'])}")
    print(f"Hourly bars simulated: {r['hours_simulated']}")
    print(f"Starting equity: ${r['starting_equity']:,.2f}")
    print(f"Final equity:    ${r['final_equity']:,.2f}")
    print(f"Strategy return:      {r['total_return_pct']:+.2f}%")
    print(f"Buy & hold return:    {r['buy_hold_return_pct']:+.2f}%  (equal-weight watchlist, held the whole period)")
    print(f"Worst drawdown:       {r['max_drawdown_pct']:.2f}%")
    print(f"Total trades executed: {r['trade_count']} ({r['completed_trades']} completed round-trips)")
    if r["win_rate_pct"] is not None:
        print(f"Win rate:             {r['win_rate_pct']:.1f}%")
    else:
        print("Win rate:             n/a (no completed round-trip trades)")


if __name__ == "__main__":
    main()
