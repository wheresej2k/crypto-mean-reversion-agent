# Crypto Mean-Reversion Agent (paper trading, 100% free, zero exchange accounts needed)

An automated crypto trading bot: a free, rule-based strategy (mean reversion on 15-minute bars -
see `strategy.py`) watches a crypto watchlist 24/7 for coins trading meaningfully below their own
recent rolling average, buys the dip, and sells once the price has reverted back up - taking the
bounce as profit rather than waiting for a sustained trend. A risk-limit layer filters those
signals before anything is "traded," and every trade is executed in a locally-simulated paper
ledger against real live Kraken prices - see "Why there's no exchange API key" below for why. The
bot checks for trades every 15 minutes, matching the bar size the strategy trades on.

**This runs entirely on free services: Kraken's public market data, Alpaca's free public crypto
history for backtesting, and GitHub Actions' free automation minutes. No exchange account, no
API key, and no paid API is involved anywhere.**

## Sibling project, different temperament

This is a sibling to `crypto-trading-agent`, which trades a slow, patient trend-following
crossover strategy on Alpaca's paper-trading environment and can sit out for days or weeks
waiting for a sustained move. **This bot is built to be the opposite: aggressive and active.**
Mean reversion treats "a temporary dip" as an actionable signal, and temporary dips happen far
more often than sustained trends. The bot trades on 15-minute bars and checks for trades every 15
minutes (not hourly) so trade decisions themselves happen far more often, not just a more frequent
check on an hourly signal - see the validated backtest below for real trade-frequency numbers.

**"Aggressive/active" describes how often it trades and how often it checks in - not a promise
that every day is profitable.** No real strategy wins every single day; some individual trades
here will lose (the backtest below has a real, non-zero loss rate). The goal is a favorable
average over many trades, with the stop-loss capping how bad any single loss can get - not a
guarantee stated or implied about daily results.

**This project trades on Kraken, not Alpaca** - a deliberate choice made after discussing it, since
Kraken is the exchange whose app is already on your phone. See the next section for what that
actually means mechanically, since it's not a simple broker swap.

## Why there's no exchange API key

Alpaca (used by the sibling bot) offers a first-class **paper-trading account**: a real API-backed
sandbox with its own fake balance, its own dashboard, real order objects, real fills. Kraken has
no equivalent for spot markets - only a separate "Futures Demo" product (leveraged perpetual
futures, a different instrument than spot buy/sell dips).

So "paper trading on Kraken" here means something different from the sibling bot: this project
pulls **real, live Kraken prices** (`kraken_client.py`, Kraken's public market data - no account or
key needed) and simulates every trade in a **local ledger** (`paper_broker.py`) - a virtual cash
balance and set of positions tracked in `state/paper_state.json`, committed back to the repo after
every run. Nothing here ever places a real order anywhere, on Kraken or otherwise. Historical
backtesting (`backtest.py`/`tune.py`) separately uses Alpaca's free public crypto data (also no key
needed) since it has much deeper history in a single API call than Kraken's public OHLC endpoint
(which caps at ~721 candles per call regardless of interval - at the 15-minute bars this bot
trades on, about a week).

**The real, honest tradeoff this introduces** (read this before trusting it):
- **No exchange-hosted dashboard.** Alpaca gave the sibling bot a real web dashboard to check
  positions/P&L anytime. This bot's "account" only exists in `state/paper_state.json` and
  `logs/trade_log.csv` - see "Dashboard" below for a status page built from exactly those files.
- **Stop-loss/take-profit are checked once per run, not continuously.** A real Alpaca resting
  order protects a position on the exchange itself, 24/7, independent of whether the bot happens
  to be running. This local ledger only checks a position's stop/target levels against whatever
  bars were fetched the moment `trader.py` runs. In practice this is less scary than it sounds:
  `paper_broker.py` checks *every* bar fetched since the position was last checked (not just the
  newest one), so even if a run is late or missed, the next run correctly finds the exact historical
  bar that crossed the stop/target and closes at that price - the P/L stays accurate, only the
  *timing* of the recorded exit lags behind a real resting order. Running every 15 minutes
  (matching the bars the strategy was validated on) is the same cadence `backtest.py`/`tune.py`
  used to check stop/target hits, so live behavior should track the validated backtest closely as
  long as the scheduled workflow keeps running - which is exactly what `watchdog.py` exists to
  catch if it doesn't.
- **Kraken's asset naming is quirky.** Dogecoin is `XDG` on Kraken, not `DOGE` - confirmed against
  Kraken's own `/0/public/AssetPairs` endpoint, not guessed. `kraken_client.SYMBOL_MAP` handles
  this translation so the rest of the codebase only ever sees this project's own `"BASE/USD"`
  naming.

## The strategy: buy the dip, sell the reversion

`strategy.py` computes a rolling mean and standard deviation of price over the last `window` bars
(15 minutes each - a Bollinger-Band-style approach) for every symbol on the watchlist, then
converts the latest price into a z-score - how many standard deviations above or below its own
recent average the price currently sits.

- **BUY** when the z-score drops to `entry_zscore` or further below zero (price is unusually low
  relative to its own recent range - "oversold", likely to bounce) and there's no existing
  position in that symbol.
- **SELL** when the z-score climbs back up to `exit_zscore` or above **while holding a position**
  (price has reverted back toward - or past - its recent average; take the bounce as profit).
- Otherwise **HOLD** - no actionable signal.

The stop-loss/take-profit levels (`paper_broker.py`) are the real safety net if a "dip" just keeps
falling instead of reverting: the strategy's own SELL signal is the target case (reversion
happened, take it), the stop-loss is the fallback case (reversion never came, cut the loss).

## The goal, stated concretely (read this before trusting any of it)

"Working" means a specific, quantified bar, not a vibe. Before it's trusted with even paper money,
a set of strategy parameters must, when backtested across **three different historical windows**
(~3 months, ~1 year, and ~5 years - close to the full history Alpaca has for crypto, which starts
2021-01-01):

- **Never draw down more than 32%** from its peak equity in any window
- **Never have a win rate below 35%** on completed round-trip trades in any window
- Among whatever parameter combinations clear both of those, **pick whichever made the most
  money on average** across the three windows

This is exactly what `tune.py`'s safety filter enforces (see `is_safe()`, `SAFE_MAX_DRAWDOWN_PCT`
and `MIN_WIN_RATE_PCT` at the top of that file). These are the same numeric bar the sibling
trend-following project arrived at after real backtest data showed a stricter guess didn't survive
contact with crypto's 2022 crash - re-used here rather than re-litigated, since the underlying
"could this wreck my account" question doesn't change with the strategy.

**Return itself is deliberately NOT a third gate** - see the caveat at the top of `tune.py` for the
full story (drawdown/win-rate answer "could this wreck my account"; a rally or a crash can make
return look artificially bad or good on its own, in either direction, regardless of strategy
quality).

### Current cost-aware paper settings (2026-09-17)

After the first live paper trades, the simulator was updated to charge fees and slippage. That
changed the conclusion: the original active settings traded too often for Kraken's spot taker
fees and lost badly in the cost-aware one-year backtest. The current config is intentionally more
selective:

`window=96 bars (24h), trend_window=192 bars (2 days), entry_zscore=3.5, exit_zscore=0.0, stop_loss=4%, take_profit=8%, min_confidence=50, trading_fee_pct=0.8, slippage_pct=0.05, min_signal_exit_profit_pct=1.0`

| Window | Strategy return | Buy & hold | Max drawdown | Win rate | Notes |
|---|---|---|---|---|---|
| ~1 year | +0.15% | -53.40% | -0.21% | 100.0% | Cost-aware run with only 1 completed round trip |

This is not proof of a durable edge. One completed round trip is too small a sample to trust. It
is a defensive reset away from overtrading while the bot gathers more fee-aware paper evidence.

`tune.py` (or the monthly automated re-tune, `auto_retune.py`) can still be run later to
search for a better combination whenever there's enough data and time to let it run; it will
only ever propose a change via a reviewed pull request, never apply one silently - see "Automated
re-tuning" below.

**Read the return numbers with real skepticism, not as a promise:**
- **Curve-fitting risk.** Even a hand-picked default (not the output of a search) can happen to fit
  the specific stretch of history it was checked against. Treat this as a hypothesis worth testing
  on paper, not a proven result - see `tune.py`'s module docstring.
- **High trade count is expensive.** The first fee-aware review showed that frequent small exits
  can get crushed by spot taker fees. The current settings prefer fewer, larger signals.
- **Fees and slippage are modeled now.** The paper ledger and backtest charge the configurable
  `trading_fee_pct` and `slippage_pct` values in `config/params.json`. The default fee is based
  on Kraken Pro's lowest-volume spot taker tier as of the current fee schedule. Real fills can
  still differ from this estimate, especially if spreads widen.
- **Some trades lose, by design.** The stop-loss exists specifically to cap how bad any one of
  those losses gets - it is not a sign something is broken when a trade closes red.
- **Backtested on Alpaca's prices, traded live on Kraken's.** The two venues track the same
  underlying assets closely (arbitrage keeps them tight), but they are not identical feeds - live
  results will differ somewhat from the backtest even after the fee/slippage assumptions are applied.

See `diagnose()` in `tune.py` if you want to re-derive any of this yourself. Tighten or loosen
further in `tune.py` if your risk tolerance changes.

## The four pillars this project is built around

### 1. ACCURATE - the right data, in the right shape
- `kraken_client.py` pulls 15-minute bars directly from Kraken's public market data for live
  trading; `backtest.py`/`tune.py` use Alpaca's public crypto data for historical validation.
- **Data granularity matches the strategy**: the bot checks for trades every 15 minutes, trading
  on 15-minute bars.
- `data_validator.py` checks every bar of market data **before** the strategy ever sees it:
  staleness, gaps, and implausible single-bar moves. A symbol that fails any check is dropped
  from *that run only* and logged - and, notably, is also skipped for stop/target reconciliation
  that run (see "Why there's no exchange API key" above), rather than risking a false trigger on
  bad data.

### 2. RELIABLE - runs 24/7 without you babysitting it
- `retry.py` wraps every network call (Kraken and Alpaca) in automatic retries with exponential
  backoff.
- Every error is logged clearly (`logs/trade_log.csv` gets a row, GitHub Actions shows the full
  traceback) - nothing fails silently.
- **Missed-run detection**: `heartbeat.py` records a timestamp after every successful run; a
  completely separate scheduled workflow (`.github/workflows/watchdog.yml`, `watchdog.py`) checks
  every 15 minutes and texts a plain-text warning if more than `WATCHDOG_ALERT_AFTER_HOURS`
  (default 0.5h = 30 min) has passed with no successful run.
- **Stop-loss/take-profit protection depends on the bot actually running** - the real reliability
  tradeoff of not having a real exchange behind this bot. See "Why there's no exchange API key"
  above for exactly what this does and doesn't affect.

### 3. WELL-DEFINED GOAL - see the section above
Explicit, quantified, and enforced by `tune.py`'s safety filter before any parameter set is
considered "good," not just implied by whatever the code happens to do.

### 4. SELF-IMPROVING - learns from every trade, safely
- **Full lifecycle logging**: `trade_log.py` records the rolling mean and z-score the strategy
  actually saw at decision time, its reasoning, and what happened after a position closes
  (stop-loss, take-profit, or a signal-driven exit) - real entry/exit price and realized P/L.
- **Automated re-tuning, gated behind a human review.** Once a month,
  `.github/workflows/monthly-retune.yml` re-runs `tune.py`'s exact safety-filtered sweep against
  fresh data (`auto_retune.py`). If a different combination now scores best *and still passes the
  same safety filter*, it opens a **pull request** proposing the change to `config/params.json`.
  **It never merges automatically, never touches the live bot directly, and never touches your
  risk-tolerance settings** (position size, total exposure, daily-loss caps). You review and merge
  yourself, or don't.
- `tune.py`/`auto_retune.py` are hard-coded to never touch `max_position_pct`,
  `max_total_exposure_pct`, or `max_daily_loss_pct` - an automated process silently widening how
  much of your account it risks, even in paper trading, is exactly the kind of self-modifying-risk
  behavior this project's design brief calls out as needing a human decision, not an algorithm.

## How this differs from the sibling trend-following bot (not a copy-paste)

| | crypto-trading-agent (sibling) | crypto-mean-reversion-agent (this project) |
|---|---|---|
| Signal | SMA crossover + long-term trend filter | Rolling mean/std z-score (Bollinger-Band-style) |
| Bar size / run cadence | Hourly bars, runs hourly | 15-minute bars, runs every 15 minutes |
| Temperament | Patient - waits for a sustained move, often sits out for days | Active - treats a temporary dip as actionable, trades far more often |
| Exchange | Alpaca (paper-trading account) | Kraken (live prices) + a locally-simulated ledger |
| Execution | Real paper orders on Alpaca's API | Fully simulated locally - no real order ever placed anywhere |
| Account/API key needed | Yes - Alpaca paper keys | **None at all** - both data sources (Kraken, Alpaca) are used unauthenticated |
| Stop-loss/take-profit | Real resting orders, protect 24/7 independent of run frequency | Checked once per run against fetched bars (see tradeoff above) |
| Monitoring | Alpaca's own web dashboard | A free static dashboard on GitHub Pages (see "Dashboard" below), rebuilt by GitHub Actions every run - no Claude involved, works even if you're out of Claude usage |
| Repo visibility | Private (fits under the free Actions-minutes cap at hourly cadence) | **Public** - every-15-minutes cadence would exceed a private repo's free 2,000 min/month; public repos get unlimited free minutes |
| Observed trade frequency | Occasional - can sit out for days/weeks | See validated backtest above |
| Everything else (risk limits, retries, watchdog, logging schema shape) | Same proven infrastructure, copied unmodified where the logic is strategy-agnostic | |

## One-time setup

### 1. Install Python
If you don't already have it, install Python 3.11+ from [python.org](https://www.python.org/downloads/)
(check "Add python.exe to PATH" during install on Windows).

### 2. Install dependencies
Open a terminal in this folder and run:

```bash
pip install -r requirements.txt
```

### 3. Configure notifications
Copy `.env.example` to a new file named `.env` in this same folder, then fill in your phone/Gmail
info and watchlist. **No exchange API key goes in here** - see "Why there's no exchange API key"
above.

**Never commit or share your `.env` file.** It's already listed in `.gitignore` so `git` won't
track it.

### 4. Review the strategy/risk parameters
`config/params.json` holds the tunable numbers - it's already seeded with the conservative,
cost-aware defaults described above. Re-run `tune.py` yourself any time you want to search for a
better combination against fresher data.

### 5. Set up phone notifications (optional but recommended)
1. **Carrier MMS gateway**: find your carrier's free email-to-picture-message address, e.g.
   `5551234567@vzwpix.com` (Verizon), `5551234567@tmomail.net` (T-Mobile),
   `5551234567@mypixmessages.com` (AT&T). Put it in `.env` as `PHONE_MMS_ADDRESS`.
2. **Gmail App Password**: turn on 2-Step Verification on your Google account, then generate an
   App Password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).

## Running it

Always test with `--dry-run` first:

```bash
python trader.py --dry-run
```

Check `logs/trade_log.csv` and the console output. Once comfortable, run it for real (still
simulated - nothing here ever places a real order):

```bash
python trader.py
```

There's no `--force`/market-hours flag - crypto trades 24/7, every run is "live" as far as market
availability goes.

## Backtesting and tuning

```bash
python backtest.py                 # last 35040 bars (~1 year of 15-min bars)
python backtest.py --bars 175200   # ~5 years - close to the full history Alpaca has for crypto
```

```bash
python tune.py            # always fetches fresh history from Alpaca
python tune.py --cache    # reuse a local cache while iterating - don't use for a real decision
```

Both pull historical data from Alpaca's free public crypto API (no key needed) - this is
deliberately separate from the Kraken prices `trader.py`/`notify.py` use live (see "Why there's no
exchange API key" above for why the two data sources differ). Expect the fetch to take a few
minutes; the sweep itself is fast thanks to `strategy.rolling_mean_std_series`'s O(n) rolling
computation. Running `tune.py` never changes anything live on its own - copy the winning
combination into `config/params.json` yourself, or let the monthly automated retune propose it as
a pull request.

## Running it automatically, 24/7

Three separate GitHub Actions workflows, all free:

- **`.github/workflows/trade.yml`** - runs `trader.py` every 15 minutes, texts a daily summary
  image once a day, commits the updated trade log and state back to the repo.
- **`.github/workflows/watchdog.yml`** - runs independently every 15 minutes, texts a plain
  warning if no successful run has completed in over `WATCHDOG_ALERT_AFTER_HOURS` hours.
- **`.github/workflows/monthly-retune.yml`** - runs on the 1st of each month, may open a pull
  request proposing updated strategy parameters (never auto-merged).

### Setting your secrets
Set these once via the GitHub CLI (prompts you securely) - just three, since no exchange API key
is needed anywhere in this project:

```bash
gh secret set GMAIL_ADDRESS
gh secret set GMAIL_APP_PASSWORD
gh secret set PHONE_MMS_ADDRESS
```

### On repo visibility - this repo is PUBLIC
Unlike the sibling bot, this repo is set up as **public**, not private. That wasn't the original
plan - it became necessary once trading moved to every 15 minutes: at that cadence the main
workflow alone uses roughly 2,880 Actions-minutes/month (4x an hourly bot's usage), which exceeds
a **private** repo's free 2,000 min/month allowance and would either incur real charges or halt
the workflow once the quota was hit. **Public repos get unlimited free GitHub Actions minutes**,
which is why this project is public.

What that means concretely: the code and `logs/trade_log.csv`/`state/paper_state.json` trade
history are visible to anyone. No money, API keys, or personal information are ever in the repo -
`.env` is git-ignored and never committed, GitHub Secrets are encrypted and never appear in the
repo's code, and GitHub automatically redacts any registered secret's value from Actions logs.
This project's own code goes further and never prints the raw phone/email at all
(`notify.py`/`watchdog.py`), as a second, independent layer on top of GitHub's own redaction. If
you'd rather keep this private and accept either the Actions cost or a slower cadence, you can
flip the repo back to private and reduce the schedule in `.github/workflows/trade.yml` accordingly.

## Reviewing results

### Dashboard
**https://wheresej2k.github.io/crypto-mean-reversion-agent/** - a status page (cash, equity, open
positions, live strategy parameters, recent activity) rebuilt by `build_dashboard.py` and
committed as `docs/index.html` after every 15-minute trading run, hosted free by GitHub Pages.
This is deliberately a plain static page with no client-side data fetching and no dependency on
Claude in any way (an earlier version of this dashboard was a scheduled Claude session that
rebuilt and republished a claude.ai Artifact - that stopped updating whenever Claude usage ran
out, which defeats the point of a 24/7 bot). GitHub Actions - not Claude - regenerates this page
every single run, so it keeps updating no matter what.

- `logs/trade_log.csv` - one row per decision, plus a linked row when a position closes, with the
  rolling mean/z-score the strategy saw and the realized P/L.
- `state/paper_state.json` - the entire simulated account: cash, and every currently open position
  with its stop/target levels. This file and the trade log are the ultimate source of truth (the
  dashboard above is generated from exactly these two files).
- `state/last_success.json` - when the bot last completed a run successfully.

## Important limitations - please read

- **This is not a validated trading strategy.** Mean reversion is a simple, transparent, free
  technical rule, not an edge over the market. Treat any paper-trading results as exploratory, not
  predictive of real performance.
- **This is a local simulation, not real trading of any kind, on any platform.** No order is ever
  placed on Kraken, Alpaca, or anywhere else. If you ever want to go live for real, that requires
  wiring up Kraken's actual private trading API with real credentials, a much longer paper track
  record than this project has, and a clear-eyed, explicit conversation about money you could
  fully afford to lose.
- **No day is guaranteed green.** Even a selective paper strategy can lose. The current settings
  are intentionally quiet because frequent small exits were not profitable after fees.
- **The daily-loss limit is a circuit breaker, not a guarantee.** It stops *new* buys once the
  day's loss threshold is hit; existing positions are still only protected when the bot next runs
  (see "Why there's no exchange API key" above).
- **One open position per symbol at a time** - the bot won't add to a position it's already
  holding.
- **Backtest returns are not a promise.** The current one-year result came from only one completed
  round trip, which is too small a sample to trust as a durable edge.
- **This is built for 15-minute decisions on a five-coin watchlist**, not sub-minute
  high-frequency or scalping-style trading.
- **Backtested on Alpaca prices, lives on Kraken prices** - a deliberate, documented tradeoff (see
  above), not an oversight, but real enough that live results will differ somewhat from the
  validated backtest even after the fee/slippage assumptions are applied.
