"""Regression test: transcripts and logs stop growing without bound (dino disk, 2026-09-28).

1. The kit's settings installer (entrypoint runs it into every account's CLAUDE_CONFIG_DIR each
   boot) sets cleanupPeriodDays, so Claude Code ages transcripts out at 7 days, not 30.
2. librarian.py runs its retention sweep BEFORE the scrub: the scrub is cut at 840s every tick on
   a real corpus, and retention placed after it never ran (0 compressed files on dino).
3. log_rotate.py copytruncates <member>.log / access.jsonl past the cap, keeps the inode (so
   O_APPEND writers and tailers keep working), and never touches runs.jsonl.
4. entrypoint.sh schedules log_rotate.py.

Run: python3 scripts/test_transcript_and_log_growth.py
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent


class CleanupPeriodDays(unittest.TestCase):
    def test_installer_sets_cleanup_period(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / ".claude-acct"
            cfg.mkdir()
            (cfg / "settings.json").write_text(json.dumps({"theme": "dark"}))
            subprocess.run([sys.executable, str(KIT / "scripts" / "worktree_guard_hook_install.py"), str(cfg)],
                           check=True, capture_output=True)
            s = json.loads((cfg / "settings.json").read_text())
            self.assertEqual(s.get("cleanupPeriodDays"), 7)
            self.assertEqual(s.get("theme"), "dark")
            # Already-installed hooks but no cleanupPeriodDays (every real account today) must
            # still get it on the next boot.
            del s["cleanupPeriodDays"]
            (cfg / "settings.json").write_text(json.dumps(s))
            subprocess.run([sys.executable, str(KIT / "scripts" / "worktree_guard_hook_install.py"), str(cfg)],
                           check=True, capture_output=True)
            self.assertEqual(json.loads((cfg / "settings.json").read_text()).get("cleanupPeriodDays"), 7)


class LibrarianRetentionNotStarved(unittest.TestCase):
    def test_retention_runs_even_when_scrub_is_killed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "projects"
            (root / "p").mkdir(parents=True)
            old = root / "p" / "old.jsonl"
            old.write_text('{"x": 1}\n')
            t = time.time() - 40 * 86400
            os.utime(old, (t, t))
            # Stand-in for a scrub that never finishes inside the wrapper's timeout.
            code = ("import sys, time; sys.path.insert(0, %r); import librarian as L\n"
                    "L.run_scrub = lambda *a, **k: time.sleep(60)\n"
                    "sys.argv = ['librarian.py', '--execute', '--root', %r, '--state-file', %r]\n"
                    "L.main()\n") % (str(KIT / "members" / "librarian"), str(root), str(Path(d) / "st.json"))
            try:
                subprocess.run([sys.executable, "-c", code], timeout=5, capture_output=True)
            except subprocess.TimeoutExpired:
                pass
            self.assertTrue((root / "p" / "old.jsonl.gz").exists(),
                            "40-day-old transcript not compressed: retention starved behind the scrub")


class LogRotate(unittest.TestCase):
    def test_copytruncate_keeps_inode_and_skips_state_files(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            big, acc, small, runs = d / "minion.log", d / "access.jsonl", d / "gru.log", d / "runs.jsonl"
            big.write_bytes(b"pass start\n" * 20000)
            acc.write_bytes(b'{"ts": 1}\n' * 20000)
            small.write_bytes(b"x\n")
            runs.write_bytes(b'{"r": 1}\n' * 20000)
            ino = big.stat().st_ino
            fh = open(big, "ab")  # a live O_APPEND writer, like `>> minion.log`
            subprocess.run([sys.executable, str(KIT / "scripts" / "log_rotate.py"), str(d), "--max-mb", "0.1"],
                           check=True, capture_output=True)
            fh.write(b"after\n"); fh.close()
            self.assertEqual(big.stat().st_ino, ino)
            self.assertEqual(big.read_bytes(), b"after\n")
            self.assertEqual(gzip.open(d / "minion.log.1.gz").read(), b"pass start\n" * 20000)
            self.assertEqual(acc.stat().st_size, 0)
            self.assertTrue((d / "access.jsonl.1.gz").exists())
            self.assertEqual(small.read_bytes(), b"x\n")
            self.assertEqual(runs.stat().st_size, len(b'{"r": 1}\n') * 20000)

    def test_entrypoint_schedules_it(self):
        self.assertIn("scripts/log_rotate.py $LOG_DIR", (KIT / "entrypoint.sh").read_text())


if __name__ == "__main__":
    unittest.main()
