"""fk#1281: a retired (blue-green cutover) container must stop STARTING passes, its drain must be
bounded, and fleet.db locks must not outlive an error.

Measured 2026-09-24 on dino: philanthropy-retired had cron disabled at cutover, yet 50 min
later it was running 2 nerd + 3 worktree_builder passes spawned by a gru pass's own Bash tool,
and a webhook-launched judge-judy loop still merging PRs 2.5h in. Its dashboard's fleet.db sync
thread held the write lock 14:40-14:46 after a `database is locked` left a transaction open.
Every auto_deploy tick for two hours logged ABANDONED.

No podman in CI, so deploy.sh's helpers run against a stub `podman` on PATH.

Run: python3 scripts/test_retired_stops_scheduling.py
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "scripts"))
DEPLOY = (KIT / "scripts" / "deploy.sh").read_text()


def _fn(src: str, name: str) -> str:
    start = src.index(f"{name}() {{")
    return src[start:src.index("\n}\n", start) + 3]


class RetiredFlagGatesEveryNewPass(unittest.TestCase):
    def _run(self, flag: Path, run_now: str = "0") -> subprocess.CompletedProcess:
        script = f'. "{KIT}/scripts/fleet_enabled.sh"; log() {{ echo "LOG $*"; }}; fleet_enabled_or_exit t; echo STARTED'
        env = {**os.environ, "FLEET_RETIRED_FLAG": str(flag), "FLEET_ENABLED": "true", "FLEET_RUN_NOW": run_now}
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=20)

    def test_live_container_starts(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIn("STARTED", self._run(Path(d) / ".retired").stdout)

    def test_retired_container_refuses_even_a_run_now(self):
        with tempfile.TemporaryDirectory() as d:
            flag = Path(d) / ".retired"
            flag.write_text("1790276700\n")
            for run_now in ("0", "1"):
                p = self._run(flag, run_now)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertNotIn("STARTED", p.stdout, f"retired container started a pass (FLEET_RUN_NOW={run_now})")
                self.assertIn("RETIRED", p.stdout)

    def test_default_flag_is_in_the_kit_dir_not_the_shared_fleet_env(self):
        src = (KIT / "scripts" / "fleet_enabled.sh").read_text()
        self.assertIn('/..', src)
        self.assertIn('.retired', src)
        self.assertIn(".retired", (KIT / ".dockerignore").read_text(), "a stray flag baked into an image would stop a LIVE build")

    def test_judge_judy_loop_checks_the_flag_before_each_next_pr(self):
        src = (KIT / "members" / "judge-judy" / "judge-judy.sh").read_text()
        loop = src[src.index("\nwhile :; do"):]
        first = loop[:loop.index("pick_pr ")]
        self.assertIn("fleet_retired", first, "judge-judy must stop picking PRs once retired")
        self.assertIn("break", first[first.index("fleet_retired"):])

    def test_entrypoint_does_not_reschedule_a_retired_container(self):
        src = (KIT / "entrypoint.sh").read_text()
        body = src[src.index("  cron-foreground)"):]
        self.assertLess(body.index("/fleet-kit/.retired"), body.index("fleet_view_server.py"))


class PassPattern(unittest.TestCase):
    PAT = re.search(r"^PASS_PATTERN='([^']+)'", DEPLOY, re.M).group(1)

    def test_matches_the_passes_seen_live(self):
        for args in ("bash /fleet-kit/scripts/run_member.sh nerd --task lane=claim",
                     "/bin/bash /fleet-kit/scripts/run_member.sh judge-judy",
                     "/bin/bash /fleet-kit/members/judge-judy/judge-judy.sh",
                     "bash /fleet-kit/scripts/worktree_builder.sh --item 7748 --onto-pr 7743"):
            self.assertRegex(args, self.PAT)

    def test_leaves_the_rest_alone(self):
        for args in ("python3 scripts/fleet_view_server.py", "cron -f", "python3 scripts/webhook_receiver.py --port 8562",
                     "/bin/bash /fleet-kit/entrypoint.sh cron-foreground"):
            self.assertNotRegex(args, self.PAT)


STUB_PODMAN = r"""#!/bin/bash
echo "$*" >> "$STUB_DIR/calls"
case "$*" in
  *pgrep*) n="$(cat "$STUB_DIR/n")"; echo "$n"; [ "$n" -gt 0 ];;
  *pkill*) echo 0 > "$STUB_DIR/n";;  # the passes honour SIGTERM
  *) true;;
esac
"""


class BoundedDrain(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "podman").write_text(STUB_PODMAN)
        (self.d / "podman").chmod(0o755)
        (self.d / "n").write_text("3\n")
        self.env = {**os.environ, "PATH": f"{self.d}:{os.environ['PATH']}", "STUB_DIR": str(self.d)}

    def _bash(self, body: str) -> subprocess.CompletedProcess:
        pat = re.search(r"^PASS_PATTERN=.*$", DEPLOY, re.M).group(0)
        script = "\n".join([pat, "RETIRE_KILL_GRACE_S=10", _fn(DEPLOY, "inflight_in"),
                            _fn(DEPLOY, "terminate_and_stop"), body])
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=self.env, timeout=60)

    def test_terminate_sends_sigterm_to_passes_before_stopping(self):
        p = self._bash('terminate_and_stop philanthropy-retired')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "0", "passes that honoured SIGTERM must report 0 left")
        calls = (self.d / "calls").read_text().splitlines()
        kill = next(i for i, c in enumerate(calls) if "pkill -TERM -f" in c)
        stop = next(i for i, c in enumerate(calls) if c.startswith("stop "))
        self.assertLess(kill, stop, "SIGTERM (status=killed rows) must precede podman stop (SIGKILL)")

    def test_inflight_counts_zero_without_failing_under_pipefail(self):
        (self.d / "n").write_text("0\n")
        p = self._bash('set -euo pipefail; echo "n=$(inflight_in x)"')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("n=0", p.stdout)


class DeployShWiring(unittest.TestCase):
    def test_retire_max_default_is_bounded(self):
        m = re.search(r'RETIRE_MAX_S="\$\{FLEET_RETIRE_MAX_S:-(\d+)\}"', DEPLOY)
        self.assertTrue(m and 300 <= int(m.group(1)) <= 900, m and m.group(0))
        m = re.search(r'DRAIN_MAX_S="\$\{FLEET_DRAIN_MAX_S:-(\d+)\}"', DEPLOY)
        self.assertTrue(m and int(m.group(1)) <= 900, m and m.group(0))

    def test_cutover_retires_services_before_the_rename(self):
        body = _fn(DEPLOY, "proxy_deploy")
        cut = body.index('podman rename "$CONTAINER" "$RETIRED_MARKER"')
        self.assertIn('retire_services_in "$CONTAINER"', body[:cut])

    def test_retire_services_sets_flag_and_stops_dashboard_and_receiver(self):
        fn = _fn(DEPLOY, "retire_services_in")
        for needle in ("/fleet-kit/.retired", "fleet_view_server[.]py", "webhook_receiver[.]py", "9>&-"):
            self.assertIn(needle, fn)

    def test_abandon_is_bounded_by_retire_max(self):
        body = _fn(DEPLOY, "proxy_deploy")
        guard = body[body.index('running "$RETIRED_MARKER"'):body.index("podman build")]
        self.assertIn('-lt "$RETIRE_MAX_S"', guard, "ABANDONED must only apply while inside the drain budget")
        self.assertIn('terminate_and_stop "$RETIRED_MARKER"', guard, "past the budget the tick must end the drain itself")

    def test_reaper_uses_the_same_termination_code(self):
        fn = DEPLOY[DEPLOY.index("spawn_reaper() {"):DEPLOY.index("proxy_deploy() {")]
        self.assertIn("declare -f inflight_in terminate_and_stop", fn)
        self.assertIn('terminate_and_stop "$name"', fn)

    def test_rollback_clears_the_flag(self):
        self.assertIn("rm -f /fleet-kit/.retired", _fn(DEPLOY, "proxy_rollback"))


class FleetDbReleasesLocks(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.db = self.d / "fleet.db"
        self.runs = self.d / "runs.jsonl"
        self.runs.write_text("".join(
            f'{{"run_id": "r{i}", "member": "gru", "status": "ok", "ts": {1790000000 + i}}}\n' for i in range(3)))
        import fleet_db
        self.fleet_db = fleet_db

    def test_error_mid_sync_rolls_back_and_frees_the_write_lock(self):
        conn = self.fleet_db.connect(self.db)
        real, calls = self.fleet_db._row_from_record, []

        def boom(rec):
            calls.append(rec)
            if len(calls) == 2:
                raise sqlite3.OperationalError("database is locked")
            return real(rec)

        self.fleet_db._row_from_record = boom
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.fleet_db.sync(conn, self.runs)
        finally:
            self.fleet_db._row_from_record = real
        self.assertFalse(conn.in_transaction, "sync left its transaction (and the write lock) open")
        other = sqlite3.connect(str(self.db), timeout=0)
        other.execute("BEGIN IMMEDIATE")  # raises 'database is locked' if the lock leaked
        other.rollback()
        self.assertEqual(self.fleet_db.sync(conn, self.runs), 3, "the retry must re-read from the unadvanced offset")

    def test_connect_waits_out_a_short_write_burst(self):
        self.fleet_db.connect(self.db).close()
        holder = sqlite3.connect(str(self.db), check_same_thread=False)
        holder.execute("BEGIN IMMEDIATE")
        threading.Thread(target=lambda: (time.sleep(0.5), holder.rollback()), daemon=True).start()
        conn = self.fleet_db.connect(self.db)
        self.assertEqual(self.fleet_db.sync(conn, self.runs), 3)


if __name__ == "__main__":
    unittest.main()
