#!/usr/bin/env python3
"""ci_minutes.py -- GitHub Actions minutes used today and this month, for the mandate.

Reif, 2026-10-08: "we say: max CI mins used today 500 ... do what you must". The limit is
only steerable if every pass can read the number. Reads the org billing API with the gh on
PATH; prints `today=<n> month=<n>` and exits 0, or `today=unavailable month=unavailable` and
exits 0 when gh or the API fails (a pass never blocks on it; the report says it was unread).

Each successful read also saves the per-day totals to $FLEET_LOG_DIR/ci_minutes.json
({"ts": <read time>, "days": {"YYYY-MM-DD": minutes}}), merged with what was saved before, so
fleet_metrics.py can score a prediction on `ci_minutes_per_day` (the mandate's own number).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

KEEP_DAYS = 40


def cache_path() -> Path:
    return Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit")) / "ci_minutes.json"


def per_day(items: list[dict]) -> dict[str, float]:
    days: dict[str, float] = {}
    for it in items or []:
        d = str(it.get("date") or "")[:10]
        if len(d) == 10:
            days[d] = days.get(d, 0.0) + minutes([it])
    return days


def save_cache(items: list[dict], now: float, path: Path | None = None) -> None:
    """Merge this read's per-day totals into the cache. Never raises: the number is for scoring."""
    p = path or cache_path()
    try:
        try:
            old = json.loads(p.read_text()).get("days") or {}
        except (OSError, ValueError):
            old = {}
        days = {**old, **per_day(items)}
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(now - KEEP_DAYS * 86400))
        days = {d: v for d, v in sorted(days.items()) if d >= cutoff}
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": now, "days": days}, sort_keys=True))
        os.replace(tmp, p)
    except OSError:
        pass


def minutes(items: list[dict], day_prefix: str | None = None) -> float:
    """Sum the Actions minute quantities, optionally only for dates starting with day_prefix."""
    total = 0.0
    for it in items or []:
        if (it.get("product") or "") != "actions":
            continue
        if "minute" not in (it.get("unitType") or "").lower():
            continue
        if day_prefix and not str(it.get("date") or "").startswith(day_prefix):
            continue
        try:
            total += float(it.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
    return total


def fetch(org: str, year: int, month: int) -> list[dict]:
    out = subprocess.run(
        ["gh", "api", f"/organizations/{org}/settings/billing/usage?year={year}&month={month}"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    return json.loads(out).get("usageItems") or []


def main(argv: list[str]) -> int:
    org = argv[1] if len(argv) > 1 else os.environ.get("FLEET_GH_ORG", "PhilanthropyOrg")
    ts = time.time()
    now = time.gmtime(ts)
    try:
        items = fetch(org, now.tm_year, now.tm_mon)
    except Exception as e:  # noqa: BLE001 -- fail open, say so
        print(f"today=unavailable month=unavailable ({type(e).__name__})")
        return 0
    all_items = items
    if now.tm_mday == 1:  # a 24h window on the 1st reaches back into last month
        y, m = (now.tm_year, now.tm_mon - 1) if now.tm_mon > 1 else (now.tm_year - 1, 12)
        try:
            all_items = fetch(org, y, m) + items
        except Exception:  # noqa: BLE001 -- this month alone still scores most windows
            all_items = items
    save_cache(all_items, ts)
    day = time.strftime("%Y-%m-%d", now)
    print(f"today={int(minutes(items, day))} month={int(minutes(items))} day={day}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
