#!/usr/bin/env python3
"""worktree_guard_hook_install.py -- gh#592.

Idempotently registers worktree_guard_hook.py as a PreToolUse hook (matcher "Edit|Write|Bash")
inside one or more Claude Code user settings.json files -- merges into whatever is already
there, never overwrites an operator's own hooks/permissions, and is a no-op on a second run.

WHY MULTIPLE FILES, NOT JUST THE BARE $HOME/.claude/settings.json GH#592'S AC1 NAMES: the
member runner's own `claude -p ... --setting-sources user` (run_member.sh) loads settings from
$CLAUDE_CONFIG_DIR/settings.json, and CLAUDE_CONFIG_DIR is account-specific
($HOME/.claude-<account>) for EVERY real pass -- account_pool.sh's account_pool_run sets this
even for the single-account "primary" case, and entrypoint.sh's own boot-time credential check
already enumerates the exact same per-account dirs. A settings.json written only at the bare
path would build clean, pass a spec-compliance read of AC1, and still never actually run
against a real scheduled pass -- the identical "fix looks complete, does nothing live" shape
this whole issue exists to end. So callers (entrypoint.sh) pass BOTH the bare default and every
account dir it already knows about.

Usage: worktree_guard_hook_install.py <settings.json-path> [<settings.json-path> ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MATCHER = "Edit|Write|Bash"


def _hook_commands() -> list[str]:
    """Every PreToolUse guard this kit ships, registered in one pass.

    Same matcher, same settings files, same idempotence -- a second installer would duplicate
    all of that to register one more line. pretest_push_hook.py joins the list rather than
    getting its own entrypoint. So does checkpoint_pr_hook.py (2026-09-26, #8110: a minion
    checkpoint PR leaves draft only through `minion_checkpoint.py ready`).
    """
    scripts = Path(__file__).resolve().parent
    return [f"python3 {scripts / name}" for name in
            ("worktree_guard_hook.py", "pretest_push_hook.py", "checkpoint_pr_hook.py")]


def _stop_hook_commands() -> list[str]:
    """Stop hooks (2026-09-25): pr_done_hook.py refuses to let a minion / the-fixer --item pass
    end its turn while its PR is red or still running CI. It is inert outside a fleet worktree
    pass ($WT_PATH unset), so registering it in every account's settings is safe."""
    scripts = Path(__file__).resolve().parent
    return [f"python3 {scripts / 'pr_done_hook.py'}"]


STOP_TIMEOUT_S = 180  # pr_done_hook reads the PR and, when red, the failing job logs


def merge_one(path: Path, hook_cmds: list[str] | str, stop_cmds: list[str] | None = None) -> bool:
    """Registers every hook command in path's settings.json. True iff the file changed."""
    if isinstance(hook_cmds, str):
        hook_cmds = [hook_cmds]
    stop_cmds = list(stop_cmds or [])
    if path.exists():
        try:
            settings = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            print(f"worktree_guard_hook_install: {path} is not valid JSON ({exc}), leaving untouched",
                  file=sys.stderr)
            return False
    else:
        settings = {}

    pre_list = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    present = {h.get("command") for entry in pre_list for h in entry.get("hooks", [])}
    missing = [c for c in hook_cmds if c not in present]

    stop_missing: list[str] = []
    if stop_cmds:
        stop_list = settings["hooks"].setdefault("Stop", [])
        stop_present = {h.get("command") for entry in stop_list for h in entry.get("hooks", [])}
        stop_missing = [c for c in stop_cmds if c not in stop_present]

    if not missing and not stop_missing:
        return False  # already registered, nothing to do

    for cmd in missing:
        pre_list.append({"matcher": MATCHER, "hooks": [{"type": "command", "command": cmd}]})
    for cmd in stop_missing:
        settings["hooks"]["Stop"].append(
            {"hooks": [{"type": "command", "command": cmd, "timeout": STOP_TIMEOUT_S}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    return True


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: worktree_guard_hook_install.py <settings.json-or-CLAUDE_CONFIG_DIR-path> [...]",
              file=sys.stderr)
        return 2
    hook_cmds = _hook_commands()
    exit_code = 0
    for raw in argv:
        p = Path(raw)
        # entrypoint.sh's real call shape passes CLAUDE_CONFIG_DIR directories
        # (/root/.claude, /root/.claude-<account>), not settings.json paths directly -- a bare
        # directory here used to reach merge_one()'s `path.read_text()` and crash with
        # IsADirectoryError (caught in review). Accept either: a path already ending in
        # .json is used as-is, anything else is treated as the config dir and settings.json
        # is appended.
        if p.suffix != ".json":
            p = p / "settings.json"
        try:
            if merge_one(p, hook_cmds, _stop_hook_commands()):
                print(f"worktree_guard_hook_install: registered PreToolUse guards + Stop hook in {p}")
            else:
                print(f"worktree_guard_hook_install: {p} already up to date")
        except OSError as exc:
            print(f"worktree_guard_hook_install: could not install into {p} ({exc})", file=sys.stderr)
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
