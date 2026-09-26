"""gru's minions and nerds must outlive gru's own pass.

2026-09-26: gru launched minions with Bash(run_in_background). It ended its turn at 13:45:21 and
minion #7938 (resuming checkpoint #8134) was SIGTERMed at 13:45:27, rc=143. Those kills benched
five reif-priority items as dead ends (fk#1313). dispatch_member.sh runs each pass in its own
session. RED without it: the stub dies with the dispatcher's process group.

Run: python3 scripts/test_dispatch_member.py
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "dispatch_member.sh"


class DispatchMember(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.stub = Path(self.d, "run_member.sh")
        self.stub.write_text(f'sleep 2; echo "$FLEET_RUN_NOW $@" >> {self.d}/done\n')
        self.env = dict(os.environ, FLEET_RUN_MEMBER=str(self.stub), FLEET_LOG_DIR=self.d,
                        FLEET_DISPATCH_POLL_S="0.2")

    def _wait_done(self, lines=1):
        deadline = time.time() + 10
        f = Path(self.d, "done")
        while time.time() < deadline and (not f.exists() or len(f.read_text().splitlines()) < lines):
            time.sleep(0.2)
        return f.read_text() if f.exists() else ""

    def test_minion_survives_its_dispatcher(self):
        parent = subprocess.Popen(
            ["bash", "-c", f"bash {SCRIPT} minion --items 7937,7938; "
                           f"bash {SCRIPT} nerd --task 'lane=claim why'; sleep 30"],
            env=self.env, start_new_session=True, stdout=subprocess.PIPE, text=True)
        time.sleep(0.8)
        os.killpg(parent.pid, signal.SIGKILL)  # gru's pass ends, taking its process group
        parent.wait()
        out = self._wait_done(2)
        self.assertIn("1 minion --items 7937,7938", out, "the minion died with its dispatcher")
        self.assertIn("1 nerd --task lane=claim why", out)
        self.assertTrue(Path(self.d, "minion-items7937_7938.dispatch.log").exists())
        self.assertTrue(Path(self.d, "nerd-lane-claim.dispatch.log").exists())

    def test_wait_reports_done_and_running(self):
        out = subprocess.run(["bash", str(SCRIPT), "minion", "--items", "7941"], env=self.env,
                             capture_output=True, text=True).stdout
        pid = re.search(r"pid=(\d+)", out).group(1)
        sleeper = subprocess.Popen(["sleep", "30"])
        try:
            env = dict(self.env, FLEET_DISPATCH_WAIT_S="4")
            res = subprocess.run(["bash", str(SCRIPT), "--wait", pid, str(sleeper.pid)], env=env,
                                 capture_output=True, text=True).stdout
            self.assertIn(f"pid={pid} done", res)
            self.assertIn(f"pid={sleeper.pid} running", res)
        finally:
            sleeper.kill()

    def test_bad_usage_launches_nothing(self):
        for args in (["minion"], ["minion", "--item", "1"], ["the-fixer", "--item", "1"], []):
            r = subprocess.run(["bash", str(SCRIPT), *args], env=self.env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, args)
        time.sleep(2.5)
        self.assertFalse(Path(self.d, "done").exists())

    def test_gru_md_dispatches_detached(self):
        gru = (HERE.parent / "members" / "gru" / "gru.md").read_text()
        self.assertIn("bash /fleet-kit/scripts/dispatch_member.sh minion --items <n1,n2,n3>", gru)
        self.assertIn("bash /fleet-kit/scripts/dispatch_member.sh nerd --task", gru)
        self.assertNotIn("run_member.sh minion --items", gru)
        self.assertNotIn("TaskOutput", gru)


if __name__ == "__main__":
    unittest.main()
