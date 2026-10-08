#!/usr/bin/env python3
"""Build only fixes for issues plus the one funnel lane, at most N minions an hour (Reif, 2026-10-08).

"we listen hard for issues, and only build fixes for issues, and then the one funnel thing we
need - orgverify ... cut the prs way down, one per hour - big moves". 145 minion starts in the
24h before; the last 100 merged PRs moved no outcome number and cost ~5,850 CI minutes a day.

RED without the change: run_member.sh dispatches a minion for any item gru hands it, however
many an hour; gate_drops.py candidates lists every backlog item; fanout.py batches returns every
batch the turn budget fits. GREEN: with FLEET_BUILD_ONLY_LABELS an off-lane item is not a
candidate and is refused at dispatch; with FLEET_MINION_MAX_PER_HOUR the packer defers the
batches the hour has no room for and dispatch refuses the over-cap minion. Both unset: no change.
Run: python3 scripts/test_build_lane_rule.py
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

import build_lane_rule as rule  # noqa: E402
import fanout  # noqa: E402
import gate_drops  # noqa: E402

FAKE_GH = """#!/bin/sh
# gh issue view <n> --repo <r> --json labels --jq ...  -> labels of a pretend issue
case "$3" in
  7001) echo "fleet:backlog,bug" ;;
  7002) echo "fleet:backlog,lane:orgverify,fleet:reif-priority" ;;
  *) echo "fleet:backlog,fleet:reif-priority" ;;
esac
"""


def _started(ts: float, member: str = "minion", status: str = "started") -> str:
    return json.dumps({"member": member, "run_id": f"{member}-{int(ts)}", "kind": "llm",
                       "ts": str(ts), "status": status})


class PureRules(unittest.TestCase):
    def test_off_lane_needs_one_allowed_label(self):
        labels = {1: ["fleet:backlog", "bug"], 2: ["fleet:backlog"], 3: ["lane:orgverify"]}
        self.assertEqual(rule.off_lane(labels, ["bug", "lane:orgverify"]), [2])
        self.assertEqual(rule.off_lane(labels, []), [], "no rule set: nothing is off lane")

    def test_env_parsing_is_forgiving(self):
        self.assertEqual(rule.allowed_labels({"FLEET_BUILD_ONLY_LABELS": " bug, ,lane:orgverify "}),
                         ["bug", "lane:orgverify"])
        self.assertEqual(rule.allowed_labels({}), [])
        self.assertEqual(rule.hour_cap({"FLEET_MINION_MAX_PER_HOUR": "1"}), 1)
        self.assertEqual(rule.hour_cap({"FLEET_MINION_MAX_PER_HOUR": "x"}), 0)
        self.assertEqual(rule.hour_cap({}), 0)

    def test_started_in_window_counts_only_recent_minion_starts(self):
        now = 1_800_000_000.0
        # the window is 50 minutes (WINDOW_S), not an hour: see the note in build_lane_rule.py
        lines = [_started(now - 100), _started(now - 2999), _started(now - 3001),
                 _started(now - 10, member="the-fixer"), _started(now - 10, status="ok"),
                 "not json", ""]
        self.assertEqual(rule.started_in_window(lines, now=now), 2)
        self.assertEqual(rule.remaining(1, 2), 0)
        self.assertEqual(rule.remaining(3, 2), 1)
        self.assertIsNone(rule.remaining(0, 2), "no cap: None, the packer leaves the result alone")

    def test_cap_batches_keeps_the_first_and_defers_the_rest(self):
        result = {"n_batches": 3, "batches": [{"items": [{"number": 1}]}, {"items": [{"number": 2}]},
                                              {"items": [{"number": 3}, {"number": 4}]}],
                  "deferred": [{"number": 9, "why": "minimum"}]}
        out = rule.cap_batches(result, 1, cap=1, started=0)
        self.assertEqual(out["n_batches"], 1)
        self.assertEqual([b["items"][0]["number"] for b in out["batches"]], [1])
        self.assertEqual([d["number"] for d in out["deferred"]], [9, 2, 3, 4])
        self.assertIn("FLEET_MINION_MAX_PER_HOUR=1", out["deferred"][1]["why"])
        self.assertEqual(rule.cap_batches(result, None), result, "no cap: untouched")
        self.assertEqual(rule.cap_batches(result, 0, cap=1, started=1)["batches"], [])

    def test_candidates_drop_off_lane_items_before_gru_claims_them(self):
        backlog = [{"number": 1, "labels": [{"name": "fleet:backlog"}, {"name": "fleet:priority-high"}],
                    "createdAt": "2026-10-01"},
                   {"number": 2, "labels": [{"name": "fleet:backlog"}, {"name": "bug"}],
                    "createdAt": "2026-10-02"}]
        self.assertEqual([i["number"] for i in gate_drops.candidates(backlog)], [1, 2])
        self.assertEqual([i["number"] for i in gate_drops.candidates(backlog, ["bug"])], [2])


class PackerHourCap(unittest.TestCase):
    def _cli(self, items, runs_dir: str, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_MINION_")}
        e.update(env, FLEET_LOG_DIR=runs_dir)
        r = subprocess.run([sys.executable, str(HERE / "fanout.py"), "batches", "--turn-budget", "0",
                            "--unit-turns", "30", "--items", json.dumps(items)],
                           capture_output=True, text=True, env=e, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_one_batch_an_hour_the_rest_wait(self):
        items = [{"number": 9300 + i, "complexity": 5, "area": ""} for i in range(4)]
        with tempfile.TemporaryDirectory() as d:
            r = self._cli(items, d, FLEET_MINION_TARGET_ITEMS="2", FLEET_MINION_MIN_ITEMS="0")
            self.assertEqual(len(r["batches"]), 2, "no cap: both batches")
            r = self._cli(items, d, FLEET_MINION_TARGET_ITEMS="2", FLEET_MINION_MIN_ITEMS="0",
                          FLEET_MINION_MAX_PER_HOUR="1")
            self.assertEqual(len(r["batches"]), 1)
            self.assertEqual([x["number"] for x in r["deferred"]], [9302, 9303])
            Path(d, "runs.jsonl").write_text(_started(time.time() - 60) + "\n")
            r = self._cli(items, d, FLEET_MINION_TARGET_ITEMS="2", FLEET_MINION_MIN_ITEMS="0",
                          FLEET_MINION_MAX_PER_HOUR="1")
            self.assertEqual(r["batches"], [], "one already started this hour: nothing more")
            self.assertEqual(r["hour_cap"]["started_last_hour"], 1)


class DispatchGuard(unittest.TestCase):
    """run_member.sh refuses in one second, before any lock, worktree or model. The fake `gh`
    answers the label lookup; nothing past the guard runs because the guard exits 2."""

    def _run(self, args: list[str], runs_lines: list[str], **env) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as d:
            Path(d, "gh").write_text(FAKE_GH)
            os.chmod(Path(d, "gh"), 0o755)
            Path(d, "runs.jsonl").write_text("".join(l + "\n" for l in runs_lines))
            e = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_")}
            e.update(env, PATH=d + os.pathsep + os.environ.get("PATH", ""), FLEET_REPO="x/y",
                     FLEET_LOG_DIR=d, FLEET_ENV_FILE=str(Path(d, "none.env")), HOME=d)
            return subprocess.run(["bash", str(HERE / "run_member.sh"), "minion", *args],
                                  capture_output=True, text=True, env=e, timeout=20)

    def test_off_lane_item_is_refused(self):
        r = self._run(["--items", "7001,7003"], [], FLEET_BUILD_ONLY_LABELS="bug,lane:orgverify")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("FATAL: minion item(s) 7003 carry none of FLEET_BUILD_ONLY_LABELS", r.stderr)

    def test_single_item_form_is_checked_too(self):
        r = self._run(["--item", "7003"], [], FLEET_BUILD_ONLY_LABELS="bug")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("7003 carry none", r.stderr)

    def test_on_lane_items_pass_the_label_guard_then_hit_the_hour_cap(self):
        now = time.time()
        r = self._run(["--items", "7001,7002"], [_started(now - 120)],
                      FLEET_BUILD_ONLY_LABELS="bug,lane:orgverify", FLEET_MINION_MAX_PER_HOUR="1")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("carry none", r.stderr, "7001 has bug, 7002 has lane:orgverify")
        self.assertIn("FATAL: 1 minion(s) already started in the last hour; FLEET_MINION_MAX_PER_HOUR=1",
                      r.stderr)

    def test_a_start_older_than_an_hour_does_not_count(self):
        self.assertEqual(rule.started_in_window([_started(time.time() - 3700)]), 0)
        with tempfile.TemporaryDirectory() as d:
            Path(d, "runs.jsonl").write_text(_started(time.time() - 3700) + "\n")
            out = subprocess.run([sys.executable, str(HERE / "build_lane_rule.py"), "hour-count",
                                  "--runs", str(Path(d, "runs.jsonl"))],
                                 capture_output=True, text=True, timeout=20).stdout.strip()
            self.assertEqual(out, "0")


class HourSlotIsTakenAtomically(unittest.TestCase):
    """2026-10-08 04:12Z: four minions launched within 11s against a cap of 1. Each counted
    runs.jsonl before any of them had written its `started` row, so all four saw 0. RED with a
    bare hour-count: four concurrent dispatchers all pass. GREEN with reserve: exactly one does."""

    def test_four_dispatchers_at_once_get_one_slot(self):
        with tempfile.TemporaryDirectory() as d:
            cmd = [sys.executable, str(HERE / "build_lane_rule.py"), "reserve",
                   "--runs", str(Path(d, "runs.jsonl")), "--ledger", str(Path(d, "slots.log")),
                   "--cap", "1"]
            procs = [subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True) for _ in range(4)]
            rcs = sorted(p.wait(timeout=20) for p in procs)
            self.assertEqual(rcs, [0, 3, 3, 3])

    def test_a_racers_newer_ledger_line_still_counts(self):
        # CI 2026-10-08 05:43Z: two of four got a slot. Each dispatcher read the clock BEFORE
        # the lock; the one that lost the lock race then filtered out the winner's line because
        # its timestamp was a few ms after the loser's `now`. A ledger line is only ever written
        # under the lock, so a newer one is a real start and counts.
        with tempfile.TemporaryDirectory() as d:
            runs, ledger = Path(d, "runs.jsonl"), Path(d, "slots.log")
            ledger.write_text(f"{time.time() + 2} minion 1\n")
            self.assertEqual(rule.reserve(runs, ledger, cap=1, now=time.time()), (False, 1))

    def test_a_started_row_still_counts_and_an_old_slot_does_not(self):
        with tempfile.TemporaryDirectory() as d:
            runs, ledger = Path(d, "runs.jsonl"), Path(d, "slots.log")
            ledger.write_text(f"{time.time() - 3700} minion 1\n")
            self.assertEqual(rule.reserve(runs, ledger, cap=1), (True, 0))
            self.assertEqual(rule.reserve(runs, ledger, cap=1), (False, 1))
            runs.write_text(_started(time.time() - 60) + "\n")
            self.assertEqual(rule.reserve(runs, Path(d, "fresh.log"), cap=1), (False, 1))



class CiDailyBudgetTests(unittest.TestCase):
    """MANDATE.md 2026-10-08, "max CI mins used today 500": 2,581 read that day, 5,000+ before."""

    def test_the_dial_reads_like_the_hour_cap(self):
        self.assertEqual(rule.ci_daily_max({"FLEET_CI_MINUTES_DAILY_MAX": "500"}), 500)
        self.assertEqual(rule.ci_daily_max({}), 0)
        self.assertEqual(rule.ci_daily_max({"FLEET_CI_MINUTES_DAILY_MAX": "lots"}), 0)

    def test_a_spent_day_stops_and_an_unread_meter_fails_open(self):
        self.assertTrue(rule.ci_spent(500, 2581))
        self.assertTrue(rule.ci_spent(500, 500))
        self.assertFalse(rule.ci_spent(500, 499))
        self.assertFalse(rule.ci_spent(500, None))
        self.assertFalse(rule.ci_spent(0, 9999))

    def test_a_spent_day_defers_every_batch_and_names_the_dial(self):
        result = {"batches": [{"items": [{"number": 1}]}, {"items": [{"number": 2}]}],
                  "n_batches": 2, "deferred": []}
        out = rule.cap_batches(result, 0, why="CI day spent: 2581 of FLEET_CI_MINUTES_DAILY_MAX=500")
        self.assertEqual(out["n_batches"], 0)
        self.assertEqual([d["number"] for d in out["deferred"]], [1, 2])
        self.assertIn("FLEET_CI_MINUTES_DAILY_MAX", out["deferred"][0]["why"])

    def test_dispatch_refuses_a_minion_when_the_day_is_spent(self):
        src = (HERE / "run_member.sh").read_text()
        gate = src.index('"${FLEET_CI_MINUTES_DAILY_MAX:-0}" -gt 0')
        self.assertLess(gate, src.index("build_lane_rule.py\" reserve"))
        self.assertIn("ci-budget", src[gate:gate + 300])

if __name__ == "__main__":
    unittest.main()


def test_a_start_55_minutes_ago_does_not_block_the_next_hourly_pass(tmp_path):
    # 2026-10-08: gru runs at :03 and starts its minion at :06-:15. With a 60-minute window the
    # 09:10 start still counted at the 10:06 pass, so the 10 o'clock slot was lost (same for 07
    # after a 06:51 deploy-kicked start). Half the day's one-an-hour slots went unused.
    import time
    now = time.time()
    runs = tmp_path / "runs.jsonl"
    ledger = tmp_path / "ledger"
    runs.write_text(json.dumps({"member": "minion", "status": "started", "ts": now - 55 * 60}) + "\n")
    ledger.write_text(f"{now - 55 * 60} minion 1\n")
    assert rule.started_in_window(runs.read_text().splitlines(), now=now) == 0
    assert rule.reserve(runs, ledger, cap=1, now=now) == (True, 0)
    # a start 20 minutes ago still holds the slot
    runs.write_text(json.dumps({"member": "minion", "status": "started", "ts": now - 20 * 60}) + "\n")
    ledger.write_text(f"{now - 20 * 60} minion 1\n")
    assert rule.reserve(runs, ledger, cap=1, now=now) == (False, 1)
