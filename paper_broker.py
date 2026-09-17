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

    def prune_dust_positions(self, latest_prices: dict[str, float], min_market_value: float = 1.0) -> list[dict]:
        """Remove near-zero residual positions left by old rounded full exits.

        These positions have effectively no P/L impact but block new entries because the bot
        correctly enforces one open position per symbol.
        """
        events = []
        for symbol, pos in list(self.state["positions"].items()):
            price = latest_prices.get(symbol, pos["entry_price"])
            market_value = pos["qty"] * price
            if 0 < market_value < min_market_value:
                self.state["positions"].pop(symbol)
                events.append({
                    "symbol": symbol,
                    "trade_id": pos["trade_id"],
                    "exit_reason": "dust_cleanup",
                    "entry_price": pos["entry_price"],
                    "exit_price": price,
                    "pl_pct": 0.0,
                })
        return events

    @staticmethod
    def buy_fill_price(price: float, slippage_pct: float = 0.0) -> float:
        return price * (1 + slippage_pct / 100)

    @staticmethod
    def sell_fill_price(price: float, slippage_pct: float = 0.0) -> float:
        return price * (1 - slippage_pct / 100)

    @staticmethod
    def fee(notional: float, trading_fee_pct: float = 0.0) -> float:
        return notional * trading_fee_pct / 100

    def open_position(
        self,
        symbol: str,
        notional_usd: float,
        price: float,
        stop_loss_pct: float,
        take_profit_pct: float,
        trading_fee_pct: float = 0.0,
        slippage_pct: float = 0.0,
    ) -> dict:
        fill_price = self.buy_fill_price(price, slippage_pct)
        qty = notional_usd / fill_price
        entry_fee = self.fee(notional_usd, trading_fee_pct)
        self.state["cash"] -= notional_usd + entry_fee
        trade_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        pos = {
            "qty": qty,
            "entry_price": fill_price,
            "entry_notional": notional_usd,
            "entry_fee": entry_fee,
            "stop_price": fill_price * (1 - stop_loss_pct / 100),
            "target_price": fill_price * (1 + take_profit_pct / 100),
            "trade_id": trade_id,
            "opened_at": now,
            "checked_through": now,
        }
        self.state["positions"][symbol] = pos
        return pos

    def close_position(self, symbol: str, exit_price: float, exit_reason: str, trading_fee_pct: float = 0.0, slippage_pct: float = 0.0) -> dict:
        """Fully closes a position at exit_price (either a signal-driven exit or a stop/target
        hit found by check_stop_target_hits) and returns a close-event dict for trade_log.py.
        """
        pos = self.state["positions"].pop(symbol)
        fill_price = self.sell_fill_price(exit_price, slippage_pct)
        gross_proceeds = pos["qty"] * fill_price
        exit_fee = self.fee(gross_proceeds, trading_fee_pct)
        net_proceeds = gross_proceeds - exit_fee
        self.state["cash"] += net_proceeds
        entry_notional = pos.get("entry_notional", pos["qty"] * pos["entry_price"])
        entry_fee = pos.get("entry_fee", 0.0)
        cost_basis = entry_notional + entry_fee
        pl_pct = (net_proceeds - cost_basis) / cost_basis * 100 if cost_basis else 0.0
        return {
            "symbol": symbol,
            "trade_id": pos["trade_id"],
            "exit_reason": exit_reason,
            "entry_price": pos["entry_price"],
            "exit_price": fill_price,
            "pl_pct": pl_pct,
            "exit_fee": exit_fee,
        }

    def partial_sell(self, symbol: str, qty: float, price: float, trading_fee_pct: float = 0.0, slippage_pct: float = 0.0):
        """A signal-driven SELL for less than the full position (risk_manager can size a sell
        below 100%). Full exits should go through close_position instead so the position is
        dropped from tracking and a close event is produced.
        """
        pos = self.state["positions"][symbol]
        sell_qty = min(qty, pos["qty"])
        fill_price = self.sell_fill_price(price, slippage_pct)
        gross_proceeds = sell_qty * fill_price
        exit_fee = self.fee(gross_proceeds, trading_fee_pct)
        self.state["cash"] += gross_proceeds - exit_fee
        fraction_sold = sell_qty / pos["qty"] if pos["qty"] else 1.0
        if "entry_notional" in pos:
            pos["entry_notional"] *= 1 - fraction_sold
        if "entry_fee" in pos:
            pos["entry_fee"] *= 1 - fraction_sold
        pos["qty"] -= sell_qty

    def check_stop_target_hits(self, symbol: str, new_bars: list, trading_fee_pct: float = 0.0, slippage_pct: float = 0.0) -> dict | None:
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
                return self.close_position(symbol, pos["stop_price"], "stop_loss", trading_fee_pct, slippage_pct)
            if bar.high >= pos["target_price"]:
                return self.close_position(symbol, pos["target_price"], "take_profit", trading_fee_pct, slippage_pct)

        pos["checked_through"] = relevant[-1].timestamp.isoformat(timespec="seconds")
        return None
