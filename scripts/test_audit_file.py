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


def test_a_doubled_lane_prefix_is_the_same_pattern():
    # 10-17: drafts with pattern "scout-report:x" filed as "scout-report:scout-report:x".
    doubled = {**GOOD, "pattern": "990-truth:csv_link_is_pdf"}
    assert af.pattern_key(doubled) == af.pattern_key(GOOD) == "990-truth:csv_link_is_pdf"
    old = {"number": 902, "body": "Audit-pattern: 990-truth:990-truth:csv_link_is_pdf\n"}
    assert af.plan([GOOD], [old], {})[0][:3:2] == ("comment", 902)


def test_same_defect_under_a_new_key_comments_on_the_open_issue():
    # #12944, #13012, #13029: one months-of-cash bug, three keys, three issues (10-17).
    renamed = {**GOOD, "pattern": "csv_href_points_at_pdf"}
    open_issue = {"number": 903, "title": "Report CSV link opens the PDF", "body": "x"}
    key = af.pattern_key(renamed)
    assert af.plan([renamed], [open_issue], {key: (False, "same as #903", 903)})[0][:3:2] == (
        "comment", 903)
    # a duplicate_of that is not an open issue is a plain kill, never a comment elsewhere
    assert af.plan([renamed], [open_issue], {key: (False, "dup", 77)})[0][0] == "drop"
    assert "#903 Report CSV link opens the PDF" in af.kill_prompt([renamed], [open_issue])


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



def _job_page(pid, ein="530196605", ld=True):
    js = f'<script type="application/ld+json">{{"title": "Job {pid}"}}</script>' if ld else ""
    return f'{js}<h1>Job {pid}</h1><a class="btn ghost" href="/jobs/at/{ein}/x">All jobs</a>'


def _fake_site(gone=(), broken=()):
    """A fake fetcher for the jobs sampler: every list page links 12-hex pids, every /go hop
    answers 302 to one ATS by pid, prior pids in `gone` answer 410, paths in `broken` 400."""
    hosts = {"a": "x.wd1.myworkdayjobs.com", "b": "boards.greenhouse.io", "c": "jobs.lever.co",
             "d": "workforcenow.adp.com"}
    lists = {"/jobs/all": "a00000000001 b00000000002", "/jobs/leadership-openings": "c00000000003",
             "/jobs/pay/ceo": "d00000000004 a00000000005"}

    def fetch(path, follow=True):
        if path in broken:
            return 400, "", ""
        if path == "/jobs":
            return 200, "", ('<x id="pay"><a href="/jobs/pay/clerk">x</a> $40,000 '
                             '<a href="/jobs/pay/ceo">x</a> $250,000')
        if path in lists:
            return 200, "", " ".join(f'href="/jobs/j/{p}/s"' for p in lists[path].split())
        pid = path.split("/")[3]
        if path.endswith("/go"):
            return 302, f"https://{hosts[pid[0]]}/job/1", ""
        return (410, "", "") if pid in gone else (200, "", _job_page(pid))
    return fetch


def _jobs_rows(fetch, state):
    real = asm.fetch
    asm.fetch, asm.JOBS_STATE = fetch, str(state)
    try:
        return asm.jobs_sample(0)
    finally:
        asm.fetch = real


def test_every_jobs_stratum_yields_rows_from_public_pages():
    import tempfile
    state = Path(tempfile.mkdtemp()) / "pids.json"
    state.write_text(json.dumps(["e00000000009"]))
    rows = _jobs_rows(_fake_site(gone={"e00000000009"}), state)
    got = {r["stratum"] for r in rows if "pid" in r}
    want = {"newest", "highest_paid", "leadership", "workday", "greenhouse", "lever", "adp",
            "closed"}
    assert got == want, rows
    lead = next(r for r in rows if r["stratum"] == "leadership")
    assert (lead["ein"], lead["title"], lead["apply_host"]) == (
        "530196605", "Job c00000000003", "jobs.lever.co"), lead
    assert next(r for r in rows if r["stratum"] == "highest_paid")["pid"] == "d00000000004"
    assert next(r for r in rows if r["stratum"] == "closed")["status"] == 410
    assert "a00000000001" in json.loads(state.read_text())  # next pass can find it closed


def test_a_400_or_timeout_on_a_jobs_page_is_an_error_row_never_silence():
    import tempfile
    state = Path(tempfile.mkdtemp()) / "pids.json"
    rows = _jobs_rows(_fake_site(broken={"/jobs/leadership-openings"}), state)
    assert any(r["stratum"] == "leadership" and "HTTP 400" in r["error"] for r in rows), rows

    def timeout(path, follow=True):
        raise TimeoutError("timed out")
    rows = _jobs_rows(timeout, state)
    errs = {r["stratum"] for r in rows if "error" in r}
    assert errs >= {"newest", "highest_paid", "leadership", "workday", "closed"}, rows
    assert not [r for r in rows if "pid" in r], rows


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
