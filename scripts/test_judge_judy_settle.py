"""judge-judy leaves a head that is minutes old to a later tick (2026-10-05).

Every push fires judge-judy through the webhook, so a head seconds old was reviewed while its
author was still pushing: 172 of 565 reviews on 2026-10-03..04 ($18) were replaced by the next
head within 10 minutes. pick_pr now skips a head younger than JUDGE_JUDY_SETTLE_S, except a
main-merge head (carry-forward approves those with no model call) and an explicit PR.
"""
import json
import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "members" / "judge-judy" / "judge-judy.sh").read_text()
PICK = SRC[SRC.index("pick_pr() {"):SRC.index("report_run() {")]
JQ = re.search(r"gh pr view \"\$pr\" --json headRefOid,commits -q '([^']+)'", PICK).group(1)


def head_line(commits) -> str:
    doc = json.dumps({"headRefOid": "abc123", "commits": commits})
    return subprocess.run(["jq", "-r", JQ], input=doc, capture_output=True, text=True,
                          check=True).stdout.rstrip("\n")


class SettleTest(unittest.TestCase):
    def test_a_code_commit_carries_its_time(self):
        line = head_line([{"messageHeadline": "Fix x", "committedDate": "2026-10-05T01:00:00Z"}])
        self.assertEqual(line, "abc123 2026-10-05T01:00:00Z")

    def test_a_main_merge_has_no_time_so_it_never_waits(self):
        line = head_line([{"messageHeadline": "Merge origin/main into b",
                           "committedDate": "2026-10-05T01:00:00Z"}])
        self.assertEqual(line.split(" ")[0], "abc123")
        self.assertEqual(line.split(" ", 1)[1], "")

    def test_no_commits_still_yields_the_head(self):
        self.assertEqual(head_line([]).split(" ")[0], "abc123")

    def test_the_wait_skips_explicit_runs_and_can_be_turned_off(self):
        self.assertIn('SETTLE_S="${JUDGE_JUDY_SETTLE_S:-600}"', SRC)
        self.assertIn('[ -z "$explicit" ] && [ "$SETTLE_S" -gt 0 ] && [ -n "$pushed_at" ]', PICK)
        # the wait comes before the status lookup, so a fresh head costs no extra API call
        self.assertLess(PICK.index("SETTLE_S"), PICK.index("statuses=$("))


if __name__ == "__main__":
    unittest.main()
