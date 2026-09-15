"""This project's own paper-trading ledger. Kraken has no sandbox/demo environment for spot
trading (unlike Alpaca, which offers a first-class paper account the sibling bots use) - so here,
"paper trading on Kraken" means simulating fills locally against Kraken's real live prices
(kraken_client.py) with a virtual cash/position ledger, persisted to state/paper_state.json
(committed back to the repo after every run, same pattern as the other state files). No real
order of any kind is ever placed anywhere - this module never calls Kraken's (or any) trading API.

This deliberately mirrors backtest.py's exact simulation rules (same instant-fill-at-decision-
price assumption, same "check the newest bar's high/low against a stored stop/target level" rule
for stop-loss/take-profit) - live paper trading should behave like the backtest that validated
this strategy's parameters, not a different, looser simulation.

IMPORTANT reliability difference from the Alpaca-based sibling bots (read this before trusting
it): Alpaca's real resting stop-loss/take-profit orders protect a position continuously, 24/7, on
the exchange itself, independent of whether the bot happens to be running. This paper broker has
no exchange-side orders at all - stop-loss/take-profit are only checked against whatever new bars
were fetched the moment trader.py actually runs. Between runs, if price spikes through a stop and
back within one run interval, this ledger won't catch it (this is why README recommends running
more often than the sibling bots' hourly cadence).
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from models import AccountSnapshot, PositionSnapshot

STATE_PATH = Path(__file__).parent / "state" / "paper_state.json"
STARTING_CASH = 100_000.0


def _default_state() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "cash": STARTING_CASH,
        "starting_cash": STARTING_CASH,
        "day_start_equity": STARTING_CASH,
        "day_start_date": today,
        "positions": {},
    }


class PaperBroker:
    def __init__(self):
        self.state = self._load()

    @staticmethod
    def _load() -> dict:
        if not STATE_PATH.exists():
            return _default_state()
        with open(STATE_PATH) as f:
            return json.load(f)

    def save(self):
        STATE_PATH.parent.mkdir(exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(self.state, f, indent=2)

    def _roll_day_if_needed(self, equity_now: float):
        """The daily-loss circuit breaker (risk_manager.py) needs today's starting equity. Alpaca
        provided this for the sibling bots (`last_equity`); here, nothing tracks it but us - roll
        it over ourselves the first time a run happens on a new UTC date.
        """
        today = datetime.now(timezone.utc).date().isoformat()
        if self.state["day_start_date"] != today:
            self.state["day_start_date"] = today
            self.state["day_start_equity"] = equity_now

    def get_account(self, latest_prices: dict[str, float]) -> AccountSnapshot:
        equity = self.state["cash"] + sum(
            pos["qty"] * latest_prices.get(sym, pos["entry_price"])
            for sym, pos in self.state["positions"].items()
        )
        self._roll_day_if_needed(equity)
        return AccountSnapshot(equity=equity, cash=self.state["cash"], last_equity=self.state["day_start_equity"])

    def get_positions(self, latest_prices: dict[str, float]) -> dict[str, PositionSnapshot]:
        out = {}
        for symbol, pos in self.state["positions"].items():
            price = latest_prices.get(symbol, pos["entry_price"])
            market_value = pos["qty"] * price
            unrealized_plpc = (price - pos["entry_price"]) / pos["entry_price"] * 100
            out[symbol] = PositionSnapshot(symbol, pos["qty"], market_value, pos["entry_price"], unrealized_plpc)
        return out

    def open_position(self, symbol: str, notional_usd: float, price: float, stop_loss_pct: float, take_profit_pct: float) -> dict:
        qty = notional_usd / price
        self.state["cash"] -= notional_usd
        trade_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        pos = {
            "qty": qty,
            "entry_price": price,
            "stop_price": price * (1 - stop_loss_pct / 100),
            "target_price": price * (1 + take_profit_pct / 100),
            "trade_id": trade_id,
            "opened_at": now,
            "checked_through": now,
        }
        self.state["positions"][symbol] = pos
        return pos

    def close_position(self, symbol: str, exit_price: float, exit_reason: str) -> dict:
        """Fully closes a position at exit_price (either a signal-driven exit or a stop/target
        hit found by check_stop_target_hits) and returns a close-event dict for trade_log.py.
        """
        pos = self.state["positions"].pop(symbol)
        proceeds = pos["qty"] * exit_price
        self.state["cash"] += proceeds
        pl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
        return {
            "symbol": symbol,
            "trade_id": pos["trade_id"],
            "exit_reason": exit_reason,
            "entry_price": pos["entry_price"],
            "exit_price": exit_price,
            "pl_pct": pl_pct,
        }

    def partial_sell(self, symbol: str, qty: float, price: float):
        """A signal-driven SELL for less than the full position (risk_manager can size a sell
        below 100%). Full exits should go through close_position instead so the position is
        dropped from tracking and a close event is produced.
        """
        pos = self.state["positions"][symbol]
        sell_qty = min(qty, pos["qty"])
        self.state["cash"] += sell_qty * price
        pos["qty"] -= sell_qty

    def check_stop_target_hits(self, symbol: str, new_bars: list) -> dict | None:
        """Walks any bars fetched since this position was last checked (oldest first) and applies
        the exact same trigger rule backtest.py's simulate() uses: a stop-loss hit takes priority
        over a take-profit hit within the same bar. Advances `checked_through` regardless of
        whether anything triggered, so the next run only looks at genuinely new bars.
        """
        pos = self.state["positions"].get(symbol)
        if not pos:
            return None

        checked_through = pos["checked_through"]
        relevant = [b for b in new_bars if b.timestamp.isoformat(timespec="seconds") > checked_through]
        if not relevant:
            return None

        for bar in relevant:
            if bar.low <= pos["stop_price"]:
                return self.close_position(symbol, pos["stop_price"], "stop_loss")
            if bar.high >= pos["target_price"]:
                return self.close_position(symbol, pos["target_price"], "take_profit")

        pos["checked_through"] = relevant[-1].timestamp.isoformat(timespec="seconds")
        return None
