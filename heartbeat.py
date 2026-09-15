"""RELIABLE pillar: a simple heartbeat file, updated at the end of every trader.py run that
completes without crashing (regardless of whether any trade happened). watchdog.py checks how
old this file's timestamp is and texts an alert if it's gone stale.

This is the direct fix for the exact problem the stock bot hit: a GitHub Actions cron schedule
silently missing a run, with nothing to notice unless someone happens to check by hand.

Committed back to the repo after every run (same pattern as trade_log.csv and
state/open_brackets.json) so it survives between GitHub Actions' disposable runners.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT_PATH = Path(__file__).parent / "state" / "last_success.json"


def record_success(summary: dict):
    HEARTBEAT_PATH.parent.mkdir(exist_ok=True)
    previous_count = 0
    if HEARTBEAT_PATH.exists():
        with open(HEARTBEAT_PATH) as f:
            previous_count = json.load(f).get("run_count", 0)
    payload = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **summary,
        "run_count": previous_count + 1,
    }
    with open(HEARTBEAT_PATH, "w") as f:
        json.dump(payload, f, indent=2)


def hours_since_last_success(now: datetime | None = None) -> float | None:
    """Returns None if the heartbeat file has never been written (e.g. brand new repo, before
    the first successful run) - callers should treat that as 'unknown, not necessarily broken'
    rather than immediately alerting.
    """
    if not HEARTBEAT_PATH.exists():
        return None
    with open(HEARTBEAT_PATH) as f:
        data = json.load(f)
    last = datetime.fromisoformat(data["timestamp_utc"])
    now = now or datetime.now(timezone.utc)
    return (now - last).total_seconds() / 3600
