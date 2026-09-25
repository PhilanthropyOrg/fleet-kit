"""gru sends fixers to its own red PRs BEFORE it builds anything new.

2026-09-25, philanthropy: three gru passes in a row (13:03, 14:03, 15:03 UTC) went straight to
step 2a -- open fleet:reif-priority items outrank everything -- and built more, while #7975,
#7982 and #7986, the PRs of those same Reif-priority items, sat red. Step 2a-bis (fix issues
for red PRs) came AFTER 2a, so with any Reif-priority item open it was never reached.

Pinned here: step 0 exists, comes before step 1 and 2a, runs `red_prs.py due`, dispatches
`the-fixer --item <PR>`, is waited on, and the manifest's first checklist line says so; and
`red_prs.py due` end to end (stubbed gh) puts the Reif-priority red PR first.

Run: python3 scripts/test_gru_red_prs_first.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE.parent
sys.path.insert(0, str(HERE))

import pr_ci_wait  # noqa: E402
import red_prs  # noqa: E402

GRU = (KIT / "members" / "gru" / "gru.md").read_text()
SPEC = json.loads((KIT / "members" / "gru" / "gru.fleet.json").read_text())


class Charter(unittest.TestCase):
    def test_step_zero_runs_before_the_allowance_and_before_reif_priority_items(self):
        s0 = GRU.index("0. **Your own red PRs first")
        s1 = GRU.index("1. **Read this hour's allowance")
        s2a = GRU.index("2a. **First, check for an open Reif-priority epic")
        self.assertLess(s0, s1)
        self.assertLess(s1, s2a)

    def test_step_zero_dispatches_fixers_from_the_detector(self):
        step0 = GRU[GRU.index("0. **Your own red PRs first"):GRU.index("1. **Read this hour's allowance")]
        self.assertIn("python3 /fleet-kit/scripts/red_prs.py due", step0)
        self.assertIn("bash /fleet-kit/scripts/dispatch_fixer.sh <PR> <PR> ...", step0)
        self.assertIn("DETACHED", step0)
        self.assertIn("even when step 1 ends the pass early", step0)
        self.assertIn("Review findings are included", step0)

    def test_reif_priority_never_outranks_step_zero(self):
        self.assertIn("never step 0", GRU[GRU.index("2a. **First"):GRU.index("2a-bis.")])

    def test_step_zero_fixers_are_detached_not_waited_for(self):
        # 20:26 UTC 2026-09-25: backgrounded fixers died (exit 143) when gru's turn ended.
        self.assertIn("step-0 fixers are detached and\n   are NOT waited for", GRU)
        self.assertNotIn("each `task_id` from steps 0 and 5", GRU)

    def test_todo_count_matches_steps(self):
        self.assertIn("exactly these 11 items (steps 0-10)", GRU)

    def test_manifest_checklist_leads_with_it(self):
        first = SPEC["mandate"]["checklist"][0]
        self.assertIn("red_prs.py due", first)
        self.assertIn("dispatch_fixer.sh", first)
        self.assertIn("red_prs.py due", SPEC["mandate"]["target"])

    def test_the_fixer_knows_what_a_dispatched_sub_pass_is(self):
        fixer = (KIT / "members" / "the-fixer" / "the-fixer.md").read_text()
        self.assertIn("FIRE assigned-pr #<N>", fixer)
        self.assertIn("pr_ci_wait.py <N>", fixer)
        self.assertIn("review finding", fixer)


def _pr(number, branch, failed, real_mins_ago):
    import time
    t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - real_mins_ago * 60))
    return {"number": number, "state": "OPEN", "headRefName": branch, "headRefOid": f"{number:040d}",
            "isDraft": False, "autoMergeRequest": {}, "labels": [], "comments": [],
            "createdAt": t, "url": "",
            "commits": [{"oid": f"{number:040x}", "messageHeadline": "work", "committedDate": t}],
            "statusCheckRollup": [{"__typename": "CheckRun", "name": n, "status": "COMPLETED",
                                   "conclusion": "FAILURE" if n in failed else "SUCCESS",
                                   "completedAt": t, "detailsUrl": ""} for n in ("lint", "test")]}


class DetachedDispatch(unittest.TestCase):
    """A dispatched fixer must outlive the pass that dispatched it. Reproduces the 20:26 kill:
    the dispatcher's whole process group is SIGKILLed right after dispatching, and the fixer
    (a stub run_member.sh that sleeps, then writes a marker) must still finish."""

    def test_fixer_survives_its_dispatcher(self):
        import signal
        import subprocess
        import time
        d = tempfile.mkdtemp()
        stub = Path(d, "run_member.sh")
        stub.write_text(f'sleep 2; echo "$@" > {d}/done\n')
        env = dict(os.environ, FLEET_RUN_MEMBER=str(stub), FLEET_LOG_DIR=d)
        parent = subprocess.Popen(
            ["bash", "-c", f"bash {HERE / 'dispatch_fixer.sh'} 7982 '#7975' nope; sleep 30"],
            env=env, start_new_session=True, stdout=subprocess.PIPE, text=True)
        time.sleep(0.8)
        os.killpg(parent.pid, signal.SIGKILL)  # the dispatcher's pass ends, taking its group
        parent.wait()
        deadline = time.time() + 10
        while not Path(d, "done").exists() and time.time() < deadline:
            time.sleep(0.2)
        self.assertTrue(Path(d, "done").exists(), "the fixer died with its dispatcher")
        self.assertIn("the-fixer --item", Path(d, "done").read_text())
        self.assertTrue(Path(d, "the-fixer-item7982.dispatch.log").exists())
        self.assertTrue(Path(d, "the-fixer-item7975.dispatch.log").exists(), "'#7975' is a PR too")


class DueEndToEnd(unittest.TestCase):
    def test_reif_priority_red_pr_first_other_stalled_next_fresh_one_waits(self):
        prs = {7986: _pr(7986, "member/minion-item7942_7937-1-2", ["lint"], 30),
               7742: _pr(7742, "member/minion-item7700-1-2", ["test"], 1300),
               7990: _pr(7990, "member/minion-item7800-1-2", ["lint"], 10),
               7991: _pr(7991, "fix/a-human-branch", ["lint"], 500)}

        def gh(args, timeout=60):
            if args[:2] == ["pr", "list"]:
                return 0, json.dumps([{"number": n, "headRefName": p["headRefName"], "isDraft": False}
                                      for n, p in prs.items()])
            if args[:2] == ["issue", "list"]:
                return 0, json.dumps([{"number": 7942}])
            if args[:2] == ["pr", "view"]:
                return 0, json.dumps(prs[int(args[2])])
            return 1, ""

        orig = pr_ci_wait._gh
        with tempfile.TemporaryDirectory() as d:
            os.environ["FLEET_LOG_DIR"] = d
            red_prs._open_prs.__defaults__ = (gh,)
            red_prs._priority_issues.__defaults__ = (gh,)
            red_prs.rows_now.__defaults__ = (gh, None)
            pr_ci_wait.fetch.__defaults__ = (gh,)
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.assertEqual(red_prs.main(["due"]), 0)
                out = json.loads(buf.getvalue())
            finally:
                red_prs._open_prs.__defaults__ = (orig,)
                red_prs._priority_issues.__defaults__ = (orig,)
                red_prs.rows_now.__defaults__ = (orig, None)
                pr_ci_wait.fetch.__defaults__ = (orig,)
                os.environ.pop("FLEET_LOG_DIR", None)
        self.assertEqual([r["number"] for r in out["due"]], [7986, 7742])
        self.assertTrue(out["due"][0]["reif_priority"])
        self.assertEqual(out["not_yet_stalled"], [7990])


if __name__ == "__main__":
    unittest.main()
