"""judge-judy's re-review of a blocked PR starts from the last block's findings (2026-10-08).

Each re-review used to be a fresh full review, so each round found something new: 80 PRs took
216 reviews in a day and #11647 was blocked six times on six different findings. The block
writes its head and findings to pr-<N>.block; the next review is told to check those first and
to block on a new finding only if it is high or in code the push changed.
"""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "members" / "judge-judy" / "judge-judy.sh").read_text()


class PriorBlockTest(unittest.TestCase):
    def test_block_records_head_and_findings(self):
        block = SRC[SRC.index('if [ "$VERDICT" = "VERDICT: approve" ]; then'):]
        block = block[block.index("  else\n"):]
        self.assertIn('printf \'%s\\n%s\\n\' "$HEAD_SHA" "$FINDINGS" > "$BLOCK_FILE"', block)

    def test_approve_clears_the_record(self):
        approve = SRC[SRC.index('if [ "$VERDICT" = "VERDICT: approve" ]; then'):]
        self.assertLess(approve.index('rm -f "$BLOCK_FILE"'), approve.index("  else\n"))

    def test_prompt_carries_the_prior_findings(self):
        prompt = SRC[SRC.index('PROMPT="You are the merge-blocking'):]
        prompt = prompt[:prompt.index('A block with zero findings is not a valid answer."')]
        self.assertIn("${PRIOR_NOTE}", prompt)
        self.assertLess(SRC.index('PRIOR_NOTE=""'), SRC.index('PROMPT="You are the merge-blocking'))

    def test_only_a_new_high_blocks(self):
        note = SRC[SRC.index('PRIOR_NOTE="This PR was reviewed before.'):]
        self.assertIn("blocks this round only if its severity is high", note[:900])


if __name__ == "__main__":
    unittest.main()
