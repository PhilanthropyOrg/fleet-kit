#!/usr/bin/env python3
"""checkpoint_pr_hook.py -- a minion checkpoint PR leaves draft only through
`minion_checkpoint.py ready`, never through a raw `gh pr ready` / `gh pr merge`.

2026-09-26 08:32 UTC: #8110, the green checkpoint of a pass on #7942, was a draft. The same pass
got "Pull request is a draft" from arm_pr_auto_merge, ran `gh pr ready 8110`, re-armed, and a
partial slice merged 12s later. The pass's own report said "#7942 is only partly done".

A PreToolUse hook (registered by worktree_guard_hook_install.py beside the other guards). For
a Bash command that runs `gh pr ready` (not --undo) or `gh pr merge` (not --disable-auto), or
the GraphQL markPullRequestReadyForReview mutation, it reads the target PR (the number in the
command, else the cwd's branch). If that PR is a checkpoint (minion_checkpoint.is_checkpoint_pr),
the command is blocked (exit 2) and the model is sent to `minion_checkpoint.py ready`, which
checks done-criteria. Everything else, and any error reading the PR, is allowed (exit 0):
merge_arm.sh refuses to arm a checkpoint PR too, so failing open here is not the only wall.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import minion_checkpoint as mc  # noqa: E402

GH_PR = re.compile(r"\bgh\s+pr\s+(ready|merge)\b([^;&|\n]*)")
GQL_READY = re.compile(r"markPullRequestReadyForReview")


def targets(command: str) -> list[str | None]:
    """PR selectors the command would un-draft or merge: a number/branch arg, or None for "the
    current branch". Empty list = nothing to check."""
    out: list[str | None] = []
    for verb, rest in GH_PR.findall(command or ""):
        if verb == "ready" and "--undo" in rest:
            continue
        if verb == "merge" and "--disable-auto" in rest:
            continue
        toks, skip = rest.split(), False
        arg = None
        for t in toks:
            if skip:
                skip = False
                continue
            if t in ("-R", "--repo", "-b", "--body", "-t", "--subject", "--match-head-commit",
                     "-A", "--author-email", "-F", "--body-file"):
                skip = True
                continue
            if not t.startswith("-"):
                arg = t.strip("'\"")
                break
        out.append(arg)
    return out


def decide(command: str, cwd: str, view=None) -> str | None:
    """Block message, or None to allow."""
    if GQL_READY.search(command or ""):
        return ("Blocked: un-drafting a PR through the GraphQL API skips the checkpoint gate. "
                "Run `python3 /fleet-kit/scripts/minion_checkpoint.py ready` instead.")
    view = view or _view
    for t in targets(command):
        pr = view(t, cwd)
        if pr and mc.is_checkpoint_pr(pr.get("title"), pr.get("body")):
            return (f"Blocked: PR #{pr.get('number')} is a minion checkpoint (draft WIP). It must "
                    "never merge as a checkpoint. When EVERY item on the branch meets its "
                    "done-criteria: `gh pr edit` the title (no WIP) and body (`Closes #N` per "
                    "item, no `Part of` / `Remaining:`), push, run verified_test.sh, then run "
                    "`python3 /fleet-kit/scripts/minion_checkpoint.py ready`. It checks all of "
                    "that and marks the PR ready. If an item is not done, leave the PR a draft "
                    "and say what remains in your report; the next pass resumes it.")
    return None


def _view(target: str | None, cwd: str) -> dict | None:
    args = ["pr", "view"] + ([target] if target else []) + ["--json", "number,title,body"]
    rc, out = mc._gh(args, cwd=cwd or ".", timeout=30)
    try:
        return json.loads(out) if rc == 0 else None
    except ValueError:
        return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    msg = decide(cmd, payload.get("cwd") or ".")
    if msg:
        print(msg, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
