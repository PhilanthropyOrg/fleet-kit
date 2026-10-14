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
import unittest

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


class Rules(unittest.TestCase):
    def test_a_quiet_day_files_nothing(self):
        self.assertEqual(jobs_daily.rules(_doc(), _doc()), [])

    def test_a_first_day_with_no_yesterday_only_checks_todays_shape(self):
        self.assertEqual(jobs_daily.rules(_doc(), None), [])

    def test_each_rule_names_one_finding_with_a_stable_title(self):
        prev = _doc()
        cases = [
            (_doc(open_n=1000, new=0, closed=0), "Jobs harvest did not run in the last day"),
            (_doc(boards=30), "Jobs boards fell by more than a quarter in a day"),
            (_doc(open_n=600, with_role=500), "Open jobs fell by more than a third in a day"),
            (_doc(with_role=400), "Half of the open jobs have no role page"),
            (_doc(state_pages=0), "Jobs pay pages are empty"),
        ]
        for today, title in cases:
            titles = [f.title for f in jobs_daily.rules(today, prev)]
            self.assertEqual(titles, [title])
            self.assertFalse(any(ch.isdigit() for ch in title), title)  # numbers live in the body

    def test_a_failed_section_or_missing_tables_is_one_finding_and_stops(self):
        [f] = jobs_daily.rules(_doc(errors={"jobs": "OperationalError: x"}), _doc())
        self.assertEqual(f.title, "Jobs KPI section failed")
        self.assertIn("OperationalError", f.body)
        [f] = jobs_daily.rules({"errors": {"jobs.fetch": "URLError: down"}}, _doc())
        self.assertEqual(f.title, "Jobs KPI section failed")
        self.assertIn("URLError", f.body)
        doc = _doc()
        doc["jobs"]["postings"] = {"open": None, "orgs_hiring": None, "open_with_role": None, "daily_30d": None}
        [f] = jobs_daily.rules(doc, _doc())
        self.assertEqual(f.title, "Jobs tables are missing on prod")

    def test_a_body_carries_the_fleet_gate_and_the_numbers(self):
        [f] = jobs_daily.rules(_doc(boards=30), _doc())
        for line in ("Vision-link: okr.traffic", "Strategy: sauce", "Best in class:", "Given ", "When ", "Then "):
            self.assertIn(line, f.body)
        self.assertIn("- boards: 30 (yesterday 50)", f.body)


class Report(unittest.TestCase):
    def test_the_report_is_a_table_with_both_days_and_what_was_filed(self):
        text = jobs_daily.render(_doc(), _doc(open_n=900), [], [])
        self.assertIn("| open jobs | 1,000 | 900 |", text)
        self.assertIn("Nothing to file", text)
        findings = jobs_daily.rules(_doc(boards=30), _doc())
        text = jobs_daily.render(_doc(boards=30), _doc(), findings, ["https://github.com/x/y/issues/9"])
        self.assertIn("- Jobs boards fell by more than a quarter in a day (https://github.com/x/y/issues/9)", text)
        text = jobs_daily.render(None, None, [], [])
        self.assertIn("| open jobs | – | – |", text)


class Plumbing(unittest.TestCase):
    def test_save_and_previous_read_the_last_day_before_today(self):
        keep = jobs_daily.SIG_DIR
        with tempfile.TemporaryDirectory() as d:
            jobs_daily.SIG_DIR = pathlib.Path(d)
            try:
                jobs_daily.save(_doc(open_n=1), "2026-10-12")
                jobs_daily.save(_doc(open_n=2), "2026-10-13")
                jobs_daily.save(_doc(open_n=3), "2026-10-14")
                self.assertEqual(jobs_daily.previous("2026-10-14")["jobs"]["postings"]["open"], 2)
                self.assertIsNone(jobs_daily.previous("2026-10-12"))
                saved = json.loads((pathlib.Path(d) / "jobs-2026-10-14.json").read_text())
                self.assertEqual(saved["jobs"]["postings"]["open"], 3)
            finally:
                jobs_daily.SIG_DIR = keep

    def test_filing_goes_through_issue_cluster_with_the_bug_labels(self):
        calls = []

        class FakeCluster:
            @staticmethod
            def file_issue(title, body, labels, who=""):
                calls.append((title, labels, who))
                return {"ok": True, "action": "created", "url": "https://github.com/x/y/issues/42"}

        keep = sys.modules.get("issue_cluster")
        sys.modules["issue_cluster"] = FakeCluster  # type: ignore[assignment]
        try:
            refs = jobs_daily.file_findings(jobs_daily.rules(_doc(boards=30), _doc()))
        finally:
            if keep is None:
                del sys.modules["issue_cluster"]
            else:
                sys.modules["issue_cluster"] = keep
        self.assertEqual(refs, ["https://github.com/x/y/issues/42"])
        [(title, labels, who)] = calls
        self.assertEqual(title, "Jobs boards fell by more than a quarter in a day")
        self.assertEqual(labels, ["fleet:backlog", "quality:solid", "bug"])
        self.assertEqual(who, "jobs_daily")

    def test_base_url_comes_from_the_number_url(self):
        keep = os.environ.get("FLEET_NUMBER_URL")
        try:
            os.environ["FLEET_NUMBER_URL"] = "https://philanthropy.org/990/api/number"
            self.assertEqual(jobs_daily.base_url(), "https://philanthropy.org/990")
            os.environ["FLEET_NUMBER_URL"] = ""
            self.assertEqual(jobs_daily.base_url(), "")
        finally:
            if keep is None:
                os.environ.pop("FLEET_NUMBER_URL", None)
            else:
                os.environ["FLEET_NUMBER_URL"] = keep


if __name__ == "__main__":
    unittest.main()
