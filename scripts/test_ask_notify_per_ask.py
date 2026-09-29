"""Two escalations for the same member inside one hour must both reach Reif.

fk#1383 routed every ask through dumbledore, so each email is now a deliberate one-way-door
escalation. The old per-member-per-hour dedupe key silently dropped the second one.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ask  # noqa: E402


class NotifyPerAskTest(unittest.TestCase):
    def test_second_escalation_same_member_same_hour_gets_its_own_page(self):
        with mock.patch.object(ask.subprocess, "run") as run:
            ask._notify("sentry", 90, "a", sender="dumbledore")
            ask._notify("sentry", 91, "b", sender="dumbledore")
        problems = [c.args[0][c.args[0].index("--problem") + 1] for c in run.call_args_list]
        self.assertEqual(len(set(problems)), 2, problems)


if __name__ == "__main__":
    unittest.main()
