"""Tests for pretest_push_hook.py: blocks a whole-tree pytest, never a push (fk#1676).

The receipt helpers stay keyed on CONTENT, not HEAD: verified_test.sh writes with them and
minion_checkpoint.py reads with them.

Run: python3 scripts/test_pretest_push_hook.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pretest_push_hook as hook  # noqa: E402


def git(args: list[str], cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class Worktree:
    """A real git repo -- the hook shells out to git, so a fake would test nothing."""

    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp()
        git(["init", "-q", "-b", "main"], self.dir)
        git(["config", "user.email", "t@example.com"], self.dir)
        git(["config", "user.name", "t"], self.dir)
        self.write("app.py", "x = 1\n")
        git(["add", "-A"], self.dir)
        git(["commit", "-qm", "init"], self.dir)
        git(["branch", "-f", "origin/main", "HEAD"], self.dir)  # stand-in for the remote ref

    def write(self, name: str, body: str) -> None:
        (Path(self.dir) / name).write_text(body)

    def receipt(self, status: str = "pass", content: str | None = None) -> None:
        content = hook.content_hash(self.dir) if content is None else content
        hook.receipt_path(self.dir).write_text(
            json.dumps({"status": status, "content": content, "args": "full", "exit": 0, "ts": 0}))

    def decide(self, command: str = "git push -u origin HEAD") -> str | None:
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
        return hook.decide(payload, {"WT_PATH": self.dir})


class ReceiptTests(unittest.TestCase):
    """verified_test.sh writes the receipt and minion_checkpoint.py reads it; the hook no
    longer gates a push on it (fk#1676), but the content keying must still hold."""

    def setUp(self) -> None:
        self.wt = Worktree()

    def test_the_hash_survives_committing_the_very_content_it_certified(self):
        self.wt.write("app.py", "x = 2\n")
        before = hook.content_hash(self.wt.dir)
        git(["add", "app.py"], self.wt.dir)
        git(["commit", "-qm", "the tested change"], self.wt.dir)
        self.assertEqual(before, hook.content_hash(self.wt.dir))

    def test_editing_moves_the_hash(self):
        before = hook.content_hash(self.wt.dir)
        self.wt.write("app.py", "x = 999\n")
        self.assertNotEqual(before, hook.content_hash(self.wt.dir))

    def test_the_receipt_never_shows_up_as_an_untracked_file(self):
        # postflight_dirty_check.sh (gh#78/#183) calls a pass dirty on any leftover file.
        self.wt.receipt()
        status = subprocess.run(["git", "status", "--porcelain=v1"], cwd=self.wt.dir,
                                capture_output=True, text=True).stdout
        self.assertNotIn("receipt", status)


class PushTests(unittest.TestCase):
    """Reif 2026-10-15: no pre-push test runs; lint, push, CI tests (fk#1676)."""

    def setUp(self) -> None:
        self.wt = Worktree()

    def test_a_push_with_no_receipt_is_allowed(self):
        self.wt.write("app.py", "x = 3\n")
        for cmd in ("git push -u origin HEAD", "git add -A && git push origin HEAD",
                    "git checkout -b me/push-fix"):
            self.assertIsNone(self.wt.decide(cmd), cmd)

    def test_a_push_with_a_red_receipt_is_allowed(self):
        self.wt.receipt(status="fail")
        self.assertIsNone(self.wt.decide())


class WholeSuitePytestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wt = Worktree()

    def test_a_whole_tree_pytest_is_blocked(self):
        for cmd in ("pytest", "python3 -m pytest -q -n auto tests", "cd x && pytest tests/ 2>&1"):
            self.assertIn("BLOCKED", self.wt.decide(cmd) or "", cmd)

    def test_a_targeted_pytest_is_allowed(self):
        for cmd in ("pytest -q tests/test_x.py", "pytest -k claim tests"):
            self.assertIsNone(self.wt.decide(cmd), cmd)

    def test_pytest_only_quoted_in_an_argument_is_not_a_run(self):
        self.assertIsNone(self.wt.decide('gh issue comment 1 --body "ran pytest tests/"'))

    def test_a_pass_that_is_not_worktree_isolated_is_exempt(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "pytest"}}
        self.assertIsNone(hook.decide(payload, {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
