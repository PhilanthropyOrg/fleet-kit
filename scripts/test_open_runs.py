"""A run live in another container (a deploy's draining -retired build) must count as live.

2026-09-26 16:39 UTC: minions for #7939/#7941/#7950 were resuming their checkpoints in
`philanthropy-retired`. From the new container, /proc and /tmp showed nothing, so
stale_claims.py's idle-checkpoint rule (fk#1315) could release their claims, and run_member.sh's
dead-holder resume (fk#1316) could remove a live worktree's entry. runs.jsonl is shared.

Run: python3 scripts/test_open_runs.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import open_runs  # noqa: E402

NOW = time.time()


def rec(run_id, status, age):
    return {"run_id": run_id, "member": run_id.split("-item")[0], "status": status, "ts": NOW - age}


RECS = [
    rec("minion-item7939-58808-1790438828", "started", 1800),        # live in -retired
    rec("minion-item7950-58883-1790438829", "started", 1800),
    rec("minion-item7950-58883-1790438829", "ok", 60),                # finished
    rec("minion-item7941_7942-58848-1790438830", "started", 600),    # live batch
    rec("minion-item7938-4499-1790416901", "started", 3 * 3600),     # SIGKILLed long ago
    rec("the-fixer-item8136-1-1", "started", 60),                     # not a minion
]


class OpenRuns(unittest.TestCase):
    def test_open_items(self):
        self.assertEqual(open_runs.open_items(RECS, NOW), {7939, 7941, 7942})

    def test_has_prefix_cli_reads_the_shared_file(self):
        d = tempfile.mkdtemp()
        Path(d, "runs.jsonl").write_text("\n".join(json.dumps(r) for r in RECS) + "\n")
        env = dict(os.environ, FLEET_LOG_DIR=d)
        run = lambda *a: subprocess.run([sys.executable, str(HERE / "open_runs.py"), *a],  # noqa: E731
                                        env=env, capture_output=True, text=True)
        self.assertEqual(run("has", "--prefix", "minion-item7939-58808-").returncode, 0)
        self.assertEqual(run("has", "--prefix", "minion-item7950-58883-").returncode, 1)
        self.assertEqual(run("has", "--prefix", "minion-item7938-4499-").returncode, 1)
        self.assertEqual(json.loads(run("items").stdout), [7939, 7941, 7942])

    def test_resume_holder_in_another_container_is_live(self):
        import re
        fn = re.search(r"  resume_holder_dead\(\) \{\n.*?\n  \}\n",
                       (HERE / "run_member.sh").read_text(), re.S).group(0)
        d = tempfile.mkdtemp()
        Path(d, "runs.jsonl").write_text("\n".join(json.dumps(r) for r in RECS) + "\n")
        env = dict(os.environ, FLEET_LOG_DIR=d, KIT_DIR=str(HERE.parent))
        for path, want in (("/tmp/fleet-run-minion-item7939-58808", 1),   # gone here, live there
                           ("/tmp/fleet-run-minion-item7938-4499", 0)):    # gone, run long dead
            r = subprocess.run(["bash", "-c", f'{fn}\nresume_holder_dead "{path}"'], env=env,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, want, (path, r.stderr))

    def test_stale_claims_sweep_unions_open_runs(self):
        src = (HERE / "stale_claims.py").read_text()
        self.assertIn("live_numbers() | open_runs.open_items(", src)


if __name__ == "__main__":
    unittest.main()
