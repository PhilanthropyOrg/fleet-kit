#!/usr/bin/env python3
"""auditor (a1): filing is one issue per pattern, deduped, and only after the kill pass; the
scoreboard counts auditor vs hand-found holes. RED without the fix: the scripts do not exist.
Plain-python test, no pytest -- matches ci.yml.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_file as af  # noqa: E402
import audit_scoreboard as sb  # noqa: E402

GOOD = {"pattern": "csv_link_is_pdf", "lane": "990-truth",
        "title": "Report CSV link opens the PDF", "count": 1840,
        "sample_urls": ["https://philanthropy.org/990/report/362193608/x"],
        "evidence": "CSV href == PDF href", "fix": "one download-link helper in record.html",
        "guard_test": "tests/test_report_links.py", "given": "a report", "when": "CSV clicked",
        "then": "a text/csv file downloads"}


def test_a_page_report_without_count_or_shared_fix_is_dropped():
    bad = {**GOOD, "count": 0, "fix": ""}
    [(action, _, detail)] = af.plan([bad], [], {})
    assert action == "drop" and "fix" in detail and "count" in detail, detail


def test_an_open_pattern_gets_a_comment_not_a_twin():
    open_issue = {"number": 901, "body": "x\nAudit-pattern: 990-truth:csv_link_is_pdf\n"}
    [(action, _, detail)] = af.plan([GOOD], [open_issue], {})
    assert (action, detail) == ("comment", 901)


def test_only_findings_the_kill_pass_keeps_are_filed():
    key = af.pattern_key(GOOD)
    assert af.plan([GOOD], [], {key: (True, "real")})[0][0] == "file"
    assert af.plan([GOOD], [], {key: (False, "weak")})[0][0] == "drop"
    assert af.plan([GOOD], [], {})[0][0] == "drop"  # no verdict = not filed (fail closed)


def test_issue_body_carries_count_gwt_vision_link_and_pattern_key():
    body = af.issue_body(GOOD)
    for want in ("1840 affected", "## Fix in shared code", "## Guard test", "- Given a report",
                 "Vision-link: okr.traffic", "Audit-pattern: 990-truth:csv_link_is_pdf"):
        assert want in body, want


def test_scoreboard_counts_open_viken_rows_and_auditor_issues():
    text = ("| V1a | Schedules row | LIVE | x |\n| V1b | CSV link | NOT STARTED | x |\n"
            "| V10 | Scout+ | context only | x |\n| V2 | Endowment box | NOT STARTED | x |\n")
    assert [v for v, _ in sb.viken_holes(text)] == ["V1b", "V2"]
    now = dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc)
    issues = [
        {"number": 1, "title": "Report CSV", "createdAt": "2026-10-15T00:00:00Z",
         "labels": [{"name": "lane:audit"}]},
        {"number": 2, "title": "Jobs leaves hospitals in", "createdAt": "2026-10-15T00:00:00Z",
         "labels": [{"name": "lane:ui"}]},
        {"number": 3, "title": "Jobs old", "createdAt": "2026-10-01T00:00:00Z", "labels": []},
    ]
    audit, hand = sb.split_issues(issues, now - dt.timedelta(days=7))
    assert [i["number"] for i in audit] == [1] and [i["number"] for i in hand] == [2]
    md = sb.render(sb.viken_holes(text), audit, hand, now)
    assert "| Auditor (lane:audit) | 1 |" in md and "| Viken (open rows in viken-status.md) | 2 |" in md


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
