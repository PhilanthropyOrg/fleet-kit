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
import re

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


# ---------------------------------------------------------------------------------------------
# Issues resolved per hour (2026-09-24, operator ask): the fleet's core throughput number.
# resolved = a merged PR closes the issue via Closes/Fixes/Resolves (GitHub sets the issue's
# own stateReason to COMPLETED when a linking PR merges), AND a later successful deploy run
# covers the merge commit -- merged-but-not-yet-deployed is 0 until one does (same "live" rule
# `live_merges` above already uses for shipped_live_per_day, applied per-merge here instead of
# only against the fleet-wide latest deploy, so an hourly bucket can place the resolve at the
# deploy that actually carried it rather than at merge time).
#
# stateReason NOT_PLANNED -- duplicate/stale/superseded/wontfix, however it got that way --
# never counts even when a merged+deployed PR happens to reference the issue. See
# issue_cluster.py's own mega-child convention: a folded child is closed `--reason "not
# planned"` ("tracked in #M"); only the mega's own COMPLETED close is real resolved work. Credit
# the child too and a mega would double the count for one fix; NOT_PLANNED is exactly the signal
# that keeps a fold from being counted twice.
#
# A fleet:mega issue counts as its FULL folded-children checklist (issue_cluster.mega_body()'s
# own format), not 1 -- marie's Part B2 folds N real issues sharing one root cause into one
# PR-sized item; crediting that as "1" would make batching look like it destroys throughput
# instead of concentrating it. There is no structural signal in a mega's body today that
# distinguishes "just absorbed its children" from "also fixed something else the checklist
# doesn't name" (the "+1 for original work" case in the operator's own spec) -- rather than
# invent a heuristic that could inflate the number, this only ever counts the checklist, which
# undercounts that edge case and never overcounts it. Flagged, not guessed.
# ---------------------------------------------------------------------------------------------

_COMPLETED = "COMPLETED"
_MEGA_LABEL = "fleet:mega"


def _labels_of(issue: dict) -> set[str]:
    out = set()
    for lab in issue.get("labels") or []:
        out.add(lab.get("name") if isinstance(lab, dict) else str(lab))
    return {x for x in out if x}


def mega_child_numbers(issue: dict) -> list[int]:
    """Child issue numbers folded into a fleet:mega issue, read from its own checklist
    (`- [ ] #N ...` / `- [x] #N ...` under `## Children`, issue_cluster.checklist_line()'s
    format -- boxes are created unticked and nothing ticks them later, so both states must
    match). The `## Linked Reif asks` section (pointers kept open, never closed by this mega)
    is excluded by stopping at that heading. [] for a non-mega issue, or a mega whose body has
    no readable checklist."""
    if _MEGA_LABEL not in _labels_of(issue):
        return []
    body = issue.get("body") or ""
    section = body.split("## Linked Reif asks")[0]
    return [int(n) for n in re.findall(r"^- \[[ xX]\] #(\d+)", section, re.M)]


def resolved_weight(issue: dict) -> int:
    """How many "issues resolved" one closed issue is worth. 0 unless it closed COMPLETED (a
    missing/other stateReason -- NOT_PLANNED, or an unset field on an old row -- is never
    guessed as a completion). A fleet:mega issue is worth its full child count, floored at 1 so
    an unreadable checklist still credits the mega's own real close instead of vanishing."""
    if str(issue.get("stateReason") or "").upper() != _COMPLETED:
        return 0
    children = mega_child_numbers(issue)
    return len(children) if children else 1


def _successful_deploy_ts(deploy_runs: list[dict]) -> list[float]:
    return sorted(t for t in (_ts(d.get("createdAt")) for d in deploy_runs
                              if d.get("conclusion") in (None, "", "success")) if t is not None)


def _covering_deploy_ts(merged_at: float, deploys_sorted: list[float]) -> float | None:
    """The first successful deploy at/after this merge -- when it actually went live. None if
    no deploy has covered it yet."""
    for t in deploys_sorted:
        if t >= merged_at:
            return t
    return None


def resolved_events(merged_prs: list[dict], deploy_runs: list[dict],
                    issues_by_number: dict[int, dict]) -> list[dict]:
    """One row per (merged PR x closed issue it references) that actually counts toward the
    KPI: {"ts": <epoch it went live>, "weight": <int>, "pr": <number>, "issue": <number>}.
    `issues_by_number` is `{number: issue dict with state/stateReason/labels/body}` -- a PR's
    own closingIssuesReferences doesn't always carry a fresh stateReason, so callers fetch
    issues separately (see fleet_view_server._scoreboard_issues_by_number)."""
    deploys = _successful_deploy_ts(deploy_runs)
    out = []
    for pr in merged_prs:
        merged_at = _ts(pr.get("mergedAt"))
        if merged_at is None:
            continue
        live_at = _covering_deploy_ts(merged_at, deploys)
        if live_at is None:
            continue
        for ref in pr.get("closingIssuesReferences") or []:
            num = ref.get("number")
            issue = issues_by_number.get(num) if num is not None else None
            if not issue:
                continue
            w = resolved_weight(issue)
            if w:
                out.append({"ts": live_at, "weight": w, "pr": pr.get("number"), "issue": num,
                           "is_mega": bool(mega_child_numbers(issue))})
    return out


def resolved_per_hour_buckets(events: list[dict], now: float, hours: int = 24) -> list[int]:
    """Sum of weights per hour-ago bucket, oldest first, current hour last -- same shape as
    fleet_view_server's native fleet.ok_runs_per_hour tile."""
    buckets = [0] * hours
    for e in events:
        age_h = int((now - e["ts"]) // 3600)
        if 0 <= age_h < hours:
            buckets[hours - 1 - age_h] += e["weight"]
    return buckets


def resolved_referenced_numbers(merged_prs: list[dict]) -> set[int]:
    """Every issue number any merged PR's closingIssuesReferences ever named (merged or not yet
    deployed) -- the set closed_without_pr_count excludes, so a closed issue that WAS linked to
    a PR (even one still awaiting deploy) never gets miscounted as a bare manual close."""
    out = set()
    for pr in merged_prs:
        for ref in pr.get("closingIssuesReferences") or []:
            num = ref.get("number")
            if num is not None:
                out.add(num)
    return out


def closed_without_pr_count(issues: list[dict], referenced_numbers: set[int],
                            now: float, hours: int = 24) -> int:
    """Closed issues in the window with NO merged PR ever linking to them -- manual closes,
    which is where dupe/stale/wontfix busywork lives. Shown alongside the headline so a burst
    of closes that didn't move the real (PR + deploy gated) number is visible as exactly that,
    not silently folded into it."""
    lo = now - hours * 3600
    n = 0
    for issue in issues:
        if str(issue.get("state") or "").upper() != "CLOSED":
            continue
        t = _ts(issue.get("closedAt"))
        if t is None or not (lo < t <= now):
            continue
        if issue.get("number") in referenced_numbers:
            continue
        n += 1
    return n
