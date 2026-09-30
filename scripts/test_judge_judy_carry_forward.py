"""judge-judy carries an approval forward when only main moved (2026-09-30).

auto_update_branch.sh merges main into every open PR, and each merge is a new head with no
fleet-code-review status, so judge-judy paid for a full review of the same diff again: #8555
was approved 11 times. The fingerprint is `git patch-id --stable` of the PR diff plus the PR
body, so a main merge keeps it and any change to the PR's own lines or body breaks it.
"""
import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "members" / "judge-judy" / "judge-judy.sh").read_text()

DIFF = """diff --git a/x.py b/x.py
index 1111111..2222222 100644
--- a/x.py
+++ b/x.py
@@ -10,3 +10,3 @@ def f():
     a = 1
-    b = 2
+    b = 3
     return a + b
"""


def patch_id(text: str) -> str:
    out = subprocess.run(["git", "patch-id", "--stable"], input=text, capture_output=True,
                         text=True, check=True).stdout
    return out.split()[0]


class PatchIdTest(unittest.TestCase):
    def test_main_merge_keeps_the_fingerprint(self):
        moved = DIFF.replace("@@ -10,3 +10,3 @@", "@@ -42,3 +42,3 @@").replace(
            "index 1111111..2222222", "index 3333333..4444444")
        self.assertEqual(patch_id(DIFF), patch_id(moved))

    def test_a_changed_pr_line_breaks_it(self):
        self.assertNotEqual(patch_id(DIFF), patch_id(DIFF.replace("b = 3", "b = 4")))


class WiringTest(unittest.TestCase):
    def test_fingerprint_is_taken_before_truncation(self):
        self.assertLess(SRC.index("DIFF_ID=$(git patch-id --stable"),
                        SRC.index('head -c "$MAX_DIFF_BYTES"'))

    def test_body_is_part_of_the_fingerprint(self):
        self.assertIn('FP="$DIFF_ID $(sha256sum < "$BODY_FILE"', SRC)

    def test_carry_runs_before_any_review_spend(self):
        carry = SRC.index('[ "$(head -1 "$FP_FILE")" = "$FP" ]')
        self.assertLess(carry, SRC.index("closes_gate.py"))
        self.assertLess(carry, SRC.index('PROMPT="You are the merge-blocking'))

    def test_approve_records_and_block_clears(self):
        approve = SRC[SRC.index('if [ "$VERDICT" = "VERDICT: approve" ]; then'):]
        record = approve.index('> "$FP_FILE"')
        clear = approve.index('rm -f "$FP_FILE"')
        self.assertLess(record, clear, "approve branch writes, block branch removes")

    def test_carry_never_arms_a_checkpoint(self):
        block = SRC[SRC.index("approval carried, no model call"):]
        block = block[: block.index("continue")]
        self.assertIn('if ! pr_is_checkpoint "$PR"', block)


if __name__ == "__main__":
    unittest.main()
