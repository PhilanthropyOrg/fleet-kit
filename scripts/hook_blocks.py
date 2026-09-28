#!/usr/bin/env python3
"""hook_blocks.py -- a guard hook's block is the guard working, not a sandbox refusal.

2026-09-28: every pass runs `claude -p --dangerously-skip-permissions`, so the only things that
land in a pass's `permission_denials` are --disallowedTools entries AND the PreToolUse guards
(checkpoint_pr_hook, pretest_push_hook, worktree_guard_hook) exiting 2. denial_asks.py could not
tell them apart, so each guard block became "<member>'s sandbox refused gh ..." plus an ask whose
proposed fix was "allow it in tools.deny" -- a list that never held the command. asks #53, #75,
#78, #80, #82, #88 and #89 were all guard blocks (#89: checkpoint_pr_hook refusing `gh pr ready`
on checkpoint PR #8366; #88: pretest_push_hook asking for a test receipt), and jefe re-diagnosed
the same pair 4-5 times in a day. Each guard already tells the model its remedy in stderr.

So a guard records its block here by tool_use_id, and denial_asks.py skips those ids -- the same
move issue_create_hook.py already makes with dedupe_redirects.jsonl. Best-effort: never raises.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()
FILES = ("hook_blocks.jsonl", "dedupe_redirects.jsonl")


def record(payload: dict, hook: str, reason: str) -> None:
    """Log one guard block, keyed by the tool call it refused."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / FILES[0]).open("a") as fh:
            fh.write(json.dumps({"ts": time.time(), "hook": hook,
                                 "tool_use_id": payload.get("tool_use_id"),
                                 "reason": (reason or "")[:300]}) + "\n")
    except (OSError, AttributeError):
        pass


def blocked_ids(log_dir: Path | None = None) -> set[str]:
    """tool_use_ids a guard hook blocked (or issue_create_hook redirected), last 500 of each."""
    ids = set()
    for name in FILES:
        try:
            lines = ((log_dir or LOG_DIR) / name).read_text().splitlines()[-500:]
        except OSError:
            continue
        for line in lines:
            try:
                ids.add(json.loads(line).get("tool_use_id"))
            except (ValueError, AttributeError):
                pass
    return {i for i in ids if i}
