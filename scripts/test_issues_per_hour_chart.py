#!/usr/bin/env python3
"""issues_per_hour_chart -- the full-width graph on fleet_home, built on scoreboard.resolved_events
(#1279's already-merged scoring). RED without the fix: no direct/mega split, no chart module.
GREEN with it. Plain python, no pytest (same convention as test_resolved_per_hour.py).

Run: python3 scripts/test_issues_per_hour_chart.py
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import scoreboard as sb  # noqa: E402
import issues_per_hour_chart as chart  # noqa: E402

NOW = dt.datetime(2026, 9, 24, 18, 0, tzinfo=dt.timezone.utc).timestamp()


def iso(hours_ago: float) -> str:
    return dt.datetime.fromtimestamp(NOW - hours_ago * 3600, dt.timezone.utc).isoformat().replace("+00:00", "Z")


MEGA_BODY = (
    "Mega issue: 3 open issues share one root cause.\n\n"
    "mega-signature: something broke\nmega-lane: lane:okr-conversion\n\n"
    "## Children (closed as tracked here)\n"
    "- [ ] #10 first firing\n- [ ] #11 second firing\n- [ ] #12 third firing\n\n"
    "Filed by marie Part B2 (scripts/issue_cluster.py megas)."
)


def test_fixture_1_mega_children_count_as_mega_child_not_direct() -> None:
    """Same fixture as test_resolved_per_hour.py's fixture 1 (3-child mega, merged + deployed)
    -- the graph must stack it as mega_child=3, direct=0, never blended."""
    issues = {5: {"number": 5, "state": "CLOSED", "stateReason": "COMPLETED",
                  "labels": [{"name": "fleet:mega"}], "body": MEGA_BODY}}
    prs = [{"number": 99, "mergedAt": iso(2), "closingIssuesReferences": [{"number": 5}]}]
    deploys = [{"createdAt": iso(1), "conclusion": "success"}]
    events = sb.resolved_events(prs, deploys, issues)
    series = chart.hourly_series(events, [], hours=3, now=NOW)
    assert sum(series["direct"]) == 0, series
    assert sum(series["mega_child"]) == 3, series
    print("ok  fixture 1: 3-child mega, merged + deployed -> mega_child=3, direct=0")


def test_fixture_2_duplicate_close_contributes_zero_to_chart() -> None:
    """Same fixture as test_resolved_per_hour.py's fixture 2 -- a NOT_PLANNED close is 0 in
    every series, including as a resolved_events row (closed_without_pr is fed separately, from
    real closed issues, not from resolved_events)."""
    issues = {7: {"number": 7, "state": "CLOSED", "stateReason": "NOT_PLANNED", "labels": [], "body": ""}}
    prs = [{"number": 100, "mergedAt": iso(2), "closingIssuesReferences": [{"number": 7}]}]
    deploys = [{"createdAt": iso(1), "conclusion": "success"}]
    events = sb.resolved_events(prs, deploys, issues)
    series = chart.hourly_series(events, [], hours=3, now=NOW)
    assert sum(series["direct"]) == 0 and sum(series["mega_child"]) == 0, series
    print("ok  fixture 2: duplicate-closed issue -> 0 in every series")


def test_fixture_3_merged_not_yet_deployed_then_deployed() -> None:
    """Same fixture as test_resolved_per_hour.py's fixture 3 -- 0 until a deploy covers the
    merge, then 1, credited as direct (not a mega)."""
    issues = {8: {"number": 8, "state": "CLOSED", "stateReason": "COMPLETED", "labels": [], "body": ""}}
    prs = [{"number": 101, "mergedAt": iso(1), "closingIssuesReferences": [{"number": 8}]}]
    before = sb.resolved_events(prs, [], issues)
    series_before = chart.hourly_series(before, [], hours=3, now=NOW)
    assert sum(series_before["direct"]) + sum(series_before["mega_child"]) == 0, series_before
    deploys = [{"createdAt": iso(0.5), "conclusion": "success"}]
    after = sb.resolved_events(prs, deploys, issues)
    series_after = chart.hourly_series(after, [], hours=3, now=NOW)
    assert sum(series_after["direct"]) == 1, series_after
    print("ok  fixture 3: merged-but-undeployed -> 0, then direct=1 once a deploy covers the merge")


def test_closed_without_pr_is_a_separate_series() -> None:
    series = chart.hourly_series([], [{"ts": NOW - 1800}], hours=2, now=NOW)
    assert sum(series["closed_without_pr"]) == 1
    assert sum(series["direct"]) == 0 and sum(series["mega_child"]) == 0
    print("ok  closed_without_pr is a separate series, never blended into direct/mega_child")


def test_hourly_series_averages() -> None:
    # NOW=18:00:00 exactly; hours=3 -> buckets [16:00, 17:00, 18:00].
    events = [{"ts": NOW - 1800, "weight": 1, "is_mega": False},      # 17:30 -> 17:00 bucket (idx -2)
             {"ts": NOW - 1800, "weight": 3, "is_mega": True},        # 17:30 -> 17:00 bucket (idx -2)
             {"ts": NOW - 3600 - 1800, "weight": 2, "is_mega": False}]  # 16:30 -> 16:00 bucket (idx 0)
    series = chart.hourly_series(events, [], hours=3, now=NOW)
    assert series["direct"][-2] == 1 and series["mega_child"][-2] == 3, series
    assert series["direct"][0] == 2, series
    assert series["current_24h_rate"] > 0
    print("ok  hourly_series buckets direct/mega separately with rolling averages")


def test_chart_svg_renders_bars_and_lines() -> None:
    series = chart.hourly_series(
        [{"ts": NOW - 1800, "weight": 1, "is_mega": False}, {"ts": NOW - 1800, "weight": 3, "is_mega": True}],
        [{"ts": NOW - 1800}], hours=2, now=NOW)
    svg = chart.chart_svg(series)
    assert svg.startswith("<svg")
    assert svg.count("<rect") == 3  # direct + mega_child + closed_without_pr, all nonzero
    assert svg.count("<polyline") == 2  # avg_24h + avg_7d
    assert "var(--accent)" in svg and "var(--ok)" in svg and "var(--faint)" in svg
    print("ok  chart_svg renders one rect per nonzero segment and two average polylines")


def test_chart_svg_empty_series_returns_empty_string() -> None:
    empty = {"hours": [], "direct": [], "mega_child": [], "closed_without_pr": [], "avg_24h": [], "avg_7d": []}
    assert chart.chart_svg(empty) == ""
    print("ok  chart_svg on an empty series returns empty string, not a broken <svg>")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"{len(fns)} passed")
