"""fk#1149: a fleet.env key assigned twice must be named, with both lines."""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_env_lint  # noqa: E402

LINT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet_env_lint.py")


class DuplicateKeysTest(unittest.TestCase):
    def test_names_every_duplicate_with_both_lines(self):
        text = "A=1\n# comment\nB=2\n\nA=3\n# A=9 is commented out, not an assignment\nC=4\nB=5\n"
        self.assertEqual(fleet_env_lint.duplicate_keys(text), {"A": [1, 5], "B": [3, 8]})

    def test_a_clean_file_has_none(self):
        self.assertEqual(fleet_env_lint.duplicate_keys("A=1\nB=2\n# A=3\n"), {})

    def test_cli_exits_1_and_says_which_line_wins(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write("FLEET_GRU_ALLOWANCE_FRACTION=0.9\nX=1\nFLEET_GRU_ALLOWANCE_FRACTION=0.6\n")
        r = subprocess.run([sys.executable, LINT, fh.name], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r)
        self.assertIn("FLEET_GRU_ALLOWANCE_FRACTION assigned 2x (lines 1, 3)", r.stdout)
        self.assertIn("line 3 wins", r.stdout)

    def test_cli_exits_0_on_clean(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write("A=1\nB=2\n")
        r = subprocess.run([sys.executable, LINT, fh.name], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
