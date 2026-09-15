"""Builds a simple summary image (gain/loss, equity, today's activity, current holdings) and
texts it to the user's phone as an MMS, via their carrier's email-to-picture-message gateway and
Gmail SMTP. Free - no SMS API or subscription involved. Run this after trader.py in the hourly CI
workflow (see .github/workflows/hourly-trade.yml) - since this bot trades far more often than the
sibling trend-following bot, "today's activity" here will typically show more rows.

Crypto-specific difference from a stock bot's notify.py: no "market was closed today" branch -
crypto trades 24/7, there's no closed state to report.

PRIVACY: this module intentionally never prints the actual phone/email address anywhere,
including in success/failure log lines - only a generic confirmation. GitHub also automatically
redacts registered secret values from Actions logs, but not printing them at all is a second,
independent layer of protection for a public repo.
"""
import csv
import os
import smtplib
import tempfile
from datetime import datetime, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart

from PIL import Image, ImageDraw, ImageFont

import kraken_client
from config import load_settings
from paper_broker import PaperBroker

LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "trade_log.csv")
IMAGE_PATH = os.path.join(tempfile.gettempdir(), "mean_reversion_daily_summary.png")

# Alpaca paper accounts start funded at $100,000. Used only to show "since you started"
# performance - if you ever reset the paper account balance, update this to match.
STARTING_EQUITY = 100_000.0

GREEN = (30, 130, 76)
RED = (178, 34, 34)
DARK = (30, 30, 30)
GRAY = (120, 120, 120)


def _font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except Exception:
        return ImageFont.load_default()


def todays_log_rows():
    today = datetime.now(timezone.utc).date().isoformat()
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH, newline="") as f:
        return [row for row in csv.DictReader(f) if row["timestamp_utc"].startswith(today)]


def _draw_arrow(draw, x, y, size, pointing_up, color):
    if pointing_up:
        draw.polygon([(x, y + size), (x + size, y + size), (x + size / 2, y)], fill=color)
    else:
        draw.polygon([(x, y), (x + size, y), (x + size / 2, y + size)], fill=color)


def _format_activity_row(r):
    if r["action"] == "CLOSE":
        try:
            pl = f"{float(r['pl_pct']):+.1f}%"
        except (ValueError, KeyError):
            pl = ""
        return f"CLOSE  {r['symbol']}  {r.get('exit_reason', '')}  {pl}"
    try:
        amount_str = f"${float(r['amount']):,.0f}" if r["action"] == "BUY" else f"{float(r['amount']):.6g}"
    except (ValueError, KeyError):
        amount_str = str(r.get("amount", ""))
    return f"{r['action']}  {r['symbol']}  {amount_str}"


def build_image(equity, day_pl_usd, day_pl_pct, rows, positions):
    width, height = 640, 500
    is_up = day_pl_usd >= 0
    accent = GREEN if is_up else RED

    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    draw.text((28, 24), datetime.now().strftime("%A, %B %d"), font=_font(22, bold=True), fill=DARK)
    draw.rectangle([0, 64, width, 70], fill=accent)

    sign = "+" if is_up else "-"
    _draw_arrow(draw, 28, 108, 26, is_up, accent)
    draw.text((66, 96), f"{sign}${abs(day_pl_usd):,.2f}", font=_font(44, bold=True), fill=accent)
    draw.text((28, 150), f"({day_pl_pct:+.2f}% since last reset point)", font=_font(20), fill=accent)
    draw.text((28, 195), f"Account value: ${equity:,.2f}", font=_font(20), fill=DARK)

    all_time_usd = equity - STARTING_EQUITY
    all_time_pct = all_time_usd / STARTING_EQUITY * 100
    all_time_color = GREEN if all_time_usd >= 0 else RED
    draw.text(
        (28, 225),
        f"Since you started: {'+' if all_time_usd >= 0 else '-'}${abs(all_time_usd):,.2f} ({all_time_pct:+.2f}%)",
        font=_font(16),
        fill=all_time_color,
    )

    y = 270
    draw.text((28, y), "Today's activity:", font=_font(18, bold=True), fill=DARK)
    y += 30
    trade_rows = [r for r in rows if r["status"] == "executed" and r["action"] in ("BUY", "SELL", "CLOSE")]
    if not trade_rows:
        draw.text((28, y), "No trades - bot held steady, no clear signal.", font=_font(16), fill=GRAY)
        y += 26
    else:
        for r in trade_rows[:6]:
            draw.text((28, y), _format_activity_row(r), font=_font(16), fill=DARK)
            y += 23
    y += 14

    holding_str = ", ".join(sorted(positions)) if positions else "nothing right now (all cash)"
    draw.text((28, y), f"Currently holding: {holding_str}", font=_font(15), fill=GRAY)

    draw.text((28, height - 32), "Paper trading - simulated money, not real trades", font=_font(13), fill=GRAY)
    img.save(IMAGE_PATH)


def send_mms(to_address, gmail_address, gmail_app_password):
    msg = MIMEMultipart()
    msg["From"] = gmail_address
    msg["To"] = to_address
    msg["Subject"] = ""
    with open(IMAGE_PATH, "rb") as f:
        msg.attach(MIMEImage(f.read(), name="summary.png"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, [to_address], msg.as_string())


def main():
    settings = load_settings()
    broker = PaperBroker()
    latest_prices = {}
    for symbol in settings.watchlist:
        try:
            latest_prices[symbol] = kraken_client.get_recent_bars(symbol, hours=1)[-1].close
        except Exception as e:
            print(f"  WARNING: could not fetch latest price for {symbol}: {e}")

    account = broker.get_account(latest_prices)
    day_pl_usd = account.equity - account.last_equity

    rows = todays_log_rows()
    positions = broker.get_positions(latest_prices)
    build_image(account.equity, day_pl_usd, account.day_pl_pct, rows, positions.keys())

    to_address = os.environ["PHONE_MMS_ADDRESS"]
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    send_mms(to_address, gmail_address, gmail_app_password)
    print("Sent daily summary MMS.")


if __name__ == "__main__":
    main()
