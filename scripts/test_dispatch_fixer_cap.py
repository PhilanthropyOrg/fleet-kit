"""dispatch_fixer.sh starts at most FLEET_FIXER_ITEM_MAX fixers at once; the rest are deferred.

2026-09-29: 18 fixer sub-passes started in one hour on a 7-core box; they queued on each other's
test runs and 25 of 33 finished passes that day timed out. Pinned: over the cap, a PR prints
`deferred` and no process starts for it; under the cap it is dispatched as before.

Run: python3 scripts/test_dispatch_fixer_cap.py
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def live_fixers() -> int:
    out = subprocess.run(
        ["pgrep", "-af", "run_member.sh the-fixer --item [0-9]"], capture_output=True, text=True
    ).stdout
    return len(set(re.findall(r"the-fixer --item (\d+)", out)))


class DispatchCap(unittest.TestCase):
    def run_dispatch(self, prs: list[str], cap: int) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "run_member.sh"
            stub.write_text("sleep 3\n")
            env = dict(os.environ, FLEET_RUN_MEMBER=str(stub), FLEET_LOG_DIR=tmp,
                       FLEET_FIXER_ITEM_MAX=str(cap))
            out = subprocess.run(["bash", str(HERE / "dispatch_fixer.sh"), *prs],
                                 capture_output=True, text=True, env=env).stdout
            time.sleep(0.3)
            return out

    def test_over_cap_is_deferred(self):
        base = live_fixers()  # the box may be running real fixers; the cap counts them too
        out = self.run_dispatch(["990001", "990002", "990003"], base + 1)
        self.assertEqual(out.count("dispatched the-fixer --item"), 1, out)
        self.assertEqual(out.count("deferred the-fixer --item"), 2, out)
        self.assertIn("--item 990001 (detached", out)

    def test_zero_cap_starts_nothing(self):
        out = self.run_dispatch(["990004"], 0)
        self.assertNotIn("dispatched", out)
        self.assertIn("deferred the-fixer --item 990004", out)


if __name__ == "__main__":
    unittest.main()
