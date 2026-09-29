"""pull_bad_bag.py: a red deploy gate pulls the merge that broke it off main (baggage claim).

The end-to-end case replays philanthropy #8805 in a throwaway git repo: two merges, each
breaking one repo-wide test, land among good merges. The script must name exactly those two,
prove the tests pass with both reverted, and leave the good merges alone.

RED without the fix: the module does not exist.
Run: python3 scripts/test_pull_bad_bag.py
"""
import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("pull_bad_bag", HERE / "pull_bad_bag.py")
pbb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pbb)

# Shape of `gh run view --log-failed` on the real 15:16Z run (36588889228).
REAL_LOG = (
    "deploy\tTest gate (catch a regression even if branch protection didn't block the merge)\t"
    "2026-09-29T15:30:19.95Z FAILED tests/test_docs_index_is_complete.py::test_every_doc_is_listed_in_the_index - AssertionError: these docs\n"
    "deploy\tTest gate (catch a regression even if branch protection didn't block the merge)\t"
    "2026-09-29T15:30:19.96Z FAILED tests/test_no_side_stripes.py::test_no_side_stripes - AssertionError: side stripes\n"
    "deploy\tTest gate\t2026-09-29T15:30:19.97Z FAILED tests/test_no_side_stripes.py::test_no_side_stripes - again\n"
    "deploy\tTest gate\t2026-09-29T15:30:20Z ERROR tests/test_broken_import.py - ImportError\n"
    "deploy\tTest gate\t2026-09-29T15:30:20Z = 2 failed, 13000 passed =\n"
)


class Pure(unittest.TestCase):
    def test_failed_tests_from_the_real_log(self):
        self.assertEqual(pbb.failed_tests(REAL_LOG), [
            "tests/test_docs_index_is_complete.py::test_every_doc_is_listed_in_the_index",
            "tests/test_no_side_stripes.py::test_no_side_stripes",
            "tests/test_broken_import.py",
        ])

    def test_infra_red_is_not_a_bag(self):
        self.assertIsNone(pbb.failed_tests(REAL_LOG + "TEST_GATE_TIMEOUT: pytest hit 3600s\n"))
        self.assertIsNone(pbb.failed_tests("::error::WORKSPACE_MISMATCH: tracked files differ"))
        self.assertEqual(pbb.failed_tests("Smoke import check failed"), [])

    def test_first_bad(self):
        commits = list("abcdefgh")
        for k in range(len(commits)):
            runs = []
            got = pbb.first_bad(commits, lambda c: runs.append(c) or commits.index(c) >= k)
            self.assertEqual(got, k)
            self.assertLessEqual(len(runs), 3, "bisect, not a walk")

    def test_pr_of_and_bodies(self):
        self.assertEqual(pbb.pr_of("Design system: add Chip and Card components (gh#7664) (#8797)"), "8797")
        self.assertIsNone(pbb.pr_of("a direct push"))
        t = ["tests/test_a.py::t", "tests/test_b.py"]
        self.assertIn("Must pass: tests/test_a.py::t tests/test_b.py", pbb.pull_body("1", "s", t, "u"))
        self.assertIn("Must pass: tests/test_a.py::t tests/test_b.py", pbb.reland_body("1", "2", "s", t))

    def test_pytest_that_never_ran_is_not_green(self):
        t = ["tests/test_a.py::t"]
        with self.assertRaises(RuntimeError):
            pbb.read_result(t, 1, "/usr/bin/python3: No module named pytest")
        self.assertEqual(pbb.read_result(t, 0, "1 passed in 0.1s"), set())
        self.assertEqual(pbb.read_result(t, 1, "FAILED tests/test_a.py::t - boom\n1 failed"), set(t))
        self.assertEqual(pbb.read_result(t, 4, "ERROR: not found\nno tests ran in 0.01s"), set(t))

    def test_owns(self):
        with tempfile.TemporaryDirectory() as d:
            pbb.STATE = Path(d) / "s.json"
            self.assertEqual(pbb.owns("abc"), 2)
            pbb.set_state("abcdef123456", "pulling")
            self.assertEqual(pbb.owns("abcdef"), 0, "a short sha from check.sh matches")
            self.assertEqual(pbb.owns("abcdef", now=time.time() + pbb.OWN_FOR_S + 1), 1, "stale claim")
            pbb.set_state("abcdef123456", "handoff", why="x")
            self.assertEqual(pbb.owns("abcdef123456"), 1)
            pbb.set_state("abcdef123456", "pulled")
            self.assertEqual(pbb.owns("abcdef123456"), 0)


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class EndToEnd(unittest.TestCase):
    """Two bad bags among good ones; a real git history and real pytest runs."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        r = self.d
        git(r, "init", "-q", "-b", "main")
        git(r, "config", "user.email", "t@t"); git(r, "config", "user.name", "t")
        (Path(r) / "tests").mkdir()
        (Path(r) / "docs").mkdir()
        (Path(r) / "docs/README.md").write_text("a.md\n")
        (Path(r) / "docs/a.md").write_text("a\n")
        (Path(r) / "style.css").write_text(".x{border:1px solid}\n")
        (Path(r) / "tests/test_docs_index.py").write_text(
            "from pathlib import Path\nR=Path(__file__).parent.parent\n"
            "def test_index():\n    idx=(R/'docs/README.md').read_text()\n"
            "    assert all(p.name in idx for p in (R/'docs').glob('*.md') if p.name!='README.md')\n")
        (Path(r) / "tests/test_stripes.py").write_text(
            "from pathlib import Path\nR=Path(__file__).parent.parent\n"
            "def test_no_stripes():\n    assert 'border-left' not in (R/'style.css').read_text()\n")
        self.commit("base (#1)")
        self.good = git(r, "rev-parse", "HEAD")
        self.write("feature.py", "x = 1\n"); self.commit("good one (#2)")
        self.write("style.css", ".x{border-left:3px solid}\n"); self.commit("org note stripe (#8772)")
        self.write("feature.py", "x = 2\n"); self.commit("good two (#3)")
        self.write("docs/card.md", "card\n"); self.commit("add Card docs (#8797)")
        self.write("other.py", "y = 1\n"); self.commit("good three (#4)")
        self.tip = git(r, "rev-parse", "HEAD")

    def write(self, rel, text):
        (Path(self.d) / rel).write_text(text)

    def commit(self, msg):
        git(self.d, "add", "-A"); git(self.d, "commit", "-q", "-m", msg)

    def test_pulls_exactly_the_two_bad_bags(self):
        runs = []

        def fake_run_tests(wt, tests):
            # CI's python has no pytest: call each test function directly.
            runs.append(git(wt, "rev-parse", "HEAD"))
            bad = set()
            for t in tests:
                f, fn = t.split("::")
                code = f"import runpy; runpy.run_path({f!r})[{fn!r}]()"
                if subprocess.run([sys.executable, "-c", code], cwd=wt, capture_output=True).returncode:
                    bad.add(t)
            return bad

        pbb.run_tests = fake_run_tests
        wt = tempfile.mkdtemp()
        git(self.d, "worktree", "add", "-q", "--detach", wt, self.tip)
        handed = []
        tests = ["tests/test_docs_index.py::test_index", "tests/test_stripes.py::test_no_stripes"]
        a = argparse.Namespace(dry_run=True)
        logged = []
        pbb.log = logged.append
        rc = pbb._pull(a, self.d, wt, self.tip, self.tip, self.good, tests, "u",
                       lambda why: handed.append(why) or 1)
        self.assertEqual((rc, handed), (0, []))
        bags = [l for l in logged if "bad bag" in l]
        self.assertEqual(len(bags), 2, logged)
        self.assertIn("(#8772)", bags[0]); self.assertIn("test_no_stripes", bags[0])
        self.assertIn("(#8797)", bags[1]); self.assertIn("test_index", bags[1])
        self.assertTrue(any("PROOF" in l for l in logged))
        self.assertLess(len(runs), 12, "bisect, not a walk of every merge per test")

    def test_already_fixed_forward_pulls_nothing(self):
        self.write("style.css", ".x{border:1px solid}\n"); self.write("docs/README.md", "a.md card.md\n")
        self.commit("fix forward (#8805)")
        pbb.run_tests = lambda wt, tests: set()
        logged = []
        pbb.log = logged.append
        rc = pbb._pull(argparse.Namespace(dry_run=True), self.d, self.d, "x", "HEAD", self.good,
                       ["tests/test_stripes.py::test_no_stripes"], "u", lambda why: 1)
        self.assertEqual(rc, 0)
        self.assertTrue(any("fixed forward" in l for l in logged))


class Wiring(unittest.TestCase):
    def test_the_fixer_leaves_a_red_deploy_to_the_puller_first(self):
        check = (HERE.parent / "members/the-fixer/check.sh").read_text()
        self.assertIn("pull_bad_bag.py", check)
        self.assertIn("--owns", check)


if __name__ == "__main__":
    unittest.main()
