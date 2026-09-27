"""jefe msg#95: a read-only command that `cd`s into $REPO and redirects its output to /tmp was
blocked, because the redirect alone made it "mutating" and the path scan then found the cd's
$REPO token. A redirect is judged by its own targets; real writes into $REPO still block."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import worktree_guard_hook as guard  # noqa: E402


def _decide(tmp_path, command):
    repo, wt = tmp_path / "repo", tmp_path / "wt"
    repo.mkdir(exist_ok=True)
    wt.mkdir(exist_ok=True)
    payload = {"tool_name": "Bash", "tool_input": {"command": command.format(repo=repo)},
               "cwd": str(wt)}
    return guard.decide(payload, {"WT_PATH": str(wt), "REPO": str(repo)})


def test_read_into_tmp_after_cd_repo_is_allowed(tmp_path):
    assert _decide(tmp_path, "cd {repo} && gh issue list --jq . > /tmp/items.json") is None
    assert _decide(tmp_path, "cd {repo} && gh issue list 2>/dev/null > /tmp/items.json") is None


def test_real_writes_into_repo_still_block(tmp_path):
    assert _decide(tmp_path, "cd {repo} && echo x > notes.txt")  # relative: lands in $REPO
    assert _decide(tmp_path, "echo x > {repo}/notes.txt")
    assert _decide(tmp_path, "cd {repo} && rm notes.txt > /tmp/log")  # rm is the write
