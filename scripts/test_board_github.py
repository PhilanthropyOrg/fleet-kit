"""Regression test for board_github.py's claim-selection exclusions, gh#5870.

This file forked from nonprofit-atlas's `scripts/fleet/board_github.py` and never picked up
its `is_human_blocked()` / `is_blocked_by_open_issue()` predicates, so `fleet:needs-human-op`
and a `Blocked by #N` line were silent no-ops for every claim made through this copy --
confirmed live against philanthropy#4907, re-selected three times after the label was applied
specifically to stop that.

Run: python3 scripts/test_board_github.py
"""
from __future__ import annotations

import json
import sys
import unittest
import unittest.mock
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "scripts"))

import board_github as bg  # noqa: E402


class IsHumanBlockedTests(unittest.TestCase):
    def test_needs_human_op_label_is_blocked(self):
        self.assertTrue(bg.is_human_blocked({"labels": [{"name": "fleet:needs-human-op"}]}))

    def test_other_labels_are_not_blocked(self):
        self.assertFalse(bg.is_human_blocked({"labels": [{"name": "lane:ui"}]}))
        self.assertFalse(bg.is_human_blocked({"labels": []}))
        self.assertFalse(bg.is_human_blocked({}))


class BlockedByOpenIssueTests(unittest.TestCase):
    def test_blocked_by_line_parses_only_declared_dependencies(self):
        issue = {"body": "Sequence: blocked by #4993, unrelated mention of #10 in prose"}
        self.assertEqual(bg.blocked_by_numbers(issue), [4993])
        self.assertTrue(bg.is_blocked_by_open_issue(issue, {4993}))
        self.assertFalse(bg.is_blocked_by_open_issue(issue, {4991}))
        self.assertFalse(bg.is_blocked_by_open_issue({"body": "no deps"}, {4993}))


def _fake_run(issues):
    def run(cmd):
        if cmd[:3] == ["gh", "issue", "list"]:
            return 0, json.dumps(issues)
        return 0, ""
    return run


class ListUnclaimedTests(unittest.TestCase):
    def test_list_unclaimed_drops_human_blocked_items(self):
        """AC1: a `fleet:needs-human-op` issue is absent from list_unclaimed().

        MUTATION: drop the is_human_blocked() exclusion from list_unclaimed() and this goes
        RED -- the exact gap gh#5870 reports, since this copy has no such exclusion today.
        """
        issues = [
            {"number": 1, "title": "pickable", "body": "", "labels": []},
            {"number": 2, "title": "blocked", "body": "",
             "labels": [{"name": "fleet:needs-human-op"}]},
        ]
        with unittest.mock.patch.object(bg, "_run", _fake_run(issues)):
            self.assertEqual([i["id"] for i in bg.list_unclaimed()], [1])

    def test_list_unclaimed_drops_items_blocked_by_an_open_board_item(self):
        """AC2: `Blocked by #N` drops the item while #N is open, keeps it once #N closes."""
        issues_open = [
            {"number": 4993, "title": "the blocker", "body": "", "labels": []},
            {"number": 5000, "title": "waits on 4993", "body": "Blocked by #4993", "labels": []},
        ]
        with unittest.mock.patch.object(bg, "_run", _fake_run(issues_open)):
            self.assertEqual([i["id"] for i in bg.list_unclaimed()], [4993])

        issues_closed = [
            {"number": 5000, "title": "waits on 4993", "body": "Blocked by #4993", "labels": []},
        ]
        with unittest.mock.patch.object(bg, "_run", _fake_run(issues_closed)):
            self.assertEqual(sorted(i["id"] for i in bg.list_unclaimed()), [5000])

    def test_list_unclaimed_still_drops_claimed_items(self):
        issues = [
            {"number": 1, "title": "free", "body": "", "labels": []},
            {"number": 2, "title": "taken", "body": "", "labels": [{"name": "fleet:claimed"}]},
        ]
        with unittest.mock.patch.object(bg, "_run", _fake_run(issues)):
            self.assertEqual([i["id"] for i in bg.list_unclaimed()], [1])


if __name__ == "__main__":
    unittest.main()
