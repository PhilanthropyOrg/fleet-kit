"""fk#1166: a member never runs the whole suite by accident, and the wrapper runs the repo's
diff-scoped tests when the repo ships them.

7 days on philanthropy to 2026-09-19: 563 whole-tree pytest calls, 1,008 commands backgrounded
at the 120s harness cap, 103 passes that ended "I'll wait for the background run" with a
finished build never pushed. verified_test.sh with no args ran the 12-minute suite, and the
push hook needs its receipt, so every pass paid at least once.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import pretest_push_hook as hook  # noqa: E402


class WholeSuiteDetection(unittest.TestCase):
    BLOCK = [
        "pytest",
        "pytest tests/",
        "pytest tests -q",
        "python3 -m pytest tests/ -q --timeout=550 2>&1 | tail -80",
        "PATH=/repo/.venv/bin:$PATH /repo/.venv/bin/python3 -m pytest -q -n auto tests",
        "cd /tmp/wt && python -m pytest -q -x -p no:cacheprovider",
        "pytest . -q",
    ]
    ALLOW = [
        "pytest tests/test_auth.py -q",
        "python3 -m pytest tests/test_auth.py tests/test_hq.py -q",
        "pytest tests/ -k claim",
        "pytest tests/api/",
        "bash /fleet-kit/scripts/verified_test.sh",
        "python3 scripts/tests_for_diff.py --run",
        "git push -u origin HEAD",
        "pytest --collect-only tests/",
    ]

    def test_whole_tree_calls_are_detected(self):
        for c in self.BLOCK:
            self.assertTrue(hook._is_whole_suite_pytest(c), c)

    def test_targeted_calls_are_not(self):
        for c in self.ALLOW:
            self.assertFalse(hook._is_whole_suite_pytest(c), c)

    def test_decide_blocks_it_inside_a_worktree_and_names_the_wrapper(self):
        with tempfile.TemporaryDirectory() as wt:
            why = hook.decide({"tool_name": "Bash", "tool_input": {"command": "pytest tests/ -q"}},
                              {"WT_PATH": wt, "FLEET_KIT": "/fleet-kit"})
        self.assertIsNotNone(why)
        self.assertIn("verified_test.sh", why)

    def test_outside_a_worktree_nothing_is_blocked(self):
        self.assertIsNone(hook.decide({"tool_name": "Bash", "tool_input": {"command": "pytest tests/"}}, {}))


class WrapperRunsTheRepoRunner(unittest.TestCase):
    """A fixture repo that ships scripts/tests_for_diff.py: the wrapper must call it (not the
    suite) and still write a passing receipt for the push hook."""

    def _repo(self):
        d = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", d], check=True)
        subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", d, "config", "user.name", "t"], check=True)
        os.makedirs(os.path.join(d, "scripts"))
        with open(os.path.join(d, "scripts", "tests_for_diff.py"), "w") as fh:
            fh.write("import sys,os\nopen(os.path.join(os.path.dirname(__file__), '..', 'RAN_DIFF'), 'w').write(' '.join(sys.argv[1:]))\nsys.exit(int(os.environ.get('FAKE_RC', '0')))\n")
        open(os.path.join(d, "x.py"), "w").write("x = 1\n")
        subprocess.run(["git", "-C", d, "add", "-A"], check=True)
        subprocess.run(["git", "-C", d, "commit", "-qm", "init"], check=True)
        return d

    def _run(self, d, **env_extra):
        env = dict(os.environ, WT_PATH=d, **env_extra)
        return subprocess.run(["bash", str(ROOT / "scripts" / "verified_test.sh")], cwd=d,
                              capture_output=True, text=True, env=env)

    def test_no_args_runs_tests_for_diff_and_writes_a_pass_receipt(self):
        d = self._repo()
        r = self._run(d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(open(os.path.join(d, "RAN_DIFF")).read().strip(), "--run")
        receipt = json.loads(pathlib.Path(subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "pretest_push_hook.py"), "--receipt-path", d],
            capture_output=True, text=True).stdout.strip()).read_text())
        self.assertEqual((receipt["status"], receipt["args"]), ("pass", "tests_for_diff"))

    def test_a_failing_diff_run_is_a_fail_receipt(self):
        d = self._repo()
        r = self._run(d, FAKE_RC="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("suite FAILED", r.stderr)


if __name__ == "__main__":
    unittest.main()
