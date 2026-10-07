"""LIVE entry point for the TREND sleeve (trend_strategy.py): Donchian-breakout trend following on
BTC/ETH/SOL, daily bars. Runs right after trader.py (the mean-reversion sleeve) in trade.yml and
shares the same paper ledger, but owns only positions tagged strategy="trend".

It acts at most once per completed daily candle. The external cron fires every 15 minutes, so
95 of every 96 runs see no new daily close and exit immediately without logging anything - low
turnover is the point, since a round trip costs ~1.65%.

Sizing: the sleeve's capital is `trend_sleeve_pct` of total account equity. Each coin's target is
its trend weight x that capital. A holding is only resized when it has drifted more than
`trend_drift_band` (relative) from target; entries and full exits always execute. Those are the
exact rules research_daily.simulate() measured.

No stop-loss or take-profit brackets: the exit is the strategy's own ratcheting channel stop,
evaluated on daily closes. Positions store inert bracket levels that cannot trigger, and
trader.py never reconciles brackets for trend positions.

Usage:
    python trend_trader.py            # live paper run
    python trend_trader.py --dry-run  # decide and print, touch nothing
    python trend_trader.py --force    # act even if this daily close was already handled
"""
import argparse
import sys
import traceback

import kraken_client
from config import load_settings
from paper_broker import TREND, PaperBroker, strategy_of
from trade_log import log_close_event, log_row
from trend_strategy import current_targets

INERT_STOP_PCT = 100.0
INERT_TARGET_PCT = 1_000_000.0
MIN_TRADE_USD = 5.0
DAILY_BARS = 720  # Kraken's per-call cap; the 360-day channel + 90-day vol need 362


def plan_orders(targets, holdings, prices, sleeve_usd, drift_band):
    """Pure sizing logic, separated for testing. Returns a list of (symbol, action, usd, reason)
    with action in BUY (new or add), SELL (partial), CLOSE (full exit)."""
    orders = []
    for t in targets:
        price = prices.get(t.symbol)
        if price is None:
            continue
        cur = holdings.get(t.symbol, 0.0) * price
        want = t.weight * sleeve_usd
        if want <= 0:
            if cur > 0:
                orders.append((t.symbol, "CLOSE", cur, t.reason))
            continue
        if cur == 0:
            if want >= MIN_TRADE_USD:
                orders.append((t.symbol, "BUY", want, t.reason))
        elif (want - cur) / want > drift_band and want - cur >= MIN_TRADE_USD:
            orders.append((t.symbol, "BUY", want - cur, t.reason + f" (topping up from ${cur:,.0f})"))
        elif (cur - want) / want > drift_band and cur - want >= MIN_TRADE_USD:
            orders.append((t.symbol, "SELL", cur - want, t.reason + f" (trimming from ${cur:,.0f})"))
    # sells first so their proceeds fund the buys
    return sorted(orders, key=lambda o: o[1] == "BUY")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    if settings.trend_sleeve_pct <= 0:
        print("Trend sleeve disabled (trend_sleeve_pct is 0).")
        return
    broker = PaperBroker()
    tstate = broker.state.setdefault("trend", {})

    bars = {}
    for s in settings.trend_symbols:
        try:
            bars[s] = kraken_client.get_daily_bars(s, DAILY_BARS)
        except Exception as e:
            print(f"  WARNING: could not fetch daily bars for {s}: {e}")
    if len(bars) != len(settings.trend_symbols):
        # Weights are normalised ACROSS coins, so a partial universe would mis-size everything.
        print("Missing daily data for part of the trend universe - not trading this run.")
        return

    last_close = max(b[-1].timestamp.date() for b in bars.values()).isoformat()
    if tstate.get("last_close_handled") == last_close and not args.force:
        return  # this daily candle was already acted on - the normal case for 95 of 96 runs
    print(f"Trend sleeve: acting on the daily close of {last_close}")

    targets = current_targets(bars, settings.trend_lookbacks)
    prices = {s: kraken_client.get_current_price(s) for s in settings.trend_symbols}
    all_prices = dict(prices)
    for s in broker.state["positions"]:
        if s not in all_prices:
            try:
                all_prices[s] = kraken_client.get_current_price(s)
            except Exception:
                pass  # falls back to entry price inside get_account
    account = broker.get_account(all_prices)
    sleeve_usd = account.equity * settings.trend_sleeve_pct / 100
    holdings = {s: p["qty"] for s, p in broker.state["positions"].items() if strategy_of(p) == TREND}
    blocked = {s for s, p in broker.state["positions"].items() if strategy_of(p) != TREND}

    print(f"Equity ${account.equity:,.2f}  trend sleeve ${sleeve_usd:,.2f} ({settings.trend_sleeve_pct:.0f}%)")
    for t in targets:
        print(f"  {t.symbol:8s} {t.channels_long}/{t.channels_total} long  target {t.weight * 100:5.1f}%  - {t.reason}")

    for symbol, action, usd, reason in plan_orders(targets, holdings, prices, sleeve_usd, settings.trend_drift_band):
        reason = "[trend] " + reason
        price = prices[symbol]
        if symbol in blocked:
            msg = "a mean-reversion position in this coin is still open - waiting for it to close"
            print(f"  SKIP  {symbol:8s} {action} - {msg}")
            if not args.dry_run:
                log_row(symbol, action, "skipped", reasoning=f"[trend] {msg}")
            continue
        if action == "BUY":
            usd = min(usd, broker.state["cash"] / (1 + (settings.trading_fee_pct + settings.slippage_pct) / 100))
            if usd < MIN_TRADE_USD:
                print(f"  SKIP  {symbol:8s} BUY - not enough cash")
                continue
        print(f"  {action:5s} {symbol:8s} ${usd:,.2f} @ ${price:,.4f} - {reason}")
        if args.dry_run:
            continue
        if action == "CLOSE":
            event = broker.close_position(symbol, price, "trend_exit", settings.trading_fee_pct, settings.slippage_pct)
            log_row(symbol, "SELL", "executed", amount=round(usd, 2), reasoning=reason,
                    exit_price=round(event["exit_price"], 6))
            log_close_event(event)
        elif action == "SELL":
            broker.partial_sell(symbol, usd / price, price, settings.trading_fee_pct, settings.slippage_pct)
            log_row(symbol, "SELL", "executed", amount=round(usd, 2), reasoning=reason)
        elif symbol in holdings:
            pos = broker.add_to_position(symbol, usd, price, settings.trading_fee_pct, settings.slippage_pct)
            log_row(symbol, "BUY", "executed", amount=round(usd, 2), reasoning=reason,
                    trade_id=pos["trade_id"], entry_price=round(pos["entry_price"], 6))
        else:
            pos = broker.open_position(symbol, usd, price, INERT_STOP_PCT, INERT_TARGET_PCT,
                                       settings.trading_fee_pct, settings.slippage_pct, strategy=TREND)
            log_row(symbol, "BUY", "executed", amount=round(usd, 2), reasoning=reason,
                    trade_id=pos["trade_id"], entry_price=round(pos["entry_price"], 6))

    if not args.dry_run:
        tstate["last_close_handled"] = last_close
        tstate["targets"] = {t.symbol: {"weight": round(t.weight, 4), "channels_long": t.channels_long,
                                        "reason": t.reason} for t in targets}
        broker.save()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        traceback.print_exc()
        try:
            log_row("SYSTEM", "ERROR", "failed", reasoning=f"[trend] {e}")
        except Exception:
            pass
        sys.exit(1)
