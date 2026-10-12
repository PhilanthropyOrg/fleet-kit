"""Dumbledore's charter must send it to a claim-ladder bet when none is open.

#1655 gave predict.py the ladder_share / ladder_orgs metrics, but the charter kept saying
"predict on a per-pass metric", so the next pass (#96, 10-12) bet on minion items_per_run
again. This test fails if the charter stops naming the ladder metrics in its bet step.
"""
import re
import unittest
from pathlib import Path

CHARTER = Path(__file__).resolve().parent.parent / "members" / "dumbledore" / "dumbledore.md"


def bet_step(text: str) -> str:
    """Pure: the text of step 4 (the registered bet), up to step 5."""
    m = re.search(r"^4\. \*\*ONE change, registered\.\*\*(.*?)^5\. ", text, re.S | re.M)
    return m.group(1) if m else ""


class OutcomeBet(unittest.TestCase):
    def test_bet_step_requires_an_open_ladder_bet(self):
        step = bet_step(CHARTER.read_text())
        self.assertTrue(step, "step 4 of dumbledore.md not found")
        self.assertIn("ladder_share:<stage>", step)
        self.assertIn("ladder_orgs:<stage>", step)
        self.assertIn("weakest step", step)


if __name__ == "__main__":
    unittest.main()
