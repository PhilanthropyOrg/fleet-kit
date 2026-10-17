"""Dumbledore's charter must send most bets to a product number.

#1655 gave predict.py the ladder_share / ladder_orgs metrics, but the charter kept saying
"predict on a per-pass metric", so the next pass (#96, 10-12) bet on minion items_per_run
again. 2026-10-17 (desk): 3 of 25 bets since 10-09 still touched the product, so the rule is now
2 of every 3 bets on `product:*` or `ladder_*` (predict.py enforces it). This test fails if
the charter stops saying so in its bet step.
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
    def test_bet_step_puts_two_of_three_bets_on_the_product(self):
        step = bet_step(CHARTER.read_text())
        self.assertTrue(step, "step 4 of dumbledore.md not found")
        self.assertIn("2 of every 3 bets name a product metric", step)
        self.assertIn("`product:<name>`", step)
        self.assertIn("`ladder_*`", step)
        self.assertIn("--over-limit", step)


if __name__ == "__main__":
    unittest.main()
