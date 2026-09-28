"""A message wake that finds its member mid-pass is not lost.

Live 2026-09-28: the-fixer hit its dispatch lock 970 times; every fleet_msg wake that landed
mid-pass exited SKIP and set the 30-min wake cooldown, so the message sat until the next cron
tick (the-fixer 7 open, oldest 2.2h). Now the losing wake leaves a flag and the running pass
starts ONE follow-up when it ends -- ten messages mid-pass still cost one extra pass.

Run: python3 scripts/test_wake_while_busy.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
RUN_MEMBER = KIT / "scripts" / "run_member.sh"


class WakeWhileBusyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fleet-wake-busy-"))
        self.locks = self.tmp / "fleet-kit-member-locks"
        self.locks.mkdir()
        self.env = dict(os.environ, TMPDIR=str(self.tmp), FLEET_LOG_DIR=str(self.tmp / "logs"))

    def run_while_locked(self, fired_by):
        lock = self.locks / "marie.lock"
        holder = subprocess.Popen(["bash", "-c", f'exec 9>"{lock}"; flock 9; sleep 3'])
        time.sleep(0.3)
        env = dict(self.env, FLEET_FIRED_BY=fired_by) if fired_by else self.env
        subprocess.run(["bash", str(RUN_MEMBER), "marie", "--dry-run"], env=env,
                       capture_output=True, text=True, timeout=30)
        holder.kill()
        return (self.locks / "marie.wake-pending").exists()

    def test_message_wake_that_loses_the_lock_leaves_a_flag(self):
        self.assertTrue(self.run_while_locked("fleet_msg"))

    def test_cron_tick_that_loses_the_lock_leaves_no_flag(self):
        self.assertFalse(self.run_while_locked(None))

    def test_pass_end_takes_the_flag_and_starts_one_followup(self):
        text = RUN_MEMBER.read_text()
        block = text[text.index("# wake-followup-begin"):text.index("# wake-followup-end")]
        stub = self.tmp / "stub.sh"
        stub.write_text(f'echo "$1 $FLEET_FIRED_BY" >> "{self.tmp}/launched"\n')
        (self.locks / "marie.wake-pending").touch()
        script = (f'LOG="{self.tmp}/marie.log"; log() {{ echo "$*" >> "$LOG"; }}\n'
                  f'MEMBER=marie; DISPATCH_LOCK_DIR="{self.locks}"; DISPATCH_LOCK_KEY=marie\n'
                  f'exec 9>"{self.locks}/marie.lock"; flock -n 9\n{block}\nwait\n')
        for _ in range(2):  # the second pass end finds no flag: exactly one follow-up
            subprocess.run(["bash", "-c", script], env=dict(self.env, FLEET_RUN_MEMBER=str(stub)),
                           check=True, timeout=10)
        time.sleep(0.5)
        self.assertEqual((self.tmp / "launched").read_text().split("\n")[:-1],
                         ["marie fleet_msg-followup"])
        self.assertFalse((self.locks / "marie.wake-pending").exists())


if __name__ == "__main__":
    unittest.main()
