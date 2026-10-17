#!/usr/bin/env python3
"""tank.py -- is the fleet using the whole week's budget? One section of NORTH.md, hourly.

Reif, 2026-10-17: "how can we more maximally utilize our resources to achieve the vision for
each of these projects?" On 10-16 the phil account had 87% of its week left with 16% of the
week to go, while the builder ran 0.71 times an hour. Nobody saw the gap, so nothing moved.

Per account (account_meter.py rows, G's meter.json): % of the week's budget left vs % of the
week left. Then builder starts this hour vs the cap, and PRs merged in 24h by MANDATE
priority. THE RULE: budget-left beating week-left by 10 points or more on a readable account
(account_meter.GAP_PTS) raises the builder cap one step for the next hour (paced_hour_cap), and
dumbledore's bet that pass is `merged_prs_per_day:<top priority>`. No readable meter is the
headline, never a quiet line.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import account_meter

# MANDATE 10-16: 1. Jobs 2. OrgVerify 3. HQ 4. auditor findings. A PR counts for a priority
# when its title, branch or labels say so.
PRIORITY = {"jobs": r"\bjobs?\b", "orgverify": r"orgverify|verif", "hq": r"\bhq\b", "audit": r"audit"}


def snapshot_path() -> Path:
    return Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit")) / "tank.jsonl"


def merged_counts(prs: list[dict], now: float) -> dict[str, int]:
    """PRs merged in the 24h before `now`: all, and per priority."""
    day = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 86400))
    recent = [p for p in prs if str(p.get("mergedAt") or "") >= day]
    out = {"merged_prs_24h": len(recent)}
    for name, rx in PRIORITY.items():
        out[f"merged_prs_24h:{name}"] = sum(
            1 for p in recent if re.search(rx, " ".join(
                [p.get("title", ""), p.get("headRefName", "")]
                + [lb.get("name", "") for lb in p.get("labels") or []]), re.I))
    return out


def lines(rows: list[dict], started: int, cap: int, merged: dict[str, int]) -> list[str]:
    out = ["", "## The tank: is the week's budget being used? (tank.py)"]
    if not any(account_meter.reads(r) for r in rows):
        out.append("UNREADABLE METER: no account reads (FLEET_METER_FILE missing, stale or all errors). "
                   "dumbledore: this is your headline.")
    for r in rows:
        g = account_meter.pace_gap(r)
        if g is None:
            out.append(f"- {r.get('name', '?')}: unreadable ({str(r.get('error') or 'no week_left')[:80]})")
        else:
            out.append(f"- {r.get('name', '?')}: {float(r['week_left']):.0f}% of the week's budget left, "
                       f"{100 - float(r['elapsed']):.0f}% of the week left ({g:+.0f} points)")
    out.append(f"- builder: {started} started this hour, cap {cap or 'none'}")
    by = ", ".join(f"{k} {merged.get(f'merged_prs_24h:{k}', 0)}" for k in PRIORITY)
    out.append(f"- PRs merged 24h: {merged.get('merged_prs_24h', '?')} ({by})")
    ahead = [r.get("name", "?") for r in rows
             if (account_meter.pace_gap(r) or 0) >= account_meter.GAP_PTS and not r.get("walled")]
    if ahead:
        top = next(iter(PRIORITY))
        out.append(f"RULE: {', '.join(ahead)} has {account_meter.GAP_PTS:.0f}+ points of budget over the "
                   f"week left: builder cap +1 next hour; dumbledore bets `merged_prs_per_day:{top}`.")
    return out


def record(merged: dict[str, int], now: float, path: Path | None = None) -> None:
    p = path or snapshot_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps({"ts": now, "stages": merged}) + "\n")


def section(slug: str, now: float, run) -> list[str]:
    """north.py's hourly call: read everything, record merged counts, return the lines."""
    import build_lane_rule
    since = time.strftime("%Y-%m-%d", time.gmtime(now - 86400))
    try:
        r = run(["gh", "pr", "list", "--repo", slug, "--state", "merged", "--limit", "500", "--search",
                 f"merged:>={since}", "--json", "number,title,headRefName,labels,mergedAt"], timeout=120)
        prs = json.loads(r.stdout or "[]") if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        prs = None
    merged = merged_counts(prs, now) if prs is not None else {}
    if merged:
        record(merged, now)
    runs = Path(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit")) / "runs.jsonl"
    try:
        started = build_lane_rule.started_in_window(runs.read_text(errors="ignore").splitlines(), now=now)
    except OSError:
        started = 0
    return lines(account_meter.load(now=now), started, build_lane_rule.hour_cap(), merged)
