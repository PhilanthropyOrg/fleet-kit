"""judge-judy re-reviews a blocked head whose body was edited after the block (2026-10-16).

A block often asks for a body-only fix ("change Closes to Part of"), which leaves the head as
it was, and pick_pr skipped any head that already had a verdict: philanthropy#12618 sat 13h on
a block its body had already answered. The jq here picks the latest verdict only if it is a block.
"""

import json
import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "members" / "judge-judy" / "judge-judy.sh").read_text()
PICK = SRC[SRC.index("pick_pr() {") : SRC.index("report_run() {")]
JQ = re.search(r"block_at=\$\(jq -r --arg c \"\$CONTEXT\" '([^']+)'", PICK).group(1)


def block_at(statuses) -> str:
    return subprocess.run(
        ["jq", "-r", "--arg", "c", "fleet-code-review", JQ],
        input=json.dumps(statuses),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class BodyEditReReviewTest(unittest.TestCase):
    def test_latest_block_gives_its_time(self):
        st = [
            {
                "context": "fleet-code-review",
                "state": "failure",
                "updated_at": "2026-10-16T01:31:39Z",
            },
            {
                "context": "fleet-code-review",
                "state": "pending",
                "updated_at": "2026-10-16T01:30:00Z",
            },
        ]
        self.assertEqual(block_at(st), "2026-10-16T01:31:39Z")

    def test_an_approval_is_never_reopened(self):
        st = [
            {
                "context": "fleet-code-review",
                "state": "success",
                "updated_at": "2026-10-16T02:00:00Z",
            },
            {
                "context": "fleet-code-review",
                "state": "failure",
                "updated_at": "2026-10-16T01:00:00Z",
            },
        ]
        self.assertEqual(block_at(st), "")

    def test_other_contexts_are_ignored(self):
        st = [
            {"context": "ci", "state": "failure", "updated_at": "2026-10-16T02:00:00Z"}
        ]
        self.assertEqual(block_at(st), "")

    def test_an_unreadable_edit_time_keeps_the_skip(self):
        self.assertIn(
            'if [ -z "$edited_at" ] || [[ ! "$edited_at" > "$block_at" ]]; then continue; fi',
            PICK,
        )


if __name__ == "__main__":
    unittest.main()
