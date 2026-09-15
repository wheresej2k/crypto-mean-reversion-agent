"""Thin wrapper around Alpaca's crypto trading + crypto market data APIs. Every network call is
wrapped in retry() (see retry.py) - the RELIABLE pillar's guard against one flaky HTTP request
killing an entire scheduled run.

Crypto-specific differences from a stock broker wrapper (this is deliberately NOT the stock
bot's alpaca_broker.py with symbols swapped):
- No is_market_open() - crypto trades 24/7, there's no market-hours gate to check.
- Crypto market data needs no API keys at all (Alpaca publishes it openly) - only the trading
  calls (account, positions, orders) need authentication.
- No native bracket orders for crypto - Alpaca's API rejects OrderClass.BRACKET for crypto pairs
  (confirmed against their docs). Stop-loss and take-profit are placed as two independent
  resting GTC orders instead, tracked and reconciled by position_tracker.py.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, StopLimitOrderRequest

from retry import retry

HOURLY = TimeFrame.Hour


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


class CryptoBroker:
    def __init__(self, api_key: str, secret_key: str):
        # paper=True hard-codes this wrapper to the paper-trading endpoint - never point this at
        # a live account without a separate, explicit decision (see README).
        self.trading = TradingClient(api_key, secret_key, paper=True)
        # Crypto data needs no keys, but passing them is harmless and avoids a second client type.
        self.data = CryptoHistoricalDataClient(api_key, secret_key)

    @retry(times=3, base_delay=2.0)
    def get_account(self) -> AccountSnapshot:
        acct = self.trading.get_account()
        return AccountSnapshot(
            equity=float(acct.equity),
            cash=float(acct.cash),
            last_equity=float(acct.last_equity),
        )

    @retry(times=3, base_delay=2.0)
    def get_positions(self) -> dict[str, PositionSnapshot]:
        positions = {}
        for p in self.trading.get_all_positions():
            if "/" not in p.symbol:
                # Not a crypto position - Alpaca's account-level position list includes every
                # asset class. If this same paper account is ALSO used by a stock bot (a plain
                # ticker like "AAPL" has no slash), its holdings must never leak into this bot's
                # exposure/risk math - crypto symbols are always "BASE/QUOTE" (e.g. "BTC/USD").
                continue
            positions[p.symbol] = PositionSnapshot(
                symbol=p.symbol,
                qty=float(p.qty),
                market_value=float(p.market_value),
                avg_entry_price=float(p.avg_entry_price),
                unrealized_plpc=float(p.unrealized_plpc) * 100,
            )
        return positions

    @retry(times=3, base_delay=2.0)
    def get_recent_bars(self, symbol: str, hours: int) -> list[Bar]:
        """Hourly bars - deliberately matches the bot's hourly run cadence: no value in pulling
        1-minute bars for a strategy that only acts once an hour (see README's data-granularity
        note). Fetches extra headroom then trims to exactly `hours` bars.
        """
        start = datetime.now(timezone.utc) - timedelta(hours=hours * 2 + 24)
        req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=HOURLY, start=start)
        raw_bars = list(self.data.get_crypto_bars(req)[symbol])[-hours:]
        return [
            Bar(b.timestamp, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))
            for b in raw_bars
        ]

    @retry(times=3, base_delay=2.0)
    def buy_notional(self, symbol: str, notional_usd: float):
        """Simple market buy - no bracket (crypto doesn't support OrderClass.BRACKET). The
        protective stop-loss/take-profit orders are placed separately, after this fills, by
        position_tracker.py.
        """
        order = MarketOrderRequest(
            symbol=symbol,
            notional=round(notional_usd, 2),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
        )
        return self.trading.submit_order(order)

    @retry(times=3, base_delay=2.0)
    def sell_qty(self, symbol: str, qty: float):
        order = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
        )
        return self.trading.submit_order(order)

    @retry(times=3, base_delay=2.0)
    def place_stop_loss(self, symbol: str, qty: float, stop_price: float, client_order_id: str):
        # Limit price set 1.5% below the stop trigger as slippage tolerance - a crypto market can
        # gap fast; without a floor, a stop-limit sell could sit unfilled below a crash instead
        # of protecting the position.
        limit_price = round(stop_price * 0.985, 6)
        order = StopLimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=round(stop_price, 6),
            limit_price=limit_price,
            client_order_id=client_order_id,
        )
        return self.trading.submit_order(order)

    @retry(times=3, base_delay=2.0)
    def place_take_profit(self, symbol: str, qty: float, limit_price: float, client_order_id: str):
        order = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            limit_price=round(limit_price, 6),
            client_order_id=client_order_id,
        )
        return self.trading.submit_order(order)

    @retry(times=3, base_delay=2.0)
    def get_order(self, order_id: str):
        return self.trading.get_order_by_id(order_id)

    @retry(times=3, base_delay=2.0)
    def cancel_order(self, order_id: str):
        try:
            self.trading.cancel_order_by_id(order_id)
        except Exception as e:
            # Already-filled/already-canceled orders raise on a second cancel attempt - not a
            # real failure, just means there was nothing left to cancel.
            print(f"    (cancel {order_id} skipped: {e})")
