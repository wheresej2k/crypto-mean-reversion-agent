"""Limit-order execution for the mean-reversion sleeve, simulated against real Kraken bars.

Kraken Pro's entry tier charges 0.40% for maker (resting limit) orders vs 0.80% for taker
(market) orders. Re-running six years of the mean-reversion rules with limit execution moved the
per-year results from -24/-30/+26/-16/-3/+51% to -15/-24/+44/+1/+23/+85%
(research_limit.py, docs/multi_strategy_2026-10-07.md). This module applies exactly the rules
that test measured:

- A BUY becomes a bid at the decision bar's close. It is resolved against the ONE bar after the
  decision bar: filled at the bid only if that bar's low traded STRICTLY below it (a touch is not
  a guaranteed fill - other orders may be ahead in the queue), otherwise cancelled. Maker fee, no
  slippage. Missed runs don't stretch an order's life: it still only gets that one bar.
- A signal SELL becomes an offer at the decision close, resolved the same way against the next
  bar's high.
- Stop-losses are NOT limit orders. They stay market orders (taker fee + slippage) in
  paper_broker.check_stop_target_hits, because a stop you have to wait on is not a stop.

Orders live in paper_state.json under "pending_orders", keyed by symbol (at most one per symbol).
"""
from datetime import datetime

WAIT = "wait"
FILLED = "filled"
UNFILLED = "unfilled"


def new_order(side: str, limit_price: float, decision_bar_ts: datetime, **extra) -> dict:
    return {"side": side, "limit": limit_price,
            "placed_after": decision_bar_ts.isoformat(timespec="seconds"), **extra}


def resolve(order: dict, bars: list) -> tuple[str, object]:
    """Returns (WAIT, None) if the next bar after the decision bar hasn't closed yet,
    (FILLED, bar) or (UNFILLED, bar) once it has. `bars` are closed bars, oldest first."""
    nxt = next((b for b in bars if b.timestamp.isoformat(timespec="seconds") > order["placed_after"]), None)
    if nxt is None:
        return WAIT, None
    if order["side"] == "buy":
        return (FILLED if nxt.low < order["limit"] else UNFILLED), nxt
    return (FILLED if nxt.high > order["limit"] else UNFILLED), nxt
