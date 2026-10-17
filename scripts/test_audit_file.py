#!/usr/bin/env python3
"""auditor (a1): filing is one issue per pattern, deduped, and only after the kill pass; the
scoreboard counts auditor vs hand-found holes. RED without the fix: the scripts do not exist.
Plain-python test, no pytest -- matches ci.yml.
"""
from __future__ import annotations

import datetime as dt
import json
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_file as af  # noqa: E402
import audit_sample as asm  # noqa: E402
import issue_create_hook as hook  # noqa: E402
import quality_gate  # noqa: E402
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


def test_a_filed_finding_passes_the_issue_create_hook_and_gru_intake():
    # fk5: the first real finding (990-PF source line) was approved, then never reached the board.
    cmd = shlex.join(["gh", "issue", "create", "--repo", "PhilanthropyOrg/philanthropy",
                      "--title", GOOD["title"], "--label", af.LABELS, "--body", af.issue_body(GOOD)])
    assert hook.decide(cmd, list_open=lambda repo: []) is None
    eligible, why = quality_gate.classify_candidate(af.LABELS.split(","), af.issue_body(GOOD), [])
    assert eligible, why


def test_a_finding_the_intake_gate_would_drop_is_not_filed():
    bad = {**GOOD, "vision_link": "Targets KR2"}
    [(action, _, detail)] = af.plan([bad], [], {af.pattern_key(bad): (True, "real")})
    assert action == "drop" and "vision-link" in detail, detail


def test_missing_labels_are_created_once_and_a_failed_create_says_so():
    calls = []

    def gh(args):
        calls.append(args[:3])
        if args[:2] == ["label", "create"] and args[2] == "quality:solid":
            raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 403")
        return '["bug", "fleet:backlog"]' if args[:2] == ["label", "list"] else ""

    try:
        af.ensure_labels("o/r", gh=gh)
        raise AssertionError("a label that cannot be created must stop the run")
    except SystemExit as e:
        assert "quality:solid" in str(e) and "HTTP 403" in str(e)
    assert ["label", "create", "lane:audit"] in calls
    assert ["label", "create", "bug"] not in calls


def test_a_stratum_that_times_out_prints_an_error_row():
    import contextlib
    import io

    real, asm.query = asm.query, lambda sql: (_ for _ in ()).throw(RuntimeError("canceling statement due to "
                                                               "statement timeout"))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        asm.main(["--lane", "orgs", "--slot", "0"])
    asm.query = real
    rows = [json.loads(line) for line in out.getvalue().splitlines()]
    assert {r["stratum"] for r in rows if "timeout" in r.get("error", "")} == set(asm.ORG_STRATA)
    assert "largest" in err.getvalue()


def test_largest_reads_the_revenue_column_not_the_filings_join():
    sql = asm.sql_for(asm.ORG_STRATA, 0)["largest"]
    assert "ORDER BY revenue_amt DESC" in sql and "JOIN" not in sql, sql


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
