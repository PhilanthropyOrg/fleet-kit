#!/usr/bin/env python3
"""pretest_push_hook.py -- blocks a whole-tree pytest from a member pass (fk#1166).

It used to also block every `git push` that had no green test receipt behind it. Reif
reversed that on 2026-10-15 (9:25 AM Central, "make it law"): no pre-push test runs on lucky
or the Mac; lint locally, push, CI runs the tests, red CI is fixed on the branch. The push
gate made that law impossible to follow (fk#1676: two dumbledore fixes stranded unpushed on
10-15/10-16), so a push is no longer judged here. `verified_test.sh` still writes a receipt
when a member chooses to run it, and minion_checkpoint.py still reads one.

Reads one PreToolUse hook payload (JSON) on stdin. Exit 0 = allow, exit 2 = block.
Also usable as `pretest_push_hook.py --content-hash <dir>`, which is how verified_test.sh
computes the receipt -- one implementation, so the writer and the reader cannot drift.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from worktree_guard_hook import _strip_prose  # noqa: E402  gh#715's quote/heredoc stripper

RECEIPT_NAME = "fleet-test-receipt.json"


def _git(args: list[str], cwd: str) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    return p.stdout.strip() if p.returncode == 0 else ""


def receipt_path(wt: str) -> Path:
    """Inside the git dir, never in the worktree itself.

    A receipt sitting at the top of the checkout is an untracked file: it shows up in
    `git status`, which makes postflight_dirty_check.sh (gh#78/#183) call the pass dirty.
    `--absolute-git-dir` resolves to .git/worktrees/<name> for a linked worktree, so each one
    carries its own receipt and none of them can ever be committed.
    """
    return Path(_git(["rev-parse", "--absolute-git-dir"], wt) or Path(wt) / ".git") / RECEIPT_NAME


def content_hash(wt: str) -> str:
    """Hash of what is in the worktree NOW, stable across committing that same content."""
    stash = _git(["stash", "create"], wt)          # empty when the worktree is clean
    ref = f"{stash}^{{tree}}" if stash else "HEAD^{tree}"
    return _git(["rev-parse", ref], wt)


_VALUE_OPTS = {"-n", "-k", "-m", "-p", "-o", "-c", "-W", "-r", "--tb", "--timeout", "--maxfail",
               "--rootdir", "--deselect", "--ignore", "--durations"}
_WHOLE_TREE = {"tests", "tests/", ".", "./tests", "./tests/"}


def _is_whole_suite_pytest(command: str) -> bool:
    """True for a raw pytest call that would collect the entire tree: `pytest`, `pytest tests/`,
    `python3 -m pytest -q -n auto tests`, with or without flags, in any segment of a chain.
    A call naming a test file/dir under tests/ or a `-k` expression is targeted and allowed.

    fk#1166: 563 such calls in 7 days on philanthropy, 1,008 commands backgrounded at the 120s
    harness cap, 103 passes ended "waiting" with a finished build never pushed. None of them
    could even produce the receipt this hook wants -- only verified_test.sh writes it -- so a
    whole-tree pytest from a member is pure spend."""
    for seg in re.split(r"[;&|]+", command):
        toks = seg.strip().split()
        if not toks:
            continue
        idx = None
        for i, t in enumerate(toks):
            if t == "pytest" or t.endswith("/pytest"):
                idx = i
                break
            if t == "-m" and i + 1 < len(toks) and toks[i + 1] == "pytest":
                idx = i + 1
                break
        if idx is None:
            continue
        rest = toks[idx + 1:]
        if "-k" in rest or "--collect-only" in rest or "--co" in rest:
            continue
        positional, skip = [], False
        for t in rest:
            if skip:
                skip = False
                continue
            if t in _VALUE_OPTS:
                skip = True
                continue
            if t.startswith("-") or re.match(r"^\d*[<>]", t) or t.startswith("&>"):
                continue  # flags and shell redirections (`2>&1`, `>log`) are not test paths
            positional.append(t.strip("'\""))
        if all(p in _WHOLE_TREE for p in positional):
            return True
    return False


def decide(payload: dict, env: dict) -> str | None:
    """None = allow. A string = the reason to block, shown to the model."""
    wt = (env.get("WT_PATH") or "").strip()
    if not wt or not Path(wt).is_dir():
        return None  # not worktree-isolated: run_member.sh's own signal, same as gh#592's hook
    if payload.get("tool_name") != "Bash":
        return None
    # msg#56: a command that only QUOTES pytest (an --answer/--body argument, a heredoc) is not
    # a run; strip prose first, the same way worktree_guard_hook.py does (gh#715).
    command = _strip_prose((payload.get("tool_input") or {}).get("command") or "")
    if _is_whole_suite_pytest(command):
        return ("BLOCKED: a whole-tree pytest takes longer than this pass can wait (fk#1166). "
                "Lint, push, and let CI run the suite (Reif 10-15), or name the specific "
                "test file(s) you touched.")
    return None  # a push needs no receipt (Reif 10-15; fk#1676)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in ("--content-hash", "--receipt-path"):
        target = sys.argv[2] if len(sys.argv) > 2 else os.getcwd()
        print(content_hash(target) if sys.argv[1] == "--content-hash" else receipt_path(target))
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never wedge a pass on a payload we cannot read
    reason = decide(payload, os.environ)
    if reason is None:
        return 0
    import hook_blocks
    hook_blocks.record(payload, "pretest_push_hook", reason)
    print(f"pretest_push_hook: {reason}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
