"""The mandate: one file on the host sets the fleet's priority (Reif, 2026-10-08).

Given $FLEET_LOG_DIR/MANDATE.md exists, When any member's prompt is built, Then the mandate is
its first block above NORTH; dumbledore reads it as step 0 and can read the CI-minutes limit.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ci_minutes


def _mandate_block() -> str:
    src = (HERE / "run_member.sh").read_text()
    start = src.index('MANDATE_FILE="$LOG_DIR/MANDATE.md"')
    end = src.index("# INBOX (philanthropy#8215)")
    return src[start:end]


class MandateTests(unittest.TestCase):
    def test_every_pass_gets_the_mandate_above_north(self):
        src = (HERE / "run_member.sh").read_text()
        north = src.index('NORTH_FILE="$LOG_DIR/NORTH.md"')
        mandate = src.index('MANDATE_FILE="$LOG_DIR/MANDATE.md"')
        inbox = src.index("# INBOX (philanthropy#8215)")
        # prepends stack: a block placed after NORTH's prepend lands ABOVE it in the prompt
        self.assertLess(north, mandate)
        self.assertLess(mandate, inbox)

    def test_the_block_prepends_the_file_and_skips_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            script = f'LOG_DIR="{d}"; PROMPT="CHARTER"\n{_mandate_block()}\nprintf "%s" "$PROMPT"'
            out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
            self.assertEqual(out, "CHARTER")
            Path(d, "MANDATE.md").write_text("Priority: OrgVerify\nLimit: CI minutes per day: max 500\n")
            out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
            self.assertTrue(out.startswith("Priority: OrgVerify"), out)
            self.assertTrue(out.endswith("---\n\nCHARTER"), out)

    def test_dumbledore_reads_the_mandate_before_the_ledger(self):
        md = (HERE.parent / "members/dumbledore/dumbledore.md").read_text()
        self.assertLess(md.index("0. **Mandate.**"), md.index("1. **Ledger first.**"))
        self.assertIn("scripts/ci_minutes.py", md)
        self.assertIn("FLEET_MINION_MAX_PER_HOUR", md[md.index("## Authority"):])

    def test_ci_minutes_sums_only_actions_minutes_for_the_day(self):
        items = [
            {"product": "actions", "unitType": "Minutes", "date": "2026-10-08T00:00:00Z", "quantity": 100},
            {"product": "actions", "unitType": "Minutes", "date": "2026-10-08T03:00:00Z", "quantity": 50.5},
            {"product": "actions", "unitType": "Minutes", "date": "2026-10-07T00:00:00Z", "quantity": 999},
            {"product": "actions", "unitType": "GigabyteHours", "date": "2026-10-08T00:00:00Z", "quantity": 7},
            {"product": "packages", "unitType": "Minutes", "date": "2026-10-08T00:00:00Z", "quantity": 7},
        ]
        self.assertEqual(ci_minutes.minutes(items, "2026-10-08"), 150.5)
        self.assertEqual(ci_minutes.minutes(items), 1149.5)

    def test_ci_minutes_fails_open_without_gh(self):
        env = {**os.environ, "PATH": "/nonexistent"}
        out = subprocess.run([sys.executable, str(HERE / "ci_minutes.py")], capture_output=True, text=True, env=env, check=False)
        self.assertEqual(out.returncode, 0)
        self.assertIn("today=unavailable", out.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
