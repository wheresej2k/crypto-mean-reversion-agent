"""RELIABLE pillar: detects a missed scheduled run, not just a run that crashed. GitHub Actions
cron schedules are known to occasionally not fire at all under platform load - this is exactly
the failure mode that hit the stock bot (a scheduled run silently never happened, with nothing to
notice unless someone happened to check by hand).

This script runs on its OWN separate schedule (.github/workflows/watchdog.yml, independent of the
hourly trading workflow) and just checks: how long has it been since trader.py last completed
successfully? If that's longer than WATCHDOG_ALERT_AFTER_HOURS, it texts a plain warning -
deliberately a plain-text message, not the usual chart image, so it's unmistakably different from
the normal summary and impossible to mistake for routine noise.

Needs no Alpaca credentials at all - it only reads the local heartbeat file, so it stays cheap and
has nothing else that could itself fail.
"""
import os
import smtplib
from email.mime.text import MIMEText

from heartbeat import hours_since_last_success


def send_alert(to_address, gmail_address, gmail_app_password, hours):
    msg = MIMEText(
        f"WARNING: the crypto trading bot hasn't completed a successful run in over {hours:.1f} "
        f"hours. Check the GitHub Actions tab for errors, or check your Alpaca paper dashboard "
        f"directly."
    )
    msg["From"] = gmail_address
    msg["To"] = to_address
    msg["Subject"] = "Crypto bot: missed run alert"

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, [to_address], msg.as_string())


def main():
    alert_after_hours = float(os.environ.get("WATCHDOG_ALERT_AFTER_HOURS", "3"))
    hours = hours_since_last_success()

    if hours is None:
        print("No heartbeat file yet (first run hasn't completed, or state was reset) - nothing to alert on yet.")
        return

    print(f"Hours since last successful run: {hours:.2f} (alert threshold: {alert_after_hours})")

    if hours > alert_after_hours:
        to_address = os.environ["PHONE_MMS_ADDRESS"]
        gmail_address = os.environ["GMAIL_ADDRESS"]
        gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
        send_alert(to_address, gmail_address, gmail_app_password, hours)
        print("Sent missed-run alert.")
    else:
        print("Within threshold - no alert needed.")


if __name__ == "__main__":
    main()
