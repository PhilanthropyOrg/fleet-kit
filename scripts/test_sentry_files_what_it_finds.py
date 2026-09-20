"""gh: a sentry pass that walked failing journeys must file them, never self-judge them away.

Run sentry-294-1789853475 (2026-09-19 21:37Z) got 4 passed / 12 failed / 4 blocked, skipped
scripts/journey_issue_filer.py, and reported QUIET because "every failure duplicates a sibling
pass's findings from 15 minutes earlier". Twelve real failures reached the operator as one
word. Dedup is the filer's job -- it keys on journey+step+deploy-sha and can also CLOSE a
recovered issue, which a model reading a sibling's prose cannot do."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHARTER = ROOT / "members" / "sentry" / "sentry.md"


class SentryFilesWhatItFinds(unittest.TestCase):
    def setUp(self):
        self.md = CHARTER.read_text()

    def test_filer_is_mandatory_not_discretionary(self):
        self.assertIn("Run it on EVERY pass that produced a results.json", self.md)
        self.assertIn("Deduplication is the filer's job, not yours", self.md)

    def test_failed_journeys_may_not_report_quiet(self):
        self.assertIn("may never report QUIET", self.md)
        self.assertIn("journeys_failed > 0", self.md)

    def test_the_incident_is_named_so_it_is_not_relitigated(self):
        self.assertIn("sentry-294-1789853475", self.md)
        self.assertIn("4 passed / 12 failed / 4 blocked", self.md)

    def test_a_filer_crash_is_a_failed_pass(self):
        self.assertIn("that is a FAILED pass", self.md)

    def test_charter_still_carries_the_explore_step(self):
        # the two changes live in the same section; neither may silently drop the other
        self.assertIn("explore.py", self.md)
        spec = json.loads((ROOT / "members" / "sentry" / "sentry.fleet.json").read_text())
        self.assertIn("Bash", spec["llm"]["tools"]["allow"])


if __name__ == "__main__":
    unittest.main()
