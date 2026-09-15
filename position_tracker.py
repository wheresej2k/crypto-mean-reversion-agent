"""RELIABLE + SELF-IMPROVING pillars: the crypto-specific 'software bracket'. Alpaca doesn't
support bracket orders for crypto (confirmed against their docs - see crypto_broker.py), so this
module builds the same protection out of two independent resting GTC orders - a stop-limit sell
and a limit sell - placed right after a buy fills, and reconciles them on every run: if one
fills, the other (now meaningless) is canceled.

State survives between GitHub Actions runs (each run is a fresh, disposable machine) by
persisting to state/open_brackets.json, which the workflow commits back to the repo after every
run - the same pattern the trade log itself uses.

Because these are resting orders sitting on Alpaca's own exchange, they protect an open position
continuously, 24/7, even between scheduled runs - the bot doesn't need to be running for a
stop-loss to trigger. Run frequency mainly affects how fast new entries are caught, not how
protected existing positions are.
"""
import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

STATE_PATH = Path(__file__).parent / "state" / "open_brackets.json"

FILL_POLL_ATTEMPTS = 6
FILL_POLL_DELAY_SECONDS = 5


@dataclass
class Bracket:
    trade_id: str
    symbol: str
    qty: float
    entry_price: float
    stop_price: float
    target_price: float
    stop_order_id: str
    target_order_id: str
    opened_at: str


def load_brackets() -> dict[str, Bracket]:
    if not STATE_PATH.exists():
        return {}
    with open(STATE_PATH) as f:
        raw = json.load(f)
    return {symbol: Bracket(**b) for symbol, b in raw.items()}


def save_brackets(brackets: dict[str, Bracket]):
    STATE_PATH.parent.mkdir(exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump({symbol: asdict(b) for symbol, b in brackets.items()}, f, indent=2)


def wait_for_fill(broker, order_id: str):
    """Polls an order (up to ~30s) until it fills. Returns the filled order, or None if it never
    filled in the poll window - rare for a market order on a liquid pair, but real orders can
    always be left pending on Alpaca's side if something goes wrong; callers must handle None
    rather than assume a fill happened.
    """
    for _ in range(FILL_POLL_ATTEMPTS):
        current = broker.get_order(order_id)
        if current.status.value == "filled":
            return current
        time.sleep(FILL_POLL_DELAY_SECONDS)
    return None


def open_bracket(broker, symbol: str, notional_usd: float, stop_loss_pct: float, take_profit_pct: float):
    """Places the market buy, waits for it to fill, then places the two protective resting
    orders. Returns (Bracket, None) on success, or (None, error_string) on failure - e.g. the buy
    never filled in the poll window. That's logged clearly rather than silently dropped; the
    order itself is left open on Alpaca and would need a manual look if it ever actually happens
    (see README).
    """
    order = broker.buy_notional(symbol, notional_usd)
    filled = wait_for_fill(broker, order.id)

    if filled is None:
        return None, (
            f"buy order {order.id} did not fill within "
            f"{FILL_POLL_ATTEMPTS * FILL_POLL_DELAY_SECONDS}s - still pending on Alpaca, no "
            f"protective orders placed. Check the Alpaca dashboard."
        )

    entry_price = float(filled.filled_avg_price)
    qty = float(filled.filled_qty)
    trade_id = str(uuid.uuid4())[:8]

    stop_price = entry_price * (1 - stop_loss_pct / 100)
    target_price = entry_price * (1 + take_profit_pct / 100)

    stop_order = broker.place_stop_loss(symbol, qty, stop_price, client_order_id=f"stop-{trade_id}")
    target_order = broker.place_take_profit(symbol, qty, target_price, client_order_id=f"target-{trade_id}")

    bracket = Bracket(
        trade_id=trade_id,
        symbol=symbol,
        qty=qty,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        stop_order_id=str(stop_order.id),
        target_order_id=str(target_order.id),
        opened_at=filled.filled_at.isoformat() if filled.filled_at else "",
    )
    return bracket, None


def reconcile(broker, brackets: dict[str, Bracket]) -> tuple[dict[str, Bracket], list[dict]]:
    """Checks every tracked bracket: if the stop or target order has filled, cancels the sibling
    order and drops the bracket from tracking. Returns the still-open brackets plus a list of
    'close events' - this is where the SELF-IMPROVING pillar's 'what happened after' gets
    captured for a trade that already entered, feeding trade_log.py's full-lifecycle record.
    """
    still_open = {}
    close_events = []

    for symbol, b in brackets.items():
        stop_status = broker.get_order(b.stop_order_id).status.value
        target_status = broker.get_order(b.target_order_id).status.value

        if stop_status == "filled":
            broker.cancel_order(b.target_order_id)
            pl_pct = (b.stop_price - b.entry_price) / b.entry_price * 100
            close_events.append({
                "symbol": symbol, "trade_id": b.trade_id, "exit_reason": "stop_loss",
                "entry_price": b.entry_price, "exit_price": b.stop_price, "pl_pct": pl_pct,
            })
        elif target_status == "filled":
            broker.cancel_order(b.stop_order_id)
            pl_pct = (b.target_price - b.entry_price) / b.entry_price * 100
            close_events.append({
                "symbol": symbol, "trade_id": b.trade_id, "exit_reason": "take_profit",
                "entry_price": b.entry_price, "exit_price": b.target_price, "pl_pct": pl_pct,
            })
        else:
            still_open[symbol] = b

    return still_open, close_events


def cancel_bracket(broker, brackets: dict[str, Bracket], symbol: str) -> tuple[dict[str, Bracket], Bracket | None]:
    """Used when the strategy itself generates a SELL signal (bearish crossover) on a symbol that
    still has open protective orders. Those resting orders reserve the position's quantity on
    Alpaca's side, so they must be canceled BEFORE a separate signal-driven market sell is
    submitted - otherwise the sell would be rejected for insufficient available quantity.

    Returns the removed Bracket (so the caller can log its real entry_price against whatever
    price the signal-driven sell actually fills at - see trader.py) or None if this symbol had
    no tracked bracket.
    """
    b = brackets.get(symbol)
    if b is None:
        return brackets, None

    broker.cancel_order(b.stop_order_id)
    broker.cancel_order(b.target_order_id)

    remaining = {s: br for s, br in brackets.items() if s != symbol}
    return remaining, b


def build_close_event(bracket: Bracket, exit_reason: str, exit_price: float) -> dict:
    pl_pct = (exit_price - bracket.entry_price) / bracket.entry_price * 100
    return {
        "symbol": bracket.symbol, "trade_id": bracket.trade_id, "exit_reason": exit_reason,
        "entry_price": bracket.entry_price, "exit_price": exit_price, "pl_pct": pl_pct,
    }
