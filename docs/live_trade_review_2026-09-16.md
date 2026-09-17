# Live paper-trade review - 2026-09-16

This review covers `logs/trade_log.csv` through `2026-09-16T19:30:40+00:00`.

## What is working

- The bot is running regularly and logging every decision. The log has 497 rows.
- The long-term trend filter kept the bot out of several weak dip signals while the market was still below trend.
- Position sizing stayed inside the configured 15% per-position limit.
- The strategy did eventually find mean-reversion exits for all four opened positions.

## What is not working

- The log did not contain close to 100 completed trades. It contained 4 buys, 4 sell executions, and only 2 proper close rows. Most rows were skipped `HOLD` decisions.
- SELL decisions were blocked by `min_confidence`, even though low z-score confidence can be normal when price has reverted near the mean. That delayed exits.
- Full-position sells rounded quantities to 8 decimals before execution. LINK and DOGE were left with tiny residual positions, which kept the bot thinking the positions were still open.
- The live paper ledger and backtest did not charge fees or slippage, so small gross bounces looked better than they would on Kraken.
- The old backtest equity curve recorded equity before same-bar fills, which could understate the impact of current-bar fills and costs.
- After fees were added, the prior active settings failed the one-year backtest badly: roughly -55% to -60%, depending on whether the profit floor was enabled.

## Changes made

- `min_confidence` now applies to BUY decisions only. SELL exits are still checked for position ownership and valid quantity.
- Full-position SELL decisions now use the exact held quantity instead of rounded quantity.
- The paper broker and backtest now apply configurable `trading_fee_pct` and `slippage_pct`.
- BUY risk sizing reserves cash for estimated fees and slippage instead of spending all cash on notional alone.
- New BUY rows log the fee-adjusted entry price, and signal-exit SELL rows log the fee-adjusted exit price.
- The backtest now records equity after same-bar fills and uses a true daily start-equity value for the daily-loss gate.
- The live paper settings were moved to a conservative, fee-aware mode: `window=96`, `entry_zscore=3.5`, `stop_loss_pct=4`, `take_profit_pct=8`, and `min_signal_exit_profit_pct=1.0`.
- Dust positions under $1 are cleaned up automatically so old rounded residuals no longer block new entries.
- Focused tests cover low-confidence exits, exact full-position exits, and fee/slippage accounting.

## Current read

The trend filter looks worth keeping. The part to improve was execution realism and selectivity, not bigger sizing. With Kraken Pro's current lowest-volume spot taker tier at 0.80% per side, the strategy needs enough gross mean reversion to clear roughly 1.6% in round-trip fees plus slippage before a trade is truly profitable.

The final cost-aware one-year backtest of the updated config returned `+0.15%`, versus `-53.40%` for equal-weight buy-and-hold, with `-0.21%` max drawdown. That result came from only one completed round trip, so it should be treated as a defensive paper-trading reset, not proof of a durable edge.
