"""No push to philanthropy past today's Actions share; only money PRs between 80% and 100%."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import push_budget_gate as gate  # noqa: E402

NOW = 1792252800.0  # 2026-10-17 16:00Z: 14 days after today in October
NONE, CLAIMS, SALES = "Moves: none", "Moves: claims", "Moves: $150 report sales"


def meter(pct):
    return {"day": "2026-10-17", "share": 12.5, "spent": round(12.5 * pct / 100, 2), "pct": pct}


class PushBudgetGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = Path(self.tmp.name) / "logs"
        self.logs.mkdir()
        self.wt = Path(self.tmp.name) / "wt"
        subprocess.run(["git", "init", "-q", str(self.wt)], check=True)
        subprocess.run(["git", "-C", str(self.wt), "remote", "add", "origin",
                        "https://github.com/PhilanthropyOrg/philanthropy.git"], check=True)
        env = mock.patch.dict(os.environ, {"FLEET_LOG_DIR": str(self.logs)})
        env.start()
        self.addCleanup(env.stop)

    def rows(self):
        return [json.loads(x) for x in (self.logs / "push_gate.jsonl").read_text().splitlines()]

    def test_day_share_is_budget_left_over_days_left(self):
        m = gate.read_meter(NOW, fetch=lambda: (250.0, 75.0 + 6.25, 6.25))  # $175 left, 14 days
        self.assertEqual((m["share"], m["spent"], m["pct"]), (12.5, 6.25, 50.0))
        # cached for 10 minutes: a second read does not call the API
        self.assertEqual(gate.read_meter(NOW + 60, fetch=lambda: 1 / 0)["pct"], 50.0)

    def test_79_percent_lets_a_none_pr_push(self):
        self.assertEqual(gate.check(str(self.wt), now=NOW, meter=meter(79), moves=NONE),
                         (True, "ok"))

    def test_81_percent_refuses_a_none_pr_and_allows_a_money_pr(self):
        ok, why = gate.check(str(self.wt), now=NOW, meter=meter(81), moves=NONE)
        self.assertFalse(ok)
        self.assertIn("Moves: claims", why)
        self.assertTrue(gate.check(str(self.wt), now=NOW, meter=meter(81), moves=CLAIMS)[0])
        self.assertTrue(gate.check(str(self.wt), now=NOW, meter=meter(81), moves=SALES)[0])
        self.assertEqual([r["verdict"] for r in self.rows()], ["refuse", "allow", "allow"])

    def test_100_percent_refuses_everything(self):
        for moves in (NONE, CLAIMS, SALES):
            ok, why = gate.check(str(self.wt), now=NOW, meter=meter(100), moves=moves)
            self.assertFalse(ok)
            self.assertIn("next UTC day", why)

    def test_bypass_is_only_a_deploy_gate_revert_and_it_logs_as_past_cap(self):
        self.assertFalse(gate.check(str(self.wt), "need it", NOW, meter(120), NONE)[0])
        self.assertEqual(gate.check(str(self.wt), "deploy-gate revert of #12902", NOW, meter(120),
                                    NONE), (True, "bypass"))
        self.assertEqual(self.rows()[-1]["verdict"], "bypass")
        self.assertTrue(self.rows()[-1]["past_cap"])
        n = gate.today_counts(NOW)
        self.assertEqual((n["bypass"], n["refuse"], n["past_cap"]), (1, 1, 1))
        with mock.patch.object(gate, "read_meter", return_value=meter(120)):
            self.assertIn("FLEET BUG: 1 push(es) past the cap", gate.summary(NOW))

    def test_hook_blocks_a_bare_push_and_ignores_other_repos(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "git add a.py && git push -u origin HEAD"}}
        with mock.patch.object(gate, "read_meter", return_value=meter(100)), \
                mock.patch.object(gate, "moves_line", return_value=CLAIMS):
            self.assertIn("BLOCKED (push_budget_gate)", gate.decide(payload, {"WT_PATH": str(self.wt)}))
            self.assertIsNone(gate.decide({"tool_name": "Bash", "tool_input": {"command": "git status"}},
                                          {"WT_PATH": str(self.wt)}))
            wrapped = {"tool_name": "Bash", "tool_input": {"command":
                       'python3 push_budget_gate.py --why "deploy-gate revert" -- git push origin HEAD'}}
            self.assertIsNone(gate.decide(wrapped, {"WT_PATH": str(self.wt)}))
            subprocess.run(["git", "-C", str(self.wt), "remote", "set-url", "origin",
                            "https://github.com/PhilanthropyOrg/fleet-kit.git"], check=True)
            self.assertIsNone(gate.decide(payload, {"WT_PATH": str(self.wt)}))

    def test_every_member_runs_the_hook(self):
        import worktree_guard_hook_install as inst
        self.assertTrue(any(c.endswith("push_budget_gate.py") for c in inst._hook_commands()))




class BudgetFromFleetEnv(unittest.TestCase):
    def test_fleet_env_budget_is_read_when_process_env_lacks_it(self):
        import os, tempfile
        import push_budget_gate as g
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("X=1\nFLEET_ACTIONS_BUDGET_USD=250\n")
        old_env, old_file = os.environ.pop("FLEET_ACTIONS_BUDGET_USD", None), os.environ.get("FLEET_ENV_FILE")
        os.environ["FLEET_ENV_FILE"] = f.name
        try:
            self.assertEqual(g._budget_usd(), 250.0)
        finally:
            if old_env is not None:
                os.environ["FLEET_ACTIONS_BUDGET_USD"] = old_env
            if old_file is None:
                os.environ.pop("FLEET_ENV_FILE", None)
            else:
                os.environ["FLEET_ENV_FILE"] = old_file
            os.unlink(f.name)


class GitLevelPrePush(unittest.TestCase):
    """A scripted push (no literal `git push` in any Claude tool call) is still judged (philanthropy#13024)."""

    def _repo(self, tmp):
        bare = Path(tmp) / "PhilanthropyOrg" / "philanthropy.git"
        bare.parent.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        wt = Path(tmp) / "wt"
        subprocess.run(["git", "clone", "-q", str(bare), str(wt)], check=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x", "HOME": tmp}
        (wt / "f").write_text("Moves: none\n")
        subprocess.run(["git", "add", "f"], cwd=wt, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "x\n\nMoves: none (test)"], cwd=wt, check=True, env=env)
        return wt, env

    def _push(self, wt, env, pct):
        log = Path(env["HOME"]) / "log"; log.mkdir(exist_ok=True)
        import time as _t
        (log / "push_gate_meter.json").write_text(json.dumps(
            {"ts": _t.time(), "day": _t.strftime("%Y-%m-%d", _t.gmtime()), "share": 10.0, "spent": pct / 10.0, "pct": pct}))
        hooks = str(Path(__file__).resolve().parent / "git-hooks")
        e = {**env, "FLEET_LOG_DIR": str(log), "FLEET_KIT_DIR": str(Path(__file__).resolve().parents[1]),
             "PATH": os.environ["PATH"]}
        return subprocess.run(["git", "-c", f"core.hooksPath={hooks}", "push", "-q", "origin", "HEAD:refs/heads/t"],
                              cwd=wt, env=e, capture_output=True, text=True)

    def test_pre_push_refuses_a_scripted_push_at_100_percent_and_allows_at_50(self):
        with tempfile.TemporaryDirectory() as tmp:
            wt, env = self._repo(tmp)
            r = self._push(wt, env, 100.0)
            self.assertNotEqual(r.returncode, 0, r.stderr)
            self.assertIn("push_budget_gate", r.stderr)
            r = self._push(wt, env, 50.0)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_pre_push_honours_the_logged_bypass_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            wt, env = self._repo(tmp)
            env = {**env, "PUSH_BUDGET_GATE_BYPASS": "1"}
            self.assertEqual(self._push(wt, env, 100.0).returncode, 0)

    def test_entrypoint_sets_the_global_hooks_path(self):
        src = (Path(__file__).resolve().parents[1] / "entrypoint.sh").read_text()
        self.assertIn("core.hooksPath /fleet-kit/scripts/git-hooks", src)


if __name__ == "__main__":
    unittest.main()
