"""Sits between the strategy's recommendations and the broker. The strategy proposes; this
enforces limits. No trade reaches Alpaca without passing through here - this is the module to
audit/tighten if you want the bot to behave more conservatively.

The risk math itself (position caps, exposure caps, daily-loss circuit breaker) is intentionally
the same shape as the sibling trend-following bot's risk_manager.py - a position-size cap is a
position-size cap regardless of strategy. What's specific to this bot's execution model lives
elsewhere: paper_broker.py (the local simulated ledger, since Kraken has no spot paper-trading
sandbox) and kraken_client.py (live price data).
"""
from dataclasses import dataclass

from config import Settings
from strategy import TradeDecision


@dataclass
class ApprovedBuy:
    symbol: str
    notional_usd: float
    confidence: float
    reasoning: str


@dataclass
class ApprovedSell:
    symbol: str
    qty: float
    confidence: float
    reasoning: str


@dataclass
class SkippedDecision:
    symbol: str
    action: str
    reason: str


def evaluate_decisions(
    decisions: list[TradeDecision],
    settings: Settings,
    equity: float,
    cash: float,
    positions: dict,
    day_pl_pct: float,
) -> tuple[list[ApprovedBuy], list[ApprovedSell], list[SkippedDecision]]:
    approved_buys: list[ApprovedBuy] = []
    approved_sells: list[ApprovedSell] = []
    skipped: list[SkippedDecision] = []

    daily_loss_limit_hit = day_pl_pct <= -abs(settings.max_daily_loss_pct)

    current_exposure_usd = sum(p.market_value for p in positions.values())
    trades_this_run = 0

    for d in decisions:
        if d.symbol not in set(settings.watchlist):
            skipped.append(SkippedDecision(d.symbol, d.action, "symbol not in approved watchlist"))
            continue

        if d.action == "HOLD":
            skipped.append(SkippedDecision(d.symbol, d.action, d.reasoning))
            continue

        if d.action == "SELL":
            pos = positions.get(d.symbol)
            if pos is None or pos.qty <= 0:
                skipped.append(SkippedDecision(d.symbol, d.action, "no existing position to sell"))
                continue
            min_signal_exit_price = pos.avg_entry_price * (
                1 + (
                    2 * settings.trading_fee_pct
                    + settings.slippage_pct
                    + settings.min_signal_exit_profit_pct
                ) / 100
            )
            if d.price and d.price < min_signal_exit_price:
                skipped.append(
                    SkippedDecision(
                        d.symbol,
                        d.action,
                        f"signal exit below fee-adjusted profit floor (${d.price:.4f} < ${min_signal_exit_price:.4f})",
                    )
                )
                continue
            size_pct = max(0.0, min(100.0, d.size_pct))
            qty = pos.qty if size_pct >= 100.0 else round(pos.qty * size_pct / 100, 8)
            if qty <= 0:
                skipped.append(SkippedDecision(d.symbol, d.action, "computed sell quantity is zero"))
                continue
            approved_sells.append(ApprovedSell(d.symbol, qty, d.confidence, d.reasoning))
            continue

        if d.action == "BUY":
            # Cost gate, checked before anything else: a mean-reversion trade only makes money if
            # the reversion it is reaching for is bigger than the round trip costs. At Kraken's
            # lowest-volume taker tier that floor is 2 x trading_fee_pct + slippage_pct before the
            # trade breaks even, so taking dips whose whole target is under that is a guaranteed
            # slow bleed no matter how good the entry timing is. Measured over 5 years of 15-minute
            # bars, adding this filter moved a ~1 trade/day configuration from -98% to -87%; it is
            # the single most effective filter found in the sweep.
            round_trip_cost_pct = 2 * settings.trading_fee_pct + settings.slippage_pct
            required_edge_pct = max(settings.min_edge_pct, round_trip_cost_pct)
            if d.edge_pct < required_edge_pct:
                skipped.append(
                    SkippedDecision(
                        d.symbol, d.action,
                        f"reversion target {d.edge_pct:.2f}% is below the {required_edge_pct:.2f}% "
                        f"minimum edge (round-trip cost {round_trip_cost_pct:.2f}%)",
                    )
                )
                continue

            if d.confidence < settings.min_confidence:
                skipped.append(
                    SkippedDecision(
                        d.symbol, d.action, f"confidence {d.confidence:.0f} below minimum {settings.min_confidence:.0f}"
                    )
                )
                continue

            if daily_loss_limit_hit:
                skipped.append(
                    SkippedDecision(d.symbol, d.action, f"daily loss limit ({settings.max_daily_loss_pct}%) reached, no new buys")
                )
                continue

            if trades_this_run >= settings.max_trades_per_run:
                skipped.append(SkippedDecision(d.symbol, d.action, "max trades per run reached"))
                continue

            if current_exposure_usd / equity * 100 >= settings.max_total_exposure_pct:
                skipped.append(
                    SkippedDecision(d.symbol, d.action, f"portfolio exposure limit ({settings.max_total_exposure_pct}%) reached")
                )
                continue

            existing_value = positions.get(d.symbol).market_value if d.symbol in positions else 0.0
            existing_pct = existing_value / equity * 100
            room_pct = max(0.0, settings.max_position_pct - existing_pct)
            if room_pct <= 0:
                skipped.append(
                    SkippedDecision(d.symbol, d.action, f"already at max position size ({settings.max_position_pct}% of equity)")
                )
                continue

            requested_pct = max(0.0, min(d.size_pct, room_pct))
            notional = equity * requested_pct / 100

            remaining_exposure_room_usd = equity * (settings.max_total_exposure_pct / 100) - current_exposure_usd
            fee_multiplier = 1 + settings.trading_fee_pct / 100 + settings.slippage_pct / 100
            notional = min(notional, remaining_exposure_room_usd, cash / fee_multiplier)

            if notional < 1:
                skipped.append(SkippedDecision(d.symbol, d.action, "position size rounds to under $1 after risk limits"))
                continue

            approved_buys.append(ApprovedBuy(d.symbol, round(notional, 2), d.confidence, d.reasoning))
            current_exposure_usd += notional
            cash -= notional
            trades_this_run += 1
            continue

    return approved_buys, approved_sells, skipped
