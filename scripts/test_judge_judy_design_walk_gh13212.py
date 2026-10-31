"""PHIA-1451 (Reif 2026-10-30) replaces philanthropy#13212's design-walk rule: the reviewer
judges value (Cagan's four big risks) and no longer blocks on paperwork -- a 'Design walk:'
line, a 'See it:' line, a plain-words opening, or evidence per criterion. 7 days to 10-30,
~156 of 334 blocks were paperwork only. Mutation: put any of those rules back -> RED."""
import unittest
from pathlib import Path

SH = (Path(__file__).resolve().parents[1] / "members/judge-judy/judge-judy.sh").read_text()


class PaperworkRulesCut(unittest.TestCase):
    def test_no_paperwork_rule_blocks(self):
        for rule in ("Missing walk: set your verdict to block",
                     "'See it: <URL or path>'",
                     "If the first two paragraphs do not, set your verdict to block",
                     "If any criterion is not met, or has no evidence, set your verdict to block"):
            self.assertNotIn(rule, SH)

    def test_value_rubric_is_in_the_prompt(self):
        for word in ("four big risks", "Nielsen", "Kano", "ISO/IEC 25010", "research.ok",
                     "Your verdict is for correctness only"):
            self.assertIn(word, SH)

    def test_dependabot_is_exempt_from_value_send_back(self):
        i = SH.index('select(.author.login == "app/dependabot"')
        self.assertIn('VALUE_EXEMPT="--exempt"', SH[i:i + 1500])


if __name__ == "__main__":
    unittest.main()
