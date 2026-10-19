"""philanthropy#13212 (Reif 2026-10-19, "its for every bloody page type"): the reviewer's body
rules require a design walk on every PR that changes what a person sees, opened after the rule.
Mutation: delete the design-walk block from judge-judy.sh -> these go RED."""
import unittest
from pathlib import Path

SH = (Path(__file__).resolve().parents[1] / "members/judge-judy/judge-judy.sh").read_text()


class DesignWalkRule(unittest.TestCase):
    def test_design_walk_rule_is_in_the_body_rules(self):
        self.assertIn("'Design walk:'", SH)
        self.assertIn("1440 wide", SH)
        self.assertIn("390 wide", SH)
        self.assertIn("rendered mail, not the template source", SH)
        self.assertIn("Missing walk: set your verdict to block", SH)

    def test_rule_spares_prs_opened_before_it(self):
        self.assertIn('"2026-10-19T18:00:00Z"', SH)
        # appended before the dependabot override, so a pin-only bump still waives body rules
        self.assertLess(SH.index("Design walk:"), SH.index('select(.author.login == "app/dependabot"'))


if __name__ == "__main__":
    unittest.main()
