#!/usr/bin/env python3
"""Issues resolved per hour -- the fleet's core throughput number (Reif operator ask, 2026-09-24).

RED without the fix: scripts/scoreboard.py has no resolved_events/resolved_weight/
mega_child_numbers -- these tests fail with AttributeError against unmodified origin/main.
GREEN with it. Plain python, no pytest (same convention as test_scoreboard.py).
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import scoreboard as sb  # noqa: E402

NOW = dt.datetime(2026, 9, 24, 18, 0, tzinfo=dt.timezone.utc).timestamp()


def iso(hours_ago: float) -> str:
    return dt.datetime.fromtimestamp(NOW - hours_ago * 3600, dt.timezone.utc).isoformat().replace("+00:00", "Z")


MEGA_BODY = (
    "Mega issue: 3 open issues share one root cause.\n\n"
    "mega-signature: something broke\n"
    "mega-lane: lane:okr-conversion\n\n"
    "## Children (closed as tracked here)\n"
    "- [ ] #10 first firing\n"
    "- [ ] #11 second firing\n"
    "- [ ] #12 third firing\n\n"
    "Filed by marie Part B2 (scripts/issue_cluster.py megas)."
)


def test_fixture_1_mega_children_all_count() -> None:
    """3-child mega issue closed by a merged + deployed PR -> resolved count contributes 3."""
    issues = {5: {"number": 5, "state": "CLOSED", "stateReason": "COMPLETED",
                  "labels": [{"name": "fleet:mega"}], "body": MEGA_BODY}}
    prs = [{"number": 99, "mergedAt": iso(2), "closingIssuesReferences": [{"number": 5}]}]
    deploys = [{"createdAt": iso(1), "conclusion": "success"}]
    events = sb.resolved_events(prs, deploys, issues)
    assert sum(e["weight"] for e in events) == 3, events
    print("ok  fixture 1: 3-child mega, merged + deployed -> contributes 3")


def test_fixture_2_duplicate_close_contributes_zero() -> None:
    """Issue closed as duplicate (stateReason NOT_PLANNED) never counts, even referenced by a
    merged + deployed PR."""
    issues = {7: {"number": 7, "state": "CLOSED", "stateReason": "NOT_PLANNED",
                  "labels": [], "body": ""}}
    prs = [{"number": 100, "mergedAt": iso(2), "closingIssuesReferences": [{"number": 7}]}]
    deploys = [{"createdAt": iso(1), "conclusion": "success"}]
    events = sb.resolved_events(prs, deploys, issues)
    assert sum(e["weight"] for e in events) == 0, events
    # and the fully-disconnected case: closed as dupe, no PR references it at all.
    events_no_pr = sb.resolved_events([], deploys, issues)
    assert sum(e["weight"] for e in events_no_pr) == 0
    print("ok  fixture 2: duplicate-closed issue -> contributes 0, with or without a linking PR")


def test_fixture_3_merged_not_yet_deployed() -> None:
    """Issue closed by a merged PR that has NOT yet deployed -> 0 until a deploy record covers
    that merge commit, then 1."""
    issues = {8: {"number": 8, "state": "CLOSED", "stateReason": "COMPLETED",
                  "labels": [], "body": ""}}
    prs = [{"number": 101, "mergedAt": iso(1), "closingIssuesReferences": [{"number": 8}]}]
    before = sb.resolved_events(prs, [], issues)
    assert sum(e["weight"] for e in before) == 0, before
    deploys = [{"createdAt": iso(0.5), "conclusion": "success"}]
    after = sb.resolved_events(prs, deploys, issues)
    assert sum(e["weight"] for e in after) == 1, after
    print("ok  fixture 3: merged-but-undeployed -> 0, then 1 once a deploy covers the merge")


def test_deploy_must_be_at_or_after_the_merge() -> None:
    """A deploy that ran BEFORE this merge does not carry it -- only a later one does."""
    issues = {9: {"number": 9, "state": "CLOSED", "stateReason": "COMPLETED", "labels": [], "body": ""}}
    prs = [{"number": 102, "mergedAt": iso(1), "closingIssuesReferences": [{"number": 9}]}]
    deploys = [{"createdAt": iso(5), "conclusion": "success"}]  # stale, ran before the merge
    events = sb.resolved_events(prs, deploys, issues)
    assert sum(e["weight"] for e in events) == 0, events
    print("ok  a deploy older than the merge does not count as covering it")


def test_mega_with_unreadable_checklist_falls_back_to_one() -> None:
    """A fleet:mega issue whose body has no readable checklist still counts as 1, not 0 --
    an undercount from a malformed body is better than crediting nothing for real work."""
    issues = {6: {"number": 6, "state": "CLOSED", "stateReason": "COMPLETED",
                  "labels": [{"name": "fleet:mega"}], "body": "no checklist here"}}
    prs = [{"number": 103, "mergedAt": iso(2), "closingIssuesReferences": [{"number": 6}]}]
    deploys = [{"createdAt": iso(1), "conclusion": "success"}]
    events = sb.resolved_events(prs, deploys, issues)
    assert sum(e["weight"] for e in events) == 1, events
    print("ok  mega with an unreadable checklist still credits 1, not 0")


def test_resolved_per_hour_buckets_place_event_at_the_deploy_hour() -> None:
    events = [{"ts": iso and NOW - 2.5 * 3600, "weight": 3}, {"ts": NOW - 0.2 * 3600, "weight": 1}]
    buckets = sb.resolved_per_hour_buckets(events, NOW, hours=24)
    assert len(buckets) == 24
    assert buckets[-1] == 1  # current hour
    assert buckets[-3] == 3  # 2-3h ago bucket
    assert sum(buckets) == 4
    print("ok  hourly buckets place each resolved event at its live-at hour, current hour last")


def test_closed_without_pr_excludes_pr_linked_issues() -> None:
    issues = [
        {"number": 20, "state": "CLOSED", "closedAt": iso(1)},   # closed, no PR ever referenced it
        {"number": 21, "state": "CLOSED", "closedAt": iso(2)},   # closed, WAS referenced by a PR
        {"number": 22, "state": "OPEN", "closedAt": None},
    ]
    n = sb.closed_without_pr_count(issues, referenced_numbers={21}, now=NOW, hours=24)
    assert n == 1, n
    print("ok  closed_without_pr_count only counts closes with no PR link at all")


def test_tiles_registered_on_fleet_home() -> None:
    ids = {m["id"] for m in json.loads((HERE / "metrics.json").read_text())["metrics"]}
    for want in ["fleet.issues_resolved_per_hour", "fleet.issues_resolved_7d"]:
        assert want in ids, f"{want} missing from metrics.json (fleet_home renders one tile per row)"
    metrics = json.loads((HERE / "metrics.json").read_text())["metrics"]
    assert metrics[0]["id"] == "fleet.issues_resolved_per_hour", \
        "the headline KPI must be the first row (metrics.json's own doc: order = display order)"
    src = (HERE / "fleet_view_server.py").read_text()
    for want in ["fleet.issues_resolved_per_hour", "fleet.issues_resolved_7d"]:
        assert f'out["{want}"]' in src, f"{want} never computed in metrics_snapshot"
    print("ok  resolved-per-hour tiles registered first and computed")


if __name__ == "__main__":
    fails = 0
    for fn in [v for k, v in dict(globals()).items() if k.startswith("test_")]:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            fails += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    sys.exit(1 if fails else 0)
