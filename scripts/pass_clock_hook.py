#!/usr/bin/env python3
"""pass_clock_hook.py -- in its last minutes, a minion pass writes its report instead of
starting one more wait.

2026-10-04: minions still timed out 6 times a day (rc=124: no report, no cost on record) after
fk#1538 taught pr_ci_wait.py to clamp its wait to the pass clock. The clamp only speaks inside
pr_ci_wait.py, and only on its LAST line. minion-item10862 (PR #10920) got its pre-timeout
checkpoint at 07:42:56, then ran `sleep 120; pr_ci_wait.py 10920 | head -14`, `sleep 240; ...
| head -4` (the warning cut off by head, the sleep outside the clamp), started one more fix, and
was killed at 07:52 with 30 commits and no report. minion.log holds 0 "PASS CLOCK" lines.

A PreToolUse hook (registered by worktree_guard_hook_install.py beside the other guards). Once
fewer than FLEET_PR_DONE_LEAD_S (720) seconds remain before $FLEET_PASS_DEADLINE (run_member.sh
exports it), a minion pass's Bash call that would START a wait or a test run -- `sleep` of 20s
or more, `pr_ci_wait.py` without --no-wait, `verified_test.sh`, `pytest`, `gh run watch`,
`gh pr checks --watch` -- is refused with the instruction to report. Everything else (edits,
`git push`, `gh pr comment`, a `--no-wait` read) still runs. The checkpoint watcher has already
pushed the branch, and a still-red PR is red_prs.py's to route, same as after pr_done_hook.py's
last refusal.

Minion only, for now: the-fixer has its own open timed_out prediction (fk#1538); widening this
to it waits until that one resolves. Outside a timed minion pass, or on any error, it allows.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

MINION_RUN = re.compile(r"^minion-")
_CMD_START = r"(?:^|(?<=[;&|(\n]))\s*(?:\w+=\S*\s+)*(?:timeout\s+\S+\s+)?"
SLEEP = re.compile(_CMD_START + r"sleep\s+(\d+)")
STARTS_WAIT = [
    re.compile(r"pr_ci_wait\.py(?![^;&|\n]*--no-wait)"),
    re.compile(r"verified_test\.sh"),
    re.compile(r"\bpytest\b"),
    re.compile(_CMD_START + r"gh\s+run\s+watch\b"),
    re.compile(_CMD_START + r"gh\s+pr\s+checks\b[^;&|\n]*--watch"),
]
MIN_SLEEP_S = 20


def seconds_left(env: dict, now: float | None = None) -> float | None:
    """Seconds until the pass must be writing its report; None outside a timed minion pass."""
    if not MINION_RUN.match(env.get("FLEET_RUN_ID") or ""):
        return None
    try:
        deadline = float(env.get("FLEET_PASS_DEADLINE") or 0)
        lead = float(env.get("FLEET_PR_DONE_LEAD_S") or 720)
    except ValueError:
        return None
    if deadline <= 0:
        return None
    return deadline - lead - (now if now is not None else time.time())


def _mask_quotes(command: str) -> str:
    """Text inside '...' or "..." becomes `_`, so a commit message that merely names `pytest`
    is not read as running it (same idea as checkpoint_pr_hook._mask_quotes)."""
    out, quote = [], None
    for ch in command:
        if quote:
            out.append(ch if ch in (quote, "\n") else "_")
            if ch == quote:
                quote = None
        else:
            if ch in "'\"":
                quote = ch
            out.append(ch)
    return "".join(out)


def starts_wait(command: str) -> bool:
    command = _mask_quotes(command or "")
    if any(int(m.group(1)) >= MIN_SLEEP_S for m in SLEEP.finditer(command)):
        return True
    return any(p.search(command) for p in STARTS_WAIT)


def decide(command: str, env: dict, now: float | None = None) -> str | None:
    """Block message, or None to allow."""
    left = seconds_left(env, now)
    if left is None or left > 0 or not starts_wait(command):
        return None
    try:
        deadline = float(env.get("FLEET_PASS_DEADLINE") or 0)
    except ValueError:
        deadline = 0
    to_kill = max(0, int(deadline - (now if now is not None else time.time())))
    return (f"PASS CLOCK: about {to_kill // 60} min until this pass is killed, too little for "
            "another wait or test run. Your branch is already checkpointed. Do not start a new "
            "fix: comment the exact blocker on your PR (`gh pr comment`), then write your "
            "Report / Outcome / Evidence block now. red_prs.py sends the next fixer.")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    try:
        msg = decide(cmd, dict(os.environ))
    except Exception:  # noqa: BLE001 -- a guard that crashes must never block a pass
        return 0
    if msg:
        try:
            import hook_blocks
            hook_blocks.record(payload, "pass_clock_hook", msg)
        except Exception:  # noqa: BLE001
            pass
        print(msg, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main())
