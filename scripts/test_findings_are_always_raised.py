"""Reif 2026-09-19: "Need to make sure every issue found by fleet gets raised. Marie can
triage later."

persona_law.md §7c is the fleet-wide version of the sentry incident: a member may not decide a
finding is not worth filing. Dedup is mechanical (a tool with a key), triage is marie's, and a
pass that found defects and filed none may not report QUIET."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAW = ROOT / "agents" / "persona_law.md"


class FindingsAreAlwaysRaised(unittest.TestCase):
    def setUp(self):
        self.law = LAW.read_text()

    def test_section_exists_and_is_numbered_in_place(self):
        self.assertIn("## 7c.", self.law)
        self.assertLess(self.law.index("## 7c."), self.law.index("## 7b."),
                        "7c must precede 7b's anchor point it was inserted before")

    def test_triage_is_maries_job_not_the_members(self):
        self.assertIn("Triage is marie's job, never yours", self.law)

    def test_the_four_forbidden_judgements_are_named(self):
        for phrase in ("A sibling pass already found this",
                       "The board is at cap",
                       "It is small / cosmetic / not my lane",
                       "It might be a false positive"):
            self.assertIn(phrase, self.law, f"missing forbidden judgement: {phrase}")

    def test_quiet_is_defined_against_suppression(self):
        self.assertIn("filed none may not report QUIET", self.law)

    def test_the_incident_is_cited_with_its_numbers(self):
        self.assertIn("sentry-294-1789853475", self.law)
        self.assertIn("4 passed / 12 failed / 4 blocked", self.law)

    def test_mechanical_dedup_is_still_allowed(self):
        # the law must not read as "file duplicates forever" -- the escape is a TOOL, not judgement
        self.assertIn("mechanical", self.law.lower())
        self.assertIn("journey_issue_filer.py", self.law)


if __name__ == "__main__":
    unittest.main()
