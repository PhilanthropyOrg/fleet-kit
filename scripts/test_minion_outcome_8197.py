"""gh#8197: every minion run records an ending, and a deploy's drain no longer kills minions.

2026-09-26: the retired build's reaper (deploy.sh, FLEET_RETIRE_MAX_S=900) SIGTERMed #7939 and
#7941 at 16:57 and again at 19:02 UTC, ~15 min after each cutover (rc=143). Separately, runs a
SIGKILL ended (OOM, `podman stop` escalating) stayed `started` forever, and /api/minion_runs
listed each finished run's `started` row beside its ending, so finished runs read as
"never recorded an outcome".

Run: python3 scripts/test_minion_outcome_8197.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import open_runs  # noqa: E402

DEPLOY = (HERE / "deploy.sh").read_text()
NOW = time.time()


def _fn(src: str, name: str) -> str:
    start = src.index(f"{name}() {{")
    return src[start:src.index("\n}\n", start) + 3]


def rec(run_id, status, age, **kw):
    return {"run_id": run_id, "member": run_id.split("-")[0], "status": status, "ts": NOW - age, **kw}


RECS = [
    rec("minion-item7939-1-1", "started", 5400 + 3600 + 60, timeout_s=5400, item_id="7939"),  # SIGKILLed
    rec("minion-item7941-2-2", "started", 6000, timeout_s=5400),       # maybe still queued/draining
    rec("minion-item7988-3-3", "started", 20000, timeout_s=5400),
    rec("minion-item7988-3-3", "quiet", 19900, exit_code=0),           # finished: not lost
    rec("gru-4-4", "started", 7200 + 3600 + 60),                       # old row, no timeout_s
    rec("gru-5-5", "started", 60),
]


class CloseLost(unittest.TestCase):
    def test_lost_runs_are_the_dead_started_rows_only(self):
        self.assertEqual(sorted(r["run_id"] for r in open_runs.lost_runs(RECS, NOW)),
                         ["gru-4-4", "minion-item7939-1-1"])

    def test_close_lost_cli_writes_one_killed_row_with_exit_code_once(self):
        d = tempfile.mkdtemp()
        runs = Path(d, "runs.jsonl")
        runs.write_text("".join(json.dumps(r) + "\n" for r in RECS))
        env = dict(os.environ, FLEET_LOG_DIR=d)
        cli = [sys.executable, str(HERE / "open_runs.py"), "close-lost"]
        p = subprocess.run(cli, env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("minion-item7939-1-1", p.stdout)
        rows = [json.loads(line) for line in runs.read_text().splitlines()]
        new = rows[len(RECS):]
        self.assertEqual(len(new), 2, new)
        m = next(r for r in new if r["run_id"] == "minion-item7939-1-1")
        self.assertEqual((m["status"], m["exit_code"], m["lost"], m["item_id"]), ("killed", 137, True, "7939"))
        self.assertTrue(m["outcome"].startswith("LOST"), m["outcome"])
        self.assertEqual(open_runs.lost_runs(rows, NOW), [], "every run now has an ending")
        # A second sweep (the other container, the next gru pass) adds nothing.
        subprocess.run(cli, env=env, capture_output=True, text=True, check=True)
        self.assertEqual(len(runs.read_text().splitlines()), len(rows))

    def test_gru_cron_entry_sweeps_before_each_pass(self):
        src = (HERE / "run_gru_fanout.sh").read_text()
        self.assertLess(src.index("open_runs.py\" close-lost"), src.index("exec bash"))


class StartedRowCarriesTimeout(unittest.TestCase):
    def _started(self, *extra):
        p = subprocess.run([sys.executable, str(HERE / "run_report.py"), "--started", "--member", "minion",
                            "--run-id", "minion-item1-1-1", *extra], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_timeout_recorded(self):
        self.assertEqual(self._started("--timeout-s", "5400")["timeout_s"], 5400)

    def test_garbage_timeout_never_blocks_the_started_row(self):
        self.assertIsNone(self._started("--timeout-s", "None")["timeout_s"])

    def test_run_member_passes_it(self):
        src = (HERE / "run_member.sh").read_text()
        started = src[src.index('run_report.py" --started'):]
        self.assertIn('--timeout-s "$TIMEOUT_S"', started[:300])


class MinionRunsOneRowPerRun(unittest.TestCase):
    def test_started_row_collapses_into_its_ending(self):
        import importlib
        fvs = importlib.import_module("fleet_view_server")
        rows = [  # newest first, like fleet_db.query_runs
            {"run_id": "a", "recorded_at": 4.0, "status": "quiet", "exit_code": 0, "outcome": "QUIET"},
            {"run_id": "b", "recorded_at": 3.0, "status": "started"},
            {"run_id": "a", "recorded_at": 2.0, "status": "started"},
            {"run_id": "c", "recorded_at": 1.0, "status": "killed", "exit_code": 143},
        ]
        out = fvs.minion_runs_payload(rows, {}, limit=10)
        self.assertEqual([(r["run_id"], r["status"]) for r in out], [("a", "quiet"), ("b", "started"), ("c", "killed")])
        self.assertEqual(len(fvs.minion_runs_payload(rows, {}, limit=2)), 2)


class DrainSparesMinions(unittest.TestCase):
    PASS = re.search(r"^PASS_PATTERN='([^']+)'", DEPLOY, re.M).group(1)
    MINION = re.search(r"^MINION_PATTERN='([^']+)'", DEPLOY, re.M).group(1)

    def test_minion_pattern_matches_minions_only(self):
        for args in ("bash /fleet-kit/scripts/run_member.sh minion --items 7939",
                     "/bin/bash /fleet-kit/scripts/run_member.sh minion --items 7941,7942",
                     "bash /fleet-kit/scripts/run_member.sh minion"):
            self.assertRegex(args, self.MINION)
            self.assertRegex(args, self.PASS)
        for args in ("bash /fleet-kit/scripts/run_member.sh gru",
                     "bash /fleet-kit/scripts/run_member.sh nerd --task lane=minions",
                     "bash /fleet-kit/scripts/run_member.sh minion-watch"):
            self.assertNotRegex(args, self.MINION)

    def test_a_prompt_that_mentions_a_pass_is_not_a_pass(self):
        # Live 2026-09-26 21:12 UTC: these matched the unanchored pattern and got SIGTERM.
        for args in ("timeout 5400 claude -p You are minion. Run bash /fleet-kit/scripts/run_member.sh minion --items 1",
                     "claude -p see bash /fleet-kit/scripts/worktree_builder.sh --item 7",
                     "sh -c pgrep -f bash .*run_member[.]sh"):
            self.assertNotRegex(args, self.PASS)
            self.assertNotRegex(args, self.MINION)

    def test_drain_counts_minions_apart_from_other_passes(self):
        # Real processes, but patterns scoped to this temp dir: pgrep on dino also sees the
        # fleet's own containers, and this test must never signal a real pass.
        d = tempfile.mkdtemp()
        script = Path(d, "run_member.sh")
        script.write_text("sleep 60\n")
        stub = Path(d, "podman")
        stub.write_text('#!/bin/bash\n[ "$1" = exec ] && { shift 2; exec "$@"; }\n')
        stub.chmod(0o755)
        procs = {name: subprocess.Popen(argv) for name, argv in (
            ("minion", ["bash", str(script), "minion", "--items", "1"]),
            ("gru", ["bash", str(script), "gru"]),
            # a minion's own `timeout claude -p <prompt>` child: its prompt names run_member.sh
            ("minion_child", ["timeout", "60", "bash", "-c", f"sleep 60 # bash {script} gru", "x"]))}
        try:
            time.sleep(0.3)
            scoped = lambda pat: pat.replace("bash [^ ]*", f"bash {re.escape(d)}/[^ ]*", 1)  # noqa: E731
            body = "\n".join([f"PASS_PATTERN='{scoped(self.PASS)}'", f"MINION_PATTERN='{scoped(self.MINION)}'",
                              _fn(DEPLOY, "inflight_in"), _fn(DEPLOY, "minions_in"),
                              _fn(DEPLOY, "drain_budget_s"),
                              "RETIRE_MAX_S=900 RETIRE_MINION_MAX_S=5700",
                              'echo "budget=$(drain_budget_s x)"',
                              'echo "minions=$(minions_in x) all=$(inflight_in x)"'])
            env = dict(os.environ, PATH=f"{d}:{os.environ['PATH']}")
            p = subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("budget=5700", p.stdout)
            self.assertIn("minions=1 all=2", p.stdout)
        finally:
            for pr in procs.values():
                pr.kill()
                pr.wait()

    def test_reaper_waits_for_minions_up_to_their_budget(self):
        fn = DEPLOY[DEPLOY.index("spawn_reaper() {"):DEPLOY.index("proxy_deploy() {")]
        self.assertIn("declare -f inflight_in terminate_and_stop minions_in", fn)
        self.assertIn('-lt "$RETIRE_MINION_MAX_S"', fn)
        # While a minion holds the retired build up, no other pass in it is terminated early.
        self.assertNotIn("terminate_non_minions", DEPLOY)
        m = re.search(r'RETIRE_MINION_MAX_S="\$\{FLEET_RETIRE_MINION_MAX_S:-(\d+)\}"', DEPLOY)
        timeout = json.loads((HERE.parent / "members" / "minion" / "minion.fleet.json").read_text())["timeout_s"]
        interval = int(re.search(r'FLEET_DEPLOY_MIN_INTERVAL_S:-(\d+)',
                                 (HERE / "auto_deploy.sh").read_text()).group(1))
        self.assertTrue(timeout < int(m.group(1)) < interval,
                        "the minion drain must outlast a minion's timeout and end before the next deploy")


if __name__ == "__main__":
    unittest.main()
