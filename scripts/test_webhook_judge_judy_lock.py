"""gh#9810: a PR event must not launch judge-judy while its dispatch lock is held.

Run: python3 scripts/test_webhook_judge_judy_lock.py
"""
from __future__ import annotations

import fcntl
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import webhook_receiver as wr  # noqa: E402


class DispatchLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("TMPDIR")
        os.environ["TMPDIR"] = self.tmp.name
        (Path(self.tmp.name) / "fleet-kit-member-locks").mkdir()

    def tearDown(self):
        if self.old is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = self.old
        self.tmp.cleanup()

    def test_no_lock_file_is_not_held(self):
        self.assertFalse(wr._dispatch_lock_held("judge-judy"))

    def test_free_lock_is_not_held(self):
        (Path(self.tmp.name) / "fleet-kit-member-locks" / "judge-judy.lock").touch()
        self.assertFalse(wr._dispatch_lock_held("judge-judy"))

    def test_held_lock_is_held(self):
        path = Path(self.tmp.name) / "fleet-kit-member-locks" / "judge-judy.lock"
        with open(path, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(wr._dispatch_lock_held("judge-judy"))
        self.assertFalse(wr._dispatch_lock_held("judge-judy"))


if __name__ == "__main__":
    unittest.main()
