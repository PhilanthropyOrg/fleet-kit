"""A minion resumes its checkpoint branch even when a dead pass's worktree still holds it.

2026-09-26 14:42 UTC: #7937's minion found checkpoint #8152's branch, but the killed 12:33 pass
had never removed its worktree (another container's /tmp), so `git worktree add -B` failed with
"already checked out". The minion started fresh from main and opened duplicate draft #8156.
run_member.sh now overrides git's guard when the holder is dead, and only then.

Run: python3 scripts/test_minion_resume_stale_holder.py
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

RUN_MEMBER = (Path(__file__).resolve().parent / "run_member.sh").read_text()
FUNC = re.search(r"  resume_holder_dead\(\) \{\n.*?\n  \}\n", RUN_MEMBER, re.S).group(0)
AWK = re.search(r"holder=\$\(git -C \"\$REPO\" worktree list --porcelain \| awk .*?head -1\)",
                RUN_MEMBER, re.S).group(0)


def sh(script: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], cwd=cwd, capture_output=True, text=True)


class ResumeStaleHolder(unittest.TestCase):
    def test_holder_liveness(self):
        d = tempfile.mkdtemp()
        live = Path(d, "fleet-run-minion-item7937-1"); live.mkdir()      # pid 1 is always alive
        dead = Path(d, "fleet-run-minion-item7937-999999"); dead.mkdir()
        odd = Path(d, "not-a-run"); odd.mkdir()
        for path, want in ((Path(d, "gone-123"), 0), (dead, 0), (live, 1), (odd, 1)):
            r = sh(f'{FUNC}\nresume_holder_dead "{path}"', d)
            self.assertEqual(r.returncode, want, (path, r.stderr))

    def test_dead_holder_is_overridden_and_resume_lands_on_the_checkpoint(self):
        d = tempfile.mkdtemp()
        br = "member/minion-item7937-123018-1790425867"
        stale = f"{d}/fleet-run-minion-item7937-123018"
        r = sh(f"""set -e
          git init -q -b main origin && cd origin && git -c user.email=a@b -c user.name=a commit -q --allow-empty -m base
          git checkout -q -b {br} && git -c user.email=a@b -c user.name=a commit -q --allow-empty -m checkpoint-work
          git checkout -q main && cd .. && git clone -q origin repo && cd repo
          git fetch -q origin +refs/heads/{br}:refs/remotes/origin/{br}
          git worktree add -q -B {br} {stale} origin/{br}
          rm -rf {stale}   # the killed pass's worktree vanished with its container
          """, d)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = sh(f"""REPO={d}/repo; RESUME_BRANCH={br}; WT_PATH={d}/fleet-run-minion-item7937-4707
          log() {{ echo "$*"; }}
          {FUNC}
          git -C "$REPO" worktree add -B "$RESUME_BRANCH" "$WT_PATH" "origin/$RESUME_BRANCH" 2>/dev/null; rc=$?
          [ $rc -ne 0 ] || {{ echo "git guard did not fire"; exit 9; }}
          {AWK}
          [ -n "$holder" ] && resume_holder_dead "$holder" && git -C "$REPO" worktree add -q -f -B "$RESUME_BRANCH" "$WT_PATH" "origin/$RESUME_BRANCH"
          git -C "$WT_PATH" log --format=%s -1
          """, d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("checkpoint-work", r.stdout, "did not resume on the checkpoint branch")


if __name__ == "__main__":
    unittest.main()
