"""Entry point. Run this to do one trading pass: reconcile any protective orders that filled
since the last run -> fetch hourly market data -> validate it -> generate signals (free,
rule-based mean reversion) -> apply risk limits -> place paper orders on Alpaca. Meant to run once
an hour via a scheduled GitHub Actions workflow - crypto trades 24/7, so unlike a stock bot
there's no market-hours gate to check (no --force flag needed here).

Usage:
    python trader.py            # live paper-trading run
    python trader.py --dry-run  # do everything except actually submit orders or touch state
"""
import argparse
import sys
import traceback

from crypto_broker import CryptoBroker
from config import load_settings
from data_validator import validate
from heartbeat import record_success
from position_tracker import (
    build_close_event,
    cancel_bracket,
    load_brackets,
    open_bracket,
    reconcile,
    save_brackets,
    wait_for_fill,
)
from risk_manager import SkippedDecision, evaluate_decisions
from strategy import generate_signals
from trade_log import log_close_event, log_row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Don't actually submit any orders or touch saved state")
    args = parser.parse_args()

    settings = load_settings()
    broker = CryptoBroker(settings.alpaca_api_key, settings.alpaca_secret_key)

    account = broker.get_account()
    positions = broker.get_positions()
    print(f"Equity: ${account.equity:.2f}  Cash: ${account.cash:.2f}  Day P/L: {account.day_pl_pct:.2f}%")
    print(f"Open positions: {list(positions.keys()) or 'none'}")

    # --- Reconcile protective orders: did a stop-loss or take-profit fill since last run? ---
    brackets = load_brackets()
    if not args.dry_run:
        brackets, close_events = reconcile(broker, brackets)
        for event in close_events:
            print(f"  CLOSED {event['symbol']:10s} {event['exit_reason']:12s} P/L {event['pl_pct']:+.2f}%")
            log_close_event(event)
        save_brackets(brackets)
    else:
        print("  (--dry-run: skipping bracket reconciliation, no orders touched)")

    # --- ACCURATE pillar: fetch, then validate, hourly market data before trusting it ---
    print(f"Fetching hourly bars for: {', '.join(settings.watchlist)}")
    fetch_hours = settings.window + 10
    raw_bars = {}
    for symbol in settings.watchlist:
        try:
            raw_bars[symbol] = broker.get_recent_bars(symbol, hours=fetch_hours)
        except Exception as e:
            print(f"  WARNING: could not fetch data for {symbol}: {e}")
            log_row(symbol, "DATA", "failed", reasoning=f"fetch failed: {e}")

    valid_bars, issues = validate(raw_bars, settings.window)
    for issue in issues:
        print(f"  DATA SKIP {issue.symbol:10s} - {issue.reason}")
        log_row(issue.symbol, "DATA", "skipped", reasoning=issue.reason)

    if not valid_bars:
        print("No symbols passed data validation this run - nothing to trade.")
        if not args.dry_run:
            record_success({"equity": account.equity, "trades_executed": 0, "note": "no valid data this run"})
        return

    # --- Generate signals, apply risk limits ---
    print("Generating signals (mean reversion, hourly bars)...")
    decisions = generate_signals(valid_bars, positions, settings.window, settings.entry_zscore, settings.exit_zscore)
    decisions_by_symbol = {d.symbol: d for d in decisions}

    approved_buys, approved_sells, skipped = evaluate_decisions(
        decisions, settings, account.equity, account.cash, positions, account.day_pl_pct
    )

    # One bracket per symbol at a time: don't add to a position that's already open and
    # protected - avoids having to merge two different stop/target pairs into one. If you want to
    # scale into a position, it'll pick back up on the next signal after the current one closes.
    filtered_buys = []
    for buy in approved_buys:
        if buy.symbol in brackets:
            skipped.append(SkippedDecision(buy.symbol, "BUY", "position already open and protected - not adding to it"))
        else:
            filtered_buys.append(buy)
    approved_buys = filtered_buys

    print(f"\n{len(approved_buys)} buy(s), {len(approved_sells)} sell(s), {len(skipped)} skipped\n")

    for s in skipped:
        print(f"  SKIP  {s.symbol:10s} {s.action:5s} - {s.reason}")
        log_row(s.symbol, s.action, "skipped", reasoning=s.reason)

    trades_executed = 0

    for sell in approved_sells:
        print(f"  SELL  {sell.symbol:10s} qty={sell.qty} (confidence {sell.confidence:.0f}) - {sell.reasoning}")
        if args.dry_run:
            log_row(sell.symbol, "SELL", "dry-run", amount=sell.qty, confidence=sell.confidence, reasoning=sell.reasoning)
            continue
        try:
            # Cancel any resting stop/target orders FIRST - they reserve qty on Alpaca's side, so
            # a sell would otherwise be rejected for insufficient available quantity.
            brackets, closed_bracket = cancel_bracket(broker, brackets, sell.symbol)
            order = broker.sell_qty(sell.symbol, sell.qty)
            filled = wait_for_fill(broker, order.id)
            fill_price = float(filled.filled_avg_price) if filled else None

            log_row(sell.symbol, "SELL", "executed", amount=sell.qty, confidence=sell.confidence,
                    reasoning=sell.reasoning, order_id=str(order.id))
            if closed_bracket and fill_price is not None:
                log_close_event(build_close_event(closed_bracket, "signal_exit", fill_price))
            trades_executed += 1
        except Exception as e:
            print(f"    ORDER FAILED: {e}")
            log_row(sell.symbol, "SELL", "failed", amount=sell.qty, confidence=sell.confidence,
                    reasoning=f"{sell.reasoning} | ERROR: {e}")

    for buy in approved_buys:
        print(f"  BUY   {buy.symbol:10s} ${buy.notional_usd} (confidence {buy.confidence:.0f}) - {buy.reasoning}")
        d = decisions_by_symbol[buy.symbol]
        if args.dry_run:
            log_row(buy.symbol, "BUY", "dry-run", amount=buy.notional_usd, confidence=buy.confidence,
                    reasoning=buy.reasoning, rolling_mean=round(d.rolling_mean, 6), zscore=round(d.zscore, 4))
            continue
        try:
            bracket, error = open_bracket(broker, buy.symbol, buy.notional_usd, settings.stop_loss_pct, settings.take_profit_pct)
            if error:
                print(f"    WARNING: {error}")
                log_row(buy.symbol, "BUY", "failed", amount=buy.notional_usd, confidence=buy.confidence,
                        reasoning=f"{buy.reasoning} | {error}", rolling_mean=round(d.rolling_mean, 6), zscore=round(d.zscore, 4))
                continue
            brackets[buy.symbol] = bracket
            log_row(buy.symbol, "BUY", "executed", amount=buy.notional_usd, confidence=buy.confidence,
                    reasoning=buy.reasoning, rolling_mean=round(d.rolling_mean, 6), zscore=round(d.zscore, 4),
                    trade_id=bracket.trade_id, order_id=bracket.stop_order_id)
            trades_executed += 1
        except Exception as e:
            print(f"    ORDER FAILED: {e}")
            log_row(buy.symbol, "BUY", "failed", amount=buy.notional_usd, confidence=buy.confidence,
                    reasoning=f"{buy.reasoning} | ERROR: {e}", rolling_mean=round(d.rolling_mean, 6), zscore=round(d.zscore, 4))

    if not args.dry_run:
        save_brackets(brackets)
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
