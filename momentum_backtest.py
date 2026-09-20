"""Backtest the time-series momentum research strategy (momentum_strategy.py) on real 15-minute
crypto bars, charging the same fees and slippage as the live mean-reversion bot.

This does NOT touch live state and is not part of any workflow. It is a measurement tool.

Usage:
    python momentum_backtest.py                       # ~5 years, the settings from the research doc
    python momentum_backtest.py --bars 35040          # ~1 year
    python momentum_backtest.py --lookback-days 42 --rebalance-days 21 --regime-days 180
    python momentum_backtest.py --exposure 50
"""
import argparse

from alpaca.data.historical import CryptoHistoricalDataClient

from backtest import fetch_all_bars
from config import load_settings
from momentum_strategy import BARS_PER_DAY, select

STARTING_CASH = 100_000.0


def simulate(bars_by_symbol, *, lookback, rebalance, regime_window, max_exposure_pct,
             fee_pct, slippage_pct, max_names=None):
    closes = {s: [b.close for b in v] for s, v in bars_by_symbol.items()}
    n = min(len(v) for v in bars_by_symbol.values())
    start = max(lookback, regime_window) + 1
    if n <= start:
        raise SystemExit("Not enough history for these settings - try fewer lookback/regime days.")

    cash = STARTING_CASH
    qty: dict[str, float] = {}
    basis: dict[str, float] = {}
    trade_pls: list[float] = []
    equity_curve: list[float] = []
    trades = 0

    def buy(symbol, spend, price):
        nonlocal cash, trades
        fill = price * (1 + slippage_pct / 100)
        units = spend / fill
        fee = spend * fee_pct / 100
        cash -= spend + fee
        qty[symbol] = qty.get(symbol, 0.0) + units
        basis[symbol] = basis.get(symbol, 0.0) + spend + fee
        trades += 1

    def sell(symbol, units, price):
        nonlocal cash, trades
        fill = price * (1 - slippage_pct / 100)
        gross = units * fill
        net = gross - gross * fee_pct / 100
        cash += net
        fraction = units / qty[symbol]
        realized_basis = basis[symbol] * fraction
        if realized_basis:
            trade_pls.append((net - realized_basis) / realized_basis * 100)
        basis[symbol] -= realized_basis
        qty[symbol] -= units
        if qty[symbol] <= 1e-12:
            qty.pop(symbol); basis.pop(symbol, None)
        trades += 1

    for i in range(start, n):
        if (i - start) % rebalance == 0:
            picks = select(closes, i, lookback, regime_window, max_names=max_names)
            chosen = [p.symbol for p in picks if p.selected]

            for symbol in list(qty):
                if symbol not in chosen:
                    sell(symbol, qty[symbol], closes[symbol][i])

            if chosen:
                equity = cash + sum(q * closes[s][i] for s, q in qty.items())
                target = equity * (max_exposure_pct / 100) / len(chosen)
                for symbol in chosen:
                    held = qty.get(symbol, 0.0) * closes[symbol][i]
                    delta = target - held
                    if delta > 1:
                        spend = min(delta, cash / (1 + fee_pct / 100 + slippage_pct / 100))
                        if spend >= 1:
                            buy(symbol, spend, closes[symbol][i])
                    elif delta < -1 and symbol in qty:
                        units = min(-delta, held) / closes[symbol][i]
                        if 0 < units < qty[symbol]:
                            sell(symbol, units, closes[symbol][i])
        equity_curve.append(cash + sum(q * closes[s][i] for s, q in qty.items()))

    final = equity_curve[-1]
    peak = STARTING_CASH
    max_dd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        max_dd = min(max_dd, (e - peak) / peak * 100)

    per_symbol = STARTING_CASH / len(bars_by_symbol)
    bh_final = sum(per_symbol / closes[s][start] * closes[s][n - 1] for s in bars_by_symbol)

    wins = [p for p in trade_pls if p > 0]
    losses = [p for p in trade_pls if p <= 0]
    days = (n - start) / BARS_PER_DAY
    return {
        "days": days,
        "total_return_pct": (final - STARTING_CASH) / STARTING_CASH * 100,
        "buy_hold_return_pct": (bh_final - STARTING_CASH) / STARTING_CASH * 100,
        "max_drawdown_pct": max_dd,
        "trades": trades,
        "completed_trades": len(trade_pls),
        "trades_per_day": trades / days if days else 0,
        "win_rate_pct": 100 * len(wins) / len(trade_pls) if trade_pls else None,
        "avg_win_pct": sum(wins) / len(wins) if wins else None,
        "avg_loss_pct": sum(losses) / len(losses) if losses else None,
        "expectancy_pct": sum(trade_pls) / len(trade_pls) if trade_pls else None,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bars", type=int, default=175200, help="15-minute bars of history (default ~5 years)")
    p.add_argument("--lookback-days", type=int, default=56)
    p.add_argument("--rebalance-days", type=int, default=14)
    p.add_argument("--regime-days", type=int, default=150, help="0 disables the regime filter")
    p.add_argument("--exposure", type=float, default=30.0, help="max %% of equity invested at once")
    p.add_argument("--max-names", type=int, default=None)
    args = p.parse_args()

    settings = load_settings()
    client = CryptoHistoricalDataClient()  # no keys - Alpaca crypto market data is public
    print(f"Fetching {args.bars} bars for {', '.join(settings.watchlist)}...")
    bars = fetch_all_bars(settings, client, args.bars)

    r = simulate(
        bars,
        lookback=args.lookback_days * BARS_PER_DAY,
        rebalance=args.rebalance_days * BARS_PER_DAY,
        regime_window=args.regime_days * BARS_PER_DAY,
        max_exposure_pct=args.exposure,
        fee_pct=settings.trading_fee_pct,
        slippage_pct=settings.slippage_pct,
        max_names=args.max_names,
    )

    print(f"\nTime-series momentum - lookback {args.lookback_days}d, rebalance every "
          f"{args.rebalance_days}d, regime filter {args.regime_days}d, max exposure {args.exposure:.0f}%")
    print(f"Simulated {r['days']:.0f} days ({r['days'] / 365.25:.1f} years)")
    print(f"Strategy return:      {r['total_return_pct']:+.2f}%")
    print(f"Buy & hold return:    {r['buy_hold_return_pct']:+.2f}%")
    print(f"Worst drawdown:       {r['max_drawdown_pct']:.2f}%")
    print(f"Trades:               {r['trades']} ({r['completed_trades']} completed round trips, "
          f"{r['trades_per_day']:.3f}/day)")
    if r["win_rate_pct"] is not None:
        print(f"Win rate:             {r['win_rate_pct']:.1f}%")
        print(f"Average win:          {r['avg_win_pct']:+.2f}%  (net of fees and slippage)")
        print(f"Average loss:         {r['avg_loss_pct']:+.2f}%  (net of fees and slippage)")
        print(f"Expectancy per trade: {r['expectancy_pct']:+.3f}%")


if __name__ == "__main__":
    main()
