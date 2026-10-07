# Multi-strategy review, 2026-10-07: two sleeves, one paper account

Triggered by: "I need multiple winning strategies so whatever the market is doing this bot knows
what to do." Everything below is measured at the live costs: 0.80% fee per side + 0.05% slippage
(~1.65% round trip). Survivorship caveat for every number: the coins were chosen in 2026.

## What was tested (daily data 2021-01 to 2026-10, `research_daily.py`)

Same start for all (2021-12-30, after the 360-day warm-up). Equal-weight holding of all 32 coins
over that period: **-57.7%**, max drawdown -79%.

| Strategy | Universe | Total | Max DD | Verdict |
|---|---|---|---|---|
| Time-series momentum 56/14/150 | 32 coins | -44.6% | -72% | fails (concentrates into single alts) |
| Donchian ensemble (paper, 5-360d) | 32 coins | -5.9% | -37% | fails on alts |
| Donchian, no 5/10-day | 32 coins | +3.8% | -31% | flat |
| RSI(2) pullback in uptrend | 32 coins | -83.0% | -85% | fails badly |
| Time-series momentum | BTC/ETH/SOL | +77.8% | -51% | fragile: 21/36 neighbours positive |
| **Donchian, no 5/10-day** | **BTC/ETH/SOL** | **+51.2%** | **-25%** | **robust: 7/8 lookback sets positive** |
| Holding BTC/ETH/SOL | — | -28.7% | -80% | — |

Research consulted: Zarattini, Pagani & Barbon (2025) "Catching Crypto Trends" (Donchian ensemble,
Sharpe > 1.5 on BTC + liquid alts); Liu & Tsyvinski (time-series momentum); calendar and
time-of-day effects in crypto do not survive costs (not built). Combining sleeves: fixed,
pre-set allocations beat regime-switching classifiers for robustness; each sleeve here has its
own built-in "go to cash" rule instead.

**Trend following works on the majors and fails on the altcoins**, which bleed against BTC.

## Mean reversion, year by year (15-minute, altcoins, `research_mr_years.py`)

Each year an independent run from flat. Live rules (10% stop, trend filter off):

| Year | Coins | Return | Expectancy/trade | Holding alts |
|---|---|---|---|---|
| 2021 | 11 | -24.3% | -1.40% | +202% |
| 2022 | 11 | -30.3% | -1.78% | -78% |
| 2023 | 15 | +26.0% | +1.21% | +85% |
| 2024 | 16 | -15.6% | -0.55% | +66% |
| 2025 | 19 | -3.3% | -0.06% | -49% |
| 2026 | 29 | +51.2% | +1.31% | +6% |

The trend filter at 12% (-25/-30/+21/-24/-8/+41) and 6% (-21/-26/+17/-21/-14/+28) made it worse.
**The earlier 1-year +35% backtest was one of two good years. Mean reversion is not a reliable
edge at these costs.**

## Live setup chosen (user's choice, "trend-led, small MR")

- **Trend sleeve** (`trend_trader.py`): Donchian ensemble on BTC/ETH/SOL, 60% of equity.
- **Mean reversion** (`trader.py`): the 29 altcoins, half size (0.5% per position, 10% max), kept
  for daily activity.
- Universes are disjoint, so the sleeves never contend for a coin. Positions carry a `strategy`
  tag. Mean-reversion positions opened in BTC/ETH/SOL before the switch are still managed by
  `trader.py` until they close; until then the trend sleeve skips those coins.

Combined backtest (`research_combo.py`, MR at half size + trend 60%):

| Year | Account | Worst drop | Holding BTC/ETH/SOL |
|---|---|---|---|
| 2022 | -22.4% | -22.4% | -80.3% |
| 2023 | +26.6% | -4.8% | +147.3% |
| 2024 | +11.5% | -20.5% | +70.2% |
| 2025 | -2.0% | -15.0% | -15.5% |
| 2026 (to Oct) | +23.5% | -3.3% | -7.4% |
| **2022-now** | **+32.6%** | | **-35.0%** |

This does NOT make money every year. Expect losing years; the design goal is losing far less than
the market in crashes and participating in uptrends.
