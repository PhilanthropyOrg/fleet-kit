"""A brand-new product repo can push its first PRs, and "no tests" never reads as "tests passed".

verified_test.sh with no arguments ran `pytest` over the whole tree. In a repo with nothing to
test yet pytest exits 5 (or is not installed), the receipt is red, and pretest_push_hook.py then
blocks the very PR that would add the first test. The new branch is taken ONLY when there is
nothing to run -- no arguments, no scripts/tests_for_diff.py, not one test-shaped file -- and
its receipt says so.

RED without the change: the empty-repo case exits non-zero with a `fail` receipt.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verified_test.sh"
HOOK = ROOT / "scripts" / "pretest_push_hook.py"
RAN = "PYTEST_RAN"


def repo(files: dict[str, str]) -> str:
    d = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", d], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", d, "config", k, v], check=True)
    for rel, text in files.items():
        p = pathlib.Path(d, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "-C", d, "add", "-A"], check=True)
    subprocess.run(["git", "-C", d, "commit", "-qm", "init"], check=True)
    return d


def run(d: str, *args: str):
    """Runs the real script with a stand-in `python3 -m pytest` that records it was called and
    exits 5, which is what pytest does when it collects nothing."""
    shim = pathlib.Path(tempfile.mkdtemp())
    (shim / "python3").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then echo "$@" > "$FLEET_TEST_MARK"; exit 5; fi\n'
        f'exec "{sys.executable}" "$@"\n')
    (shim / "python3").chmod(0o755)
    mark = shim / RAN
    env = dict(os.environ, WT_PATH=d, PATH=f"{shim}{os.pathsep}{os.environ['PATH']}", FLEET_TEST_MARK=str(mark),
               FLEET_LOG_DIR=tempfile.mkdtemp(), FLEET_EATMYDATA_BIN="no-such-binary")
    r = subprocess.run(["bash", str(SCRIPT), *args], cwd=d, capture_output=True, text=True, env=env)
    path = subprocess.run([sys.executable, str(HOOK), "--receipt-path", d], capture_output=True, text=True).stdout.strip()
    receipt = json.loads(pathlib.Path(path).read_text()) if path and pathlib.Path(path).exists() else None
    return r, receipt, mark.exists()


class NewRepo(unittest.TestCase):
    def test_a_repo_with_nothing_to_test_can_push_and_the_receipt_says_no_test_ran(self):
        d = repo({"docs/VISION.md": "# Shopfront\n", "fleet/okr.json": "{}\n"})
        r, receipt, pytest_ran = run(d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(pytest_ran, "there is nothing to run")
        self.assertEqual(receipt["status"], "pass", "the push hook must let the first PR out")
        self.assertTrue(receipt["args"].startswith("no-tests "), receipt)
        self.assertIn("nothing ran", receipt["args"])
        self.assertEqual(receipt["preflight"], "pass", "the lint/repo gates still ran")
        self.assertIn("NOTHING TO TEST", r.stdout)
        self.assertIn("nothing was test-verified", r.stdout)
        for lie in ("PASS (", "green", "full suite"):
            self.assertNotIn(lie, r.stdout, "a run with no tests must not read as a test pass")

    def test_a_stack_with_no_tests_yet_is_still_nothing_to_run(self):
        d = repo({"package.json": "{}\n", "src/index.js": "console.log(1)\n", "README.md": "x\n",
                  "src/latest.js": "x\n", "src/contest.py": "x = 1\n", "docs/attestation.md": "x\n"})
        r, receipt, pytest_ran = run(d)
        self.assertEqual((r.returncode, pytest_ran), (0, False), r.stdout + r.stderr)
        self.assertTrue(receipt["args"].startswith("no-tests "))


class EveryOtherRepoIsUntouched(unittest.TestCase):
    """The branch must be impossible to reach on a repo that has anything to run."""

    def assert_runs_pytest(self, files, *args):
        d = repo(files)
        r, receipt, pytest_ran = run(d, *args)
        self.assertTrue(pytest_ran, f"{sorted(files)}: pytest must still run\n{r.stdout}{r.stderr}")
        self.assertEqual(r.returncode, 5)
        self.assertEqual(receipt["status"], "fail")
        self.assertFalse(receipt["args"].startswith("no-tests"))
        self.assertNotIn("NOTHING TO TEST", r.stdout)

    def test_any_test_shaped_file_means_the_tests_run(self):
        for files in ({"tests/test_app.py": "def test_x(): pass\n"},
                      {"scripts/test_selftest.py": "x = 1\n"},          # fleet-kit's own layout
                      {"pkg/app_test.go": "package pkg\n"},
                      {"src/app.test.ts": "x\n"},
                      {"src/app.spec.js": "x\n"},
                      {"web/__tests__/a.js": "x\n"},
                      {"test/helper.rb": "x\n"},
                      {"spec/models/a_spec.rb": "x\n"}):
            with self.subTest(files=sorted(files)):
                self.assert_runs_pytest(files)

    def test_an_untracked_new_test_file_counts(self):
        d = repo({"README.md": "x\n"})
        pathlib.Path(d, "test_new.py").write_text("def test_x(): pass\n")
        r, receipt, pytest_ran = run(d)
        self.assertTrue(pytest_ran, r.stdout + r.stderr)
        self.assertEqual(receipt["status"], "fail")

    def test_explicit_arguments_always_run_what_was_named(self):
        self.assert_runs_pytest({"README.md": "x\n"}, "tests/test_x.py")

    def test_a_repo_with_its_own_diff_runner_never_reaches_the_branch(self):
        d = repo({"scripts/tests_for_diff.py": "import sys\nsys.exit(1)\n", "README.md": "x\n"})
        r, receipt, pytest_ran = run(d)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual((receipt["status"], receipt["args"]), ("fail", "tests_for_diff"))
        self.assertFalse(pytest_ran)

    def test_this_repo_and_the_live_product_shape_are_not_new_repos(self):
        import re
        text = SCRIPT.read_text()
        pattern = re.search(r"^TEST_FILE_RE='(.+)'$", text, re.M).group(1)
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True).stdout
        self.assertTrue(re.search(pattern, tracked, re.M | re.I), "fleet-kit itself must count as having tests")
        self.assertLess(text.index("-f scripts/tests_for_diff.py"), text.index("TREE_FILES="),
                        "the repo's own diff runner is checked first, so a repo that ships one never gets here")


if __name__ == "__main__":
    unittest.main()
