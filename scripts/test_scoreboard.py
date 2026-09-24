#!/usr/bin/env python3
"""Lever 4 (2026-09-24): the scoreboard tiles compute what they claim.

RED without the fix: scripts/scoreboard.py does not exist and metrics.json has no such tiles.
GREEN with it. Plain python, no pytest.
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


def test_closed_per_run_counts_only_merged_minion_closes() -> None:
    runs = [{"member": "minion", "status": "ok", "ts": NOW - 3600, "item_id": "1_2_3"},
            {"member": "minion", "status": "quiet", "ts": NOW - 7200, "item_id": "4"},
            {"member": "minion", "status": "started", "ts": NOW - 60, "item_id": "5"}]
    prs = [{"headRefName": "member/minion-1-2", "mergedAt": iso(1), "closingIssuesReferences": [{}, {}, {}]},
           {"headRefName": "fix/by-hand", "mergedAt": iso(1), "closingIssuesReferences": [{}]},
           {"headRefName": "member/minion-9-9", "mergedAt": iso(30), "closingIssuesReferences": [{}]}]
    assert sb.closed_per_run(prs, runs, NOW) == 1.5, sb.closed_per_run(prs, runs, NOW)
    assert sb.closed_per_run(prs, [], NOW) is None
    print("ok  closed_per_run: merged minion closes in 24h / executed minion runs")


def test_shipped_live_needs_a_later_successful_deploy() -> None:
    prs = [{"mergedAt": iso(5)}, {"mergedAt": iso(2)}, {"mergedAt": iso(0.5)}]
    deploys = [{"createdAt": iso(1.9), "conclusion": "success"}, {"createdAt": iso(0.1), "conclusion": "failure"}]
    assert len(sb.live_merges(prs, deploys)) == 2, "the PR merged after the last good deploy is not live"
    print("ok  shipped_live: merged AND carried by a later successful deploy")


def test_backlog_trend_is_net_change() -> None:
    series = [{"day": f"d{i}", "value": v} for i, v in enumerate([300, 310, 320, 330, 334, 330, 320, 310, 290])]
    assert sb.backlog_trend(series) == 290 - 310, sb.backlog_trend(series)
    assert sb.backlog_trend(series[:1]) is None
    print("ok  backlog_trend: last minus 7 points earlier (negative = draining)")


def test_tiles_registered_on_fleet_home() -> None:
    ids = {m["id"] for m in json.loads((HERE / "metrics.json").read_text())["metrics"]}
    for want in ["fleet.items_per_minion_run", "fleet.closed_per_minion_run",
                 "fleet.shipped_live_per_day", "fleet.backlog_trend"]:
        assert want in ids, f"{want} missing from metrics.json (fleet_home renders one tile per row)"
    src = (HERE / "fleet_view_server.py").read_text()
    for want in ["fleet.items_per_minion_run", "fleet.closed_per_minion_run",
                 "fleet.shipped_live_per_day", "fleet.backlog_trend"]:
        assert f'out["{want}"]' in src, f"{want} never computed in metrics_snapshot"
    print("ok  scoreboard tiles registered and computed")


if __name__ == "__main__":
    fails = 0
    for fn in [v for k, v in dict(globals()).items() if k.startswith("test_")]:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            fails += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    sys.exit(1 if fails else 0)
