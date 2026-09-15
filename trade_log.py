"""SELF-IMPROVING pillar: full-lifecycle trade logging. Every decision (executed, skipped,
dry-run, or failed) gets a row - same as the sibling trend-following bot - but this schema
captures what data the strategy saw (rolling_mean/zscore at decision time, instead of
short_sma/long_sma) and, later, what happened after (a second row when a position closes, linked
back to the entry via trade_id, with the exit reason and realized P/L). tune.py and the monthly
retune workflow read this alongside fresh Alpaca history so re-tuning is informed by what the bot
actually experienced live, not just backtest data.
"""
import csv
import os
from datetime import datetime, timezone

LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "trade_log.csv")
FIELDS = [
    "timestamp_utc", "symbol", "action", "status", "amount", "confidence", "reasoning",
    "rolling_mean", "zscore", "order_id", "trade_id", "exit_reason", "entry_price",
    "exit_price", "pl_pct",
]


def _ensure_header():
    if not os.path.exists(LOG_PATH):
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()


def log_row(
    symbol, action, status, amount="", confidence="", reasoning="",
    rolling_mean="", zscore="", order_id="", trade_id="", exit_reason="",
    entry_price="", exit_price="", pl_pct="",
):
    _ensure_header()
    with open(LOG_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writerow({
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": symbol,
            "action": action,
            "status": status,
            "amount": amount,
            "confidence": confidence,
            "reasoning": reasoning,
            "rolling_mean": rolling_mean,
            "zscore": zscore,
            "order_id": order_id,
            "trade_id": trade_id,
            "exit_reason": exit_reason,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pl_pct": pl_pct,
        })


def log_close_event(event: dict):
    """Logs a position close (stop-loss, take-profit, or signal exit) produced by
    paper_broker.py's check_stop_target_hits()/close_position().
    """
    log_row(
        symbol=event["symbol"],
        action="CLOSE",
        status="executed",
        trade_id=event["trade_id"],
        exit_reason=event["exit_reason"],
        entry_price=round(event["entry_price"], 6),
        exit_price=round(event["exit_price"], 6),
        pl_pct=round(event["pl_pct"], 2),
    )
