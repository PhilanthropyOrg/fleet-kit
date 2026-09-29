#!/usr/bin/env python3
"""2026-09-28: 32 of 33 minion `reported_nothing` runs in 24h ended their turn "waiting for the
background verified_test.sh run". Claude Code moves any Bash call past its 120s default timeout
into the background ("...was moved to the background (ID: ...)"); a one-shot `claude -p` pass
then ends its turn on that task, or polls `pgrep -f verified_test.sh`, which matches every other
minion's test run. run_member.sh now starts a minion's `claude -p` with background tasks off and
a 20 min foreground Bash timeout, so the test output comes back in the same tool call.

RED without the fix: the minion env block is missing. Plain python, no pytest.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = (HERE / "run_member.sh").read_text()
VARS = ("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS")


def _block() -> str:
    m = re.search(r'^if \[ "\$MEMBER" = "minion" \][^\n]*; then\n  export CLAUDE_CODE_DISABLE_BACKGROUND_TASKS.*?^fi$',
                  SRC, re.M | re.S)
    assert m, "run_member.sh has no minion block exporting CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"
    return m.group(0)


def _env_after(member: str, item: str = "") -> dict:
    script = f'MEMBER={member}\nITEM={item}\n{_block()}\n' + "".join(f'echo "{v}=${{{v}:-}}"\n' for v in VARS)
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True,
                         env={"PATH": "/usr/bin:/bin"}).stdout
    return dict(line.split("=", 1) for line in out.splitlines())


def test_minion_claude_runs_without_background_tasks() -> None:
    env = _env_after("minion")
    assert env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1", env
    assert int(env["BASH_DEFAULT_TIMEOUT_MS"]) >= 15 * 60 * 1000, env  # verified_test.sh ~10 min
    assert int(env["BASH_MAX_TIMEOUT_MS"]) >= int(env["BASH_DEFAULT_TIMEOUT_MS"]), env
    print("ok  a minion pass gets background tasks off and a 20 min foreground Bash timeout")


def test_other_members_keep_background_tasks() -> None:
    # the-fixer's parent pass fans its sub-passes out with run_in_background; leave it alone.
    assert _env_after("the-fixer") == {v: "" for v in VARS}
    assert _env_after("gru") == {v: "" for v in VARS}
    print("ok  other members' env is unchanged")


def test_a_fixer_item_subpass_is_a_one_shot_builder_too() -> None:
    # 2026-09-29: the fixer on #8836 re-polled a backgrounded verified_test.sh for 10+ min.
    assert _env_after("the-fixer", "8836")["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    print("ok  a the-fixer --item sub-pass also runs with background tasks off")


def test_set_before_claude_is_invoked() -> None:
    assert SRC.index(_block()) < SRC.index("claude -p \"$PROMPT\"")
    print("ok  exported before the one `claude -p` call site")


if __name__ == "__main__":
    test_minion_claude_runs_without_background_tasks()
    test_other_members_keep_background_tasks()
    test_a_fixer_item_subpass_is_a_one_shot_builder_too()
    test_set_before_claude_is_invoked()
