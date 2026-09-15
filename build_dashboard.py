"""Builds docs/index.html - a fully static status dashboard for GitHub Pages, generated fresh
after every trading run by trade.yml (and by watchdog.yml, so a missed-run alert shows up too).

This deliberately replaces an earlier design where a scheduled Claude session cloned the repo,
rebuilt the page, and republished it as a claude.ai Artifact - that worked, but stops updating the
moment the user is out of Claude usage, since running the routine at all requires Claude. Nothing
in this script or in GitHub Pages hosting depends on Claude in any way: GitHub Actions builds this
file with plain Python, and GitHub Pages serves the static result, both for free, indefinitely,
regardless of anyone's Claude usage.

Usage:
    python build_dashboard.py
"""
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from config import load_params

ROOT = Path(__file__).parent
STATE_DIR = ROOT / "state"
LOG_PATH = ROOT / "logs" / "trade_log.csv"
OUT_PATH = ROOT / "docs" / "index.html"
MAX_LOG_ROWS = 40


def load_json(path):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def load_recent_log_rows(path, limit):
    if not path.exists():
        return []
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return list(reversed(rows[-limit:]))


def fmt_money(v):
    return f"${v:,.2f}"


def fmt_pct(v, decimals=2):
    if v is None:
        return "n/a"
    return f"{v:+.{decimals}f}%"


def esc(s):
    if s is None:
        return ""
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def utc_span(timestamp_str):
    """Renders a UTC timestamp with a data-utc attribute so the page's own JS can display it in
    each viewer's local time - matches the pattern used on the Crypto Bot Ledger artifact.
    """
    if not timestamp_str:
        return "n/a"
    return f'<span class="local-time" data-utc="{esc(timestamp_str)}">{esc(timestamp_str)} UTC</span>'


def build_positions_table(positions, latest_note):
    if not positions:
        return "<p class=\"muted\">No open positions.</p>"
    rows = []
    for symbol, pos in positions.items():
        rows.append(
            "<tr>"
            f"<td>{esc(symbol)}</td>"
            f"<td>{pos['qty']:.6f}</td>"
            f"<td>{fmt_money(pos['entry_price'])}</td>"
            f"<td>{fmt_money(pos['stop_price'])}</td>"
            f"<td>{fmt_money(pos['target_price'])}</td>"
            f"<td>{utc_span(pos.get('opened_at'))}</td>"
            "</tr>"
        )
    return (
        "<div class=\"table-scroll\"><table><thead><tr><th>Symbol</th><th>Qty</th><th>Entry</th>"
        f"<th>Stop</th><th>Target</th><th>Opened</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>{latest_note}"
    )


def build_log_table(rows):
    if not rows:
        return "<p class=\"muted\">No trade log entries yet.</p>"
    out = []
    for r in rows:
        status = r.get("status", "")
        action = r.get("action", "")
        css_class = "row-buy" if action == "BUY" and status == "executed" else (
            "row-sell" if action == "SELL" and status == "executed" else (
                "row-error" if status == "failed" else ""
            )
        )
        out.append(
            f"<tr class=\"{css_class}\">"
            f"<td>{utc_span(r.get('timestamp_utc'))}</td>"
            f"<td>{esc(r.get('symbol'))}</td>"
            f"<td>{esc(action)}</td>"
            f"<td>{esc(status)}</td>"
            f"<td>{esc(r.get('amount'))}</td>"
            f"<td>{esc(r.get('reasoning'))}</td>"
            "</tr>"
        )
    return (
        "<div class=\"table-scroll\"><table><thead><tr><th>Time</th><th>Symbol</th><th>Action</th>"
        f"<th>Status</th><th>Amount</th><th>Reasoning</th></tr></thead>"
        f"<tbody>{''.join(out)}</tbody></table></div>"
    )


def main():
    state = load_json(STATE_DIR / "paper_state.json") or {}
    last_success = load_json(STATE_DIR / "last_success.json") or {}
    params = load_params()
    log_rows = load_recent_log_rows(LOG_PATH, MAX_LOG_ROWS)

    positions = state.get("positions", {})
    cash = state.get("cash", 0.0)
    starting_cash = state.get("starting_cash", 100_000.0)

    latest_prices_note = (
        "<p class=\"muted\">Position values shown are cost basis (entry price x qty) - this "
        "static page doesn't fetch live prices, so it won't show unrealized P/L. See "
        "<code>state/paper_state.json</code> in the repo, or wait for the bot's next run, for "
        "current equity.</p>"
    )
    market_value = sum(p["qty"] * p["entry_price"] for p in positions.values())
    equity_at_cost = cash + market_value
    total_return_pct = (equity_at_cost - starting_cash) / starting_cash * 100 if starting_cash else 0.0

    generated_at = datetime.now(timezone.utc).isoformat()

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crypto Mean-Reversion Bot - Kraken Paper Ledger</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #0f1216; --panel: #171b21; --border: #2a2f37; --text: #e7e9ec; --muted: #9aa4b2;
    --green: #3ecf8e; --red: #ef5f5f; --accent: #5b9dff;
  }}
  @media (prefers-color-scheme: light) {{
    :root {{ --bg: #f7f8fa; --panel: #ffffff; --border: #e2e5ea; --text: #171b21; --muted: #5b6472; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--bg); color: var(--text); margin: 0; padding: 24px 16px 60px;
    font: 14px/1.5 -apple-system, Segoe UI, Roboto, sans-serif;
  }}
  .wrap {{ max-width: 980px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .subtitle {{ color: var(--muted); margin: 0 0 24px; font-size: 13px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }}
  .card .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }}
  .card .value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
  .pos {{ color: var(--green); }} .neg {{ color: var(--red); }}
  section {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-bottom: 20px; }}
  section h2 {{ font-size: 15px; margin: 0 0 12px; }}
  .table-scroll {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }}
  td:last-child, th:last-child {{ white-space: normal; }}
  th {{ color: var(--muted); font-weight: 600; font-size: 12px; }}
  .row-buy {{ background: color-mix(in srgb, var(--green) 8%, transparent); }}
  .row-sell {{ background: color-mix(in srgb, var(--accent) 8%, transparent); }}
  .row-error {{ background: color-mix(in srgb, var(--red) 10%, transparent); }}
  .muted {{ color: var(--muted); }}
  .params {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 8px; font-size: 13px; }}
  .params div span {{ color: var(--muted); }}
  footer {{ text-align: center; color: var(--muted); font-size: 12px; margin-top: 24px; }}
  a {{ color: var(--accent); }}
  code {{ background: var(--border); padding: 1px 5px; border-radius: 4px; font-size: 12px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Crypto Mean-Reversion Bot - Kraken Paper Ledger</h1>
  <p class="subtitle">
    Fully simulated paper trading against real live Kraken prices - no real order is ever placed.
    Rebuilt automatically by GitHub Actions after every 15-minute trading run.
    Last rebuilt: <span class="local-time" data-utc="{esc(generated_at)}">{esc(generated_at)}</span>.
    Last successful bot run: {utc_span(last_success.get('timestamp_utc'))}.
  </p>

  <div class="cards">
    <div class="card"><div class="label">Cash</div><div class="value">{fmt_money(cash)}</div></div>
    <div class="card"><div class="label">Equity (at cost)</div><div class="value">{fmt_money(equity_at_cost)}</div></div>
    <div class="card"><div class="label">Total return</div><div class="value {'pos' if total_return_pct >= 0 else 'neg'}">{fmt_pct(total_return_pct)}</div></div>
    <div class="card"><div class="label">Open positions</div><div class="value">{len(positions)}</div></div>
  </div>

  <section>
    <h2>Open positions</h2>
    {build_positions_table(positions, latest_prices_note)}
  </section>

  <section>
    <h2>Live strategy parameters</h2>
    <div class="params">
      <div><span>Rolling window: </span>{params.get('window')} bars (15-min)</div>
      <div><span>Trend filter window: </span>{params.get('trend_window')} bars (15-min)</div>
      <div><span>Entry z-score: </span>{params.get('entry_zscore')}</div>
      <div><span>Exit z-score: </span>{params.get('exit_zscore')}</div>
      <div><span>Stop-loss: </span>{params.get('stop_loss_pct')}%</div>
      <div><span>Take-profit: </span>{params.get('take_profit_pct')}%</div>
      <div><span>Min confidence: </span>{params.get('min_confidence')}</div>
      <div><span>Max position size: </span>{params.get('max_position_pct')}% of equity</div>
      <div><span>Max total exposure: </span>{params.get('max_total_exposure_pct')}%</div>
      <div><span>Max daily loss: </span>{params.get('max_daily_loss_pct')}%</div>
    </div>
  </section>

  <section>
    <h2>Recent activity (last {len(log_rows)} log rows)</h2>
    {build_log_table(log_rows)}
  </section>

  <footer>
    Source: <a href="https://github.com/wheresej2k/crypto-mean-reversion-agent">github.com/wheresej2k/crypto-mean-reversion-agent</a>
    - built with plain Python by GitHub Actions, no Claude session involved in generating this page.
  </footer>
</div>
<script>
  document.querySelectorAll('.local-time').forEach(function(el) {{
    var iso = el.getAttribute('data-utc');
    if (!iso) return;
    var d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z');
    if (isNaN(d.getTime())) return;
    el.textContent = d.toLocaleString(undefined, {{
      year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'
    }});
  }});
</script>
</body>
</html>
"""

    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
