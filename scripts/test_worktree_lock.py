"""Regression + contention test for worktree_lock.sh (gh#672).

The old `mkdir "$LOCK"` + `sleep 2` spin in run_member.sh/worktree_builder.sh had no memory of
arrival order: a lock release and a brand-new contender's very first `mkdir` attempt could land
in the same instant and win exactly as often as a process that had been polling for minutes --
because a sleeping poller isn't even attempting the mkdir at the moment the lock frees. That
starved the messenger inbox pass (fk#669/#670) for 6 minutes under real load on 2026-09-07.

This test proves the flock-based replacement fixes the mechanism, WITHOUT needing a loaded box
(AC7): a helper process holds the lock while N waiters queue behind it.

Run: python3 scripts/test_worktree_lock.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
LOCK_SCRIPT = KIT / "scripts" / "worktree_lock.sh"


def run_bash(script: str, env: dict, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                           text=True, timeout=timeout)


class WorktreeLockSourceTests(unittest.TestCase):
    """AC3: the lock is flock-based, and the old mkdir spin is gone."""

    def test_run_member_no_longer_spins_on_mkdir(self):
        text = (KIT / "scripts" / "run_member.sh").read_text()
        self.assertNotIn('mkdir "$LOCK"', text)
        self.assertIn("worktree_lock.sh", text)
        self.assertIn("worktree_lock_acquire", text)

    def test_worktree_builder_no_longer_spins_on_mkdir(self):
        text = (KIT / "scripts" / "worktree_builder.sh").read_text()
        self.assertNotIn('mkdir "$LOCK"', text)
        self.assertIn("worktree_lock.sh", text)
        self.assertIn("worktree_lock_acquire", text)

    def test_lock_uses_flock(self):
        code_lines = [l for l in LOCK_SCRIPT.read_text().splitlines() if not l.strip().startswith("#")]
        code = "\n".join(code_lines)
        self.assertIn("flock", code)
        # gh#5632: `mkdir -p "$WORKTREE_LOCK_WAITERS_DIR"` is a legitimate, unrelated use -- the
        # thing this guards against is the OLD polling-lock anti-pattern (some `mkdir` variant
        # used AS the lock itself), not any appearance of mkdir at all. Rather than matching one
        # exact quoting of the old anti-pattern (which a re-quoted/re-spaced reintroduction could
        # slip past), assert every `mkdir` line in the file is this one known-legitimate use.
        mkdir_lines = [l.strip() for l in code_lines if "mkdir" in l]
        for line in mkdir_lines:
            self.assertIn('mkdir -p "$WORKTREE_LOCK_WAITERS_DIR"', line,
                          f"unexpected mkdir usage, possible lock-as-mkdir regression: {line!r}")


class WorktreeLockContentionTests(unittest.TestCase):
    """AC1/AC2/AC7: N concurrent waiters, driven by a helper process holding the lock --
    no real git operations, no loaded box required."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-worktree-lock-test-")
        # Isolate this test's lock file from any real one on the box: worktree_lock.sh derives
        # its path from $TMPDIR.
        self.env = dict(os.environ)
        self.env["TMPDIR"] = self.tmp
        self.order_file = os.path.join(self.tmp, "order.log")

    def _waiter_script(self, tag: str, hold_s: float = 0.05) -> str:
        return textwrap.dedent(f"""
            set -u
            . "{LOCK_SCRIPT}"
            worktree_lock_acquire 30 || {{ echo "TIMEOUT {tag}" >> "{self.order_file}"; exit 1; }}
            echo "OK {tag}" >> "{self.order_file}"
            sleep {hold_s}
            worktree_lock_release
        """)

    def test_n20_concurrent_all_acquire_no_timeout(self):
        """AC1: 20 concurrent attempts, none times out."""
        procs = [subprocess.Popen(["bash", "-c", self._waiter_script(f"w{i}")], env=self.env)
                 for i in range(20)]
        for p in procs:
            self.assertEqual(p.wait(timeout=30), 0, "a waiter timed out acquiring the lock")
        lines = Path(self.order_file).read_text().splitlines()
        self.assertEqual(len(lines), 20)
        self.assertTrue(all(l.startswith("OK ") for l in lines), lines)

    def test_first_requester_is_not_starved_by_later_arrivals(self):
        """AC2: a waiter queued since before K later contenders arrived acquires the lock at
        least once, within a bounded number of releases -- not perpetually last."""
        holder_script = textwrap.dedent(f"""
            set -u
            . "{LOCK_SCRIPT}"
            worktree_lock_acquire 30
            echo "HOLDING" >> "{self.order_file}"
            sleep 0.5
            worktree_lock_release
        """)
        holder = subprocess.Popen(["bash", "-c", holder_script], env=self.env)
        # Give the holder time to actually take the lock before "first" starts queuing.
        import time
        time.sleep(0.15)

        first = subprocess.Popen(["bash", "-c", self._waiter_script("first")], env=self.env)
        # Let "first" register as a blocked waiter before the later contenders show up.
        time.sleep(0.15)

        later = [subprocess.Popen(["bash", "-c", self._waiter_script(f"later{i}")], env=self.env)
                 for i in range(10)]

        holder.wait(timeout=10)
        first.wait(timeout=10)
        for p in later:
            p.wait(timeout=10)

        lines = Path(self.order_file).read_text().splitlines()
        acquired = [l.split()[1] for l in lines if l.startswith("OK ")]
        self.assertIn("first", acquired, "the first-requested waiter never acquired the lock")
        rank = acquired.index("first")
        # The old mkdir-spin bug let ANY fresh contender win regardless of arrival order --
        # "first" could land anywhere, including dead last, every single time. flock's kernel
        # wait queue means a waiter already blocked when the lock frees is not systematically
        # outrun by one that starts trying afterward: assert "first" is not the last-served of
        # the 11 total waiters (not "exactly first" -- flock is not a strict FIFO guarantee,
        # just no-longer-starved).
        self.assertLess(rank, len(acquired) - 1,
                         f"'first' was served last ({rank+1}/{len(acquired)}) -- starved: {lines}")


class WorktreeLockScaledTimeoutTests(unittest.TestCase):
    """gh#5632: the fixed 120s create-path budget was sized for ordinary concurrency and
    starts throwing FATAL at wide fanout (20/32 minions in one pass). worktree_lock_timeout_for
    is the pure scaling function -- test it directly, no real waiters needed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-worktree-lock-scale-test-")
        self.env = dict(os.environ)
        self.env["TMPDIR"] = self.tmp

    def _timeout_for(self, base: int, n_waiters: int, extra_env: dict | None = None) -> int:
        waiters_dir = os.path.join(self.tmp, "fleet-kit-worktree-add.waiters")
        os.makedirs(waiters_dir, exist_ok=True)
        for i in range(n_waiters):
            Path(os.path.join(waiters_dir, f"fake{i}")).touch()
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        out = run_bash(f'. "{LOCK_SCRIPT}"; worktree_lock_timeout_for {base}', env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return int(out.stdout.strip())

    def test_single_waiter_leaves_base_timeout_unchanged(self):
        """AC: existing single/few-minion concurrency continues to work unchanged."""
        self.assertEqual(self._timeout_for(120, n_waiters=1), 120)

    def test_wide_fanout_scales_the_create_path_budget(self):
        """32-wide fanout (this issue's reproduction) must clear a scaled budget that a fixed
        120s could not: 31 other waiters ahead * ~20s/add comfortably exceeds 120s."""
        timeout = self._timeout_for(120, n_waiters=32)
        self.assertGreater(timeout, 120)
        self.assertEqual(timeout, 640)  # 32 * 20

    def test_cleanup_path_budget_is_never_scaled(self):
        """Non-goal: the exit-time cleanup call (worktree_lock_acquire 30) stays exactly as
        bounded as it already was, even under the same heavy contention that scales create."""
        self.assertEqual(self._timeout_for(30, n_waiters=32), 30)

    def test_scaling_is_capped(self):
        timeout = self._timeout_for(120, n_waiters=1000,
                                     extra_env={"WORKTREE_LOCK_MAX_TIMEOUT": "300"})
        self.assertEqual(timeout, 300)

    def test_stale_marker_past_30min_is_deleted_not_just_excluded(self):
        """A marker from a process that died mid-attempt (kill -9) must not leak forever: past
        30 minutes it is actually removed, not just excluded from the waiter count."""
        waiters_dir = os.path.join(self.tmp, "fleet-kit-worktree-add.waiters")
        os.makedirs(waiters_dir, exist_ok=True)
        stale = Path(os.path.join(waiters_dir, "dead12345"))
        stale.touch()
        old_time = time.time() - 31 * 60
        os.utime(stale, (old_time, old_time))
        self._timeout_for(120, n_waiters=1)  # any call reaps as a side effect
        self.assertFalse(stale.exists(), "a marker older than 30 minutes should be deleted")


class WorktreeLockEndToEndScalingTests(unittest.TestCase):
    """Same shape as WorktreeLockContentionTests above, but with a base timeout too small for
    the real concurrency to clear unscaled -- proving the scaling actually prevents the FATAL
    this issue reports, not just that the formula computes a number."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-worktree-lock-e2e-test-")
        self.env = dict(os.environ)
        self.env["TMPDIR"] = self.tmp
        # Scaled down from the real 10-20s/add, 120s budget so the test runs in ~seconds while
        # keeping the same ratio that made the real incident fail: ~14x more queued wait than
        # the fixed per-attempt budget allows.
        self.env["WORKTREE_LOCK_SEC_PER_WAITER"] = "0.2"
        self.env["WORKTREE_LOCK_SCALE_MIN_TIMEOUT"] = "0"
        self.order_file = os.path.join(self.tmp, "order.log")

    def _waiter_script(self, tag: str, base_timeout: str, hold_s: float = 0.15) -> str:
        return textwrap.dedent(f"""
            set -u
            . "{LOCK_SCRIPT}"
            worktree_lock_acquire {base_timeout} || {{ echo "TIMEOUT {tag}" >> "{self.order_file}"; exit 1; }}
            echo "OK {tag}" >> "{self.order_file}"
            sleep {hold_s}
            worktree_lock_release
        """)

    def test_unscaled_fixed_budget_times_out_under_wide_fanout(self):
        """Control: reproduce the reported failure. With scaling disabled (per-waiter cost 0),
        a too-small fixed budget cannot clear 15 waiters each holding 0.15s."""
        env = dict(self.env)
        env["WORKTREE_LOCK_SEC_PER_WAITER"] = "0"  # disable scaling for this control case
        procs = [subprocess.Popen(["bash", "-c", self._waiter_script(f"w{i}", "1")], env=env)
                 for i in range(15)]
        for p in procs:
            p.wait(timeout=15)
        lines = Path(self.order_file).read_text().splitlines()
        self.assertTrue(any(l.startswith("TIMEOUT") for l in lines),
                         f"expected at least one timeout with scaling disabled: {lines}")

    def test_scaled_budget_clears_the_same_wide_fanout(self):
        """gh#5632 fix: the same 15-way contention, same tiny base timeout, but with scaling
        live -- nobody times out."""
        procs = [subprocess.Popen(["bash", "-c", self._waiter_script(f"w{i}", "1")], env=self.env)
                 for i in range(15)]
        for p in procs:
            p.wait(timeout=15)
        lines = Path(self.order_file).read_text().splitlines()
        self.assertEqual(len(lines), 15, lines)
        self.assertTrue(all(l.startswith("OK ") for l in lines),
                         f"a waiter timed out despite scaling: {lines}")


if __name__ == "__main__":
    unittest.main()
