#!/usr/bin/env python3
"""scoreboard -- the four numbers that make the throughput levers self-correcting (2026-09-24).

Reif: "think like a startup, it's systems that fix it." A lever nobody measures drifts back.
These sit on fleet_home as tiles (scripts/metrics.json) so a regression is visible the day it
happens, not in a 14-day audit:

  fleet.items_per_minion_run   median items one minion run carried (target FLEET_MINION_TARGET_ITEMS)
  fleet.closed_per_minion_run  issues closed by a MERGED minion PR, per executed minion run
  fleet.shipped_live_per_day   merged PRs that a later successful deploy run carried to prod
  fleet.backlog_trend          net change in the open backlog over 7 days (negative = draining)

Pure functions over rows the caller fetched (fleet_view_server.metrics_snapshot does the gh
calls, cached); no network here, same split as cost_bridge / fanout.
"""
from __future__ import annotations

import datetime as _dt

import fleet_metrics

MINION_BRANCH_PREFIX = "member/minion-"


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def items_per_run(runs: list[dict], now: float, hours: float = 24.0) -> float | None:
    return fleet_metrics.compute("items_per_run:minion", runs, at=now, hours=hours)


def closed_per_run(merged_prs: list[dict], runs: list[dict], now: float, hours: float = 24.0) -> float | None:
    """Issues closed by merged minion PRs in the window / executed minion runs in the window.
    None (not 0) when no minion run executed -- no denominator is not a zero rate."""
    lo = now - hours * 3600
    executed = [r for r in runs if r.get("member") == "minion" and r.get("status") in fleet_metrics.EXECUTED
                and lo < float(r.get("ts") or 0) <= now]
    if not executed:
        return None
    closed = 0
    for pr in merged_prs:
        t = _ts(pr.get("mergedAt"))
        if t is None or not (lo < t <= now):
            continue
        if not str(pr.get("headRefName") or "").startswith(MINION_BRANCH_PREFIX):
            continue
        closed += len(pr.get("closingIssuesReferences") or [])
    return closed / len(executed)


def live_merges(merged_prs: list[dict], deploy_runs: list[dict]) -> list[float]:
    """mergedAt of every PR a later SUCCESSFUL deploy run carried (a deploy of the default
    branch created at or after the merge includes it). Merged-but-not-yet-deployed is not live."""
    deploys = sorted(t for t in (_ts(d.get("createdAt")) for d in deploy_runs
                                 if (d.get("conclusion") in (None, "", "success"))) if t is not None)
    if not deploys:
        return []
    last = deploys[-1]
    return sorted(t for t in (_ts(p.get("mergedAt")) for p in merged_prs) if t is not None and t <= last)


def shipped_live_per_day(merged_prs: list[dict], deploy_runs: list[dict], days: list[str], day_of) -> dict[str, int]:
    per = {d: 0 for d in days}
    for t in live_merges(merged_prs, deploy_runs):
        d = day_of(t)
        if d in per:
            per[d] += 1
    return per


def backlog_trend(series: list[dict], days: int = 7) -> float | None:
    """Latest open-backlog value minus the value `days` points earlier (or the oldest point we
    have, when history is shorter). Negative means the backlog is draining."""
    pts = [p for p in series if p.get("value") is not None]
    if len(pts) < 2:
        return None
    base = pts[-1 - days] if len(pts) > days else pts[0]
    return float(pts[-1]["value"]) - float(base["value"])
