"""Entry point. Run this to do one trading pass: fetch live Kraken hourly bars -> validate them ->
reconcile any open position's stop-loss/take-profit against newly seen bars -> generate signals
(free, rule-based mean reversion) -> apply risk limits -> "place" trades in the local paper ledger
(paper_broker.py). Crypto trades 24/7, so there's no market-hours gate to check.

Unlike the Alpaca-based sibling bots, nothing here ever touches a real exchange order - see
paper_broker.py's module docstring for why (Kraken has no spot paper-trading sandbox) and for the
real reliability tradeoff this introduces (stop-loss/take-profit are only checked once per run,
not continuously by a resting exchange order).

Usage:
    python trader.py            # live paper-trading run (against real Kraken prices, simulated fills)
    python trader.py --dry-run  # do everything except actually touching saved state
"""
import argparse
import sys
import traceback

import kraken_client
from config import load_settings
from data_validator import validate
from heartbeat import record_success
from paper_broker import PaperBroker
from risk_manager import SkippedDecision, evaluate_decisions
from strategy import generate_signals
from trade_log import log_close_event, log_row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Don't actually touch saved state")
    args = parser.parse_args()

    settings = load_settings()
    broker = PaperBroker()

    # --- ACCURATE pillar: fetch, then validate, hourly market data before trusting it ---
    print(f"Fetching hourly bars from Kraken for: {', '.join(settings.watchlist)}")
    raw_bars = {}
    for symbol in settings.watchlist:
        try:
            raw_bars[symbol] = kraken_client.get_recent_bars(symbol)
        except Exception as e:
            print(f"  WARNING: could not fetch data for {symbol}: {e}")
            log_row(symbol, "DATA", "failed", reasoning=f"fetch failed: {e}")

    valid_bars, issues = validate(raw_bars, settings.window)
    for issue in issues:
        print(f"  DATA SKIP {issue.symbol:10s} - {issue.reason}")
        log_row(issue.symbol, "DATA", "skipped", reasoning=issue.reason)

    # --- Reconcile: did a stop-loss or take-profit level get crossed since the last run? ---
    open_symbols = list(broker.state["positions"].keys())
    for symbol in open_symbols:
        bars = valid_bars.get(symbol)
        if not bars:
            continue  # this symbol's data didn't pass validation this run - can't safely check it
        event = broker.check_stop_target_hits(symbol, bars)
        if event:
            print(f"  CLOSED {event['symbol']:10s} {event['exit_reason']:12s} P/L {event['pl_pct']:+.2f}%")
            if not args.dry_run:
                log_close_event(event)

    if not valid_bars:
        print("No symbols passed data validation this run - nothing to trade.")
        if not args.dry_run:
            broker.save()
            record_success({"equity": broker.get_account({}).equity, "trades_executed": 0, "note": "no valid data this run"})
        return

    latest_prices = {symbol: bars[-1].close for symbol, bars in valid_bars.items()}
    account = broker.get_account(latest_prices)
    positions = broker.get_positions(latest_prices)
    print(f"Equity: ${account.equity:.2f}  Cash: ${account.cash:.2f}  Day P/L: {account.day_pl_pct:.2f}%")
    print(f"Open positions: {list(positions.keys()) or 'none'}")

    # --- Generate signals, apply risk limits ---
    print("Generating signals (mean reversion, hourly bars from Kraken)...")
    decisions = generate_signals(valid_bars, positions, settings.window, settings.entry_zscore, settings.exit_zscore)
    decisions_by_symbol = {d.symbol: d for d in decisions}

    approved_buys, approved_sells, skipped = evaluate_decisions(
        decisions, settings, account.equity, account.cash, positions, account.day_pl_pct
    )

    # One position per symbol at a time: don't add to a position that's already open - avoids
    # ambiguity about which stop/target level should apply. If you want to scale into a position,
    # it'll pick back up on the next signal after the current one closes.
    filtered_buys = []
    for buy in approved_buys:
        if buy.symbol in broker.state["positions"]:
            skipped.append(SkippedDecision(buy.symbol, "BUY", "position already open - not adding to it"))
        else:
            filtered_buys.append(buy)
    approved_buys = filtered_buys

    print(f"\n{len(approved_buys)} buy(s), {len(approved_sells)} sell(s), {len(skipped)} skipped\n")

    for s in skipped:
        print(f"  SKIP  {s.symbol:10s} {s.action:5s} - {s.reason}")
        log_row(s.symbol, s.action, "skipped", reasoning=s.reason)

    trades_executed = 0

    for sell in approved_sells:
        price = latest_prices[sell.symbol]
        print(f"  SELL  {sell.symbol:10s} qty={sell.qty} @ ${price:.4f} (confidence {sell.confidence:.0f}) - {sell.reasoning}")
        if args.dry_run:
            log_row(sell.symbol, "SELL", "dry-run", amount=sell.qty, confidence=sell.confidence, reasoning=sell.reasoning)
            continue
        pos_qty = broker.state["positions"][sell.symbol]["qty"]
        if sell.qty >= pos_qty - 1e-9:
            event = broker.close_position(sell.symbol, price, "signal_exit")
            log_row(sell.symbol, "SELL", "executed", amount=sell.qty, confidence=sell.confidence, reasoning=sell.reasoning)
            log_close_event(event)
        else:
            broker.partial_sell(sell.symbol, sell.qty, price)
            log_row(sell.symbol, "SELL", "executed", amount=sell.qty, confidence=sell.confidence, reasoning=sell.reasoning)
        trades_executed += 1

    for buy in approved_buys:
        price = latest_prices[buy.symbol]
        d = decisions_by_symbol[buy.symbol]
        print(f"  BUY   {buy.symbol:10s} ${buy.notional_usd} @ ${price:.4f} (confidence {buy.confidence:.0f}) - {buy.reasoning}")
        if args.dry_run:
            log_row(buy.symbol, "BUY", "dry-run", amount=buy.notional_usd, confidence=buy.confidence,
                    reasoning=buy.reasoning, rolling_mean=round(d.rolling_mean, 6), zscore=round(d.zscore, 4))
            continue
        pos = broker.open_position(buy.symbol, buy.notional_usd, price, settings.stop_loss_pct, settings.take_profit_pct)
        log_row(buy.symbol, "BUY", "executed", amount=buy.notional_usd, confidence=buy.confidence,
                reasoning=buy.reasoning, rolling_mean=round(d.rolling_mean, 6), zscore=round(d.zscore, 4),
                trade_id=pos["trade_id"])
        trades_executed += 1

    if not args.dry_run:
        broker.save()
        record_success({"equity": account.equity, "trades_executed": trades_executed})

    print("\nDone. See logs/trade_log.csv for the full history.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # RELIABLE pillar: never fail silently. Print the full traceback (visible in the GitHub
        # Actions log), log a row so it shows up in the trade log too, and exit non-zero so the
        # workflow run is clearly marked failed - and crucially, do NOT record a heartbeat here,
        # so persistent crashes eventually surface through the watchdog as well as through
        # GitHub's own failed-run indicator.
        print(f"FATAL ERROR: {e}")
        traceback.print_exc()
        try:
            log_row("SYSTEM", "ERROR", "failed", reasoning=str(e))
        except Exception:
            pass
        sys.exit(1)
