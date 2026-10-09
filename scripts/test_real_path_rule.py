#!/usr/bin/env python3
"""Builders write tests that run the real path (2026-10-09).

In the 24h to 04:30Z fleet-code-review blocked #11903 twice and #11912 once on the same point:
the test read a template's text or posted a hand-built body, so it passed even when the feature
did nothing. Each block costs a fixer pass and a full CI run.

RED without the change: minion.md and the-fixer.md say nothing about what a test must run.
GREEN: both carry the rule, so every build and every fix pass reads it.
Run: python3 scripts/test_real_path_rule.py
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RULE = "A test runs the real path"


class RealPathRule(unittest.TestCase):
    def test_minion_and_fixer_carry_the_rule(self):
        for member in ("minion", "the-fixer"):
            text = (ROOT / "members" / member / f"{member}.md").read_text()
            self.assertIn(RULE, text, member)


if __name__ == "__main__":
    unittest.main()
