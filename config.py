"""Loads settings from two deliberately separate places:

- .env (git-ignored, holds phone/Gmail notification secrets) plus WATCHLIST, which is your
  choice, not something tune.py touches.
- config/params.json (git-committed, NOT secret: the strategy/risk numbers tune.py can propose
  changes to). Keeping tunable knobs in a plain JSON file - not buried in a GitHub Actions
  workflow's env: block - is what lets the monthly auto-retune workflow (see tune.py and
  .github/workflows/monthly-retune.yml) propose changes as a reviewable file diff in a pull
  request, instead of silently rewriting live automation.

NO EXCHANGE ACCOUNT OR API KEY IS NEEDED for this project at all. Live trading is executed
entirely in a local simulated ledger (paper_broker.py) against live Kraken prices (kraken_client.py,
public data, no key), and backtesting uses Alpaca's free public crypto market data (also no key -
see kraken_client.py's docstring for why the two data sources differ). This is a deliberate design
choice made after the user asked to trade on Kraken specifically, which has no spot paper-trading
sandbox - see README's "Why there's no exchange API key" section.

Fails loudly (exits with a clear message) if anything required is missing, rather than silently
trading with defaults - this is part of the ACCURATE pillar: no run should ever proceed on
guessed-at configuration.
"""
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PARAMS_PATH = Path(__file__).parent / "config" / "params.json"

REQUIRED_PARAM_KEYS = [
    "window",
    "trend_window",
    "entry_zscore",
    "exit_zscore",
    "stop_loss_pct",
    "take_profit_pct",
    "min_confidence",
    "max_position_pct",
    "max_total_exposure_pct",
    "max_daily_loss_pct",
    "max_trades_per_run",
]


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"Missing required setting '{name}' in .env (see .env.example)")
    return value


@dataclass(frozen=True)
class Settings:
    watchlist: list[str]
    window: int
    trend_window: int
    entry_zscore: float
    exit_zscore: float
    stop_loss_pct: float
    take_profit_pct: float
    min_confidence: float
    max_position_pct: float
    max_total_exposure_pct: float
    max_daily_loss_pct: float
    max_trades_per_run: int
    trading_fee_pct: float = 0.80
    slippage_pct: float = 0.05
    min_signal_exit_profit_pct: float = 0.50


def load_params(path: Path = PARAMS_PATH) -> dict:
    if not path.exists():
        sys.exit(f"Missing {path} - this file holds the tuned strategy/risk parameters and must "
                  f"exist. It should already be committed in the repo; if it's missing, restore "
                  f"it from git or copy config/params.json from the README example.")
    with open(path) as f:
        params = json.load(f)

    missing = [k for k in REQUIRED_PARAM_KEYS if k not in params]
    if missing:
        sys.exit(f"{path} is missing required keys: {missing}")
    return params


def load_settings(params_path: Path = PARAMS_PATH) -> Settings:
    watchlist = [s.strip().upper() for s in _require("WATCHLIST").split(",") if s.strip()]
    if not watchlist:
        sys.exit("WATCHLIST in .env is empty")

    params = load_params(params_path)

    return Settings(
        watchlist=watchlist,
        window=int(params["window"]),
        trend_window=int(params["trend_window"]),
        entry_zscore=float(params["entry_zscore"]),
        exit_zscore=float(params["exit_zscore"]),
        stop_loss_pct=float(params["stop_loss_pct"]),
        take_profit_pct=float(params["take_profit_pct"]),
        min_confidence=float(params["min_confidence"]),
        max_position_pct=float(params["max_position_pct"]),
        max_total_exposure_pct=float(params["max_total_exposure_pct"]),
        max_daily_loss_pct=float(params["max_daily_loss_pct"]),
        max_trades_per_run=int(params["max_trades_per_run"]),
        trading_fee_pct=float(params.get("trading_fee_pct", 0.80)),
        slippage_pct=float(params.get("slippage_pct", 0.05)),
        min_signal_exit_profit_pct=float(params.get("min_signal_exit_profit_pct", 0.50)),
    )
