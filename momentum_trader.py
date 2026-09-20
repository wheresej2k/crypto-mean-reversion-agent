"""LIVE entry point for the time-series momentum strategy.

Replaced the mean-reversion trader on 2026-09-19. The old strategy had no profitable configuration
at Kraken's 0.80%-per-side taker fee (docs/strategy_review_2026-09-19.md); this one beat buy & hold
in 26 of 27 tested parameter combinations with positive expectancy in all 27
(docs/momentum_research_2026-09-19.md).

HOW THIS DIFFERS FROM THE OLD trader.py
---------------------------------------
- **Daily bars, not 15-minute.** The regime filter needs 150 days of history; at 15-minute
  granularity that is ~14,400 candles and Kraken returns at most ~721 per call.
- **Rebalances on a schedule, not every run.** It only changes the book every `rebalance_days`.
  Every other run is a no-op by design - low turnover IS the edge, because a round trip costs
  ~1.65% in fees and slippage.
- **No stop-loss and no take-profit.** The exit is "momentum turned negative at the next
  rebalance". Capping winners is exactly what made mean reversion lose money: it produced a 58%
  win rate with -1.42% expectancy per trade. So this never calls the broker's
  check_stop_target_hits(), and stores inert stop/target levels that cannot trigger.
- **Symbols are aligned by DATE before being compared.** Coins have different history lengths
  (SOL's history starts months after BTC's). Indexing positionally would compare coins across
  different dates - a bug that materially distorted an early version of the backtest.

Usage:
    python momentum_trader.py            # live paper run
    python momentum_trader.py --dry-run  # decide and print, touch nothing
    python momentum_trader.py --force    # rebalance now, ignoring the schedule
"""
import argparse
import sys
import traceback
from datetime import date, datetime, timezone

import kraken_client
from config import load_settings
from heartbeat import record_success
from momentum_strategy import select
from paper_broker import PaperBroker
from trade_log import log_close_event, log_row

# Stored on momentum positions so the broker's bracket fields exist but can never fire. The
# momentum path deliberately never calls check_stop_target_hits(); these values mean that even if
# some future code path did, a 100% stop sits at price 0 and the target sits far above any price.
INERT_STOP_PCT = 100.0
INERT_TARGET_PCT = 1_000_000.0

MIN_TRADE_USD = 5.0


def aligned_closes(bars_by_symbol):
    """Reduce every symbol to the dates ALL of them have, so index i means the same day for each.

    Returns (dates, closes_by_symbol). Exits if the overlap is empty.
    """
    common = None
    for bars in bars_by_symbol.values():
        days = {b.timestamp.date() for b in bars}
        common = days if common is None else (common & days)
    if not common:
        raise SystemExit("No overlapping dates across the watchlist - cannot compare coins safely.")
    dates = sorted(common)
    by_symbol = {}
    for symbol, bars in bars_by_symbol.items():
        lookup = {b.timestamp.date(): b.close for b in bars}
        by_symbol[symbol] = [lookup[d] for d in dates]
    return dates, by_symbol


def due_for_rebalance(state, rebalance_days, today):
    last = state.get("last_rebalance_date")
    if not last:
        return True, "no previous rebalance recorded - this is the first one"
    elapsed = (today - date.fromisoformat(last)).days
    if elapsed >= rebalance_days:
        return True, f"{elapsed} days since the last rebalance (every {rebalance_days})"
    return False, f"only {elapsed} of {rebalance_days} days since the last rebalance"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Decide and print, but change nothing")
    parser.add_argument("--force", action="store_true", help="Rebalance now, ignoring the schedule")
    args = parser.parse_args()

    settings = load_settings()
    broker = PaperBroker()

    lookback = settings.momentum_lookback_days
    regime = settings.momentum_regime_days
    needed = max(lookback, regime) + 2

    print(f"Fetching daily bars from Kraken for: {', '.join(settings.watchlist)}")
    bars_by_symbol = {}
    for symbol in settings.watchlist:
        try:
            bars = kraken_client.get_daily_bars(symbol)
            if len(bars) < needed:
                print(f"  SKIP {symbol}: only {len(bars)} daily bars, need {needed}")
                if not args.dry_run:
                    log_row(symbol, "DATA", "skipped",
                            reasoning=f"only {len(bars)} daily bars, need {needed}")
                continue
            bars_by_symbol[symbol] = bars
        except Exception as e:
            print(f"  WARNING: could not fetch {symbol}: {e}")
            if not args.dry_run:
                log_row(symbol, "DATA", "failed", reasoning=f"fetch failed: {e}")

    if not bars_by_symbol:
        print("No symbols had usable data this run - nothing to do.")
        if not args.dry_run:
            record_success({"equity": broker.get_account({}).equity, "trades_executed": 0,
                            "note": "no valid data this run"})
        return

    dates, closes = aligned_closes(bars_by_symbol)
    index = len(dates) - 1
    latest_prices = {s: c[index] for s, c in closes.items()}
    print(f"Aligned on {len(dates)} common dates, latest {dates[index]}")

    account = broker.get_account(latest_prices)
    positions = broker.get_positions(latest_prices)
    print(f"Equity: ${account.equity:.2f}  Cash: ${account.cash:.2f}")
    print(f"Open positions: {list(positions.keys()) or 'none'}")

    today = datetime.now(timezone.utc).date()
    due, why = due_for_rebalance(broker.state, settings.momentum_rebalance_days, today)
    if args.force:
        due, why = True, "forced with --force"
    print(f"Rebalance due: {due} - {why}")

    picks = select(closes, index, lookback, regime, max_names=settings.momentum_max_names)
    for p in sorted(picks, key=lambda p: -p.trailing_return_pct):
        flag = "HOLD " if p.selected else "SKIP "
        print(f"  {flag} {p.symbol:10s} {p.reason}")

    if not due:
        print("\nNot a rebalance day - leaving the book untouched.")
        # Deliberately no per-symbol log rows here. An external scheduler triggers this workflow
        # every 15 minutes, and a 14-day rebalance means the overwhelming majority of runs are
        # no-ops - logging five rows each time would bury the real trades under ~480 rows a day
        # in the very file the strategy gets reviewed against.
        if not args.dry_run:
            broker.save()
            record_success({"equity": account.equity, "trades_executed": 0})
        return

    chosen = [p.symbol for p in picks if p.selected]
    trades = 0

    # --- exits first, so their cash is available to the entries below ---
    for symbol in list(broker.state["positions"].keys()):
        if symbol in chosen:
            continue
        price = latest_prices.get(symbol)
        if price is None:
            print(f"  WARNING: holding {symbol} but it has no price this run - leaving it alone")
            continue
        reason = next((p.reason for p in picks if p.symbol == symbol), "no longer selected")
        print(f"  SELL  {symbol:10s} - {reason}")
        if args.dry_run:
            continue
        event = broker.close_position(symbol, price, "momentum_exit",
                                      settings.trading_fee_pct, settings.slippage_pct)
        log_row(symbol, "SELL", "executed", amount=event.get("qty"), reasoning=reason,
                exit_price=round(event["exit_price"], 6))
        log_close_event(event)
        trades += 1

    # --- then size the chosen names equal-weight ---
    if chosen:
        account = broker.get_account(latest_prices)
        positions = broker.get_positions(latest_prices)
        target_usd = account.equity * (settings.max_total_exposure_pct / 100) / len(chosen)
        for symbol in chosen:
            held = positions[symbol].market_value if symbol in positions else 0.0
            delta = target_usd - held
            if delta < MIN_TRADE_USD:
                if symbol in positions:
                    print(f"  KEEP  {symbol:10s} already ~${held:,.2f} of a ${target_usd:,.2f} target")
                continue
            fee_multiplier = 1 + settings.trading_fee_pct / 100 + settings.slippage_pct / 100
            spend = min(delta, broker.state["cash"] / fee_multiplier)
            if spend < MIN_TRADE_USD:
                print(f"  SKIP  {symbol:10s} not enough cash to reach the target")
                continue
            reason = next((p.reason for p in picks if p.symbol == symbol), "positive momentum")
            print(f"  BUY   {symbol:10s} ${spend:,.2f} @ ${latest_prices[symbol]:.4f} - {reason}")
            if args.dry_run:
                continue
            pos = broker.open_position(symbol, round(spend, 2), latest_prices[symbol],
                                       INERT_STOP_PCT, INERT_TARGET_PCT,
                                       settings.trading_fee_pct, settings.slippage_pct)
            log_row(symbol, "BUY", "executed", amount=round(spend, 2), reasoning=reason,
                    trade_id=pos["trade_id"], entry_price=round(pos["entry_price"], 6))
            trades += 1
    else:
        print("  Nothing passes the momentum and regime filters - sitting in cash.")

    if not args.dry_run:
        broker.state["last_rebalance_date"] = today.isoformat()
        broker.save()
        record_success({"equity": broker.get_account(latest_prices).equity,
                        "trades_executed": trades})
    print(f"\nDone. {trades} trade(s) this rebalance.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        traceback.print_exc()
        try:
            log_row("SYSTEM", "ERROR", "failed", reasoning=str(e))
        except Exception:
            pass
        sys.exit(1)
