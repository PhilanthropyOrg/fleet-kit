"""jobs_daily.py: the rules are plain comparisons, the report reads, filing dedupes by title.

Reif, 2026-10-14: self-improvement runs daily. Given yesterday's and today's KPI `jobs`
section, When the rules run, Then each broken thing is one Finding with a stable title
and a body that carries the fleet gate lines; When nothing broke, Then nothing is filed.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import jobs_daily  # noqa: E402


def _doc(open_n=1000, with_role=800, boards=50, state_pages=40, new=30, closed=20, errors=None):
    return {
        "errors": errors or {},
        "jobs": {
            "postings": {
                "open": open_n,
                "orgs_hiring": 45,
                "open_with_role": with_role,
                "roles_with_open_jobs": 60,
                "daily_30d": [{"d": "2026-10-13", "new": 5, "closed": 1}, {"d": "2026-10-14", "new": new, "closed": closed}],
            },
            "boards": {"total": boards, "by_proof": {"site-link": 30, "name": boards - 30}, "jobs_on_boards": 1100, "by_ats": {"greenhouse": boards}},
            "pay": {"cells": 34000, "titles": 120, "pages_by_place": {"us": 120, "state": state_pages, "city": 300}},
            "posted": {"by_status": {"draft": 2, "open": 1}},
            "pages_indexable": 1300,
        },
    }


def test_a_quiet_day_files_nothing():
    assert jobs_daily.rules(_doc(), _doc()) == []


def test_a_first_day_with_no_yesterday_only_checks_todays_shape():
    assert jobs_daily.rules(_doc(), None) == []


def test_each_rule_names_one_finding_with_a_stable_title():
    prev = _doc()
    titles = [f.title for f in jobs_daily.rules(_doc(open_n=1000, new=0, closed=0), prev)]
    assert titles == ["Jobs harvest did not run in the last day"]
    titles = [f.title for f in jobs_daily.rules(_doc(boards=30), prev)]
    assert titles == ["Jobs boards fell by more than a quarter in a day"]
    titles = [f.title for f in jobs_daily.rules(_doc(open_n=600, with_role=500), prev)]
    assert titles == ["Open jobs fell by more than a third in a day"]
    titles = [f.title for f in jobs_daily.rules(_doc(with_role=400), prev)]
    assert titles == ["Half of the open jobs have no role page"]
    titles = [f.title for f in jobs_daily.rules(_doc(state_pages=0), prev)]
    assert titles == ["Jobs pay pages are empty"]
    for t in titles:
        assert not any(ch.isdigit() for ch in t), t  # numbers live in the body, so twins dedupe


def test_a_failed_section_or_missing_tables_is_one_finding_and_stops():
    [f] = jobs_daily.rules(_doc(errors={"jobs": "OperationalError: x"}), _doc())
    assert f.title == "Jobs KPI section failed" and "OperationalError" in f.body
    [f] = jobs_daily.rules({"errors": {"jobs.fetch": "URLError: down"}}, _doc())
    assert f.title == "Jobs KPI section failed" and "URLError" in f.body
    doc = _doc()
    doc["jobs"]["postings"] = {"open": None, "orgs_hiring": None, "open_with_role": None, "daily_30d": None}
    [f] = jobs_daily.rules(doc, _doc())
    assert f.title == "Jobs tables are missing on prod"


def test_a_body_carries_the_fleet_gate_and_the_numbers():
    [f] = jobs_daily.rules(_doc(boards=30), _doc())
    for line in ("Vision-link: okr.traffic", "Strategy: sauce", "Best in class:", "Given ", "When ", "Then "):
        assert line in f.body, line
    assert "- boards: 30 (yesterday 50)" in f.body


def test_the_report_is_a_table_with_both_days_and_what_was_filed():
    text = jobs_daily.render(_doc(), _doc(open_n=900), [], [])
    assert "| open jobs | 1,000 | 900 |" in text
    assert "Nothing to file" in text
    findings = jobs_daily.rules(_doc(boards=30), _doc())
    text = jobs_daily.render(_doc(boards=30), _doc(), findings, ["https://github.com/x/y/issues/9"])
    assert "- Jobs boards fell by more than a quarter in a day (https://github.com/x/y/issues/9)" in text
    text = jobs_daily.render(None, None, [], [])
    assert "| open jobs | – | – |" in text


def test_save_and_previous_read_the_last_day_before_today(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(jobs_daily, "SIG_DIR", pathlib.Path(d))
        jobs_daily.save(_doc(open_n=1), "2026-10-12")
        jobs_daily.save(_doc(open_n=2), "2026-10-13")
        jobs_daily.save(_doc(open_n=3), "2026-10-14")
        assert jobs_daily.previous("2026-10-14")["jobs"]["postings"]["open"] == 2
        assert jobs_daily.previous("2026-10-12") is None
        assert json.loads((pathlib.Path(d) / "jobs-2026-10-14.json").read_text())["jobs"]["postings"]["open"] == 3


def test_filing_goes_through_issue_cluster_with_the_bug_labels(monkeypatch):
    calls = []

    class FakeCluster:
        @staticmethod
        def file_issue(title, body, labels, who=""):
            calls.append((title, labels, who))
            return {"ok": True, "action": "created", "url": "https://github.com/x/y/issues/42"}

    monkeypatch.setitem(sys.modules, "issue_cluster", FakeCluster)
    refs = jobs_daily.file_findings(jobs_daily.rules(_doc(boards=30), _doc()))
    assert refs == ["https://github.com/x/y/issues/42"]
    [(title, labels, who)] = calls
    assert title == "Jobs boards fell by more than a quarter in a day"
    assert labels == ["fleet:backlog", "quality:solid", "bug"] and who == "jobs_daily"


def test_base_url_comes_from_the_number_url(monkeypatch):
    monkeypatch.setenv("FLEET_NUMBER_URL", "https://philanthropy.org/990/api/number")
    assert jobs_daily.base_url() == "https://philanthropy.org/990"
    monkeypatch.setenv("FLEET_NUMBER_URL", "")
    assert jobs_daily.base_url() == ""


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
