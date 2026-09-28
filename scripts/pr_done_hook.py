#!/usr/bin/env python3
"""pr_done_hook.py -- a builder's pass is not over while its PR is red.

A Claude Code Stop hook (registered by worktree_guard_hook_install.py, same settings files as
the PreToolUse guards). It fires when a pass tries to end its turn. For a minion pass, or a
the-fixer --item sub-pass, it looks up that pass's PR and asks pr_ci_wait.classify() whether
it is done:

  GREEN / MERGED / CLOSED / no PR  -> allow the stop (exit 0)
  RED / BLOCK / PENDING            -> refuse the stop (exit 2), telling the model exactly what
                                      is red and to run pr_ci_wait.py in the foreground

WHY A HOOK AND NOT ONE MORE PARAGRAPH (2026-09-25). minion.md already said to wait for CI and
to never end a turn "waiting". Passes ended anyway, right after `arm_pr_auto_merge`, and #7975,
#7982, #7986 sat red for hours with nobody on them. Prose is advice; a refused stop is a gate.

Bounded so it can never trap a pass: at most FLEET_PR_DONE_MAX_BLOCKS (4) refusals per run,
then it lets go (the PR is then red_prs.py's to route to gru). Any error reading the PR fails
OPEN. Outside a fleet worktree pass ($WT_PATH unset) it does nothing at all.

Which PR: `the-fixer-item<N>-...` run ids name it; a minion's is the open PR whose head branch
is the worktree's current branch (run_member.sh's WT_BRANCH).

A PASS IS NOT OVER WHILE ITS OWN BACKGROUND TASK IS RUNNING EITHER (2026-09-27). 56 of the 60
minion runs that landed `reported_nothing` that day ($154 of $299) ended their turn with
"I'll wait for the verified_test.sh completion notification": in `claude -p` the session ends
with the turn, the task dies with it, and no notification ever comes back. The other 4 wrote
their report, then a stale Monitor/sleep they had left running fired a notification AFTER it,
and the one-line reply to that became the pass's `result` -- the report was lost. minion.md
already said "TaskOutput(block: true) inside THIS turn"; prose again. So for any fleet pass
(FLEET_RUN_ID set) this hook reads the session transcript Claude Code hands it and refuses the
stop while a task it launched has no completion on record. Same bound, same fail-open.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pr_ci_wait  # noqa: E402

FIXER_RUN = re.compile(r"^the-fixer-item(\d+)-")
MINION_RUN = re.compile(r"^minion-")


def max_blocks(env: dict) -> int:
    try:
        return int(env.get("FLEET_PR_DONE_MAX_BLOCKS", 4))
    except ValueError:
        return 4


def _branch(wt: str) -> str:
    p = subprocess.run(["git", "-C", wt, "rev-parse", "--abbrev-ref", "HEAD"],
                       capture_output=True, text=True, timeout=30)
    return p.stdout.strip() if p.returncode == 0 else ""


def pr_for_pass(env: dict, gh=pr_ci_wait._gh, branch_of=_branch) -> int | None:
    run_id = env.get("FLEET_RUN_ID") or ""
    m = FIXER_RUN.match(run_id)
    if m:
        return int(m.group(1))
    if not MINION_RUN.match(run_id):
        return None
    branch = branch_of(env["WT_PATH"])
    if not branch or branch == "HEAD":
        return None
    rc, out = gh(["pr", "list", "--head", branch, "--state", "all", "--json", "number,state",
                  "--limit", "5"])
    if rc != 0:
        return None
    try:
        prs = json.loads(out)
    except json.JSONDecodeError:
        return None
    open_ = [p for p in prs if (p.get("state") or "").upper() == "OPEN"]
    pick = open_ or prs
    return int(pick[0]["number"]) if pick else None


def _counter(env: dict, kind: str = "") -> Path:
    rid = re.sub(r"[^A-Za-z0-9_.-]", "_", env.get("FLEET_RUN_ID") or "run")
    return Path(env.get("TMPDIR") or "/tmp") / f"fleet-pr-done-{rid}{kind}.count"


def _bump(counter: Path, env: dict) -> int | None:
    """The refusal number this stop would be, or None once the bound is spent."""
    try:
        used = int(counter.read_text().strip() or 0) if counter.exists() else 0
    except (OSError, ValueError):
        used = 0
    if used >= max_blocks(env):
        return None
    try:
        counter.write_text(str(used + 1))
    except OSError:
        pass
    return used + 1


# Claude Code 2.1.x's own tool_result wording (copied from live minion transcripts) for a task
# that is now running without the model, and for one that is over.
_LAUNCHED = re.compile(r"(?:running in background with ID: |moved to the background \(ID: "
                       r"|Monitor started \(task )([A-Za-z0-9]+)")
_FINISHED = [
    re.compile(r"<task[-_]id>([A-Za-z0-9]+)</task[-_]id>(?:(?!<task[-_]id>).)*?"
               r"<status>(?:completed|failed|killed|stopped)</status>", re.S),
    # A Monitor is over when it expires or the process it watched exits.
    re.compile(r"<task-id>([A-Za-z0-9]+)</task-id>(?:(?!<task-id>).)*?(?:Monitor expired|PROC_EXITED)", re.S),
    re.compile(r"Successfully stopped task: ([A-Za-z0-9]+)"),
    re.compile(r"No task found with ID: ([A-Za-z0-9]+)"),
    re.compile(r"Task ([A-Za-z0-9]+) is not running"),
]


def _texts(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for x in node:
            yield from _texts(x)
    elif isinstance(node, dict):
        for k in ("text", "content"):
            if k in node:
                yield from _texts(node[k])


def pending_tasks(transcript_path: str | None) -> list[str]:
    """Background task ids this session launched with no completion on record, in launch order.
    Launches count only from tool results (type=user); completions also from the notifications
    the harness queues mid-turn (type=attachment `prompt`, type=queue-operation `content`).
    Never the model's own prose. A false positive costs one TaskOutput call that answers
    `completed`; an unreadable transcript is no pending tasks (fail open)."""
    launched: dict[str, None] = {}
    done: set[str] = set()
    try:
        lines = Path(transcript_path).read_text(errors="ignore").splitlines() if transcript_path else []
    except OSError:
        return []
    for line in lines:
        try:
            evt = json.loads(line)
        except ValueError:
            continue
        if not isinstance(evt, dict) or evt.get("type") == "assistant":
            continue
        if evt.get("type") == "user":
            for text in _texts((evt.get("message") or {}).get("content")):
                launched.update(dict.fromkeys(_LAUNCHED.findall(text)))
        att = evt.get("attachment") if isinstance(evt.get("attachment"), dict) else {}
        for text in [*_texts((evt.get("message") or {}).get("content")),
                     *_texts(att.get("prompt")), *_texts(evt.get("content"))]:
            for rx in _FINISHED:
                done.update(rx.findall(text))
    return [t for t in launched if t not in done]


def decide(env: dict, gh=pr_ci_wait._gh, branch_of=_branch, payload: dict | None = None) -> str | None:
    """None = allow the stop. A string = why not, shown to the model."""
    pending = pending_tasks((payload or {}).get("transcript_path")) if env.get("FLEET_RUN_ID") else []
    if pending:
        n = _bump(_counter(env, "-bg"), env)
        if n is not None:
            ids = ", ".join(pending)
            return (f"Not done: background task(s) {ids} are still running (stop refused "
                    f"{n}/{max_blocks(env)}). This is a one-shot `claude -p` pass: when this turn "
                    f"ends the session ends, those tasks die, and no notification ever reaches "
                    f"you, and no tool waits on it (`TaskOutput` does not exist).\n1. `TaskStop` each "
                    f"one, then re-run what you still need in the FOREGROUND with "
                    f"`Bash(..., timeout: 600000)` (targeted tests if 10 minutes is not enough)."
                    f"\n2. Then finish the job (push, PR, "
                    f"`pr_ci_wait.py` in the foreground).\n3. End with your full Report / Outcome "
                    f"/ Evidence block, with nothing left running.")
    wt = (env.get("WT_PATH") or "").strip()
    if not wt or not Path(wt).is_dir():
        return None
    pr = pr_for_pass(env, gh, branch_of)
    if pr is None:
        return None
    counter = _counter(env)
    try:
        used = int(counter.read_text().strip() or 0) if counter.exists() else 0
    except (OSError, ValueError):
        used = 0
    if used >= max_blocks(env):
        return None
    raw = pr_ci_wait.fetch(pr, env.get("FLEET_REPO_SLUG") or None, gh)
    if raw is None:
        return None
    info = pr_ci_wait.classify(raw)
    if info["state"] in ("GREEN", "MERGED", "CLOSED"):
        return None
    try:
        counter.write_text(str(used + 1))
    except OSError:
        pass
    detail = pr_ci_wait.render(info, env.get("FLEET_REPO_SLUG") or None, gh,
                               with_logs=info["state"] == "RED")
    return (f"Not done: your PR #{pr} is {info['state']} "
            f"(stop refused {used + 1}/{max_blocks(env)}). A pass ends at a green PR, not at "
            f"'waiting for CI'.\n"
            f"1. `python3 /fleet-kit/scripts/pr_ci_wait.py {pr}` in the FOREGROUND (it blocks "
            f"up to 9 min, inside one Bash call; never background it).\n"
            f"2. RED/BLOCK: fix every failing check and review finding on the PR's own branch, "
            f"`bash /fleet-kit/scripts/verified_test.sh`, push, and go back to 1.\n"
            f"3. Then end with your full Report / Outcome / Evidence block again.\n\n{detail}")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    try:
        reason = decide(dict(os.environ), payload=payload if isinstance(payload, dict) else {})
    except Exception as exc:  # a broken hook must never wedge a pass
        print(f"pr_done_hook: skipped ({exc})", file=sys.stderr)
        return 0
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
