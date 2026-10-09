#!/usr/bin/env python3
"""ci_minutes.py -- GitHub Actions minutes used today and this month, for the mandate.

Reif, 2026-10-08: "we say: max CI mins used today 500 ... do what you must". The limit is
only steerable if every pass can read the number. Reads the org billing API with the gh on
PATH; prints `today=<n> month=<n>` and exits 0, or `today=unavailable month=unavailable` and
exits 0 when gh or the API fails (a pass never blocks on it; the report says it was unread).

Each successful read also saves the per-day totals to $FLEET_LOG_DIR/ci_minutes.json
({"ts": <read time>, "days": {"YYYY-MM-DD": minutes}}), merged with what was saved before, so
fleet_metrics.py can score a prediction on `ci_minutes_per_day` (the mandate's own number).

`--by` adds one line naming who used today's minutes: deploys, the fleet's own branches
(`member/`, and every fleet-kit run), dependabot, other main-branch jobs, and everyone else's
branches. Billing has no per-run split, so this sums run wall time from the runs API (approx;
a matrix job bills more than its wall time). dumbledore hand-built it 2 passes running.
"""

from __future__ import annotations

import calendar
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


def source(repo: str, run: dict) -> str:
    """Which bucket a workflow run's minutes belong to."""
    branch = str(run.get("head_branch") or "")
    if (run.get("name") or "") == "DEPLOY":
        return "deploy"
    if repo.endswith("/fleet-kit") or branch.startswith("member/"):
        return "fleet"
    if branch.startswith("dependabot/"):
        return "dependabot"
    if branch == "main":
        return "main-ops"
    return "other-branches"


def wall_minutes(run: dict) -> float:
    def ts(v: object) -> float:
        return float(calendar.timegm(time.strptime(str(v), "%Y-%m-%dT%H:%M:%SZ")))
    try:
        return max(0.0, (ts(run["updated_at"]) - ts(run["run_started_at"])) / 60)
    except (KeyError, ValueError, TypeError):
        return 0.0


def by_source(runs: dict[str, list[dict]]) -> dict[str, int]:
    out: dict[str, float] = {}
    for repo, rs in runs.items():
        for r in rs:
            k = source(repo, r)
            out[k] = out.get(k, 0.0) + wall_minutes(r)
    return {k: round(v) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}


def fetch_runs(repo: str, day: str) -> list[dict]:
    out = subprocess.run(
        ["gh", "api", "--paginate", f"repos/{repo}/actions/runs?created={day}&per_page=100",
         "-q", ".workflow_runs[]|{name,head_branch,run_started_at,updated_at}"],
        capture_output=True, text=True, timeout=120, check=True,
    ).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def main(argv: list[str]) -> int:
    by = "--by" in argv
    argv = [a for a in argv if a != "--by"]
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
    if by:
        repos = sorted({str(it.get("repositoryName") or "") for it in items
                        if str(it.get("date") or "").startswith(day) and minutes([it]) > 0} - {""})
        try:
            split = by_source({f"{org}/{r}": fetch_runs(f"{org}/{r}", day) for r in repos})
            print("by (wall min, approx): " + " ".join(f"{k}={v}" for k, v in split.items()))
        except Exception as e:  # noqa: BLE001 -- the total above still stands
            print(f"by: unavailable ({type(e).__name__})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
