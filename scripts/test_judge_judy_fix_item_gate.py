"""fk#1154: judge-judy files a fix item only for a fleet-authored PR, and only one per PR.

A human's PR already has a human on it: the review comment is the whole hand-off. PR #6802
(a person's branch) took six blocks in three hours on 2026-09-18 and got six fix items,
all closed by marie as cruft. For a fleet PR, a re-push that fails again must comment on
the open item, not file a twin with a fresh summary in the title.
"""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FixItemGateTest(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "members" / "judge-judy" / "judge-judy.sh").read_text()
        self.tail = self.src[self.src.index('FIX_TITLE="fix: PR #$PR failed code review"'):]

    def test_the_head_branch_is_read(self):
        self.assertIn("headRefName", self.src, "nothing reads which branch the PR is on")

    def test_only_a_fleet_branch_gets_a_fix_item(self):
        file_at = self.tail.index('board_github.py" file "$FIX_TITLE"')
        gate_at = self.tail.index("!= member/*")
        self.assertLess(gate_at, file_at, "the member/ gate must come before the file call")

    def test_a_repeat_block_comments_on_the_open_item(self):
        file_at = self.tail.index('board_github.py" file "$FIX_TITLE"')
        before = self.tail[:file_at]
        self.assertIn("EXISTING_FIX", before, "no lookup for an already-open fix item")
        self.assertIn("gh issue comment", before, "a repeat must comment, not file")
        self.assertIn('startswith(p)', before, "lookup must match on the title prefix, not the exact title")


if __name__ == "__main__":
    unittest.main()
