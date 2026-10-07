"""2026-09-29: at most FLEET_TEST_SLOTS verified_test.sh runs execute at once; the rest queue.

dino (7 cores) ran ~15 minion passes with their test runs all at once: load 12-21, a 25-file
scoped run took 333s, one 457-test run took 24 min, and passes timed out at 90 min. The
passes themselves keep running (mostly API wait); only preflight + pytest take a slot. The
wait is foreground, prints progress, and gives up with a clear message inside the minion's
20 min Bash limit instead of hanging.

RED without the fix: two runs overlap, and a busy box never makes a run wait or fail fast.
Run: python3 scripts/test_verified_test_slots.py
"""
import os
import pathlib
import re
import subprocess
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
VT = ROOT / "scripts" / "verified_test.sh"

# Stand-in for the repo's diff-scoped runner: records when it ran, holds the CPU for a while.
FAKE_TFD = (
    "import os, sys, time\n"
    "log = os.environ['SLOT_LOG']\n"
    "open(log, 'a').write(f'start {time.time()}\\n')\n"
    "time.sleep(float(os.environ.get('HOLD_S', '1.5')))\n"
    "open(log, 'a').write(f'end {time.time()}\\n')\n"
)


def _repo() -> str:
    d = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", d], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", d, "config", k, v], check=True)
    os.makedirs(os.path.join(d, "scripts"))
    pathlib.Path(d, "scripts", "tests_for_diff.py").write_text(FAKE_TFD)
    pathlib.Path(d, "x.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", d, "add", "-A"], check=True)
    subprocess.run(["git", "-C", d, "commit", "-qm", "init"], check=True)
    return d


class TestSlots(unittest.TestCase):
    def setUp(self):
        self.slots = tempfile.mkdtemp()
        self.log = os.path.join(tempfile.mkdtemp(), "slots.log")

    def _env(self, d, **extra):
        return dict(os.environ, WT_PATH=d, SLOT_LOG=self.log, FLEET_TEST_SLOT_DIR=self.slots,
                    FLEET_TEST_SLOT_PROGRESS_S="1", **extra)

    def _start(self, d, **extra):
        return subprocess.Popen(["bash", str(VT)], cwd=d, env=self._env(d, **extra),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_one_slot_means_runs_never_overlap(self):
        procs = [self._start(_repo(), FLEET_TEST_SLOTS="1") for _ in range(3)]
        outs = [p.communicate(timeout=120) for p in procs]
        self.assertEqual([p.returncode for p in procs], [0, 0, 0], outs)
        events = sorted((float(t), +1 if kind == "start" else -1)
                        for kind, t in (ln.split() for ln in pathlib.Path(self.log).read_text().splitlines()))
        running = peak = 0
        for _, step in events:
            running += step
            peak = max(peak, running)
        self.assertEqual(sum(1 for _, s in events if s > 0), 3, events)
        self.assertEqual(peak, 1, f"{peak} test runs executed at once with FLEET_TEST_SLOTS=1")
        stdout = "".join(o[0] for o in outs)
        self.assertIn("got test slot 1/1", stdout)
        self.assertIn("waiting for test slot -- all 1 busy", stdout)

    def test_no_slot_within_the_limit_fails_fast_with_a_clear_message(self):
        holder = subprocess.Popen(["bash", "-c", f'exec 8>"{self.slots}/slot-0.lock"; flock 8; sleep 30'])
        try:
            time.sleep(0.5)
            d = _repo()
            t = time.time()
            r = subprocess.run(["bash", str(VT)], cwd=d, capture_output=True, text=True, timeout=60,
                               env=self._env(d, FLEET_TEST_SLOTS="1", FLEET_TEST_SLOT_WAIT_S="2"))
            took = time.time() - t
        finally:
            holder.kill()
        self.assertEqual(r.returncode, 75, r.stdout + r.stderr)
        self.assertIn("NO TEST SLOT", r.stderr)
        self.assertLess(took, 20)
        self.assertFalse(os.path.exists(self.log), "tests ran without a slot")

    def test_a_pass_near_its_deadline_is_told_at_once_not_queued(self):
        # 2026-10-07: the-fixer passes died at their 60 min kill inside a test run queued with
        # 3-14 min left. Under the run reserve, verified_test.sh must refuse at once and say so.
        d = _repo()
        t = time.time()
        r = subprocess.run(["bash", str(VT)], cwd=d, capture_output=True, text=True, timeout=60,
                           env=self._env(d, FLEET_TEST_SLOTS="1",
                                         FLEET_PASS_DEADLINE=str(int(time.time()) + 120)))
        self.assertEqual(r.returncode, 75, r.stdout + r.stderr)
        self.assertIn("PASS CLOCK", r.stderr)
        self.assertLess(time.time() - t, 20)
        self.assertFalse(os.path.exists(self.log), "tests ran with no time left to finish them")

    def test_the_pass_clock_shortens_the_wait_for_a_busy_slot(self):
        holder = subprocess.Popen(["bash", "-c", f'exec 8>"{self.slots}/slot-0.lock"; flock 8; sleep 30'])
        try:
            time.sleep(0.5)
            d = _repo()
            t = time.time()
            r = subprocess.run(["bash", str(VT)], cwd=d, capture_output=True, text=True, timeout=60,
                               env=self._env(d, FLEET_TEST_SLOTS="1", FLEET_TEST_SLOT_RUN_RESERVE_S="10",
                                             FLEET_PASS_DEADLINE=str(int(time.time()) + 13)))
            took = time.time() - t
        finally:
            holder.kill()
        self.assertEqual(r.returncode, 75, r.stdout + r.stderr)
        self.assertIn("PASS CLOCK -- no test slot freed", r.stderr)
        self.assertLess(took, 15)

    def test_a_far_deadline_changes_nothing(self):
        d = _repo()
        r = subprocess.run(["bash", str(VT)], cwd=d, capture_output=True, text=True, timeout=60,
                           env=self._env(d, FLEET_TEST_SLOTS="1",
                                         FLEET_PASS_DEADLINE=str(int(time.time()) + 3600)))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("got test slot 1/1", r.stdout)

    def test_the_wait_plus_a_typical_run_fits_one_minion_bash_call(self):
        # 2026-09-29, 6h live on dino: runs held a slot p75 458s / p90 743s, and a 600s wait gave
        # up 17 times against 25 successes. The wait must outlast a p90 hold, and the wait plus a
        # p75 run must fit the minion's default Bash timeout (run_member.sh) or the call dies.
        wait = int(subprocess.run(["bash", "-c", f'. "{ROOT}/scripts/test_slots.sh"; echo "$TEST_SLOT_WAIT_S"'],
                                  capture_output=True, text=True, env={"PATH": os.environ["PATH"]}).stdout)
        rm = (ROOT / "scripts" / "run_member.sh").read_text()
        bash_default_s = int(re.search(r"BASH_DEFAULT_TIMEOUT_MS=(\d+)", rm).group(1)) // 1000
        self.assertGreaterEqual(wait, 743 + 60)
        self.assertLessEqual(wait + 458, bash_default_s)

    def test_default_is_half_the_cores(self):
        out = subprocess.run(["bash", "-c", f'. "{ROOT}/scripts/test_slots.sh"; echo "$TEST_SLOT_N"'],
                             capture_output=True, text=True, env={"PATH": os.environ["PATH"]}).stdout
        self.assertEqual(int(out), max(1, (os.cpu_count() or 4) // 2))


if __name__ == "__main__":
    unittest.main()
