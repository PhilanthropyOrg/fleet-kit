"""gh#6483 (philanthropy): a repeat block at a new head SHA must not re-post the same
finding text onto an already-open fix item.

Before this fix, every re-review of a stuck PR appended a fresh "Blocked again" comment
even when the findings hadn't changed -- #6074 alone collected 16 near-identical entries
this way. AC2 requires checking the fix item's newest comment first and staying silent
(logging "block unchanged", writing nothing) when it already contains this tick's
finding text.
"""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class DedupeCommentTest(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "members" / "judge-judy" / "judge-judy.sh").read_text()
        self.tail = self.src[self.src.index('if [ -n "$EXISTING_FIX" ]; then'):]
        self.existing_block = self.tail[: self.tail.index("\n      else\n")]

    def test_newest_comment_is_fetched_before_commenting(self):
        comment_at = self.existing_block.index('gh issue comment "$EXISTING_FIX"')
        fetch_at = self.existing_block.index("comments[-1]")
        self.assertLess(fetch_at, comment_at, "the newest comment must be read before deciding to comment")

    def test_identical_findings_are_compared(self):
        self.assertIn("FINDINGS", self.existing_block)
        self.assertIn('"$LATEST_FIX_COMMENT" == *"$FINDINGS"*', self.existing_block)

    def test_unchanged_block_logs_and_skips_the_comment(self):
        unchanged_at = self.existing_block.index("block unchanged since")
        comment_at = self.existing_block.index('gh issue comment "$EXISTING_FIX"')
        self.assertLess(unchanged_at, comment_at, "the unchanged-log branch must precede the comment call")

    def test_lookup_failure_falls_through_to_commenting(self):
        # the fetch must tolerate failure (non-fatal) so a broken lookup still comments
        # rather than silently going quiet on a real, unconfirmed block.
        fetch_line = [l for l in self.existing_block.splitlines() if "comments[-1]" in l][0]
        self.assertIn("|| true", fetch_line)


if __name__ == "__main__":
    unittest.main()
