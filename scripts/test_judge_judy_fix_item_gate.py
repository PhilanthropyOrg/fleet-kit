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

    def test_the_fix_item_passes_the_quality_gate(self):
        # jefe msg#23: every fix item bounced at gru's spec gate for want of a criterion.
        import re, sys
        sys.path.insert(0, str(ROOT / "scripts"))
        import quality_gate as qg
        m = re.search(r'FIX_BODY="(.*?)"\n', self.tail, re.S)
        body = m.group(1).replace("$FINDINGS", "- `app.py:12` raises on empty input")
        ok, reason = qg.classify_candidate(["quality:ship-it"], body, [])
        self.assertTrue(ok, reason)


    def test_a_prose_vision_link_is_not_carried_onto_the_fix_item(self):
        # jefe msg#753 (philanthropy#10782): PR #10781's free-text line was copied verbatim and
        # gru's gate parked the fix item needs-spec. Only a gate-valid line may be inherited.
        import re, subprocess, sys
        sys.path.insert(0, str(ROOT / "scripts"))
        import run_report, vision_link_gate as g
        fn = re.search(r"^gate_valid_vision_link\(\) \{.*?^\}\n", self.src, re.S | re.M).group(0)
        prose = "Vision-link: Atlas revenue (paid product). Moves no funnel KR"
        for line, fallback in ((prose, "Vision-link: none (maintenance) -- x"),
                               (prose, "Vision-link: okr.verified_claims -- guardrail"),
                               ("", "Vision-link: none (maintenance) -- x"),
                               ("Vision-link: okr.conversion -- claim page", "unused")):
            out = subprocess.run(["bash", "-c", fn + 'gate_valid_vision_link "$1" "$2"', "_", line, fallback],
                                 env={"KIT_DIR": str(ROOT), "PATH": "/usr/bin:/bin"},
                                 capture_output=True, text=True).stdout.strip()
            self.assertNotEqual(g._classify_value(run_report._vision_claim(out))[0], g.STATUS_MISSING, out)
        self.assertIn("gate_valid_vision_link", self.tail, "the block path must use the helper")

if __name__ == "__main__":
    unittest.main()
