"""A builder's pass lints before it pushes and does not end while its PR is red.

2026-09-25: #7986 went red on `I001 Import block is un-sorted` in a new test file plus the
repo-health ratchet; #7982 on `ruff format --check`; #7975 on the ratchet plus a review BLOCK.
The image had no ruff, verified_test.sh ran only tests, and the minion ended its pass at
"auto-merge armed". Pinned here:

  - preflight_gate.py autofixes lint on the changed files, fails on what the autofix cannot fix,
    and runs the repo's own gate scripts (a ratchet failing blocks);
  - verified_test.sh runs it first, so a red preflight is a red receipt and the push hook blocks;
  - pr_done_hook.py refuses a minion's stop while its PR is RED/BLOCK/PENDING, lets it go when
    GREEN, is inert outside a fleet worktree pass, and gives up after a bounded number of tries;
  - the charter and manifest say the same.

A real-ruff test runs when ruff is available (FLEET_RUFF or PATH, as on the fleet image) and is
skipped otherwise; every other test uses a stub ruff so it runs anywhere.

Run: python3 scripts/test_preflight_gate.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE.parent
sys.path.insert(0, str(HERE))

import pr_ci_wait  # noqa: E402
import pr_done_hook  # noqa: E402
import preflight_gate  # noqa: E402

UNSORTED = "import sys\nimport os\n\nprint(os, sys)\n"


def git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def repo(pyproject=True, ci_pin="0.16.7", gates=()):
    d = tempfile.mkdtemp()
    git(["init", "-q", "-b", "main"], d)
    git(["config", "user.email", "t@t"], d)
    git(["config", "user.name", "t"], d)
    if pyproject:
        Path(d, "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect = [\"I\", \"F\"]\n")
    if ci_pin:
        Path(d, ".github", "workflows").mkdir(parents=True)
        Path(d, ".github", "workflows", "ci.yml").write_text(f"steps:\n  - run: pip install --quiet ruff=={ci_pin}\n")
    Path(d, "scripts").mkdir()
    for name, rc in gates:
        Path(d, "scripts", name).write_text(f"import sys\nprint('{name} says rc={rc}')\nsys.exit({rc})\n")
    Path(d, "untouched.py").write_text(UNSORTED)  # already on main: not this branch's to fix
    git(["add", "-A"], d)
    git(["commit", "-qm", "init"], d)
    git(["branch", "-f", "origin/main", "HEAD"], d)
    git(["checkout", "-qb", "member/minion-item1-1-1"], d)
    return d


def stub_ruff(d: str, check_rc_after_fix: int = 0) -> str:
    """Records every call; `check --fix` sorts imports the crude way; plain `check` returns rc."""
    p = Path(d, "ruff-stub")
    p.write_text(f"""#!/usr/bin/env python3
import sys, pathlib
open({str(Path(d, 'ruff-calls'))!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')
args = sys.argv[1:]
files = [a for a in args if a.endswith('.py')]
if args[:2] == ['check', '--fix']:
    for f in files:
        t = pathlib.Path(f).read_text().replace('import sys\\nimport os\\n', 'import os\\nimport sys\\n')
        pathlib.Path(f).write_text(t)
    sys.exit(0)
if args[0] == 'check':
    sys.exit({check_rc_after_fix})
sys.exit(0)
""")
    p.chmod(0o755)
    return str(p)


class Preflight(unittest.TestCase):
    def test_autofixes_the_changed_file_and_leaves_main_alone(self):
        d = repo(gates=[("repo_health.py", 0)])
        Path(d, "app.py").write_text(UNSORTED)  # the #7986 shape: new file, I001
        env = dict(os.environ, FLEET_RUFF=stub_ruff(d), FLEET_PREFLIGHT_CHECKS="python3 scripts/repo_health.py --check")
        out = []
        self.assertEqual(preflight_gate.run(d, env, out.append), 0, "\n".join(out))
        self.assertEqual(Path(d, "app.py").read_text(), "import os\nimport sys\n\nprint(os, sys)\n")
        self.assertEqual(Path(d, "untouched.py").read_text(), UNSORTED)
        calls = Path(d, "ruff-calls").read_text().splitlines()
        self.assertEqual([c.split()[0:2] for c in calls],
                         [["check", "--fix"], ["format", "--quiet"], ["check", "app.py"], ["format", "--check"]])

    def test_what_autofix_cannot_fix_fails(self):
        d = repo()
        Path(d, "app.py").write_text("x = undefined_name\n")
        out = []
        env = dict(os.environ, FLEET_RUFF=stub_ruff(d, check_rc_after_fix=1), FLEET_PREFLIGHT_CHECKS="")
        self.assertEqual(preflight_gate.run(d, env, out.append), 1)
        self.assertIn("ruff check", out[-1])

    def test_a_failing_repo_gate_fails_and_a_missing_one_is_skipped(self):
        # #7975/#7986: `RATCHET ... a giant may shrink or split, never grow`.
        d = repo(pyproject=False, gates=[("repo_health.py", 1)])
        Path(d, "scripts", "repo_health.py").write_text("print('RATCHET x.py: 1689 > 1669'); raise SystemExit(1)\n")
        out = []
        self.assertEqual(preflight_gate.run(d, dict(os.environ), out.append), 1)
        text = "\n".join(out)
        self.assertIn("RATCHET", text)
        self.assertNotIn("check_docs.py", text, "a gate this repo does not ship must be skipped")

    def test_pin_comes_from_the_repos_ci(self):
        self.assertEqual(preflight_gate.ruff_pin(repo(ci_pin="0.16.7")), "0.16.7")
        self.assertIsNone(preflight_gate.ruff_pin(repo(ci_pin=None)))

    def test_prefers_the_image_copy_of_the_pinned_version(self):
        d = tempfile.mkdtemp()
        b = Path(d, "0.16.7", "bin")
        b.mkdir(parents=True)
        (b / "ruff").write_text("#!/bin/sh\n")
        path, where = preflight_gate.find_ruff("0.16.7", {"FLEET_RUFF_IMAGE_DIR": d}, install=False)
        self.assertEqual(path, str(b / "ruff"))

    def test_no_ruff_anywhere_is_a_loud_skip_not_a_silent_pass(self):
        d = repo()
        Path(d, "app.py").write_text(UNSORTED)
        orig = preflight_gate.find_ruff
        preflight_gate.find_ruff = lambda pin, env, install=True: (None, "no ruff available")
        try:
            out = []
            self.assertEqual(preflight_gate.run(d, dict(os.environ, FLEET_PREFLIGHT_CHECKS=""), out.append), 0)
        finally:
            preflight_gate.find_ruff = orig
        self.assertIn("LINT SKIPPED", out[0])

    @unittest.skipUnless(os.environ.get("FLEET_RUFF") or shutil.which("ruff"), "no ruff on this box")
    def test_real_ruff_fixes_i001(self):
        d = repo()
        Path(d, "app.py").write_text(UNSORTED)
        env = dict(os.environ, FLEET_RUFF=os.environ.get("FLEET_RUFF") or shutil.which("ruff"),
                   FLEET_PREFLIGHT_CHECKS="")
        out = []
        self.assertEqual(preflight_gate.run(d, env, out.append), 0, "\n".join(out))
        self.assertTrue(Path(d, "app.py").read_text().startswith("import os\nimport sys\n"))


class VerifiedTestRunsPreflightFirst(unittest.TestCase):
    def _run(self, d, **extra):
        env = dict(os.environ, WT_PATH=d, **extra)
        return subprocess.run(["bash", str(HERE / "verified_test.sh")], cwd=d, capture_output=True,
                              text=True, env=env)

    def _receipt(self, d):
        p = subprocess.run([sys.executable, str(HERE / "pretest_push_hook.py"), "--receipt-path", d],
                           capture_output=True, text=True).stdout.strip()
        return json.loads(Path(p).read_text())

    def test_red_preflight_is_a_red_receipt_and_tests_never_run(self):
        d = repo(pyproject=False, gates=[("repo_health.py", 1)])
        Path(d, "scripts", "tests_for_diff.py").write_text("open('RAN','w').write('x')\n")
        r = self._run(d)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self._receipt(d)["status"], "fail")
        self.assertFalse(Path(d, "RAN").exists())

    def test_clean_preflight_then_tests_pass_receipt(self):
        d = repo(pyproject=False, gates=[("repo_health.py", 0)])
        Path(d, "scripts", "tests_for_diff.py").write_text("import sys; sys.exit(0)\n")
        r = self._run(d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rc = self._receipt(d)
        self.assertEqual((rc["status"], rc["preflight"]), ("pass", "pass"))


def _pr(state_checks, comments=()):
    return {"number": 7986, "state": "OPEN", "headRefName": "member/minion-item1-1-1",
            "headRefOid": "f" * 40, "autoMergeRequest": {}, "statusCheckRollup": state_checks,
            "commits": [{"oid": "a" * 40, "messageHeadline": "work", "committedDate": "2026-09-25T15:15:00Z"}],
            "comments": list(comments), "labels": []}


def _check(name, concl="SUCCESS", status="COMPLETED"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": concl,
            "completedAt": "2026-09-25T15:20:00Z", "detailsUrl": ""}


class StopHook(unittest.TestCase):
    def setUp(self):
        self.wt = tempfile.mkdtemp()
        self.tmp = tempfile.mkdtemp()
        self.env = {"WT_PATH": self.wt, "FLEET_RUN_ID": f"minion-item1_2-{time.time_ns()}", "TMPDIR": self.tmp}

    def _gh(self, pr):
        def gh(args, timeout=60):
            if args[:2] == ["pr", "list"]:
                return 0, json.dumps([{"number": 7986, "state": "OPEN"}])
            if args[:2] == ["pr", "view"]:
                return 0, json.dumps(pr)
            return 1, "no logs in tests"
        return gh

    def _decide(self, pr, env=None):
        return pr_done_hook.decide(env or self.env, gh=self._gh(pr), branch_of=lambda wt: "member/minion-item1-1-1")

    def test_red_pr_refuses_the_stop_and_says_what_to_run(self):
        why = self._decide(_pr([_check("test"), _check("lint", "FAILURE")]))
        self.assertIsNotNone(why)
        self.assertIn("pr_ci_wait.py 7986", why)
        self.assertIn("FAILED: lint", why)

    def test_pending_refuses_green_allows(self):
        self.assertIsNotNone(self._decide(_pr([_check("test", None, "IN_PROGRESS")])))
        self.assertIsNone(self._decide(_pr([_check("test"), _check("lint")])))

    def test_review_block_refuses(self):
        block = {"body": "**fleet-code-review: BLOCK** (head ffff)\n\n- high -- x", "createdAt": "2026-09-25T15:30:00Z"}
        why = self._decide(_pr([_check("test")], comments=[block]))
        self.assertIn("REVIEW BLOCK", why)

    def test_bounded(self):
        red = _pr([_check("lint", "FAILURE")])
        results = [self._decide(red) for _ in range(pr_done_hook.max_blocks(self.env) + 1)]
        self.assertIsNotNone(results[0])
        self.assertIsNone(results[-1], "must let go after FLEET_PR_DONE_MAX_BLOCKS refusals")

    def test_inert_outside_a_builder_pass(self):
        red = _pr([_check("lint", "FAILURE")])
        self.assertIsNone(self._decide(red, {"TMPDIR": self.tmp}))  # no WT_PATH: a human session
        self.assertIsNone(self._decide(red, dict(self.env, FLEET_RUN_ID="gru-1-2")))

    def test_fixer_item_pass_is_keyed_on_its_pr(self):
        self.assertEqual(pr_done_hook.pr_for_pass({"FLEET_RUN_ID": "the-fixer-item7982-55-1790"}), 7982)

    def test_installer_registers_the_stop_hook_once(self):
        import worktree_guard_hook_install as inst
        p = Path(tempfile.mkdtemp(), "settings.json")
        self.assertTrue(inst.merge_one(p, inst._hook_commands(), inst._stop_hook_commands()))
        self.assertFalse(inst.merge_one(p, inst._hook_commands(), inst._stop_hook_commands()))
        stops = json.loads(p.read_text())["hooks"]["Stop"]
        self.assertEqual(len(stops), 1)
        self.assertTrue(stops[0]["hooks"][0]["command"].endswith("pr_done_hook.py"))


class TestInterpreter(unittest.TestCase):
    """2026-09-25: #7975's and #7982's fixers each spent most of a 30-minute pass building a
    venv by hand and timed out with the fix unpushed. test_python.sh builds ONE cached env per
    dependency set and every later pass reuses it."""

    SH = str(HERE / "test_python.sh")

    def _fake_uv(self, d):
        uv = Path(d, "uv")
        uv.write_text(f"""#!/bin/bash
echo "$@" >> {d}/uv-calls
if [ "$1" = venv ]; then v="${{@: -1}}"; mkdir -p "$v/bin"; printf '#!/bin/sh\nexit 0\n' > "$v/bin/python"; chmod +x "$v/bin/python"; fi
exit 0
""")
        uv.chmod(0o755)
        return d

    def _run(self, wt, **env):
        e = {k: v for k, v in os.environ.items() if k != "FLEET_TEST_PYTHON"}
        e.update(env)
        return subprocess.run(["bash", self.SH, wt], capture_output=True, text=True, env=e)

    def test_repo_without_project_deps_gets_python3(self):
        d = repo(pyproject=False)
        self.assertEqual(self._run(d).stdout.strip(), shutil.which("python3"))

    def test_builds_once_then_reuses(self):
        d = repo(pyproject=False)
        Path(d, "pyproject.toml").write_text("[project]\nname='x'\ndependencies=['jinja2']\n"
                                             "[project.optional-dependencies]\ndev = ['pytest']\n")
        root, bindir = tempfile.mkdtemp(), self._fake_uv(tempfile.mkdtemp())
        env = {"FLEET_TEST_VENV_ROOT": root, "PATH": f"{bindir}:{os.environ['PATH']}"}
        first = self._run(d, **env)
        self.assertTrue(first.stdout.strip().startswith(root), first.stderr)
        self.assertIn("--extra dev", Path(bindir, "uv-calls").read_text())
        calls = Path(bindir, "uv-calls").read_text()
        self.assertEqual(self._run(d, **env).stdout.strip(), first.stdout.strip())
        self.assertEqual(Path(bindir, "uv-calls").read_text(), calls, "second call must not rebuild")

    def test_override_wins(self):
        self.assertEqual(self._run(repo(), FLEET_TEST_PYTHON="/x/py").stdout.strip(), "/x/py")

    def test_verified_test_and_fixer_timeout_use_it(self):
        self.assertIn("test_python.sh", (HERE / "verified_test.sh").read_text())
        rm = (HERE / "run_member.sh").read_text()
        self.assertIn('FLEET_FIXER_ITEM_TIMEOUT_S:-3600', rm)
        self.assertLess(rm.index("TIMEOUT_S=$(jget"), rm.index("FLEET_FIXER_ITEM_TIMEOUT_S"))
        self.assertIn("pip3 install --no-cache-dir uv", (KIT / "Dockerfile").read_text())


class CharterSaysTheSame(unittest.TestCase):
    def test_minion_charter_and_manifest(self):
        md = (KIT / "members" / "minion" / "minion.md").read_text()
        self.assertNotIn("arming auto-merge IS finishing the job", md)
        self.assertIn("pr_ci_wait.py", md)
        self.assertIn("preflight_gate.py", md)
        spec = json.loads((KIT / "members" / "minion" / "minion.fleet.json").read_text())
        self.assertTrue(any("pr_ci_wait.py" in c for c in spec["mandate"]["checklist"]))

    def test_image_ships_ruff(self):
        self.assertIn("/opt/fleet-ruff/", (KIT / "Dockerfile").read_text())


if __name__ == "__main__":
    unittest.main()
