"""fk#1164: a detached child of a cron tick must not keep the tick's flock.

Measured 2026-09-19 on dino: deploy.sh's reaper (setsid+nohup, closes fd 9) held the
crontab's OUTER `flock -w 240 .git/.auto_deploy.lock` on fd 3 for the retired container's
whole lifetime, so every 5-minute tick waited 240s, gave up, and logged nothing. Two fixes,
both tested here: the crontab line uses `flock -o` (close the fd before exec), and the
reaper drops every inherited fd above 2.
"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class CrontabUsesCloseFlag(unittest.TestCase):
    def test_generated_auto_deploy_line_closes_the_lock_fd(self):
        src = (ROOT / "scripts" / "control_plane.py").read_text()
        line = next(l for l in src.splitlines() if ".auto_deploy.lock bash scripts/auto_deploy.sh" in l)
        self.assertIn("flock -o -w 240", line, line)

    def test_reaper_drops_inherited_fds(self):
        src = (ROOT / "scripts" / "deploy.sh").read_text()
        reaper = src[src.index("spawn_reaper() {"):src.index("proxy_deploy() {")]
        self.assertIn("/proc/$$/fd/*", reaper, "reaper must close every inherited fd, not only 9")
        self.assertIn("9>&-", reaper)


@unittest.skipUnless(shutil.which("flock") and os.path.isdir("/proc"), "needs flock(1) and /proc (Linux)")
class LockIsNotInheritedByDetachedChild(unittest.TestCase):
    """The mechanism itself: a detached grandchild spawned under flock(1) keeps the lock
    unless the fd was closed first. Same shape as cron -> flock -> auto_deploy.sh -> reaper."""

    def _held_after_detached_child(self, *flock_flags):
        lock = os.path.join(tempfile.mkdtemp(), "l")
        subprocess.run(["flock", *flock_flags, "-w", "5", lock, "bash", "-c",
                        "setsid nohup sleep 20 >/dev/null 2>&1 &"], check=True)
        r = subprocess.run(["flock", "-n", lock, "true"])
        subprocess.run(["pkill", "-f", "sleep 20"], check=False)
        return r.returncode != 0

    def test_without_close_flag_the_child_keeps_the_lock(self):
        self.assertTrue(self._held_after_detached_child(), "control: a plain flock leaks to the child")

    def test_with_close_flag_the_lock_is_free(self):
        self.assertFalse(self._held_after_detached_child("-o"), "flock -o must not leak the lock")


if __name__ == "__main__":
    unittest.main()
