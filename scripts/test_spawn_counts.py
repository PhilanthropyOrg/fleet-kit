"""spawn_counts.sh counts minion passes from both hosts (baby-g, 2026-10-24).

Minions run on the worker node (lucky); its minion.log stays there, so dino's log showed 2 passes
for 10-24 while lucky ran 7. The node's run rows are relayed into dino's runs.jsonl, so the count
comes from there, with dino's own log as the floor and the fallback."""
import datetime as dt
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "spawn_counts.sh"
DAY = "2026-10-24"


def ts(hhmm):
    return dt.datetime.fromisoformat(f"{DAY} {hhmm}").timestamp()


def row(run_id, status, at, **kw):
    return json.dumps({"member": "minion", "run_id": run_id, "status": status, "ts": ts(at), **kw})


class SpawnCounts(unittest.TestCase):
    def run_script(self, runs=None, minion_log="", gru_log=""):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            if runs is not None:
                (tmp / "runs.jsonl").write_text("\n".join(runs) + "\n")
            (tmp / "minion.log").write_text(minion_log)
            (tmp / "gru.log").write_text(gru_log)
            out = subprocess.run(["bash", str(SCRIPT), DAY], capture_output=True, text=True,
                                 env={**os.environ, "FLEET_LOG_DIR": str(tmp)}, check=True)
            line = json.loads(out.stdout)
            self.assertEqual(json.loads((tmp / "spawn_counts.jsonl").read_text()), line)
            return line

    def test_node_passes_in_runs_jsonl_are_counted(self):
        runs = [
            row("a", "started", "03:08"), row("a", "ok", "04:24", exit_code=0),       # node
            row("b", "started", "15:06"), row("b", "ok", "15:15", exit_code=0),       # node
            row("c", "started", "19:08"), row("c", "timed_out", "19:36", exit_code=124),
            row("d", "started", "20:07"), row("d", "budget_declined", "20:07"),       # never ran
            json.dumps({"member": "gru", "run_id": "g", "status": "ok", "ts": ts("10:00")}),
        ]
        dino_log = f"[{DAY} 19:36:00 CDT] pass end rc=124 (account=a)\n"
        gru_log = "".join(f"[{DAY} {h:02d}:01:00 CDT] pass end rc=0\n" for h in range(0, 23))
        got = self.run_script(runs, dino_log, gru_log)
        self.assertEqual(got["minion_passes"], 3)
        self.assertEqual(got["minion_failed"], 1)
        self.assertEqual(got["minion_hours"], 4)            # 03, 04, 15, 19; the declined run builds in no hour
        self.assertEqual(got["minion_passes_dino"], 1)
        self.assertEqual(got["minion_passes_node"], 2)
        self.assertEqual(got["minion_source"], "runs.jsonl")
        self.assertEqual(got["idle_hours_forfeited"], 23 - got["minion_hours"])
        for k in ("day", "gru_passes", "gru_hours", "minion_passes", "minion_hours",
                  "minion_failed", "idle_hours_forfeited"):
            self.assertIn(k, got)

    def test_a_running_pass_counts_its_hours(self):
        now = dt.datetime.now()
        if now.hour < 2:
            self.skipTest("needs two local hours of today behind us")
        start = (now - dt.timedelta(hours=1)).timestamp()
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "runs.jsonl").write_text(json.dumps(
                {"member": "minion", "run_id": "r", "status": "started", "ts": start, "timeout_s": 6600}) + "\n")
            out = subprocess.run(["bash", str(SCRIPT), now.strftime("%F")], capture_output=True, text=True,
                                 env={**os.environ, "FLEET_LOG_DIR": tmp}, check=True)
        got = json.loads(out.stdout)
        self.assertEqual((got["minion_passes"], got["minion_hours"]), (0, 2))

    def test_without_runs_jsonl_falls_back_to_dino_log(self):
        log = f"[{DAY} 08:00:00 CDT] pass end rc=0\n[{DAY} 09:00:00 CDT] pass end rc=1\n"
        got = self.run_script(None, log)
        self.assertEqual((got["minion_passes"], got["minion_failed"], got["minion_hours"]), (2, 1, 2))
        self.assertEqual(got["minion_source"], "minion.log")

    def test_dino_log_is_the_floor(self):
        log = f"[{DAY} 08:00:00 CDT] pass end rc=0\n[{DAY} 09:00:00 CDT] pass end rc=0\n"
        got = self.run_script([row("x", "started", "08:00"), row("x", "ok", "08:30")], log)
        self.assertEqual(got["minion_passes"], 2)
        self.assertEqual(got["minion_passes_node"], 0)


if __name__ == "__main__":
    unittest.main()
