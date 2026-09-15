"""Live market data ONLY - Kraken's public REST API, no account, no API keys, no real orders ever
placed through this module. Used by trader.py/notify.py to get current prices for this project's
self-built paper-trading ledger (see paper_broker.py): Kraken has no sandbox/demo environment for
spot trading (unlike Alpaca, which offers a first-class paper account), so "paper trading on
Kraken" means simulating fills locally against Kraken's real live prices, not placing real paper
orders on Kraken itself.

Kraken uses its own asset naming, confirmed against Kraken's own /0/public/AssetPairs endpoint
2026-09-14 rather than guessed - notably Dogecoin is "XDG" on Kraken, not "DOGE". SYMBOL_MAP
translates this project's Alpaca-style "BASE/USD" watchlist symbols to Kraken's pair altnames so
the rest of the codebase (strategy.py, risk_manager.py, trade_log.py, config.py's WATCHLIST) never
has to know Kraken's naming quirks.

Historical backtesting (backtest.py/tune.py) deliberately stays on Alpaca's free public crypto
data instead of Kraken - Kraken's OHLC endpoint caps each call at ~721 candles regardless of
interval, so multi-year 15-minute history would need many hundreds of paginated calls per symbol.
A single live run only ever needs a few days of bars at most, well within Kraken's per-call limit,
so live trading uses Kraken (per the user's explicit choice - it's the exchange they actually
watch) while backtesting keeps using Alpaca's deeper, single-call-friendly history. The two venues
track the same underlying asset prices closely; this is a deliberate, documented tradeoff (see
README), not an oversight.

BAR_MINUTES = 15: the bot trades on 15-minute bars (not hourly) so trade decisions themselves
happen roughly every 15 minutes, matching how often the live workflow runs - not just a more
frequent check on an hourly signal. strategy.py's `window`/`entry_zscore`/`exit_zscore` are tuned
against this same 15-minute granularity (see tune.py).
"""
from datetime import datetime, timezone

import requests

from models import Bar
from retry import retry

KRAKEN_API = "https://api.kraken.com/0/public"

BAR_MINUTES = 15

SYMBOL_MAP = {
    "BTC/USD": "XBTUSD",
    "ETH/USD": "ETHUSD",
    "SOL/USD": "SOLUSD",
    "LINK/USD": "LINKUSD",
    "DOGE/USD": "XDGUSD",
}

# Comfortably covers both the strategy's rolling window and enough history to reconcile a
# position's stop-loss/take-profit even if it's stayed open for several days - well under
# Kraken's ~721-candle-per-call cap for the 15-minute interval (721 bars =~ 7.5 days).
DEFAULT_FETCH_BARS = 500


class KrakenDataError(RuntimeError):
    pass


@retry(times=3, base_delay=2.0)
def get_recent_bars(symbol: str, bars: int = DEFAULT_FETCH_BARS) -> list[Bar]:
    """15-minute bars for `symbol` (this project's "BASE/USD" naming), oldest first. Drops
    Kraken's last returned candle - Kraken's own docs say it's always the current, not-yet-closed
    bar, and the strategy should only ever see fully closed bars (same rule the Alpaca-based
    sibling bots follow).
    """
    pair = SYMBOL_MAP.get(symbol)
    if pair is None:
        raise KrakenDataError(f"no Kraken pair mapping for {symbol} - add it to SYMBOL_MAP")

    resp = requests.get(f"{KRAKEN_API}/OHLC", params={"pair": pair, "interval": BAR_MINUTES}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise KrakenDataError(f"Kraken API error for {symbol}: {data['error']}")

    result = data["result"]
    key = next(k for k in result if k != "last")
    candles = result[key][:-1]  # drop the current, not-yet-closed candle

    parsed = [
        Bar(
            timestamp=datetime.fromtimestamp(int(c[0]), tz=timezone.utc),
            open=float(c[1]), high=float(c[2]), low=float(c[3]), close=float(c[4]),
            volume=float(c[6]),
        )
        for c in candles
    ]
    return parsed[-bars:]


def get_all_recent_bars(watchlist: list[str], bars: int = DEFAULT_FETCH_BARS) -> dict[str, list[Bar]]:
    bars_by_symbol = {}
    for symbol in watchlist:
        try:
            bars_by_symbol[symbol] = get_recent_bars(symbol, bars)
        except Exception as e:
            print(f"  WARNING: could not fetch Kraken data for {symbol}: {e}")
    return bars_by_symbol
