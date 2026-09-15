"""Shared, broker-agnostic data shapes used across strategy.py, data_validator.py, backtest.py,
and paper_broker.py. Kept separate from any one data source or execution venue on purpose - this
project pulls historical data from Alpaca (backtest.py/tune.py) and live prices from Kraken
(kraken_client.py, via trader.py/notify.py) but trades are executed entirely in a local simulated
ledger (paper_broker.py), never on a real exchange. None of that should leak into these shapes.
"""
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class AccountSnapshot:
    equity: float
    cash: float
    last_equity: float

    @property
    def day_pl_pct(self) -> float:
        if self.last_equity == 0:
            return 0.0
        return (self.equity - self.last_equity) / self.last_equity * 100


@dataclass
class PositionSnapshot:
    symbol: str
    qty: float
    market_value: float
    avg_entry_price: float
    unrealized_plpc: float
