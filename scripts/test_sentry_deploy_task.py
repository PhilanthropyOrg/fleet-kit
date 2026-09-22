"""sentry_deploy_task.py builds sentry's --task string from what just shipped (gh#1217).

RED without build_task deriving-and-capping logic: a naive "join every PR title" either
produces nothing when prs is empty (wrong -- caller needs the empty-string fallback contract
honored precisely) or lists every PR unbounded (wrong -- a big auto-merge batch would eat
sentry's whole pass on step 0 rather than leaving budget for its normal steps 1-9, the exact
risk gh#1217 names).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sentry_deploy_task as sdt  # noqa: E402


class BuildTaskTest(unittest.TestCase):
    def test_empty_prs_yields_empty_string(self):
        self.assertEqual(sdt.build_task([], max_prs=5), "")

    def test_single_pr_names_number_title_and_body_first_line(self):
        prs = [{"number": 42, "title": "Fix login bug",
                "body": "Users could not sign in with expired sessions.\n\nMore detail here."}]
        task = sdt.build_task(prs, max_prs=5)
        self.assertIn("#42: Fix login bug", task)
        self.assertIn("Users could not sign in with expired sessions.", task)
        self.assertIn("derive in one sentence what person and what job", task)
        self.assertIn("steps 1-9", task)

    def test_title_only_body_is_not_duplicated(self):
        prs = [{"number": 7, "title": "Bump dependency", "body": "Bump dependency"}]
        task = sdt.build_task(prs, max_prs=5)
        # The body's first line equals the title -- must not print it twice.
        self.assertEqual(task.count("Bump dependency"), 1)

    def test_caps_at_max_prs_and_states_the_overflow_explicitly(self):
        prs = [{"number": i, "title": f"PR {i}", "body": ""} for i in range(1, 8)]
        task = sdt.build_task(prs, max_prs=3)
        for i in range(1, 4):
            self.assertIn(f"#{i}: PR {i}", task)
        for i in range(4, 8):
            self.assertNotIn(f"#{i}: PR {i}", task)
        self.assertIn("4 additional PR(s)", task)

    def test_no_overflow_note_when_under_the_cap(self):
        prs = [{"number": 1, "title": "Solo PR", "body": ""}]
        task = sdt.build_task(prs, max_prs=5)
        self.assertNotIn("additional PR(s)", task)

    def test_missing_body_does_not_crash(self):
        prs = [{"number": 9, "title": "No body field"}]
        task = sdt.build_task(prs, max_prs=5)
        self.assertIn("#9: No body field", task)


class FetchRecentMergedPrsFailsOpenTest(unittest.TestCase):
    def test_gh_failure_returns_empty_list_not_an_exception(self):
        # A repo path that cannot possibly be a git repo gh can query -- gh exits nonzero.
        prs = sdt.fetch_recent_merged_prs("/nonexistent-repo-path-xyz", window_s=3600)
        self.assertEqual(prs, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
