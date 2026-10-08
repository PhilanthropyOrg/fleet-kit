#!/usr/bin/env python3
"""ci_minutes.py -- GitHub Actions minutes used today and this month, for the mandate.

Reif, 2026-10-08: "we say: max CI mins used today 500 ... do what you must". The limit is
only steerable if every pass can read the number. Reads the org billing API with the gh on
PATH; prints `today=<n> month=<n>` and exits 0, or `today=unavailable month=unavailable` and
exits 0 when gh or the API fails (a pass never blocks on it; the report says it was unread).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


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
    now = time.gmtime()
    try:
        items = fetch(org, now.tm_year, now.tm_mon)
    except Exception as e:  # noqa: BLE001 -- fail open, say so
        print(f"today=unavailable month=unavailable ({type(e).__name__})")
        return 0
    day = time.strftime("%Y-%m-%d", now)
    print(f"today={int(minutes(items, day))} month={int(minutes(items))} day={day}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
