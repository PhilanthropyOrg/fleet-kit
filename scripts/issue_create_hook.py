#!/usr/bin/env python3
"""issue_create_hook.py -- a raw `gh issue create` is allowed, and deduped at birth here.

philanthropy#8215, 2026-09-26: dont-shoot-the-messenger's `gh issue create` was DENIED by its
sandbox (20:08 UTC), so a real org's removal request (Beyond the Books) sat unfiled; sentry's
was denied the same way at 19:02 UTC (the Cloudflare BIC finding). The deny (fk#1272) existed
only to stop twins: the board took ~130 issues/day against ~50 merges. A hard deny traded a
twin for a lost finding, which is the worse failure.

So the dedupe moves into this PreToolUse hook (registered by worktree_guard_hook_install.py
beside the other guards) and the deny comes off the members that must be able to file. For a
Bash command running `gh issue create`, it reads the title/labels/repo, lists the open board
and asks issue_cluster.find_existing for a twin or mega (the same rule `issue_cluster.py file`
applies). A twin blocks the create (exit 2) and names the twin, so the model comments there
instead. No twin, a Reif ask, or any error reading the board: allowed (exit 0) -- a twin beats
a dropped finding, same as issue_cluster.file_issue.

A block is logged to $FLEET_LOG_DIR/dedupe_redirects.jsonl by tool_use_id so denial_asks.py
never turns a dedupe redirect into an ask for Reif.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import issue_cluster as ic  # noqa: E402

CREATE = re.compile(r"\bgh\s+issue\s+create\b")
LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()
REDIRECTS = LOG_DIR / "dedupe_redirects.jsonl"


def parse(command: str) -> dict | None:
    """{"title", "labels", "repo"} of the first `gh issue create` in command, or None."""
    m = CREATE.search(command or "")
    if not m:
        return None
    rest = command[m.end():]
    try:
        toks = shlex.split(rest, comments=False, posix=True)
    except ValueError:
        toks = None
    out = {"title": None, "labels": [], "repo": None}
    if toks is None:  # unbalanced quoting (a heredoc body): fall back to the flags we can see
        t = re.search(r"(?:--title|-t)[ =](\"([^\"]*)\"|'([^']*)')", rest)
        if t:
            out["title"] = t.group(2) if t.group(2) is not None else t.group(3)
        r = re.search(r"(?:--repo|-R)[ =]([\w.-]+/[\w.-]+)", rest)
        out["repo"] = r.group(1) if r else None
        out["labels"] = [x for g in re.findall(r"(?:--label|-l)[ =]([^\s\"']+)", rest) for x in g.split(",")]
        return out
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in (";", "&&", "||", "|"):
            break
        key, _, val = t.partition("=")
        if key in ("--title", "-t", "--repo", "-R", "--label", "-l"):
            if not val:
                i += 1
                val = toks[i] if i < len(toks) else ""
            if key in ("--title", "-t"):
                out["title"] = val
            elif key in ("--repo", "-R"):
                out["repo"] = val
            else:
                out["labels"] += [x for x in val.split(",") if x]
        i += 1
    return out


def decide(command: str, list_open=ic.list_open) -> str | None:
    """Block message naming the twin, or None to allow."""
    p = parse(command)
    if not p or not p["title"]:
        return None
    if set(p["labels"]) & ic.PROTECTED_LABELS:
        return None
    try:
        hit = ic.find_existing(p["title"], p["labels"], list_open(p["repo"]))
    except Exception:  # noqa: BLE001 -- fail open: a twin beats a dropped finding
        return None
    if not hit:
        return None
    repo = f" --repo {p['repo']}" if p["repo"] else ""
    return (f"Not created: open issue #{hit['number']} (\"{hit.get('title', '')}\") is the same "
            f"problem. Add what you found there instead: `gh issue comment {hit['number']}{repo} "
            f"--body-file <file>`. This is dedupe, not a permission problem.")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not CREATE.search(cmd):
        return 0
    msg = decide(cmd)
    if not msg:
        return 0
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with REDIRECTS.open("a") as fh:
            fh.write(json.dumps({"ts": time.time(), "tool_use_id": payload.get("tool_use_id"),
                                 "command": cmd[:300]}) + "\n")
    except OSError:
        pass
    print(msg, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
